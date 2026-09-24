FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # Ставим ТОЛЬКО системные зависимости для Chromium (--with-deps),
    # не трогая Firefox/WebKit — их бинарники даже не скачиваются.
    && playwright install --with-deps chromium \
    # Чистим apt-кэш и списки пакетов внутри того же слоя,
    # иначе они остаются "мёртвым весом" в образе.
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY monitor.py .

ENV PYTHONUNBUFFERED=1

# HTML-файл и профиль браузера монтируются через volumes в docker-compose.yml,
# поэтому здесь они не копируются — образ универсален для любого HTML.
CMD ["python3", "monitor.py"]
