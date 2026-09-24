#!/usr/bin/env python3
"""
Docker-версия: пути и порт читаются из переменных окружения,
чтобы docker-compose.yml мог их задавать снаружи без пересборки образа.
"""

import os
import json
import logging
import secrets
import threading
import time
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ============ НАСТРОЙКИ ЗАПУСКА (из переменных окружения) ============
HTML_FILE = Path(os.environ.get("HTML_FILE", "/app/html/monitor.html"))
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/browser-profile"))

# Встроенный сервер: отдаёт monitor.html и принимает настройки (доступен из LAN/tailscale)
HTTP_BIND = os.environ.get("HTTP_BIND", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8765"))
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "/app/config/settings.json"))
HTML_MARKER = "<!--SERVER_INJECT-->"

RESTART_DELAY_SEC = int(os.environ.get("RESTART_DELAY_SEC", "15"))
HEALTHCHECK_INTERVAL_SEC = int(os.environ.get("HEALTHCHECK_INTERVAL_SEC", "15"))
DROPPED_TIMEOUT_SEC = int(os.environ.get("DROPPED_TIMEOUT_SEC", "60"))   # тишина дольше этого = трансляция остановлена
STREAM_FRESH_SEC = int(os.environ.get("STREAM_FRESH_SEC", "30"))         # данные младше этого = «поток идёт сейчас»
START_CONFIRM_SEC = int(os.environ.get("START_CONFIRM_SEC", "30"))       # столько поток должен идти непрерывно до сообщения о начале
NOTIFY_MIN_GAP_SEC = int(os.environ.get("NOTIFY_MIN_GAP_SEC", "120"))    # не чаще одного сообщения о трансляции за это время
NOTIFY_RETRY_SEC = int(os.environ.get("NOTIFY_RETRY_SEC", "60"))          # повтор недоставленного уведомления не чаще этого
CRASH_NOTIFY_MIN_GAP_SEC = int(os.environ.get("CRASH_NOTIFY_MIN_GAP_SEC", "1800"))  # сообщения «монитор упал» не чаще этого
RESTART_MAX_DELAY_SEC = int(os.environ.get("RESTART_MAX_DELAY_SEC", "300"))         # потолок паузы при повторяющихся падениях

# Пустое значение (LOG_FILE=) отключает запись в файл: логи остаются только в stdout (docker logs)
LOG_FILE = os.environ.get("LOG_FILE", "/app/logs/vdoninja_monitor.log").strip()
# =======================================================================

