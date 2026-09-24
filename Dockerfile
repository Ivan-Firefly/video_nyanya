# Официальный образ Playwright уже содержит Chromium и все системные
# зависимости (libnss3, fonts, libatk и т.д.) — избавляет от ручной
# установки, которая обычно нужна при headless Chrome в Docker.
# Версия тега должна совпадать с версией playwright в requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

ENV PYTHONUNBUFFERED=1

# HTML-файл и профиль браузера монтируются через volumes в docker-compose.yml,
# поэтому здесь они не копируются — образ универсален для любого HTML.
CMD ["python3", "monitor.py"]
