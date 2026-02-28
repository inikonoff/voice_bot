# processors.py
"""
Обработчики текста и видео: OCR, транскрибация, видео, коррекция, саммари, диалог
Версия 4.4 - исправлена совместимость с bot.py, улучшена обработка ключей
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
from typing import Optional, Tuple, List, Dict, Any, AsyncGenerator
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ GROQ
# ============================================================================

async def _make_groq_request(groq_clients: list, func, *args, **kwargs):
    """Делаем запрос с перебором ключей и улучшенной обработкой ошибок"""
    
    if not groq_clients:
        error_msg = "Нет доступных Groq клиентов"
        logger.error(error_msg)
        raise Exception(error_msg)
    
    errors = []
    client_count = len(groq_clients)
    
    for attempt in range(client_count * config.GROQ_RETRY_COUNT):
        client_index = attempt % client_count
        client = groq_clients[client_index]
        
        try:
            logger.debug(f"Попытка {attempt + 1} с клиентом {client_index}")
            return await func(client, *args, **kwargs)
        except Exception as e:
            error_msg = str(e)
            errors.append(f"Клиент {client_index}: {error_msg[:100]}")
            logger.warning(f"Ошибка запроса (попытка {attempt + 1}): {error_msg[:100]}")
            
            # Если ошибка 429 (слишком много запросов) - ждем дольше
            if "429" in error_msg or "rate_limit" in error_msg.lower():
                wait_time = 5 + (attempt * 2)
                logger.info(f"Rate limit, ждем {wait_time}с...")
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(1 + (attempt % 3))
    
    error_text = f"Все клиенты недоступны: {'; '.join(errors[:3])}"
    logger.error(error_text)
    raise Exception(error_text)


def _truncate_text_for_model(text: str, model_type: str) -> str:
    """Обрезает текст в зависимости от лимитов модели"""
    # Лимиты из документации Groq
    model_limits = {
        "basic": 5000,      # llama-3.1-8b-instant - 6K TPM, оставляем запас
        "premium": 10000,    # llama-3.3-70b-versatile - 12K TPM
        "reasoning": 25000,  # llama-4-scout - 30K TPM
    }
    
    limit = model_limits.get(model_type, 5000)
    
    if len(text) > limit:
        logger.warning(f"Текст обрезан с {len(text)} до {limit} символов для {model_type}")
        return text[:limit] + "... [текст обрезан из-за лимитов API]"
    return text


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
        logger.info(f"VisionProcessor инициализирован с {len(groq_clients)} клиентами")
    
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
    def _format_timecode(seconds: float) -> str:
        """Форматирует секунды в [ЧЧ:ММ:СС] или [ММ:СС]"""
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"[{h:02d}:{m:02d}:{s:02d}]"
        return f"[{m:02d}:{s:02d}]"

    @staticmethod
    async def extract_youtube_subtitles(video_id: str, with_timecodes: bool = True) -> Optional[str]:
        """Извлечение субтитров из YouTube видео (с таймкодами по умолчанию)"""
        if not YT_TRANSCRIPT_API_AVAILABLE:
            return None
        
        try:
            for lang in config.YOUTUBE_SUBTITLES_LANGS:
                try:
                    api_lang = lang.replace('a.', '')
                    transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[api_lang])
                    
                    if with_timecodes:
                        lines = []
                        for item in transcript:
                            tc = VideoPlatformProcessor._format_timecode(item['start'])
                            text = item['text'].replace('\n', ' ').strip()
                            if text:
                                lines.append(f"{tc} {text}")
                        text = "\n".join(lines)
                    else:
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
            # Пути к файлу с куки (проверяем несколько мест)
            cookie_paths = [
                os.environ.get("YTDLP_COOKIES_FILE", ""),
                "youtube_cookies.txt",
                "/app/youtube_cookies.txt",
                "/data/youtube_cookies.txt",
            ]
            cookie_file = next((p for p in cookie_paths if p and os.path.exists(p)), None)
            
            # PO Token и Visitor Data из config/env
            po_token = config.PO_TOKEN
            visitor_data = config.VISITOR_DATA
            
            # Формируем extractor_args в зависимости от наличия PO Token
            if po_token:
                # С PO Token используем mweb — наиболее стабильный вариант
                extractor_args = {
                    'youtube': {
                        'player_client': ['mweb'],
                        'po_token': [f'mweb+https://www.youtube.com/={po_token}'],
                    }
                }
                logger.info("yt-dlp: using mweb client with PO Token")
            else:
                # Без PO Token используем tv_embedded + web — они пока не требуют токена
                extractor_args = {
                    'youtube': {
                        'player_client': ['tv_embedded', 'web'],
                    }
                }
                logger.info("yt-dlp: using tv_embedded/web clients (no PO Token)")
            
            # Добавляем visitor_data если есть
            if visitor_data:
                extractor_args['youtube']['visitor_data'] = [visitor_data]
            
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
                
                'extractor_args': extractor_args,
                
                # Заголовки
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9',
                },
                
                'extractor_retries': 5,
                'file_access_retries': 5,
                'throttledratelimit': 1000000,
            }
            
            # Добавляем куки только если файл реально существует
            if cookie_file:
                ydl_opts['cookiefile'] = cookie_file
                logger.info(f"yt-dlp: using cookies from {cookie_file}")
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"Downloading audio from: {url}")
                
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: ydl.download([url]))
                
                mp3_path = output_path + '.mp3'
                if os.path.exists(mp3_path):
                    logger.info(f"Audio downloaded: {mp3_path}")
                    return mp3_path
                
                # Проверяем другие возможные расширения
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
    async def process_video_url(url: str, groq_clients: list, with_timecodes: bool = True) -> str:
        """Обработка ссылки на видео.
        
        with_timecodes=True — текст будет содержать таймкоды вида [MM:SS] Текст...
        """
        
        is_valid, platform = VideoPlatformProcessor._validate_url(url)
        if not is_valid:
            return config.ERROR_INVALID_URL
        
        logger.info(f"Processing {platform} video: {url}")
        
        try:
            if platform == 'youtube' and config.YOUTUBE_PREFER_SUBTITLES:
                video_id = VideoPlatformProcessor._extract_youtube_video_id(url)
                if video_id:
                    subtitles = await VideoPlatformProcessor.extract_youtube_subtitles(
                        video_id, with_timecodes=with_timecodes
                    )
                    if subtitles and len(subtitles.strip()) > config.MIN_TEXT_LENGTH:
                        logger.info("Using YouTube subtitles")
                        return subtitles
            
            temp_audio_path = f"{config.TEMP_DIR}/audio_{int(time.time())}_{os.getpid()}"
            
            audio_path = await VideoPlatformProcessor.download_audio_with_ytdlp(url, temp_audio_path)
            
            if not audio_path or not os.path.exists(audio_path):
                return config.ERROR_VIDEO_NOT_FOUND
            
            with open(audio_path, 'rb') as f:
                audio_bytes = f.read()
            
            # Для YouTube и других платформ — с таймкодами через verbose_json
            text = await transcribe_voice(audio_bytes, groq_clients, with_timecodes=with_timecodes)
            
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

def _format_timecode(seconds: float) -> str:
    """Форматирует секунды в [ЧЧ:ММ:СС] или [ММ:СС]"""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _segments_to_timecoded_text(segments: list) -> str:
    """Преобразует сегменты Whisper в текст с таймкодами"""
    lines = []
    for seg in segments:
        tc = _format_timecode(seg.get("start", 0))
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"{tc} {text}")
    return "\n".join(lines)


async def transcribe_voice(audio_bytes: bytes, groq_clients: list, with_timecodes: bool = False) -> str:
    """Транскрибация голоса через Whisper.
    
    with_timecodes=True — возвращает текст с таймкодами вида [MM:SS] Текст...
    """
    
    async def transcribe(client):
        if with_timecodes:
            response = await client.audio.transcriptions.create(
                model=config.GROQ_MODELS["transcription"],
                file=("audio.ogg", audio_bytes, "audio/ogg"),
                language=config.AUDIO_LANGUAGE,
                response_format="verbose_json",
                temperature=config.MODEL_TEMPERATURES["transcription"],
            )
            # verbose_json возвращает объект с полем segments
            segments = getattr(response, "segments", None)
            if segments:
                return _segments_to_timecoded_text(segments)
            # Fallback: если сегментов нет — просто текст
            return getattr(response, "text", str(response))
        else:
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
    """Базовая коррекция: llama-3.1-8b-instant"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    # Обрезаем для Basic модели
    text = _truncate_text_for_model(text, "basic")
    
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
        # Если ошибка 413 (лимит), пробуем еще сильнее обрезать
        if "413" in str(e) or "rate_limit_exceeded" in str(e):
            logger.warning("Rate limit exceeded, trying with shorter text")
            shorter_text = text[:3000] + "... [сильно обрезано]"
            async def retry_correct(client):
                response = await client.chat.completions.create(
                    model=config.GROQ_MODELS["basic"],
                    messages=[{"role": "user", "content": config.BASIC_CORRECTION_PROMPT + f"\n\nТекст:\n{shorter_text}"}],
                    temperature=config.MODEL_TEMPERATURES["basic"],
                )
                return response.choices[0].message.content.strip()
            return await _make_groq_request(groq_clients, retry_correct)
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


