# processors.py
"""
Обработчики текста: OCR, транскрибация, коррекция, саммари
"""

import io
import logging
import base64
import asyncio
from typing import Optional
from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)


# ============================================================================
# VISION PROCESSOR (OCR)
# ============================================================================

class VisionProcessor:
    """Распознавание текста с изображений через Groq Vision"""
    
    def __init__(self):
        self.groq_clients = []
        self.current_client_index = 0
    
    def init_clients(self, groq_clients: list):
        """Инициализация клиентов Groq"""
        self.groq_clients = groq_clients
    
    async def extract_text(self, image_bytes: bytes) -> str:
        """OCR через Groq Vision"""
        
        if not self.groq_clients:
            logger.warning("No Groq clients available for Vision")
            return config.ERROR_NO_GROQ
        
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        async def extract(client):
            response = await client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": config.OCR_PROMPT
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                temperature=config.VISION_TEMPERATURE,
                max_tokens=config.VISION_MAX_TOKENS,
            )
            return response.choices[0].message.content
        
        try:
            return await _make_groq_request(self.groq_clients, extract)
        except Exception as e:
            logger.error(f"Vision OCR error: {e}")
            return f"❌ Ошибка распознавания текста: {str(e)[:100]}"


vision_processor = VisionProcessor()


# ============================================================================
# AUDIO TRANSCRIPTION
# ============================================================================

async def transcribe_voice(audio_bytes: bytes, groq_clients: list) -> str:
    """Транскрибация голоса через Whisper v3"""
    
    async def transcribe(client):
        # Используем автоопределение языка (language=None)
        response = await client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=("audio.ogg", audio_bytes, "audio/ogg"),
            language=config.AUDIO_LANGUAGE,  # None = автоопределение
            response_format="text",
        )
        return response
    
    try:
        result = await _make_groq_request(groq_clients, transcribe)
        
        if config.LOG_TRANSCRIPTION_LANGUAGE:
            logger.debug(f"Transcription result (first 100 chars): {str(result)[:100]}")
        
        # Проверка на смешивание языков (кириллица + латиница в одном слове)
        result = _validate_transcription_language(result)
        
        return result
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return f"❌ Ошибка распознавания: {str(e)[:100]}"


def _validate_transcription_language(text: str) -> str:
    """
    Проверка на смешивание языков.
    Если обнаружены слова с кириллицей и латиницей одновременно — это ошибка.
    """
    words = text.split()
    cyrillic_count = 0
    latin_count = 0
    mixed_words = 0
    
    for word in words:
        has_cyrillic = any('\u0400' <= c <= '\u04FF' for c in word if c.isalpha())
        has_latin = any(ord('a') <= ord(c) <= ord('z') or ord('A') <= ord(c) <= ord('Z') 
                       for c in word if c.isalpha())
        
        if has_cyrillic:
            cyrillic_count += 1
        if has_latin:
            latin_count += 1
        if has_cyrillic and has_latin:
            mixed_words += 1
    
    # Если много смешанных слов (больше 20% слов) — это вероятно ошибка смешивания языков
    if mixed_words > len(words) * 0.2:
        logger.warning(f"Possible language mix detected: {mixed_words} mixed words out of {len(words)}")
        # Логируем, но не пытаемся "чинить" — пусть пользователь решит
    
    return text


# ============================================================================
# TEXT PROCESSING - CORRECTION
# ============================================================================

