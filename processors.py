# processors.py
"""
Обработчики текста и видео: OCR, транскрибация, видео, коррекция, саммари
Версия 3.0 с поддержкой YouTube, TikTok, Rutube, Instagram, Vimeo
"""

import io
import os
import logging
import base64
import asyncio
import subprocess
import mimetypes
import re
from typing import Optional, Tuple, List
from datetime import timedelta
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
# VIDEO PROCESSING
# ============================================================================

class VideoProcessor:
    """Обработка видеофайлов и видеоплатформ"""
    
    @staticmethod
    async def check_video_duration(filepath: str) -> Optional[float]:
        """Получить длительность видео в секундах"""
        try:
            result = subprocess.run(
                [
                    'ffprobe', '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1:nokey=1',
                    filepath
                ],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                duration = float(result.stdout.strip())
                logger.debug(f"Video duration: {duration}s ({timedelta(seconds=int(duration))})")
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
                    '-q:a', '9',  # Качество звука
                    '-n',  # Не перезаписывать файл
                    output_path
                ],
                capture_output=True,
                timeout=300  # 5 минут
            )
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                logger.info(f"Audio extracted successfully: {output_path}")
                return True
            
            logger.error("Audio extraction failed: output file is empty")
            return False
            
        except subprocess.TimeoutExpired:
            logger.error("Audio extraction timeout")
            return False
        except Exception as e:
            logger.error(f"Audio extraction error: {e}")
            return False
    
    @staticmethod
    async def normalize_audio(input_path: str, output_path: str) -> bool:
        """Нормализация громкости аудио"""
        try:
            subprocess.run(
                [
                    'ffmpeg', '-i', input_path,
                    '-af', 'loudnorm=I=-20:TP=-1.5:LRA=11',
                    '-acodec', 'libmp3lame',
                    '-q:a', '2',
                    output_path
                ],
                capture_output=True,
                timeout=300
            )
            
            return os.path.exists(output_path) and os.path.getsize(output_path) > 0
            
        except Exception as e:
            logger.warning(f"Audio normalization failed: {e}")
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
            'youtube': ['youtube.com', 'youtu.be'],
            'tiktok': ['tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'],
            'rutube': ['rutube.ru'],
            'instagram': ['instagram.com', 'instagr.am'],
            'vimeo': ['vimeo.com']
        }
        
        for platform, domains in platforms.items():
            if any(domain in url.lower() for domain in domains):
                return True, platform
        
        return False, None
    
    @staticmethod
    async def extract_youtube_subtitles(video_id: str) -> Optional[str]:
        """Извлечение субтитров из YouTube видео"""
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            
            # Пытаемся получить субтитры в порядке: русский → английский
            for lang in config.YOUTUBE_SUBTITLES_LANGS:
                try:
                    transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang])
                    text = " ".join([item['text'] for item in transcript])
                    logger.info(f"YouTube subtitles extracted ({lang}): {len(text)} chars")
                    return text
                except Exception as e:
                    logger.debug(f"Subtitles not found for language {lang}: {e}")
                    continue
            
            logger.info("No subtitles found for video")
            return None
            
        except ImportError:
            logger.error("youtube-transcript-api not installed")
            return None
        except Exception as e:
            logger.error(f"Error extracting YouTube subtitles: {e}")
            return None
    
    @staticmethod
    def _extract_youtube_video_id(url: str) -> Optional[str]:
        """Извлечение video_id из YouTube URL"""
        patterns = [
            r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]+)',
            r'youtube\.com\/embed\/([a-zA-Z0-9_-]+)',
            r'youtube\.com\/v\/([a-zA-Z0-9_-]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None
    
    @staticmethod
    async def download_video_with_ytdlp(url: str, output_path: str) -> Optional[str]:
        """Скачивание видео с помощью yt-dlp"""
        try:
            import yt_dlp
            
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'quiet': config.YTDLP_QUIET,
                'no_warnings': config.YTDLP_NO_WARNINGS,
                'socket_timeout': config.YTDLP_SOCKET_TIMEOUT,
                'outtmpl': output_path,
                'max_filesize': config.VIDEO_SIZE_LIMIT,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"Downloading video from: {url}")
                info = ydl.extract_info(url, download=True)
                video_path = ydl.prepare_filename(info)
                logger.info(f"Video downloaded: {video_path}")
                return video_path
                
        except ImportError:
            logger.error("yt-dlp not installed")
            return None
        except Exception as e:
            logger.error(f"Error downloading video: {e}")
            return None
    
    @staticmethod
    async def process_video_url(url: str, groq_clients: list) -> str:
        """Обработка ссылки на видео (YouTube, TikTok и т.д.)"""
        
        is_valid, platform = VideoPlatformProcessor._validate_url(url)
        if not is_valid:
            logger.warning(f"Invalid video URL: {url}")
            return config.ERROR_INVALID_URL
        
        logger.info(f"Processing {platform} video: {url}")
        
        try:
            # Для YouTube сначала пытаемся получить субтитры
            if platform == 'youtube' and config.YOUTUBE_PREFER_SUBTITLES:
                video_id = VideoPlatformProcessor._extract_youtube_video_id(url)
                if video_id:
                    subtitles = await VideoPlatformProcessor.extract_youtube_subtitles(video_id)
                    if subtitles and len(subtitles.strip()) > config.MIN_TEXT_LENGTH:
                        logger.info(f"Using YouTube subtitles for {video_id}")
                        return subtitles
                    else:
                        logger.info("No subtitles found, will extract audio")
            
            # Если нет субтитров или это другая платформа - скачиваем и извлекаем звук
            temp_video_path = f"{config.TEMP_DIR}/video_{os.getpid()}_{int(asyncio.get_event_loop().time())}.mp4"
            temp_audio_path = f"{config.TEMP_DIR}/audio_{os.getpid()}_{int(asyncio.get_event_loop().time())}.wav"
            
            try:
                video_path = await VideoPlatformProcessor.download_video_with_ytdlp(url, temp_video_path)
                
                if not video_path:
                    return config.ERROR_VIDEO_NOT_FOUND
                
                # Проверяем длительность
                duration = await video_processor.check_video_duration(video_path)
                if duration and duration > config.VIDEO_MAX_DURATION:
                    logger.warning(f"Video too long: {duration}s")
                    return config.ERROR_VIDEO_TOO_LONG
                
                # Извлекаем звук
                if not await video_processor.extract_audio_from_video(video_path, temp_audio_path):
                    return "❌ Ошибка извлечения звука из видео"
                
                # Транскрибируем
                with open(temp_audio_path, 'rb') as f:
                    audio_bytes = f.read()
                
                text = await transcribe_voice(audio_bytes, groq_clients)
                
                # Чистим временные файлы
                try:
                    os.remove(video_path)
                    os.remove(temp_audio_path)
                except:
                    pass
                
                return text
                
            except Exception as e:
                logger.error(f"Error processing video platform: {e}")
                # Чистим временные файлы
                for fpath in [temp_video_path, temp_audio_path]:
                    try:
                        if os.path.exists(fpath):
                            os.remove(fpath)
                    except:
                        pass
                return f"❌ Ошибка обработки видео: {str(e)[:100]}"
        
        except Exception as e:
            logger.error(f"Error in process_video_url: {e}")
            return f"❌ Ошибка обработки видеоссылки: {str(e)[:100]}"


