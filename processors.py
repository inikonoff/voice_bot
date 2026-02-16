# processors.py
"""
Обработчики текста и видео: OCR, транскрибация, видео, коррекция, саммари, диалог
Версия 4.1 с исправлениями для PDF и YouTube
"""

import io
import os
import json
import logging
import base64
import asyncio
import subprocess
import mimetypes
import re
import time
from typing import Optional, Tuple, List, Dict, Any
from datetime import timedelta
from openai import AsyncOpenAI

import config

# Попытка импорта дополнительных библиотек
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    YT_TRANSCRIPT_API_AVAILABLE = True
except ImportError:
    YT_TRANSCRIPT_API_AVAILABLE = False

logger = logging.getLogger(__name__)

# Хранилище для диалогов о документах
document_dialogues: Dict[int, Dict[int, Dict[str, Any]]] = {}


# ============================================================================
# VISION PROCESSOR (OCR)
# ============================================================================

class VisionProcessor:
    """Распознавание текста с изображений через Groq Vision"""
    
    def __init__(self):
        self.groq_clients = []
        self.current_client_index = 0
    
    def init_clients(self, groq_clients: list):
        self.groq_clients = groq_clients
    
    async def extract_text(self, image_bytes: bytes) -> str:
        if not self.groq_clients:
            logger.warning("No Groq clients available for Vision")
            return config.ERROR_NO_GROQ
        
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        async def extract(client):
            response = await client.chat.completions.create(
                model=config.GROQ_MODELS["vision"],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": config.OCR_PROMPT},
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
# VIDEO PROCESSING
# ============================================================================

class VideoProcessor:
    """Обработка видеофайлов и кружочков"""
    
    @staticmethod
    async def check_video_duration(filepath: str) -> Optional[float]:
        """Получить длительность видео в секундах"""
        try:
            result = subprocess.run(
                [
                    'ffprobe', '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1',
                    filepath
                ],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0 and result.stdout.strip():
                duration = float(result.stdout.strip())
                logger.debug(f"Video duration: {duration}s")
                return duration
        except Exception as e:
            logger.warning(f"Error checking video duration: {e}")
        
        return None
    
    @staticmethod
    async def extract_audio_from_video(video_path: str, output_path: str) -> bool:
        """Извлечение звука из видеофайла"""
        try:
            subprocess.run(
                [
                    'ffmpeg', '-i', video_path,
                    '-vn',
                    '-acodec', 'libmp3lame',
                    '-ab', '64k',
                    '-ar', str(config.AUDIO_SAMPLE_RATE),
                    '-ac', '1',
                    '-y',
                    output_path
                ],
                capture_output=True,
                timeout=300
            )
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                logger.info(f"Audio extracted successfully: {output_path}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Audio extraction error: {e}")
            return False


video_processor = VideoProcessor()


# ============================================================================
# YOUTUBE & VIDEO PLATFORMS
# ============================================================================

class VideoPlatformProcessor:
    """Обработка видео с YouTube, TikTok, Rutube и т.д."""
    
    @staticmethod
    def _validate_url(url: str) -> Tuple[bool, Optional[str]]:
        """Проверить и определить тип видеоплатформы"""
        url = url.strip()
        
        platforms = {
            'youtube': ['youtube.com', 'youtu.be', 'm.youtube.com', 'youtube.com/shorts'],
            'tiktok': ['tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'],
            'rutube': ['rutube.ru'],
            'instagram': ['instagram.com', 'instagr.am', 'instagram.com/reel/'],
            'vimeo': ['vimeo.com']
        }
        
        for platform, domains in platforms.items():
            if any(domain in url.lower() for domain in domains):
                logger.info(f"URL validated as {platform}")
                return True, platform
        
        logger.debug("URL not recognized as video platform")
        return False, None
    
    @staticmethod
    def _extract_youtube_video_id(url: str) -> Optional[str]:
        """Извлечение video_id из YouTube URL"""
        patterns = [
            r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]+)',
            r'youtube\.com\/embed\/([a-zA-Z0-9_-]+)',
            r'youtube\.com\/shorts\/([a-zA-Z0-9_-]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None
    
    @staticmethod
    async def extract_youtube_subtitles(video_id: str) -> Optional[str]:
        """Извлечение субтитров из YouTube видео"""
        if not YT_TRANSCRIPT_API_AVAILABLE:
            return None
        
        try:
            for lang in config.YOUTUBE_SUBTITLES_LANGS:
                try:
                    api_lang = lang.replace('a.', '')
                    transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[api_lang])
                    text = " ".join([item['text'] for item in transcript])
                    logger.info(f"YouTube subtitles extracted ({lang}): {len(text)} chars")
                    return text
                except Exception:
                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"Error extracting YouTube subtitles: {e}")
            return None
    
    @staticmethod
    async def download_audio_with_ytdlp(url: str, output_path: str) -> Optional[str]:
        """Скачивание аудио с YouTube с обходом блокировок"""
        if not YT_DLP_AVAILABLE:
            logger.error("yt-dlp not installed")
            return None
        
        try:
            # Усиленные опции для обхода блокировок
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_path,
                'quiet': config.YTDLP_QUIET,
                'no_warnings': config.YTDLP_NO_WARNINGS,
                'socket_timeout': config.YTDLP_SOCKET_TIMEOUT,
                'noplaylist': True,
                'retries': 10,
                'fragment_retries': 10,
                
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '64',
                }],
                
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': '"Windows"',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Upgrade-Insecure-Requests': '1',
                    'Connection': 'keep-alive',
                },
                
                'extractor_args': {
                    'youtube': {
                        'player_client': ['android', 'web', 'ios'],
                        'skip': ['hls', 'dash'],
                    }
                },
                
                'extractor_retries': 5,
                'file_access_retries': 5,
                'throttledratelimit': 1000000,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"Downloading audio from: {url}")
                
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: ydl.download([url]))
                except Exception as e:
                    logger.error(f"First download attempt failed: {e}")
                    # Пробуем с запасными опциями
                    ydl_opts['extractor_args']['youtube']['player_client'] = ['android']
                    ydl_opts['format'] = 'worstaudio/worst'
                    logger.info("Trying fallback with android client...")
                    await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))
                
                mp3_path = output_path + '.mp3'
                if os.path.exists(mp3_path):
                    logger.info(f"Audio downloaded: {mp3_path}")
                    return mp3_path
                
                for ext in ['.m4a', '.webm', '.opus']:
                    test_path = output_path + ext
                    if os.path.exists(test_path):
                        logger.info(f"Audio downloaded (as {ext}): {test_path}")
                        return test_path
                
                return None
                
        except Exception as e:
            logger.error(f"Error downloading audio: {e}")
            return None
    
    @staticmethod
    async def process_video_url(url: str, groq_clients: list) -> str:
        """Обработка ссылки на видео"""
        
        is_valid, platform = VideoPlatformProcessor._validate_url(url)
        if not is_valid:
            return config.ERROR_INVALID_URL
        
        logger.info(f"Processing {platform} video: {url}")
        
        try:
            if platform == 'youtube' and config.YOUTUBE_PREFER_SUBTITLES:
                video_id = VideoPlatformProcessor._extract_youtube_video_id(url)
                if video_id:
                    subtitles = await VideoPlatformProcessor.extract_youtube_subtitles(video_id)
                    if subtitles and len(subtitles.strip()) > config.MIN_TEXT_LENGTH:
                        logger.info(f"Using YouTube subtitles")
                        return subtitles
            
            temp_audio_path = f"{config.TEMP_DIR}/audio_{int(time.time())}_{os.getpid()}"
            
            audio_path = await VideoPlatformProcessor.download_audio_with_ytdlp(url, temp_audio_path)
            
            if not audio_path or not os.path.exists(audio_path):
                return config.ERROR_VIDEO_NOT_FOUND
            
            with open(audio_path, 'rb') as f:
                audio_bytes = f.read()
            
            text = await transcribe_voice(audio_bytes, groq_clients)
            
            try:
                os.remove(audio_path)
            except:
                pass
            
            return text
                
        except Exception as e:
            logger.error(f"Error processing video platform: {e}")
            return f"❌ Ошибка обработки видео: {str(e)[:100]}"


