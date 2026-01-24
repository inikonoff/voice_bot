M python:3.10-slim

# Обновляем систему и устанавливаем FFmpeg (необходим для работы с аудио)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Создаём рабочую папку
WORKDIR /app

# Копируем файлы проекта в контейнер
COPY requirements.txt .
COPY bot.py .

# Устанавливаем библиотеки Python
RUN pip install --no-cache-dir -r requirements.txt

# Команда запуска бота
CMD ["python", "bot.py"]