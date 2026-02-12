FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Установка с увеличенным таймаутом и зеркалом
RUN pip install --no-cache-dir --default-timeout=200 \
    -i https://pypi.org/simple \
    -r requirements.txt

COPY . .

RUN mkdir -p /tmp && chmod 777 /tmp

CMD ["python", "bot.py"]