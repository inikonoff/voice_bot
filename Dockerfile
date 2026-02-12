FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Копирование зависимостей
COPY requirements.txt .

# Установка Python-зависимостей
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода бота
COPY bot.py .
COPY processors.py .
COPY config.py .

# Создание временной директории для видео
RUN mkdir -p /tmp && chmod 777 /tmp

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import socket; s = socket.socket(); s.connect(('localhost', 8080))" || exit 1

# Запуск бота
CMD ["python", "bot.py"]