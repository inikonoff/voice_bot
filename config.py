# config.py
import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
class Config:
    # Токены и ключи
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")
    
    # Веб-сервер
    PORT = int(os.environ.get("PORT", 8080))
    
    # Константы обработки текста
    SHORT_TEXT_WORDS = 50           # Меньше этого - нет саммари
    SHORT_TEXT_CHARS = 300          # Дополнительная проверка
    MAX_TEXT_LENGTH = 10000         # Максимальная длина для обработки
    
    # Моды обработки
    MODES = {
        "basic": {"text": "📝 Как есть", "icon": "📝"},
        "premium": {"text": "✨ Красиво", "icon": "✨"},
        "summary": {"text": "📊 Саммари", "icon": "📊"}
    }
    
    # Порядок отображения кнопок
    MODE_ORDER = ["basic", "premium", "summary"]
    
    # Форматы экспорта
    EXPORT_FORMATS = {
        "txt": {"text": "📄 TXT", "icon": "📄"},
        "pdf": {"text": "📊 PDF", "icon": "📊"}
    }

# --- ЛОГГИРОВАНИЕ ---
def setup_logging():
    """Настройка логирования"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout
    )
    return logging.getLogger(__name__)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
user_context = {}  # Централизованное хранилище состояния пользователей
logger = setup_logging()  # Глобальный логгер