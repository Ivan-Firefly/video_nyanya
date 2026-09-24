FROM python:3.12-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

# Явный apt-get update первым шагом — без этого список пакетов пуст
# сразу после FROM, и playwright install --with-deps может не найти пакеты.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Разбито на два отдельных шага — если playwright install-deps упадёт,
# в логе будет видно именно на этой команде, а не смешано с pip.
RUN playwright install chromium
RUN playwright install-deps chromium

COPY monitor.py .

ENV PYTHONUNBUFFERED=1
CMD ["python3", "run_vdoninja_monitor_docker.py"]