async def correct_text_premium(text: str, groq_clients: list) -> str:
    """Премиум коррекция: llama-3.3-70b-versatile"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    # Обрезаем для Premium модели
    text = _truncate_text_for_model(text, "premium")
    
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
        if "413" in str(e) or "rate_limit_exceeded" in str(e):
            logger.warning("Rate limit exceeded, trying with shorter text")
            shorter_text = text[:5000] + "... [сильно обрезано]"
            async def retry_correct(client):
                response = await client.chat.completions.create(
                    model=config.GROQ_MODELS["premium"],
                    messages=[{"role": "user", "content": config.PREMIUM_CORRECTION_PROMPT + f"\n\nТекст:\n{shorter_text}"}],
                    temperature=config.MODEL_TEMPERATURES["premium"],
                )
                return response.choices[0].message.content.strip()
            return await _make_groq_request(groq_clients, retry_correct)
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


# ============================================================================
# TEXT PROCESSING - SUMMARIZATION & DIALOG
# ============================================================================

async def summarize_text(text: str, groq_clients: list) -> str:
    """Создание саммари через Llama-4-Scout (30K TPM)"""
    
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    
    words_count = len(text.split())
    if words_count < config.MIN_WORDS_FOR_SUMMARY or len(text) < config.MIN_CHARS_FOR_SUMMARY:
        return config.ERROR_TEXT_TOO_SHORT_FOR_SUMMARY
    
    # Обрезаем для Reasoning модели (Scout имеет 30K лимит)
    text = _truncate_text_for_model(text, "reasoning")
    
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
        if "413" in str(e) or "rate_limit_exceeded" in str(e):
            logger.warning("Rate limit exceeded for summarization, trying with shorter text")
            shorter_text = text[:10000] + "... [сильно обрезано]"
            async def retry_summarize(client):
                response = await client.chat.completions.create(
                    model=config.GROQ_MODELS["reasoning"],
                    messages=[{"role": "user", "content": config.SUMMARIZATION_PROMPT + f"\n\nТекст:\n{shorter_text}"}],
                    temperature=config.MODEL_TEMPERATURES["reasoning"],
                )
                return response.choices[0].message.content.strip()
            return await _make_groq_request(groq_clients, retry_summarize)
        return f"❌ Ошибка создания саммари: {str(e)[:100]}"


# ============================================================================
# ДИАЛОГОВЫЙ РЕЖИМ - УЛУЧШЕННАЯ ВЕРСИЯ С ПОДДЕРЖКОЙ РАЗНЫХ КЛЮЧЕЙ
# ============================================================================

def save_document_for_dialog(user_id: int, msg_id: int, document_text: str, source: str = "unknown"):
    """
    Сохраняем документ для возможности диалога
    Поддерживает разные форматы ключей для совместимости
    """
    if user_id not in document_dialogues:
        document_dialogues[user_id] = {}
    
    # Сохраняем с несколькими ключами для максимальной совместимости
    document_dialogues[user_id][msg_id] = {
        "full_text": document_text,        # Основной ключ для processors.py
        "text": document_text,              # Для совместимости с bot.py
        "original": document_text,          # Еще один вариант из bot.py
        "history": [],
        "timestamp": time.time(),
        "source": source
    }
    
    logger.info(f"💾 Сохранен документ для диалога: user={user_id}, msg={msg_id}, длина={len(document_text)}")
    return document_dialogues[user_id][msg_id]


def get_document_text(user_id: int, msg_id: int) -> Optional[str]:
    """
    Получить текст документа из хранилища, пробуя разные ключи
    """
    if user_id not in document_dialogues or msg_id not in document_dialogues[user_id]:
        return None
    
    doc_data = document_dialogues[user_id][msg_id]
    
    # Пробуем разные возможные ключи
    for key in ["full_text", "text", "original"]:
        if key in doc_data and doc_data[key]:
            return doc_data[key]
    
    return None


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
    
    # Получаем текст документа через универсальную функцию
    full_text = get_document_text(user_id, msg_id)
    if not full_text:
        return "❌ Не удалось извлечь текст документа."
    
    history = doc_data.get("history", [])
    
    # Обрезаем документ для запроса
    if len(full_text) > 20000:
        doc_preview = full_text[:20000] + "... [документ обрезан для обработки]"
        logger.warning(f"Document truncated from {len(full_text)} to 20000 chars for dialog")
    else:
        doc_preview = full_text
    
    dialog_context = ""
    if history:
        dialog_context = "Предыдущий диалог:\n"
        for turn in history[-config.MAX_DIALOG_HISTORY:]:
            # Поддержка обоих форматов истории
            q = turn.get('question') or turn.get('q', '')
            a = turn.get('answer') or turn.get('a', '')
            dialog_context += f"Пользователь: {q}\n"
            dialog_context += f"Ассистент: {a}\n\n"
    
    qa_prompt = f"""Ты - ассистент, который отвечает на вопросы по содержанию документа.

