# processors.py
"""
Обработчики текста и видео: OCR, транскрибация, коррекция, саммари, диалог
Версия 5.0 - Внедрена новая языковая политика и детерминированные промпты
"""

import io
import os
import logging
import asyncio
from typing import Optional, List, Dict, Any, AsyncGenerator
from openai import AsyncOpenAI
import config

# Попытка импорта библиотек для работы с файлами
try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

logger = logging.getLogger(__name__)

# Хранилище для диалогов о документах (user_id -> данные)
document_dialogues: Dict[int, Dict[str, Any]] = {}

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ GROQ
# ============================================================================

async def _make_groq_request(groq_clients: list, func, *args, **kwargs):
    """Ротация ключей Groq для обхода лимитов"""
    if not groq_clients:
        raise Exception("Нет доступных API ключей Groq")
        
    for client in groq_clients:
        try:
            return await func(client, *args, **kwargs)
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                logger.warning("Rate limit hit, переключаюсь на следующий ключ...")
                continue
            logger.error(f"Groq API error: {e}")
            raise e
    raise Exception("Все API ключи исчерпали лимиты. Попробуйте позже.")

# ============================================================================
# ТЕКСТОВЫЕ ПРОЦЕССОРЫ (НОВАЯ ЛОГИКА ПРОМПТОВ)
# ============================================================================

async def correct_text_basic(text: str, groq_clients: list) -> str:
    """Режим BASIC: Минимальное вмешательство, сохранение языка, цензура мата"""
    
    system_prompt = (
        "Ты — стенограф-корректор. Твоя задача: отформатировать текст, сохраняя язык оригинала.\n"
        "⚠️ НИКОГДА не переводите текст на другой язык. Сохраняйте язык оригинального текста.\n\n"
        "ПРАВИЛА:\n"
        "1. Сохраняй словарный состав речи: слова-паразиты, повторы, разговорные конструкции, косноязычие.\n"
        "2. Исправляй ТОЛЬКО орфографию и расставляй корректную пунктуацию.\n"
        "3. Не удаляй повторяющиеся слова.\n"
        "4. ЦЕНЗУРА: Нецензурные слова заменяй на форму: первая буква + многоточие (б..., х..., п..., нах....).\n"
        "   Не используй '***' и не заменяй мат литературными аналогами.\n"
        "5. Не улучшай стиль и не редактируй структуру.\n\n"
        "СТРОГО: Не добавляй пояснений. Верни только итоговый текст."
    )

    async def _call(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODEL_LLAMA,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=4000
        )
        return response.choices[0].message.content.strip()

    return await _make_groq_request(groq_clients, _call)


async def correct_text_premium(text: str, groq_clients: list) -> str:
    """Режим PREMIUM: Литературная обработка, сохранение языка оригинала"""
    
    system_prompt = (
        "Ты — профессиональный редактор. Отредактируйте текст, сохраняя язык оригинала.\n"
        "⚠️ НИКОГДА не переводите текст на другой язык.\n\n"
        "ПРАВИЛА:\n"
        "1. Удалите слова-паразиты, повторы и бессмысленные паузы.\n"
        "2. Исправьте орфографические, грамматические и стилистические ошибки.\n"
        "3. Сделайте текст литературным, связным и читабельным.\n"
        "4. Нецензурную лексику замените нейтральными литературными аналогами.\n"
        "5. Улучшайте структуру предложений, но не искажайте смысл.\n\n"
        "СТРОГО: Не добавляй вступлений (типа 'Вот ваш текст:'). Верни только результат."
    )

    async def _call(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODEL_LLAMA,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.2,
            max_tokens=4000
        )
        return response.choices[0].message.content.strip()

    return await _make_groq_request(groq_clients, _call)


async def summarize_text(text: str, groq_clients: list) -> str:
    """Режим SUMMARY: Всегда на русском языке"""
    
    system_prompt = (
        "Составьте краткое и структурированное саммари текста на русском языке.\n"
        "ВАЖНО: Даже если исходный текст на английском, итог должен быть ПОЛНОСТЬЮ на русском.\n"
        "Передавайте смысл точно и без добавления внешней информации.\n"
        "Строго следуйте инструкциям. Не добавляйте мета-описаний. Верните только итоговый текст."
    )

    async def _call(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODEL_LLAMA,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.2,
            max_tokens=1500
        )
        return response.choices[0].message.content.strip()

    return await _make_groq_request(groq_clients, _call)


async def stream_document_answer(text: str, question: str, groq_clients: list) -> AsyncGenerator[str, None]:
    """Режим QA: Ответ всегда на русском языке"""
    
    system_prompt = (
        "Ответьте на вопрос пользователя на русском языке, используя только предоставленный текст.\n"
        "Даже если текст документа написан на английском, ответ должен быть полностью на русском.\n"
        "Не переводите документ целиком. Будьте точны.\n"
        "Не добавляйте вступительные фразы. Верните только результат обработки."
    )
    
    user_message = f"ДОКУМЕНТ:\n{text}\n\nВОПРОС: {question}"

    async def _call(client):
        return await client.chat.completions.create(
            model=config.GROQ_MODEL_LLAMA,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.3,
            stream=True
        )

    response = await _make_groq_request(groq_clients, _call)
    
    async for chunk in response:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

# ============================================================================
# РАБОТА С ФАЙЛАМИ
# ============================================================================

async def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """Извлечение текста из PDF, DOCX, TXT"""
    ext = filename.split('.')[-1].lower()
    
    if ext == 'pdf' and PDF_AVAILABLE:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return "".join(page.extract_text() or "" for page in pdf.pages)
            
    elif ext == 'docx' and DOCX_AVAILABLE:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs)
        
    elif ext == 'txt':
        return file_bytes.decode('utf-8', errors='ignore')
        
    return "Неподдерживаемый формат файла."

def get_available_modes(text: str) -> list:
    """Определяем доступные режимы"""
    if len(text) < 10: return []
    modes = ["basic", "premium"]
    if len(text.split()) > 20:
        modes.append("summary")
    return modes
