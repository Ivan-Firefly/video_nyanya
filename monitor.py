#!/usr/bin/env python3
"""
Docker-версия: пути и порт читаются из переменных окружения,
чтобы docker-compose.yml мог их задавать снаружи без пересборки образа.
"""

import os
import logging
import time
import signal
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ============ НАСТРОЙКИ ЗАПУСКА (из переменных окружения) ============
HTML_FILE = Path(os.environ.get("HTML_FILE", "/app/html/monitor.html"))
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/browser-profile"))
REMOTE_DEBUG_PORT = int(os.environ.get("REMOTE_DEBUG_PORT", "9223"))
REMOTE_DEBUG_ADDRESS = "0.0.0.0"  # доступ ограничивайте firewall'ом/сетью Docker, не здесь

RESTART_DELAY_SEC = int(os.environ.get("RESTART_DELAY_SEC", "15"))
HEALTHCHECK_INTERVAL_SEC = int(os.environ.get("HEALTHCHECK_INTERVAL_SEC", "15"))
DROPPED_TIMEOUT_SEC = int(os.environ.get("DROPPED_TIMEOUT_SEC", "60"))

LOG_FILE = Path(os.environ.get("LOG_FILE", "/app/logs/vdoninja_monitor.log"))
# =======================================================================

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("vdoninja_monitor")

_shutdown = False
_telegram_cache = {"token": None, "chat_ids": []}


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
            _telegram_cache["token"] = data["token"]
            _telegram_cache["chat_ids"] = data["chatIds"]
            log.info(f"Обновил кэш Telegram: получателей — {len(data['chatIds'])}")
    except Exception as e:
        log.warning(f"Не удалось считать Telegram-настройки со страницы: {e}")


def notify(text: str):
    token = _telegram_cache.get("token")
    chat_ids = _telegram_cache.get("chat_ids") or []
    if not token or not chat_ids:
        log.warning(f"Нет кэшированных данных Telegram, уведомление не отправлено: {text}")
        return
    for chat_id in chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"Не удалось отправить служебное уведомление в {chat_id}: {e}")


def run_session():
    file_url = f"file://{HTML_FILE.resolve()}"
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
                f"--remote-debugging-port={REMOTE_DEBUG_PORT}",
                f"--remote-debugging-address={REMOTE_DEBUG_ADDRESS}",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        page.on("console", lambda msg: log.debug(f"[page console] {msg.text}"))
        page.on("crash", lambda: log.error("Страница вылетела (crash event)"))
        page.on("pageerror", lambda exc: log.error(f"[page error] {exc}"))

        log.info(f"Открываю {file_url}")
        page.goto(file_url, wait_until="load")
        log.info(f"Remote debugging доступен на {REMOTE_DEBUG_ADDRESS}:{REMOTE_DEBUG_PORT}")

        refresh_telegram_cache(page)

        try:
            page.click("#startBtn", timeout=10000)
            log.info("Мониторинг запущен (кнопка нажата)")
        except PWTimeoutError:
            log.warning("Кнопка #startBtn не найдена сразу — возможно, ViewID не задан.")

        was_connected = False
        dropped_notified = False
        last_status = None

        while not _shutdown:
            time.sleep(HEALTHCHECK_INTERVAL_SEC)

            try:
                info = page.evaluate(
                    "() => ({"
                    "  lastMessageAt: window.__lastMessageAt || null,"
                    "  connectedAt: window.__connectedAt || null,"
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
                try:
                    page.click("#startBtn", timeout=3000)
                    log.info("Страница перезагрузилась — запустил мониторинг снова")
                    was_connected = False
                    dropped_notified = False
                except Exception:
                    pass
                continue

            if info["status"] != last_status:
                log.info(f"status: {info['status']}")
                last_status = info["status"]

            now_ms = time.time() * 1000
            last_msg = info.get("lastMessageAt")
            connected_at = info.get("connectedAt")

            if connected_at:
                was_connected = True
                dropped_notified = False

            if not was_connected:
                continue

            if last_msg and (now_ms - last_msg) > DROPPED_TIMEOUT_SEC * 1000:
                if not dropped_notified:
                    log.warning("Связь была, но сообщения не приходят — похоже, трансляция закончилась")
                    notify("🔴 Трансляция закончилась или связь потеряна — сообщения от потока не приходят.")
                    dropped_notified = True

        context.close()


def main():
    if not HTML_FILE.exists():
        log.error(f"Файл не найден: {HTML_FILE}. Проверьте volume-монтирование в docker-compose.yml")
        sys.exit(1)

    log.info("Старт супервизора vdoninja_monitor")
    while not _shutdown:
        try:
            run_session()
        except Exception as e:
            log.exception(f"Сессия завершилась с ошибкой: {e}")
            notify(f"⚠️ Мониторинг упал с ошибкой: {e}")

        if _shutdown:
            break

        log.info(f"Перезапуск через {RESTART_DELAY_SEC} сек...")
        time.sleep(RESTART_DELAY_SEC)

    log.info("Супервизор остановлен")


if __name__ == "__main__":
    main()