Документ:
{doc_preview}

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
        
        # Сохраняем в едином формате
        history.append({
            "question": question,
            "answer": answer_text,
            "q": question,  # Для обратной совместимости
            "a": answer_text,
            "timestamp": time.time()
        })
        doc_data["history"] = history[-config.MAX_DIALOG_HISTORY:]
        
        return answer_text
        
    except Exception as e:
        logger.error(f"QA error: {e}")
        return f"❌ Ошибка при ответе на вопрос: {str(e)[:100]}"


async def stream_document_answer(
    user_id: int,
    msg_id: int,
    question: str,
    groq_clients: list
) -> AsyncGenerator[str, None]:
    """
    Стриминг ответа на вопрос по документу
    Исправлена версия с поддержкой разных форматов хранения
    """
    
    # Проверка наличия Groq клиентов
    if not groq_clients:
        yield "❌ Ошибка: нет доступных Groq клиентов"
        return

    # Проверка наличия документа
    if user_id not in document_dialogues:
        logger.error(f"User {user_id} not found in document_dialogues")
        yield "❌ Документ не найден. Сначала загрузите документ."
        return

    if msg_id not in document_dialogues[user_id]:
        logger.error(f"Msg {msg_id} not found for user {user_id}")
        yield "❌ Документ не найден. Сначала загрузите документ."
        return

    doc_data = document_dialogues[user_id][msg_id]
    
    # Получаем текст документа через универсальную функцию
    full_text = get_document_text(user_id, msg_id)
    if not full_text:
        logger.error(f"No text found in doc_data for user {user_id}, msg {msg_id}")
        yield "❌ Не удалось извлечь текст документа."
        return
    
    history = doc_data.get("history", [])

    # Формируем контекст из истории
    context = ""
    for turn in history[-5:]:
        # Поддержка обоих форматов ключей
        q = turn.get('question') or turn.get('q', '')
        a = turn.get('answer') or turn.get('a', '')
        if q and a:
            context += f"Вопрос: {q}\nОтвет: {a}\n\n"

    # Обрезаем документ
    if len(full_text) > 20000:
        doc_preview = full_text[:20000] + "... [документ обрезан]"
    else:
        doc_preview = full_text

    prompt = f"""Ты - ассистент, который отвечает на вопросы по содержанию документа.

Документ:
{doc_preview}

{context}

Вопрос:
{question}

Ответь на вопрос, используя только информацию из документа. Если ответа нет в документе, так и скажи.
Ответ должен быть подробным, но по существу."""

    # Получаем клиент для стриминга
    client_index = 0
    client = groq_clients[client_index % len(groq_clients)]
    
    try:
        logger.info(f"Starting stream for user {user_id}, msg {msg_id}")
        stream = await client.chat.completions.create(
            model=config.GROQ_MODELS["reasoning"],
            messages=[
                {"role": "system", "content": "Ты отвечаешь строго по документу."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            stream=True,
        )

        full_answer = ""
        chunk_count = 0

        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                piece = chunk.choices[0].delta.content
                full_answer += piece
                chunk_count += 1
                yield piece

        logger.info(f"Stream completed: {chunk_count} chunks, {len(full_answer)} chars")

        # Сохраняем в историю в обоих форматах
        history.append({
            "question": question,
            "answer": full_answer,
            "q": question,
            "a": full_answer,
            "timestamp": time.time()
        })
        doc_data["history"] = history[-config.MAX_DIALOG_HISTORY:]
        
    except Exception as e:
        logger.error(f"Ошибка в stream_document_answer: {e}", exc_info=True)
        yield f"❌ Ошибка при генерации ответа: {str(e)[:100]}"


# ============================================================================
# FILE PROCESSING
# ============================================================================

async def process_video_file(video_bytes: bytes, filename: str, groq_clients: list, with_timecodes: bool = False) -> str:
    """Обработка локального видеофайла.
    
    with_timecodes=True — текст будет содержать таймкоды.
    """
    
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
        
        text = await transcribe_voice(audio_bytes, groq_clients, with_timecodes=with_timecodes)
        
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
    
    # Изображения
    if mime_type and mime_type.startswith('image/'):
        logger.info(f"Processing image: {filename}")
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)
    
    if file_ext in ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp']:
        logger.info(f"Processing image (by extension): {filename}")
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)
    
    # Видео
    if file_ext in config.VIDEO_SUPPORTED_FORMATS:
        logger.info(f"Processing video file: {filename}")
        return await process_video_file(file_bytes, filename, groq_clients)
    
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
    
    # Старый DOC
    if file_ext == 'doc':
        return config.ERROR_DOC_NOT_SUPPORTED
    
    logger.warning(f"Unsupported file format: {file_ext}")
    return config.ERROR_UNSUPPORTED_FORMAT


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

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
    'stream_document_answer',
    'get_document_text',
    'document_dialogues',
    'PDFPLUMBER_AVAILABLE',
    'DOCX_AVAILABLE',
    'YT_DLP_AVAILABLE',
]
