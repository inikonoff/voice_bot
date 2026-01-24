import os
import sys
from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")

# Настройки логирования
import logging

def setup_logging():
    """Настройка логирования"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stdout
    )
    return logging.getLogger(__name__)

# Контекст пользователей (глобальное хранилище)
user_context = {}