async def correct_text_basic(text: str, groq_clients: list) -> str:
    """Базовая коррекция: только ошибки и пунктуация"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    async def correct(client):
        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": config.BASIC_CORRECTION_PROMPT + f"\n\nТекст:\n{text}"}],
            temperature=config.BASIC_CORRECTION_TEMPERATURE,
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await _make_groq_request(groq_clients, correct)
    except Exception as e:
        logger.error(f"Basic correction error: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


async def correct_text_premium(text: str, groq_clients: list) -> str:
    """Премиум коррекция: деликатное причесывание"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    async def correct(client):
        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": config.PREMIUM_CORRECTION_PROMPT + f"\n\nТекст:\n{text}"}],
            temperature=config.PREMIUM_CORRECTION_TEMPERATURE,
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await _make_groq_request(groq_clients, correct)
    except Exception as e:
        logger.error(f"Premium correction error: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


async def summarize_text(text: str, groq_clients: list) -> str:
    """Создание саммари с учетом жанра"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    words_count = len(text.split())
    if words_count < config.MIN_WORDS_FOR_SUMMARY or len(text) < config.MIN_CHARS_FOR_SUMMARY:
        return config.ERROR_TEXT_TOO_SHORT_FOR_SUMMARY
    
    async def summarize(client):
        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": config.SUMMARIZATION_PROMPT + f"\n\nТекст:\n{text}"}],
            temperature=config.SUMMARIZATION_TEMPERATURE,
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await _make_groq_request(groq_clients, summarize)
    except Exception as e:
        logger.error(f"Summarization error: {e}")
        return f"❌ Ошибка создания саммари: {str(e)[:100]}"


# ============================================================================
# FILE PROCESSING
# ============================================================================

async def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Извлечение текста из PDF с помощью pdfplumber"""
    try:
        import pdfplumber
        
        pdf_buffer = io.BytesIO(pdf_bytes)
        text = ""
        page_count = 0
        
        with pdfplumber.open(pdf_buffer) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                if config.PDF_MAX_PAGES and page_num > config.PDF_MAX_PAGES:
                    break
                
                # Извлечение текста
                page_text = page.extract_text()
                if page_text:
                    text += f"\n--- Страница {page_num} ---\n"
                    text += page_text + "\n"
                
                # Если есть таблицы — попытаемся их парсить
                if page.tables:
                    for table_idx, table in enumerate(page.tables, 1):
                        text += f"\n[Таблица {table_idx} на странице {page_num}]\n"
                        for row in table:
                            text += " | ".join(str(cell) if cell else "" for cell in row) + "\n"
                
                page_count += 1
        
        if not text.strip():
            return "❌ Не удалось извлечь текст из PDF"
        
        logger.info(f"Extracted text from {page_count} PDF pages")
        return text.strip()
        
    except ImportError:
        logger.error("pdfplumber not installed")
        return "❌ Для работы с PDF требуется установить pdfplumber"
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return f"❌ Ошибка обработки PDF: {str(e)}"


async def extract_text_from_docx(docx_bytes: bytes) -> str:
    """Извлечение текста из DOCX"""
    try:
        import docx
        
        doc_buffer = io.BytesIO(docx_bytes)
        doc = docx.Document(doc_buffer)
        text = ""
        
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                text += paragraph.text + "\n"
        
        if not text.strip():
            return "❌ Документ пуст"
        
        logger.info("Extracted text from DOCX")
        return text.strip()
        
    except ImportError:
        logger.error("python-docx not installed")
        return "❌ Для работы с DOCX требуется установить python-docx"
    except Exception as e:
        logger.error(f"DOCX extraction error: {e}")
        return f"❌ Ошибка обработки DOCX: {str(e)}"


async def extract_text_from_txt(txt_bytes: bytes) -> str:
    """Извлечение текста из TXT с автоопределением кодировки"""
    try:
        encodings = ['utf-8', 'cp1251', 'koi8-r', 'windows-1251', 'iso-8859-1', 'utf-16']
        
        for encoding in encodings:
            try:
                result = txt_bytes.decode(encoding)
                logger.debug(f"TXT decoded with {encoding}")
                return result
            except UnicodeDecodeError:
                continue
        
        # Fallback с игнорированием ошибок
        logger.warning("TXT decoded with fallback (errors ignored)")
        return txt_bytes.decode('utf-8', errors='ignore')
        
    except Exception as e:
        logger.error(f"TXT reading error: {e}")
        return f"❌ Ошибка чтения текстового файла: {str(e)}"


async def extract_text_from_file(file_bytes: bytes, filename: str, groq_clients: list) -> str:
    """Определяем тип файла и извлекаем текст"""
    
    import mimetypes
    
    mime_type, _ = mimetypes.guess_type(filename)
    file_ext = filename.lower().split('.')[-1] if '.' in filename else ''
    
    # Изображения
    if mime_type and mime_type.startswith('image/'):
        logger.info(f"Processing image: {filename}")
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)
    
    if file_ext in ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp']:
        logger.info(f"Processing image (by extension): {filename}")
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)
    
    # PDF
    if mime_type == 'application/pdf' or file_ext == 'pdf':
        logger.info(f"Processing PDF: {filename}")
        return await extract_text_from_pdf(file_bytes)
    
    # DOCX
    if mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' or file_ext == 'docx':
        logger.info(f"Processing DOCX: {filename}")
        return await extract_text_from_docx(file_bytes)
    
    # TXT
    if mime_type == 'text/plain' or file_ext == 'txt':
        logger.info(f"Processing TXT: {filename}")
        return await extract_text_from_txt(file_bytes)
    
    # DOC (старый формат)
    if file_ext == 'doc':
        return config.ERROR_DOC_NOT_SUPPORTED
    
    # Неподдерживаемый формат
    logger.warning(f"Unsupported file format: {file_ext}")
    return f"{config.ERROR_UNSUPPORTED_FORMAT}"


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

async def _make_groq_request(groq_clients: list, func, *args, **kwargs):
    """Делаем запрос с перебором ключей и обработкой ошибок"""
    
    if not groq_clients:
        raise Exception("No Groq clients available")
    
    errors = []
    client_count = len(groq_clients)
    current_index = 0
    
    for attempt in range(client_count * config.GROQ_RETRY_COUNT):
        client = groq_clients[current_index % client_count]
        current_index += 1
        
        try:
            logger.debug(f"Attempt {attempt + 1} with client {current_index % client_count}")
            return await func(client, *args, **kwargs)
            
        except Exception as e:
            error_msg = str(e)
            errors.append(error_msg)
            logger.warning(f"Request error (attempt {attempt + 1}): {error_msg[:100]}")
            
            # Умная задержка: экспоненциальная на ошибки API
            await asyncio.sleep(1 + (attempt % 3) * 0.5)
    
    # Все попытки исчерпаны
    error_summary = '; '.join(errors[:3])
    logger.error(f"All Groq clients failed: {error_summary}")
    raise Exception(f"All clients failed: {error_summary}")


def get_available_modes(text: str) -> list:
    """Определяем доступные режимы обработки"""
    words_count = len(text.split())
    text_length = len(text)
    
    # Базовые режимы всегда доступны
    available = ["basic", "premium"]
    
    # Саммари только для достаточно длинных текстов
    if words_count >= config.MIN_WORDS_FOR_SUMMARY and text_length >= config.MIN_CHARS_FOR_SUMMARY:
        available.append("summary")
    
    return available