_log_handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(logging.FileHandler(LOG_FILE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("vdoninja_monitor")

_shutdown = False
_telegram_cache = {"token": None, "chat_ids": []}
_settings_changed = threading.Event()   # HTTP-поток -> основной цикл: «настройки обновились»
MONITOR_KEY = secrets.token_urlsafe(16)  # секрет для открытия страницы в режиме monitor
_state = {"status": None, "connected": False, "lastMessageAgoSec": None}  # для /api/status
# Состояние трансляции живёт между перезапусками браузерной сессии, чтобы перезагрузка страницы не давала ложных «конец/начало»
_stream = {"live": False, "announced": False, "ended_before": False, "fresh_since": None, "last_notice_at": float("-inf"),
           "last_attempt_at": float("-inf"), "notify_failing": False}


def _handle_sigterm(signum, frame):
    global _shutdown
    log.info("Получен сигнал остановки")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


def refresh_telegram_cache(page):
    try:
        data = page.evaluate(
            "() => ({ token: window.TELEGRAMBOTTOKEN || null, "
            "         chatIds: window.ACTIVE_CHAT_IDS || [] })"
        )
        if data.get("token") and data.get("chatIds"):
            changed = (_telegram_cache["token"], _telegram_cache["chat_ids"]) != (data["token"], data["chatIds"])
            _telegram_cache["token"] = data["token"]
            _telegram_cache["chat_ids"] = data["chatIds"]
            if changed:
                log.info(f"Обновил кэш Telegram: получателей — {len(data['chatIds'])}")
    except Exception as e:
        log.warning(f"Не удалось считать Telegram-настройки со страницы: {e}")


def notify(text: str, quiet: bool = False) -> bool:
    """True, если сообщение дошло хотя бы одному получателю."""
    token = _telegram_cache.get("token")
    chat_ids = _telegram_cache.get("chat_ids") or []
    if not token or not chat_ids:
        if not quiet:
            log.warning(f"Нет кэшированных данных Telegram, уведомление не отправлено: {text}")
        return False
    sent = False
    for chat_id in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            if r.ok:
                sent = True
            else:
                if not quiet:
                    log.warning(f"Telegram вернул {r.status_code} для {chat_id}")
        except Exception as e:
            if not quiet:
                log.warning(f"Не удалось отправить служебное уведомление в {chat_id}: {e}")
    return sent


def try_start(page, timeout=3000) -> bool:
    """Нажимает «Включить мониторинг». True — только если страница правда запустилась (кнопка скрылась);
    без ID трансляции или получателя страница отказывается стартовать и кнопка остаётся."""
    try:
        page.click("#startBtn", timeout=timeout)
        return page.evaluate("() => document.getElementById('startBtn').style.display === 'none'")
    except Exception:
        return False


WAITING_TEXT = "не запущен: нет ID трансляции или получателя (задайте в настройках)"


def process_stream(now_ms, last_stream_ms, baseline_ms):
    """
    Логика уведомлений о трансляции (без спама):
      * «идёт» = данные потока приходят непрерывно START_CONFIRM_SEC (короткие всплески не считаются);
      * «остановилась» = данных нет дольше DROPPED_TIMEOUT_SEC (отсчёт не раньше baseline_ms —
        момента последней перезагрузки страницы, чтобы перезапуск не выглядел как конец трансляции);
      * шлём сообщение только когда реальное состояние отличается от того, о чём пользователю уже сообщили
        (после «остановилась» следующее сообщение — только «возобновилась», и наоборот);
      * не чаще одного сообщения за NOTIFY_MIN_GAP_SEC: если поток мигнул и вернулся раньше —
        сообщений нет вообще, пользователь и так в курсе актуального состояния.
    """
    s = _stream
    fresh = last_stream_ms is not None and (now_ms - last_stream_ms) < STREAM_FRESH_SEC * 1000

    if fresh:
        if s["fresh_since"] is None:
            s["fresh_since"] = now_ms
        # данные должны продолжать приходить START_CONFIRM_SEC после первого появления (а не просто быть «свежими»)
        if not s["live"] and last_stream_ms - s["fresh_since"] >= START_CONFIRM_SEC * 1000:
            s["live"] = True
            log.info("Данные потока идут — трансляция началась")
    else:
        s["fresh_since"] = None
        silent_ms = now_ms - max(last_stream_ms or 0, baseline_ms)
        if s["live"] and silent_ms > DROPPED_TIMEOUT_SEC * 1000:
            s["live"] = False
            log.warning("Данные потока перестали приходить — трансляция остановлена")

    now_s = now_ms / 1000
    if (s["live"] != s["announced"] and now_s - s["last_notice_at"] >= NOTIFY_MIN_GAP_SEC
            and now_s - s["last_attempt_at"] >= NOTIFY_RETRY_SEC):
        if s["live"]:
            text = ("🟢 Трансляция возобновилась — поток снова приходит." if s["ended_before"]
                    else "🟢 Трансляция началась — поток приходит, мониторинг работает.")
        else:
            text = "🔴 Трансляция остановлена — данные от потока не приходят."
        s["last_attempt_at"] = now_s
        if notify(text, quiet=s["notify_failing"]):
            if s["notify_failing"]:
                log.info("Telegram снова доступен — уведомление отправлено")
            s["notify_failing"] = False
            s["announced"] = s["live"]
            s["last_notice_at"] = now_s
            if not s["live"]:
                s["ended_before"] = True
        elif not s["notify_failing"]:
            s["notify_failing"] = True
            log.warning(f"Уведомление не доставлено — повторяю раз в {NOTIFY_RETRY_SEC} с, новых записей в лог не будет до успеха")


# ============ ВСТРОЕННЫЙ HTTP-СЕРВЕР: тот же monitor.html служит и интерфейсом настроек ============
def read_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning(f"Не удалось прочитать {SETTINGS_FILE}: {e}")
        return None


def write_settings(data: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def render_html(mode: str) -> str:
    payload = json.dumps({"mode": mode, "settings": read_settings()}).replace("</", "<\\/")
    return HTML_FILE.read_text(encoding="utf-8").replace(
        HTML_MARKER, f"<script>window.__SERVER__ = {payload};</script>", 1
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/status":
            return self._send(200, json.dumps(_state))
        if url.path in ("/", "/index.html"):
            # режим monitor выдаётся только по секретному ключу, который знает лишь локальный браузер;
            # все остальные (LAN, tailscale) получают режим settings и мониторинг не запускают
            key = parse_qs(url.query).get("monitor", [""])[0]
            is_monitor = secrets.compare_digest(key.encode(), MONITOR_KEY.encode())
            return self._send(200, render_html("monitor" if is_monitor else "settings"), "text/html; charset=utf-8")
        self._send(404, "{}")

    def do_POST(self):
        if urlparse(self.path).path != "/api/settings":
            return self._send(404, "{}")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 65536:
                raise ValueError("bad length")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception:
            return self._send(400, '{"ok": false}')
        write_settings(data)
        _settings_changed.set()
        log.info("Получены новые настройки через веб-интерфейс")
        self._send(200, '{"ok": true}')


def start_http_server():
    srv = ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info(f"Интерфейс настроек слушает {HTTP_BIND}:{HTTP_PORT}")
    return srv


def run_session():
    page_url = f"http://127.0.0.1:{HTTP_PORT}/?monitor={MONITOR_KEY}"
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        page.on("console", lambda msg: log.debug(f"[page console] {msg.text}"))
        page.on("crash", lambda: log.error("Страница вылетела (crash event)"))
        page.on("pageerror", lambda exc: log.error(f"[page error] {exc}"))

        log.info(f"Открываю страницу мониторинга: http://127.0.0.1:{HTTP_PORT}/ (режим monitor)")
        _settings_changed.clear()
        page.goto(page_url, wait_until="load")

        refresh_telegram_cache(page)

        waiting_logged = False  # «жду настройки» пишем в лог один раз, а не на каждой проверке
        if try_start(page, 10000):
            log.info("Мониторинг запущен")
        else:
            log.warning("Мониторинг не запущен: не задан ID трансляции или не выбран получатель — жду настройки")
            waiting_logged = True
            _state.update(status=WAITING_TEXT, connected=False, lastMessageAgoSec=None)

        last_status = None
        baseline_ms = time.time() * 1000  # отсчёт «тишины» начинается с запуска страницы

        last_check = time.time()

        while not _shutdown:
            # тик раз в секунду: быстро реагируем и на новые настройки, и на SIGTERM
            if _settings_changed.wait(timeout=1):
                _settings_changed.clear()
                log.info("Настройки изменены — перезагружаю страницу и запускаю мониторинг")
                try:
                    page.reload(wait_until="load")
                    refresh_telegram_cache(page)
                    if try_start(page, 10000):
                        log.info("Настройки применены, мониторинг запущен")
                        waiting_logged = False
                    else:
                        log.warning("Настройки сохранены, но мониторинг не запущен: нет ID трансляции или получателя")
                        waiting_logged = True
                        _state.update(status=WAITING_TEXT, connected=False, lastMessageAgoSec=None)
                except Exception as e:
                    log.warning(f"Не удалось применить настройки сразу (подхватит проверка состояния): {e}")
                _stream["fresh_since"] = None
                baseline_ms = time.time() * 1000
                last_status = None
                last_check = time.time()
                continue

            if time.time() - last_check < HEALTHCHECK_INTERVAL_SEC:
                continue
            last_check = time.time()

            try:
                info = page.evaluate(
                    "() => ({"
                    "  lastStreamAt: window.__lastStreamAt || null,"
                    "  status: document.getElementById('status') ? document.getElementById('status').textContent : null,"
                    "  startBtnVisible: !!document.getElementById('startBtn') && "
                    "                   document.getElementById('startBtn').style.display !== 'none'"
                    "})"
                )
            except Exception as e:
                log.error(f"Страница недоступна ({e}), перезапускаю сессию")
                break

            if info.get("startBtnVisible"):
                refresh_telegram_cache(page)
                if try_start(page):
                    log.info("Страница перезагрузилась — запустил мониторинг снова")
                    _stream["fresh_since"] = None
                    baseline_ms = time.time() * 1000
                    waiting_logged = False
                else:
                    if not waiting_logged:
                        log.warning("Мониторинг не запущен: не задан ID трансляции или не выбран получатель — жду настройки")
                        waiting_logged = True
                    _state.update(status=WAITING_TEXT, connected=False, lastMessageAgoSec=None)
                continue

            if info["status"] != last_status:
                log.info(f"status: {info['status']}")
                last_status = info["status"]

            now_ms = time.time() * 1000
            last_stream = info.get("lastStreamAt")

            process_stream(now_ms, last_stream, baseline_ms)

            _state.update(
                status=info["status"],
                connected=_stream["live"],
                lastMessageAgoSec=round((now_ms - last_stream) / 1000) if last_stream else None,
            )

        context.close()


def main():
    if not HTML_FILE.exists():
        log.error(f"Файл не найден: {HTML_FILE}. Проверьте volume-монтирование в docker-compose.yml")
        sys.exit(1)

    html_text = HTML_FILE.read_text(encoding="utf-8")
    if HTML_MARKER not in html_text or "__lastStreamAt" not in html_text:
        log.error(f"{HTML_FILE}: устаревшая версия monitor.html (нет {HTML_MARKER} или __lastStreamAt)")
        sys.exit(1)
    server = start_http_server()

    log.info("Старт супервизора vdoninja_monitor")
    fails = 0  # сессий подряд, завершившихся быстро (для роста паузы перед перезапуском)
    last_crash_notice = float("-inf")
    while not _shutdown:
        started = time.time()
        crashed = None
        try:
            run_session()
        except Exception as e:
            crashed = e

        # сессия, проработавшая дольше 5 минут, считается здоровой — счётчик сбоев обнуляется
        fails = 0 if time.time() - started > 300 else fails + 1

        if crashed:
            if fails == 1:
                log.error(f"Сессия завершилась с ошибкой: {crashed}", exc_info=crashed)
            else:
                log.error(f"Сессия снова упала ({fails}-й раз подряд): {crashed}")
            if time.time() - last_crash_notice >= CRASH_NOTIFY_MIN_GAP_SEC:
                if notify(f"⚠️ Мониторинг упал с ошибкой: {crashed}"):
                    last_crash_notice = time.time()

        _state.update(status=None, connected=False, lastMessageAgoSec=None)

        if _shutdown:
            break

        delay = min(RESTART_DELAY_SEC * 2 ** min(fails - 1, 6), RESTART_MAX_DELAY_SEC) if fails else RESTART_DELAY_SEC
        log.info(f"Перезапуск через {delay} сек...")
        for _ in range(delay):  # спим по секунде, чтобы SIGTERM срабатывал сразу
            if _shutdown:
                break
            time.sleep(1)

    server.shutdown()
    log.info("Супервизор остановлен")


if __name__ == "__main__":
    main()
