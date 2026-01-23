# Используем легкий образ Python
FROM python:3.10-slim

# Обновляем систему и устанавливаем FFmpeg (ОБЯЗАТЕЛЬНО для работы с аудио)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Создаем рабочую папку
WORKDIR /app

# Копируем файлы проекта в контейнер
COPY requirements.txt .
COPY bot.py .
COPY google_services.py .

# Устанавливаем библиотеки Python
RUN pip install --no-cache-dir -r requirements.txt

# Команда запуска бота
CMD ["python", "bot.py"]