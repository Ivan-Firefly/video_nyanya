FROM python:3.12-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

# Явный список системных зависимостей Chromium вместо `playwright install-deps`,
# которая использует Ubuntu-специфичные имена пакетов (ttf-unifont,
# ttf-ubuntu-font-family), отсутствующие в Debian bookworm.
# fonts-unifont — актуальное имя пакета в Debian;
# fonts-liberation закрывает то, что раньше давал ttf-ubuntu-font-family.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    fonts-unifont \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Только сам бинарник браузера — без install-deps, зависимости уже поставлены выше вручную.
RUN playwright install chromium

COPY monitor.py .

ENV PYTHONUNBUFFERED=1
CMD ["python3", "monitor.py"]
