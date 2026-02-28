# Используем легковесный образ Python
FROM python:3.10-slim

# Устанавливаем системные зависимости: 
# ffmpeg — для обработки видео/аудио
# build-essential — для сборки некоторых python-пакетов (если потребуется)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Сначала копируем только requirements, чтобы использовать кэширование слоев Docker
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем остальные файлы проекта
COPY . .

# Создаем папку для временных файлов, если она указана в config.py
RUN mkdir -p /tmp

# Указываем порт (ваш бот использует PORT из окружения или 8080)
EXPOSE 8080

# Запуск бота
CMD ["python", "bot.py"]