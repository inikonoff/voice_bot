FROM python:3.11-slim
RUN apt-get install -y ffmpeg  # Ключевое добавление!
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py processors.py config.py .
HEALTHCHECK http://localhost:8080/health
CMD ["python", "bot.py"]