# processors.py
"""
Обработчики для упрощенной версии бота
Транскрибация, коррекция, диалоги
"""

import os
import io
import logging
import base64
import asyncio
import time
import random
from typing import Optional, List, Dict, Any, Tuple
from openai import AsyncOpenAI

import config

# Попытка импорта для файлов
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

logger = logging.getLogger(__name__)


# ============================================================================
# GROQ CLIENT MANAGER
# ============================================================================

class GroqClientManager:
    """Управление ключами Groq с circuit breaker"""
    
    def __init__(self):
        self._clients: List[AsyncOpenAI] = []
        self._health: Dict[int, Dict[str, Any]] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
    
    def is_initialized(self) -> bool:
        return self._initialized
    
    async def initialize(self, api_keys: str):
        """Инициализация клиентов из строки с ключами"""
        async with self._lock:
            if self._initialized:
                return
            
            keys = [k.strip() for k in api_keys.split(",") if k.strip()]
            if not keys:
                raise Exception("Нет ключей GROQ_API_KEYS")
            
            for idx, key in enumerate(keys):
                client = AsyncOpenAI(
                    api_key=key,
                    base_url="https://api.groq.com/openai/v1",
                    timeout=config.GROQ_TIMEOUT,
                )
                self._clients.append(client)
                self._health[idx] = {
                    "failures": 0,
                    "disabled_until": 0,
                }
            
            self._initialized = True
            logger.info(f"Инициализировано {len(self._clients)} клиентов Groq")
    
    def _get_available_clients(self):
        """Получить список доступных клиентов"""
        now = time.time()
        available = []
        for idx, client in enumerate(self._clients):
            if self._health[idx]["disabled_until"] < now:
                available.append((idx, client))
        return available
    
    async def make_request(self, func, *args, **kwargs):
        """Выполнить запрос с перебором ключей"""
        if not self._initialized:
            raise Exception("GroqClientManager не инициализирован")
        
        available = self._get_available_clients()
        if not available:
            raise Exception("Все ключи Groq временно недоступны")
        
        # Пробуем каждый доступный клиент
        errors = []
        for idx, client in available:
            try:
                result = await func(client, *args, **kwargs)
                # Успех - сбрасываем счетчик ошибок
                self._health[idx]["failures"] = 0
                return result
            except Exception as e:
                error_msg = str(e)
                errors.append(f"Клиент {idx}: {error_msg[:100]}")
                
                # Увеличиваем счетчик ошибок
                self._health[idx]["failures"] += 1
                
                # Если слишком много ошибок - отключаем на время
                if self._health[idx]["failures"] >= 3:
                    cooldown = 60  # 1 минута
                    self._health[idx]["disabled_until"] = time.time() + cooldown
                    logger.warning(f"Клиент {idx} отключен на {cooldown}с")
                
                await asyncio.sleep(1)
        
        raise Exception(f"Все клиенты недоступны: {'; '.join(errors)}")


groq_client_manager = GroqClientManager()


# ============================================================================
# ТРАНСКРИБАЦИЯ АУДИО
# ============================================================================

async def transcribe_audio(audio_bytes: bytes) -> str:
    """Транскрибация голосового сообщения"""
    
    async def transcribe(client):
        response = await client.audio.transcriptions.create(
            model=config.GROQ_MODELS["transcription"],
            file=("audio.ogg", audio_bytes, "audio/ogg"),
            response_format="text",
            temperature=config.MODEL_TEMPERATURES["transcription"],
        )
        return response
    
    try:
        result = await groq_client_manager.make_request(transcribe)
        return result.strip()
    except Exception as e:
        logger.error(f"Ошибка транскрибации: {e}")
        return f"❌ Ошибка распознавания: {str(e)[:100]}"


# ============================================================================
# КОРРЕКЦИЯ ТЕКСТА
# ============================================================================

async def correct_text_basic(text: str) -> str:
    """Базовая коррекция (только ошибки, с цензурой мата)"""
    
    if not text or not text.strip():
        return config.ERROR_NO_TEXT
    
    async def correct(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["basic"],
            messages=[
                {"role": "system", "content": config.BASIC_CORRECTION_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=config.MODEL_TEMPERATURES["basic"],
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await groq_client_manager.make_request(correct)
    except Exception as e:
        logger.error(f"Ошибка базовой коррекции: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


async def correct_text_premium(text: str) -> str:
    """Премиум коррекция (литературная обработка)"""
    
    if not text or not text.strip():
        return config.ERROR_NO_TEXT
    
    async def correct(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["premium"],
            messages=[
                {"role": "system", "content": config.PREMIUM_CORRECTION_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=config.MODEL_TEMPERATURES["premium"],
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await groq_client_manager.make_request(correct)
    except Exception as e:
        logger.error(f"Ошибка премиум коррекции: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


# ============================================================================
# ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ ФАЙЛОВ
# ============================================================================

async def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Извлечение текста из PDF"""
    if not PDFPLUMBER_AVAILABLE:
        return "❌ Для работы с PDF требуется установить pdfplumber"
    
    try:
        text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n\n"
        
        return text.strip() or "❌ Не удалось извлечь текст из PDF"
    except Exception as e:
        logger.error(f"Ошибка PDF: {e}")
        return f"❌ Ошибка чтения PDF: {str(e)[:100]}"


async def extract_text_from_docx(docx_bytes: bytes) -> str:
    """Извлечение текста из DOCX"""
    if not DOCX_AVAILABLE:
        return "❌ Для работы с DOCX требуется установить python-docx"
    
    try:
        doc = docx.Document(io.BytesIO(docx_bytes))
        text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        return text.strip() or "❌ Документ пуст"
    except Exception as e:
        logger.error(f"Ошибка DOCX: {e}")
        return f"❌ Ошибка чтения DOCX: {str(e)[:100]}"


async def extract_text_from_txt(txt_bytes: bytes) -> str:
    """Извлечение текста из TXT"""
    try:
        # Пробуем разные кодировки
        for encoding in ['utf-8', 'cp1251', 'koi8-r']:
            try:
                return txt_bytes.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return txt_bytes.decode('utf-8', errors='ignore').strip()
    except Exception as e:
        logger.error(f"Ошибка TXT: {e}")
        return f"❌ Ошибка чтения TXT: {str(e)[:100]}"


async def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """Определение типа файла и извлечение текста"""
    
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    
    if ext == 'pdf':
        return await extract_text_from_pdf(file_bytes)
    elif