video_platform_processor = VideoPlatformProcessor()


# ============================================================================
# AUDIO TRANSCRIPTION
# ============================================================================

async def transcribe_voice(audio_bytes: bytes, groq_clients: list) -> str:
    """Транскрибация голоса через Whisper"""
    
    async def transcribe(client):
        response = await client.audio.transcriptions.create(
            model=config.GROQ_MODELS["transcription"],
            file=("audio.ogg", audio_bytes, "audio/ogg"),
            language=config.AUDIO_LANGUAGE,
            response_format="text",
            temperature=config.MODEL_TEMPERATURES["transcription"],
        )
        return response
    
    try:
        result = await _make_groq_request(groq_clients, transcribe)
        return result
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return f"❌ Ошибка распознавания: {str(e)[:100]}"


# ============================================================================
# TEXT PROCESSING - CORRECTION
# ============================================================================

async def correct_text_basic(text: str, groq_clients: list) -> str:
    """Базовая коррекция: openai/gpt-oss-20b"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    async def correct(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["basic"],
            messages=[{"role": "user", "content": config.BASIC_CORRECTION_PROMPT + f"\n\nТекст:\n{text}"}],
            temperature=config.MODEL_TEMPERATURES["basic"],
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await _make_groq_request(groq_clients, correct)
    except Exception as e:
        logger.error(f"Basic correction error: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


async def correct_text_premium(text: str, groq_clients: list) -> str:
    """Премиум коррекция: llama-3.3-70b-versatile"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    async def correct(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["premium"],
            messages=[{"role": "user", "content": config.PREMIUM_CORRECTION_PROMPT + f"\n\nТекст:\n{text}"}],
            temperature=config.MODEL_TEMPERATURES["premium"],
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await _make_groq_request(groq_clients, correct)
    except Exception as e:
        logger.error(f"Premium correction error: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


# ============================================================================
# TEXT PROCESSING - SUMMARIZATION & DIALOG
# ============================================================================

async def summarize_text(text: str, groq_clients: list) -> str:
    """Создание саммари через OSS 120B"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    words_count = len(text.split())
    if words_count < config.MIN_WORDS_FOR_SUMMARY or len(text) < config.MIN_CHARS_FOR_SUMMARY:
        return config.ERROR_TEXT_TOO_SHORT_FOR_SUMMARY
    
    async def summarize(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["reasoning"],
            messages=[{"role": "user", "content": config.SUMMARIZATION_PROMPT + f"\n\nТекст:\n{text}"}],
            temperature=config.MODEL_TEMPERATURES["reasoning"],
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await _make_groq_request(groq_clients, summarize)
    except Exception as e:
        logger.error(f"Summarization error: {e}")
        return f"❌ Ошибка создания саммари: {str(e)[:100]}"


def save_document_for_dialog(user_id: int, msg_id: int, full_text: str):
    """Сохраняем документ для возможности диалога"""
    if user_id not in document_dialogues:
        document_dialogues[user_id] = {}
    
    document_dialogues[user_id][msg_id] = {
        "full_text": full_text,
        "history": [],
        "timestamp": time.time()
    }
    logger.info(f"Saved document for dialog: user={user_id}, msg={msg_id}")


async def answer_document_question(
    user_id: int,
    msg_id: int,
    question: str,
    groq_clients: list
) -> str:
    """Ответ на вопрос по документу с сохранением контекста диалога"""
    
    if user_id not in document_dialogues or msg_id not in document_dialogues[user_id]:
        return "❌ Документ не найден. Сначала загрузите документ и сделайте саммари."
    
    doc_data = document_dialogues[user_id][msg_id]
    full_text = doc_data["full_text"]
    history = doc_data.get("history", [])
    
    dialog_context = ""
    if history:
        dialog_context = "Предыдущий диалог:\n"
        for turn in history[-config.MAX_DIALOG_HISTORY:]:
            dialog_context += f"Пользователь: {turn['question']}\n"
            dialog_context += f"Ассистент: {turn['answer']}\n\n"
    
    qa_prompt = f"""Ты - ассистент, который отвечает на вопросы по содержанию документа.

Документ:
{full_text[:10000]}

{dialog_context}

Вопрос пользователя: {question}

Ответь на вопрос, используя только информацию из документа. Если ответа нет в документе, так и скажи.
Ответ должен быть подробным, но по существу."""
    
    async def answer(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["reasoning"],
            messages=[{"role": "user", "content": qa_prompt}],
            temperature=config.MODEL_TEMPERATURES["reasoning"],
        )
        return response.choices[0].message.content.strip()
    
    try:
        answer_text = await _make_groq_request(groq_clients, answer)
        
        history.append({
            "question": question,
            "answer": answer_text,
            "timestamp": time.time()
        })
        doc_data["history"] = history[-config.MAX_DIALOG_HISTORY:]
        
        return answer_text
        
    except Exception as e:
        logger.error(f"QA error: {e}")
        return f"❌ Ошибка при ответе на вопрос: {str(e)[:100]}"


# ============================================================================
# FILE PROCESSING
# ============================================================================

async def process_video_file(video_bytes: bytes, filename: str, groq_clients: list) -> str:
    """Обработка локального видеофайла"""
    
    try:
        file_ext = filename.split('.')[-1] if '.' in filename else 'mp4'
        temp_video_path = f"{config.TEMP_DIR}/video_{int(time.time())}_{os.getpid()}.{file_ext}"
        temp_audio_path = f"{config.TEMP_DIR}/audio_{int(time.time())}_{os.getpid()}.mp3"
        
        with open(temp_video_path, 'wb') as f:
            f.write(video_bytes)
        
        duration = await video_processor.check_video_duration(temp_video_path)
        if duration and duration > config.VIDEO_MAX_DURATION:
            os.remove(temp_video_path)
            return config.ERROR_VIDEO_TOO_LONG
        
        if not await video_processor.extract_audio_from_video(temp_video_path, temp_audio_path):
            os.remove(temp_video_path)
            return "❌ Ошибка извлечения звука из видео"
        
        with open(temp_audio_path, 'rb') as f:
            audio_bytes = f.read()
        
        text = await transcribe_voice(audio_bytes, groq_clients)
        
        try:
            os.remove(temp_video_path)
            os.remove(temp_audio_path)
        except:
            pass
        
        return text
        
    except Exception as e:
        logger.error(f"Error processing video file: {e}")
        return f"❌ Ошибка обработки видеофайла: {str(e)[:100]}"


async def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Извлечение текста из PDF с помощью pdfplumber"""
    if not PDFPLUMBER_AVAILABLE:
        return "❌ Для работы с PDF требуется установить pdfplumber"
    
    try:
        pdf_buffer = io.BytesIO(pdf_bytes)
        text = ""
        page_count = 0
        
        with pdfplumber.open(pdf_buffer) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                if config.PDF_MAX_PAGES and page_num > config.PDF_MAX_PAGES:
                    break
                
                page_text = page.extract_text()
                if page_text:
                    text += f"\n--- Страница {page_num} ---\n"
                    text += page_text + "\n"
                
                # ИСПРАВЛЕНО: tables -> find_tables()
                tables = page.find_tables()
                if tables:
                    for table_idx, table in enumerate(tables, 1):
                        text += f"\n[Таблица {table_idx} на странице {page_num}]\n"
                        table_data = table.extract()
                        for row in table_data:
                            if row:
                                text += " | ".join(str(cell) if cell else "" for cell in row) + "\n"
                
                page_count += 1
        
        if not text.strip():
            return "❌ Не удалось извлечь текст из PDF"
        
        logger.info(f"Extracted text from {page_count} PDF pages")
        return text.strip()
        
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return f"❌ Ошибка обработки PDF: {str(e)}"


async def extract_text_from_docx(docx_bytes: bytes) -> str:
    """Извлечение текста из DOCX"""
    if not DOCX_AVAILABLE:
        return "❌ Для работы с DOCX требуется установить python-docx"
    
    try:
        doc_buffer = io.BytesIO(docx_bytes)
        doc = docx.Document(doc_buffer)
        text = ""
        
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                text += paragraph.text + "\n"
        
        if not text.strip():
            return "❌ Документ пуст"
        
        return text.strip()
        
    except Exception as e:
        logger.error(f"DOCX extraction error: {e}")
        return f"❌ Ошибка обработки DOCX: {str(e)}"


async def extract_text_from_txt(txt_bytes: bytes) -> str:
    """Извлечение текста из TXT"""
    try:
        encodings = ['utf-8', 'cp1251', 'koi8-r', 'windows-1251']
        
        for encoding in encodings:
            try:
                return txt_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        
        return txt_bytes.decode('utf-8', errors='ignore')
        
    except Exception as e:
        logger.error(f"TXT reading error: {e}")
        return f"❌ Ошибка чтения текстового файла: {str(e)}"


async def extract_text_from_file(file_bytes: bytes, filename: str, groq_clients: list) -> str:
    """Определяем тип файла и извлекаем текст"""
    
    mime_type, _ = mimetypes.guess_type(filename)
    file_ext = filename.lower().split('.')[-1] if '.' in filename else ''
    
    if mime_type and mime_type.startswith('image/'):
        logger.info(f"Processing image: {filename}")
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)
    
    if file_ext in ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp']:
        logger.info(f"Processing image (by extension): {filename}")
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)
    
    if file_ext in config.VIDEO_SUPPORTED_FORMATS:
        logger.info(f"Processing video file: {filename}")
        return await process_video_file(file_bytes, filename, groq_clients)
    
    if mime_type == 'application/pdf' or file_ext == 'pdf':
        logger.info(f"Processing PDF: {filename}")
        return await extract_text_from_pdf(file_bytes)
    
    if mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' or file_ext == 'docx':
        logger.info(f"Processing DOCX: {filename}")
        return await extract_text_from_docx(file_bytes)
    
    if mime_type == 'text/plain' or file_ext == 'txt':
        logger.info(f"Processing TXT: {filename}")
        return await extract_text_from_txt(file_bytes)
    
    if file_ext == 'doc':
        return config.ERROR_DOC_NOT_SUPPORTED
    
    logger.warning(f"Unsupported file format: {file_ext}")
    return config.ERROR_UNSUPPORTED_FORMAT


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