video_platform_processor = VideoPlatformProcessor()


# ============================================================================
# LOCAL VIDEO FILE PROCESSING
# ============================================================================

async def process_video_file(video_bytes: bytes, filename: str, groq_clients: list) -> str:
    """Обработка локального видеофайла"""
    
    try:
        # Сохраняем во временный файл
        temp_video_path = f"{config.TEMP_DIR}/video_{os.getpid()}_{int(asyncio.get_event_loop().time())}.{filename.split('.')[-1]}"
        temp_audio_path = f"{config.TEMP_DIR}/audio_{os.getpid()}_{int(asyncio.get_event_loop().time())}.wav"
        
        with open(temp_video_path, 'wb') as f:
            f.write(video_bytes)
        
        # Проверяем длительность
        duration = await video_processor.check_video_duration(temp_video_path)
        if duration and duration > config.VIDEO_MAX_DURATION:
            logger.warning(f"Video too long: {duration}s")
            try:
                os.remove(temp_video_path)
            except:
                pass
            return config.ERROR_VIDEO_TOO_LONG
        
        # Извлекаем звук
        if not await video_processor.extract_audio_from_video(temp_video_path, temp_audio_path):
            try:
                os.remove(temp_video_path)
            except:
                pass
            return "❌ Ошибка извлечения звука из видео"
        
        # Транскрибируем
        with open(temp_audio_path, 'rb') as f:
            audio_bytes = f.read()
        
        text = await transcribe_voice(audio_bytes, groq_clients)
        
        # Чистим временные файлы
        try:
            os.remove(temp_video_path)
            os.remove(temp_audio_path)
        except:
            pass
        
        return text
        
    except Exception as e:
        logger.error(f"Error processing video file: {e}")
        return f"❌ Ошибка обработки видеофайла: {str(e)[:100]}"


# ============================================================================
# AUDIO TRANSCRIPTION (с поддержкой видео)
# ============================================================================

async def transcribe_voice(audio_bytes: bytes, groq_clients: list) -> str:
    """Транскрибация голоса через Whisper v3 (для видео и аудио)"""
    
    async def transcribe(client):
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
        
        # Проверка на смешивание языков
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
    
    if mixed_words > len(words) * 0.2:
        logger.warning(f"Possible language mix detected: {mixed_words} mixed words out of {len(words)}")
    
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
                
                page_text = page.extract_text()
                if page_text:
                    text += f"\n--- Страница {page_num} ---\n"
                    text += page_text + "\n"
                
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
        
        logger.warning("TXT decoded with fallback (errors ignored)")
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
            
            await asyncio.sleep(1 + (attempt % 3) * 0.5)
    
    error_summary = '; '.join(errors[:3])
    logger.error(f"All Groq clients failed: {error_summary}")
    raise Exception(f"All clients failed: {error_summary}")


def get_available_modes(text: str) -> list:
    """Определяем доступные режимы обработки"""
    words_count = len(text.split())
    text_length = len(text)
    
    available = ["basic", "premium"]
    
    if words_count >= config.MIN_WORDS_FOR_SUMMARY and text_length >= config.MIN_CHARS_FOR_SUMMARY:
        available.append("summary")
    
    return available