async def _make_groq_request(groq_clients: list, func, *args, **kwargs):
    """Делаем запрос с перебором ключей"""
    
    if not groq_clients:
        raise Exception("No Groq clients available")
    
    errors = []
    client_count = len(groq_clients)
    
    for attempt in range(client_count * config.GROQ_RETRY_COUNT):
        client = groq_clients[attempt % client_count]
        
        try:
            return await func(client, *args, **kwargs)
        except Exception as e:
            error_msg = str(e)
            errors.append(error_msg)
            logger.warning(f"Request error (attempt {attempt + 1}): {error_msg[:100]}")
            await asyncio.sleep(1 + (attempt % 3))
    
    raise Exception(f"All clients failed: {'; '.join(errors[:3])}")


def get_available_modes(text: str) -> list:
    """Определяем доступные режимы обработки"""
    words_count = len(text.split())
    text_length = len(text)
    
    available = ["basic", "premium"]
    
    if words_count >= config.MIN_WORDS_FOR_SUMMARY and text_length >= config.MIN_CHARS_FOR_SUMMARY:
        available.append("summary")
    
    return available


# ============================================================================
# ЭКСПОРТ
# ============================================================================

__all__ = [
    'transcribe_voice',
    'correct_text_basic',
    'correct_text_premium',
    'summarize_text',
    'extract_text_from_file',
    'get_available_modes',
    'vision_processor',
    'video_platform_processor',
    'save_document_for_dialog',
    'answer_document_question',
    'document_dialogues',
]