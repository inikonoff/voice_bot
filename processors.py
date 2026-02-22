# processors.py
"""
Обработчики текста и видео: OCR, транскрибация, видео, коррекция, саммари, диалог
Версия 5.1 Enterprise Edition: с поддержкой распределенной обработки, умной ротации ключей и VAD.
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
import random
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

try:
    from pydub import AudioSegment
    from pydub.silence import split_on_silence
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False

logger = logging.getLogger(__name__)

# Хранилище для диалогов о документах (с вложенностью по msg_id)
document_dialogues: Dict[int, Dict[int, Dict[str, Any]]] = {}


# ============================================================================
# УМНОЕ УПРАВЛЕНИЕ GROQ КЛИЕНТАМИ
# ============================================================================

class GroqClientManager:
    """Управляет пулом Groq клиентов, ротацией ключей и обработкой ошибок."""

    def __init__(self):
        self._clients: List[AsyncOpenAI] = []
        self._client_health: Dict[int, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self, api_keys: str):
        async with self._lock:
            if self._initialized: # Избегаем повторной инициализации
                return
            
            keys = [key.strip() for key in api_keys.split(",") if key.strip()]
            if not keys:
                logger.warning("GROQ_API_KEYS не настроены!")
                return

            for i, key in enumerate(keys):
                try:
                    client = AsyncOpenAI(
                        api_key=key,
                        base_url="https://api.groq.com/openai/v1",
                        timeout=config.GROQ_TIMEOUT,
                    )
                    self._clients.append(client)
                    self._client_health[i] = {
                        "last_used": time.time(),
                        "failures": 0,
                        "rate_limited_until": 0,
                        "key": key[:8] + "..."
                    }
                    logger.info(f"✅ Groq клиент инициализирован: {key[:8]}...")
                except Exception as e:
                    logger.error(f"❌ Ошибка инициализации клиента {key[:8]}...: {e}")
            
            if not self._clients:
                logger.error("Все Groq клиенты не инициализированы. Проверьте ключи.")
                raise Exception("Нет доступных Groq клиентов")

            logger.info(f"✅ Всего Groq клиентов: {len(self._clients)}")
            self._initialized = True

    def get_available_client(self) -> Tuple[int, AsyncOpenAI]:
        """Возвращает наиболее подходящего клиента для запроса."""
        if not self._clients:
            raise Exception("Нет доступных Groq клиентов")

        available_clients = []
        current_time = time.time()

        for i, client in enumerate(self._clients):
            health = self._client_health[i]
            if health["rate_limited_until"] < current_time:
                available_clients.append((i, client, health["last_used"]))
        
        if not available_clients:
            # Все клиенты rate-limited, берем того, кто освободится раньше
            earliest_release_time = min(h["rate_limited_until"] for h in self._client_health.values())
            wait_time = max(0, earliest_release_time - current_time)
            logger.warning(f"Все Groq клиенты заняты или rate-limited. Ожидание {wait_time:.2f}с.")
            # В реальной системе здесь можно было бы поднять исключение или ждать
            # Для простоты, пока просто берем первого попавшегося и надеемся на лучшее
            # или можно выбрать клиента с наименьшим rate_limited_until
            sorted_clients = sorted(self._client_health.items(), key=lambda item: item[1]["rate_limited_until"])
            idx, _ = sorted_clients[0]
            return idx, self._clients[idx]

        # Выбираем клиента, который дольше всего не использовался (простая ротация)
        available_clients.sort(key=lambda x: x[2])
        idx, client, _ = available_clients[0]
        self._client_health[idx]["last_used"] = current_time
        return idx, client

    async def _make_request(self, func, *args, is_stream: bool = False, **kwargs):
        """Делает запрос с перебором ключей и улучшенной обработкой ошибок."""
        errors = []
        for attempt in range(len(self._clients) * config.GROQ_RETRY_COUNT):
            client_idx, client = self.get_available_client()
            health = self._client_health[client_idx]

            try:
                logger.debug(f"Попытка {attempt + 1} с клиентом {health['key']} (индекс {client_idx})")
                result = await func(client, *args, **kwargs)
                health["failures"] = 0 # Сброс счетчика ошибок при успехе
                return result
            except Exception as e:
                error_msg = str(e)
                errors.append(f"Клиент {health['key']}: {error_msg[:100]}")
                logger.warning(f"Ошибка запроса (попытка {attempt + 1}) с клиентом {health['key']}: {error_msg[:100]}")

                if "429" in error_msg or "rate_limit" in error_msg.lower():
                    health["rate_limited_until"] = time.time() + (5 * (health["failures"] + 1)) # Экспоненциальная задержка
                    health["failures"] += 1
                    logger.info(f"Клиент {health['key']} rate-limited до {health['rate_limited_until']:.2f}. Ожидание.")
                    await asyncio.sleep(2 ** health["failures"]) # Экспоненциальный backoff
                elif "content_filter" in error_msg.lower():
                    logger.error(f"Content filter error с клиентом {health['key']}. Пропускаем этот ключ для данной попытки.")
                    # Не увеличиваем failures, но пробуем другой ключ сразу
                    await asyncio.sleep(1) # Небольшая задержка перед следующей попыткой
                else:
                    health["failures"] += 1
                    await asyncio.sleep(1 + (attempt % 3)) # Небольшая задержка для других ошибок
        
        error_text = f"Все клиенты недоступны после {attempt + 1} попыток: {'; '.join(errors[:3])}"
        logger.error(error_text)
        raise Exception(error_text)

    async def make_request(self, func, *args, **kwargs):
        return await self._make_request(func, *args, is_stream=False, **kwargs)

    async def make_stream_request(self, func, *args, **kwargs) -> AsyncGenerator:
        """Специальная версия для стриминговых запросов."""
        errors = []
        for attempt in range(len(self._clients) * config.GROQ_RETRY_COUNT):
            client_idx, client = self.get_available_client()
            health = self._client_health[client_idx]

            try:
                logger.debug(f"Стриминг: попытка {attempt + 1} с клиентом {health['key']} (индекс {client_idx})")
                stream = await func(client, *args, **kwargs)
                
                # Пробуем получить первый чанк, чтобы убедиться, что ключ работает
                try:
                    first_chunk = await stream.__anext__()
                    health["failures"] = 0 # Сброс счетчика ошибок при успехе
                    async def result_generator():
                        yield first_chunk
                        async for chunk in stream:
                            yield chunk
                    return result_generator()
                except StopAsyncIteration:
                    logger.warning(f"Клиент {health['key']} вернул пустой стрим. Пробуем следующий.")
                    continue
                except Exception as e:
                    if "429" in str(e) or "rate_limit" in str(e).lower():
                        health["rate_limited_until"] = time.time() + (5 * (health["failures"] + 1))
                        health["failures"] += 1
                        logger.info(f"Клиент {health['key']} rate-limited до {health['rate_limited_until']:.2f}. Ожидание.")
                        await asyncio.sleep(2 ** health["failures"])
                        continue
                    elif "content_filter" in str(e).lower():
                        logger.error(f"Content filter error при стриминге с клиентом {health['key']}. Пропускаем.")
                        await asyncio.sleep(1)
                        continue
                    raise
                
            except Exception as e:
                error_msg = str(e)
                errors.append(f"Клиент {health['key']}: {error_msg[:100]}")
                logger.warning(f"Ошибка стриминга с клиентом {health['key']}: {error_msg[:100]}")

                if "429" in error_msg or "rate_limit" in error_msg.lower():
                    health["rate_limited_until"] = time.time() + (5 * (health["failures"] + 1))
                    health["failures"] += 1
                    logger.info(f"Клиент {health['key']} rate-limited до {health['rate_limited_until']:.2f}. Ожидание.")
                    await asyncio.sleep(2 ** health["failures"])
                    continue
                elif "content_filter" in error_msg.lower():
                    logger.error(f"Content filter error при стриминге с клиентом {health['key']}. Пропускаем.")
                    await asyncio.sleep(1)
                    continue
                else:
                    health["failures"] += 1
                    await asyncio.sleep(1 + (attempt % 3))
        
        error_text = f"Все клиенты недоступны для стриминга после {attempt + 1} попыток. Последняя ошибка: {'; '.join(errors[:3])}"
        logger.error(error_text)
        raise Exception(error_text)

groq_client_manager = GroqClientManager()

def _truncate_text_for_model(text: str, model_type: str) -> str:
    """Обрезает текст в зависимости от лимитов модели"""
    # Лимиты из документации Groq
    model_limits = {
        "basic": 5000,      # llama-3.1-8b-instant
        "premium": 10000,    # llama-3.3-70b-versatile
        "reasoning": 25000,  # llama-4-scout
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
    
    async def extract_text(self, image_bytes: bytes) -> str:
        if not groq_client_manager._initialized:
            logger.warning("GroqClientManager не инициализирован для Vision")
            return config.ERROR_NO_GROQ
        
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        
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
            return await groq_client_manager.make_request(extract)
        except Exception as e:
            logger.error(f"Vision OCR error: {e}")
            return f"❌ Ошибка распознавания текста: {str(e)[:100]}"


vision_processor = VisionProcessor()


# ============================================================================
# AUDIO PROCESSING (VAD, CHUNKING, TRANSCRIPTION)
# ============================================================================

class AudioProcessor:
    """Обработка аудио: VAD, нарезка на чанки, транскрибация."""

    def __init__(self):
        if not PYDUB_AVAILABLE:
            logger.warning("pydub не установлен. VAD и сжатие аудио будут недоступны.")

    async def _run_ffmpeg_command(self, command: List[str]) -> Tuple[int, str, str]:
        """Выполняет команду ffmpeg в отдельном потоке."""
        proc = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=300)
        return proc.returncode, proc.stdout, proc.stderr

    async def compress_audio(self, input_path: str, output_path: str) -> bool:
        """Сжимает аудиофайл для уменьшения размера."""
        if not PYDUB_AVAILABLE:
            logger.warning("pydub не установлен, сжатие аудио пропущено.")
            return False
        
        logger.info(f"Сжатие аудио: {input_path} -> {output_path}")
        command = [
            'ffmpeg', '-i', input_path,
            '-vn', '-c:a', 'libmp3lame',
            '-b:a', config.AUDIO_COMPRESSION_BITRATE,
            '-ar', str(config.AUDIO_SAMPLE_RATE),
            '-ac', '1',
            '-y', output_path
        ]
        returncode, stdout, stderr = await self._run_ffmpeg_command(command)
        if returncode != 0:
            logger.error(f"Ошибка сжатия аудио: {stderr}")
            return False
        logger.info(f"Аудио сжато: {output_path}")
        return True

    async def process_audio_with_vad(self, audio_path: str, output_dir: str) -> List[str]:
        """Применяет VAD и нарезает аудио на чанки."""
        if not PYDUB_AVAILABLE or not config.VAD_ENABLED:
            logger.info("VAD отключен или pydub не установлен. Аудио будет обрабатываться целиком.")
            return [audio_path]

        logger.info(f"Применение VAD к аудио: {audio_path}")
        try:
            audio = await asyncio.to_thread(AudioSegment.from_file, audio_path)
            # Убедимся, что аудио в нужном формате для VAD
            audio = audio.set_frame_rate(config.AUDIO_SAMPLE_RATE).set_channels(1)

            # Нарезка на тишине
            chunks = await asyncio.to_thread(
                split_on_silence,
                audio,
                min_silence_len=500,  # миллисекунды
                silence_thresh=-40,   # дБ
                keep_silence=200      # миллисекунды
            )

            if not chunks:
                logger.warning("VAD не нашел активных голосовых сегментов. Возвращаем исходный файл.")
                return [audio_path]

            output_chunks = []
            for i, chunk in enumerate(chunks):
                chunk_filename = os.path.join(output_dir, f"chunk_{i}.mp3")
                await asyncio.to_thread(chunk.export, chunk_filename, format="mp3")
                output_chunks.append(chunk_filename)
            logger.info(f"VAD разделил аудио на {len(output_chunks)} чанков.")
            return output_chunks
        except Exception as e:
            logger.error(f"Ошибка при обработке аудио с VAD: {e}")
            return [audio_path] # В случае ошибки возвращаем исходный файл

    async def transcribe_audio_chunk(self, audio_file_path: str) -> str:
        """Транскрибирует один аудио-чанк через Groq API."""
        if not groq_client_manager._initialized:
            raise Exception(config.ERROR_NO_GROQ)

        async def transcribe(client):
            with open(audio_file_path, "rb") as audio_file:
                transcript = await client.audio.transcriptions.create(
                    file=audio_file,
                    model=config.GROQ_MODELS["transcription"],
                    language=config.AUDIO_LANGUAGE,
                    temperature=config.TRANSCRIPTION_TEMPERATURE,
                )
            return transcript.text

        try:
            return await groq_client_manager.make_request(transcribe)
        except Exception as e:
            logger.error(f"Ошибка транскрибации аудио чанка {audio_file_path}: {e}")
            return ""

    async def transcribe_audio(self, audio_file_path: str) -> str:
        """Основная функция транскрибации аудио с VAD и параллельной обработкой."""
        temp_chunk_dir = os.path.join(config.TEMP_DIR, f"audio_chunks_{os.path.basename(audio_file_path).replace('.', '_')}")
        os.makedirs(temp_chunk_dir, exist_ok=True)

        try:
            # Сжатие аудио перед VAD
            compressed_audio_path = os.path.join(config.TEMP_DIR, f"compressed_{os.path.basename(audio_file_path)}")
            if await self.compress_audio(audio_file_path, compressed_audio_path):
                audio_to_process = compressed_audio_path
            else:
                audio_to_process = audio_file_path

            chunks = await self.process_audio_with_vad(audio_to_process, temp_chunk_dir)
            
            # Параллельная транскрибация чанков
            tasks = [self.transcribe_audio_chunk(chunk_path) for chunk_path in chunks]
            transcriptions = await asyncio.gather(*tasks)
            
            full_text = " ".join(transcriptions).strip()
            return full_text
        finally:
            # Очистка временных чанков
            await asyncio.to_thread(lambda: [os.remove(os.path.join(temp_chunk_dir, f)) for f in os.listdir(temp_chunk_dir)])
            await asyncio.to_thread(os.rmdir, temp_chunk_dir)
            if 'compressed_audio_path' in locals() and os.path.exists(compressed_audio_path):
                await asyncio.to_thread(os.remove, compressed_audio_path)

audio_processor = AudioProcessor()


# ============================================================================
# VIDEO PROCESSING
# ============================================================================

class VideoProcessor:
    """Обработка видеофайлов и кружочков"""
    
    async def _run_ffprobe_command(self, command: List[str]) -> Tuple[int, str, str]:
        """Выполняет команду ffprobe в отдельном потоке."""
        proc = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10)
        return proc.returncode, proc.stdout, proc.stderr

    async def check_video_duration(self, filepath: str) -> Optional[float]:
        """Получить длительность видео в секундах"""
        try:
            command = [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                filepath
            ]
            returncode, stdout, stderr = await self._run_ffprobe_command(command)
            
            if returncode == 0 and stdout.strip():
                duration = float(stdout.strip())
                logger.debug(f"Video duration: {duration}s")
                return duration
        except Exception as e:
            logger.warning(f"Error checking video duration: {e}")
        
        return None
    
    async def extract_audio_from_video(self, video_path: str, output_path: str) -> bool:
        """Извлечение звука из видеофайла"""
        logger.info(f"Извлечение аудио из видео: {video_path} -> {output_path}")
        command = [
            'ffmpeg', '-i', video_path,
            '-vn',
            '-acodec', 'libmp3lame',
            '-ab', config.AUDIO_COMPRESSION_BITRATE, # Используем битрейт из конфига
            '-ar', str(config.AUDIO_SAMPLE_RATE),
            '-ac', '1',
            '-y',
            output_path
        ]
        returncode, stdout, stderr = await self._run_ffmpeg_command(command)
        
        if returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"Аудио успешно извлечено: {output_path}")
            return True
        else:
            logger.error(f"Ошибка извлечения аудио: {stderr}")
            return False

    async def download_youtube_video(self, url: str, output_path: str) -> Optional[str]:
        """Скачивает видео с YouTube/TikTok/Rutube/Instagram/Vimeo с помощью yt-dlp."""
        if not YT_DLP_AVAILABLE:
            logger.error("yt-dlp не установлен. Невозможно скачать видео.")
            return None

        logger.info(f"Скачивание видео с URL: {url}")
        # yt-dlp параметры
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': output_path, # yt-dlp сам добавит расширение
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'quiet': config.YTDLP_QUIET,
            'no_warnings': config.YTDLP_NO_WARNINGS,
            'retries': config.YTDLP_RETRIES,
            'fragment_retries': config.YTDLP_FRAGMENT_RETRIES,
            'socket_timeout': config.YTDLP_SOCKET_TIMEOUT,
            'geo_bypass': True, # Попытка обойти региональные ограничения
            'concurrent_fragments': 5, # Параллельная загрузка фрагментов
        }

        # Добавляем PoT токены, если они есть
        if config.PO_TOKEN:
            ydl_opts['cookiefile'] = 'cookies.txt' # Временный файл для куки
            # Это упрощенный подход, в реальной системе нужно управлять куками более сложно
            # Например, сохранять их в S3 или Redis
            logger.warning("Использование PO_TOKEN требует более сложного управления куками в продакшене.")

        try:
            # yt-dlp может вернуть путь с расширением, нужно его получить
            info_extractor = yt_dlp.YoutubeDL(ydl_opts)
            info = await asyncio.to_thread(info_extractor.extract_info, url, download=False)
            final_output_path = info_extractor.prepare_filename(info)
            
            await asyncio.to_thread(yt_dlp.YoutubeDL(ydl_opts).download, [url])
            
            if os.path.exists(final_output_path) and os.path.getsize(final_output_path) > 0:
                logger.info(f"Видео успешно скачано: {final_output_path}")
                return final_output_path
            else:
                logger.error(f"Ошибка скачивания видео: файл не найден или пуст {final_output_path}")
                return None
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"Ошибка yt-dlp при скачивании видео {url}: {e}")
            if "Private video" in str(e) or "Sign in to confirm your age" in str(e):
                raise Exception(config.ERROR_YOUTUBE)
            if "Video unavailable" in str(e):
                raise Exception(config.ERROR_VIDEO_NOT_FOUND)
            raise
        except Exception as e:
            logger.error(f"Неизвестная ошибка при скачивании видео {url}: {e}")
            return None

    async def get_youtube_subtitles(self, video_id: str) -> Optional[str]:
        """Пытается получить субтитры с YouTube."""
        if not YT_TRANSCRIPT_API_AVAILABLE:
            logger.warning("youtube_transcript_api не установлен. Субтитры YouTube будут недоступны.")
            return None

        logger.info(f"Поиск субтитров для YouTube видео ID: {video_id}")
        try:
            transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.list_transcripts, video_id)
            transcript = None
            for lang in config.YOUTUBE_SUBTITLES_LANGS:
                try:
                    if lang.startswith('a.'): # Автоматические субтитры
                        transcript = transcript_list.find_generated_transcript([lang[2:]])
                    else:
                        transcript = transcript_list.find_transcript([lang])
                    break
                except Exception:
                    continue
            
            if transcript:
                full_transcript = " ".join([entry['text'] for entry in await asyncio.to_thread(transcript.fetch)])
                logger.info(f"Субтитры найдены для YouTube видео ID: {video_id}")
                return full_transcript
            else:
                logger.info(f"Субтитры не найдены для YouTube видео ID: {video_id} на языках {config.YOUTUBE_SUBTITLES_LANGS}")
                return None
        except Exception as e:
            logger.warning(f"Ошибка при получении субтитров YouTube для {video_id}: {e}")
            return None


video_processor = VideoProcessor()


# ============================================================================
# TEXT PROCESSORS (CORRECTION, SUMMARIZATION)
# ============================================================================

class TextProcessor:
    """Обработка текста: коррекция, саммаризация."""

    async def _call_groq_chat_completion(self, prompt: str, text: str, model_type: str, temperature: float) -> str:
        if not groq_client_manager._initialized:
            raise Exception(config.ERROR_NO_GROQ)

        text_to_process = _truncate_text_for_model(text, model_type)

        async def chat_completion(client):
            response = await client.chat.completions.create(
                model=config.GROQ_MODELS[model_type],
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text_to_process}
                ],
                temperature=temperature,
                stream=False
            )
            return response.choices[0].message.content

        try:
            return await groq_client_manager.make_request(chat_completion)
        except Exception as e:
            logger.error(f"Ошибка Groq API для {model_type} обработки: {e}")
            return f"❌ Ошибка обработки текста: {str(e)[:100]}"

    async def basic_correction(self, text: str) -> str:
        """Базовая коррекция текста."""
        return await self._call_groq_chat_completion(
            config.BASIC_CORRECTION_PROMPT, text, "basic", config.BASIC_CORRECTION_TEMPERATURE
        )

    async def premium_correction(self, text: str) -> str:
        """Премиум коррекция текста."""
        return await self._call_groq_chat_completion(
            config.PREMIUM_CORRECTION_PROMPT, text, "premium", config.PREMIUM_CORRECTION_TEMPERATURE
        )

    async def summarize_text(self, text: str) -> str:
        """Саммаризация текста."""
        if len(text) < config.MIN_CHARS_FOR_SUMMARY:
            return config.ERROR_TEXT_TOO_SHORT_FOR_SUMMARY
        return await self._call_groq_chat_completion(
            config.SUMMARIZATION_PROMPT, text, "reasoning", config.SUMMARIZATION_TEMPERATURE
        )

text_processor = TextProcessor()


# ============================================================================
# DOCUMENT PROCESSORS (PDF, DOCX, TXT)
# ============================================================================

class DocumentProcessor:
    """Обработка документов: PDF, DOCX, TXT."""

    async def extract_text_from_pdf(self, filepath: str) -> str:
        if not PDFPLUMBER_AVAILABLE:
            logger.error("pdfplumber не установлен. Невозможно обработать PDF.")
            return config.ERROR_PDF

        logger.info(f"Извлечение текста из PDF: {filepath}")
        try:
            text_content = []
            with await asyncio.to_thread(pdfplumber.open, filepath) as pdf:
                for i, page in enumerate(pdf.pages):
                    if config.PDF_MAX_PAGES and i >= config.PDF_MAX_PAGES:
                        logger.warning(f"Достигнут лимит страниц PDF ({config.PDF_MAX_PAGES}).")
                        break
                    text_content.append(await asyncio.to_thread(page.extract_text) or "")
            return "\n".join(text_content).strip()
        except Exception as e:
            logger.error(f"Ошибка при извлечении текста из PDF {filepath}: {e}")
            return config.ERROR_PDF

    async def extract_text_from_docx(self, filepath: str) -> str:
        if not DOCX_AVAILABLE:
            logger.error("python-docx не установлен. Невозможно обработать DOCX.")
            return config.ERROR_UNSUPPORTED_FORMAT

        logger.info(f"Извлечение текста из DOCX: {filepath}")
        try:
            doc = await asyncio.to_thread(docx.Document, filepath)
            full_text = []
            for para in doc.paragraphs:
                full_text.append(para.text)
            return "\n".join(full_text).strip()
        except Exception as e:
            logger.error(f"Ошибка при извлечении текста из DOCX {filepath}: {e}")
            return config.ERROR_UNSUPPORTED_FORMAT

    async def extract_text_from_txt(self, filepath: str) -> str:
        logger.info(f"Извлечение текста из TXT: {filepath}")
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return await asyncio.to_thread(f.read)
        except Exception as e:
            logger.error(f"Ошибка при извлечении текста из TXT {filepath}: {e}")
            return config.ERROR_UNSUPPORTED_FORMAT

document_processor = DocumentProcessor()


# ============================================================================
# DIALOGUE MANAGEMENT
# ============================================================================

class DialogueManager:
    """Управление диалогами с документами."""

    def __init__(self):
        self.document_dialogues: Dict[int, Dict[int, Dict[str, Any]]] = {}

    def add_document_context(self, user_id: int, message_id: int, document_text: str):
        if user_id not in self.document_dialogues:
            self.document_dialogues[user_id] = {}
        self.document_dialogues[user_id][message_id] = {
            "text": document_text,
            "history": [],
            "last_accessed": time.time()
        }
        logger.info(f"Добавлен контекст документа для user {user_id}, message {message_id}")

    def get_document_context(self, user_id: int, message_id: int) -> Optional[Dict[str, Any]]:
        if user_id in self.document_dialogues and message_id in self.document_dialogues[user_id]:
            self.document_dialogues[user_id][message_id]["last_accessed"] = time.time()
            return self.document_dialogues[user_id][message_id]
        return None

    def update_dialogue_history(self, user_id: int, message_id: int, user_message: str, bot_response: str):
        context = self.get_document_context(user_id, message_id)
        if context:
            context["history"].append({"role": "user", "content": user_message})
            context["history"].append({"role": "assistant", "content": bot_response})
            # Обрезаем историю, если она слишком длинная
            if len(context["history"]) > config.MAX_DIALOG_HISTORY:
                context["history"] = context["history"][-config.MAX_DIALOG_HISTORY:]
            logger.debug(f"Обновлена история диалога для user {user_id}, message {message_id}")

    async def answer_document_question(self, user_id: int, message_id: int, question: str) -> str:
        context = self.get_document_context(user_id, message_id)
        if not context:
            return "❌ Контекст документа не найден. Пожалуйста, выберите документ снова."

        document_text = context["text"]
        dialogue_history = context["history"]

        messages = [
            {"role": "system", "content": config.SUMMARIZATION_PROMPT}, # Используем саммари промпт как базу для QA
            {"role": "user", "content": f"На основе следующего документа ответь на вопрос: {question}\n\nДокумент:\n{document_text}"}
        ]
        messages.extend(dialogue_history)

        async def chat_completion(client):
            response = await client.chat.completions.create(
                model=config.GROQ_MODELS["reasoning"],
                messages=messages,
                temperature=config.MODEL_TEMPERATURES["reasoning"],
                stream=False
            )
            return response.choices[0].message.content

        try:
            bot_response = await groq_client_manager.make_request(chat_completion)
            self.update_dialogue_history(user_id, message_id, question, bot_response)
            return bot_response
        except Exception as e:
            logger.error(f"Ошибка при ответе на вопрос по документу: {e}")
            return f"❌ Произошла ошибка при обработке вопроса: {str(e)[:100]}"

    def cleanup_old_dialogues(self):
        current_time = time.time()
        users_to_clean = []
        for user_id, docs in list(self.document_dialogues.items()):
            docs_to_clean = []
            for msg_id, ctx in list(docs.items()):
                if current_time - ctx["last_accessed"] > config.CACHE_TIMEOUT_SECONDS:
                    docs_to_clean.append(msg_id)
            for msg_id in docs_to_clean:
                docs.pop(msg_id)
                logger.debug(f"Очищен контекст документа {msg_id} для user {user_id}")
            if not docs:
                users_to_clean.append(user_id)
        for user_id in users_to_clean:
            self.document_dialogues.pop(user_id)
            logger.debug(f"Очищен пустой контекст пользователя {user_id}")

dialogue_manager = DialogueManager()


# ============================================================================
# MAIN PROCESSOR FUNCTION
# ============================================================================

async def process_content(file_path: Optional[str], text_content: Optional[str], content_type: str, groq_clients: list) -> Tuple[str, str]:
    """Единая точка входа для обработки всего контента."""
    
    # Инициализация GroqClientManager, если еще не инициализирован
    if not groq_client_manager._initialized:
        await groq_client_manager.initialize(os.environ.get("GROQ_API_KEYS", ""))

    processed_text = ""
    original_text = text_content if text_content else ""
    file_type = ""

    if content_type == "text":
        processed_text = text_content
        file_type = "text"
    elif content_type == "photo":
        if file_path:
            processed_text = await vision_processor.extract_text(open(file_path, "rb").read())
            file_type = "image"
    elif content_type == "audio" or content_type == "voice":
        if file_path:
            processed_text = await audio_processor.transcribe_audio(file_path)
            file_type = "audio"
    elif content_type == "video":
        if file_path:
            # Извлечение аудио из видео
            audio_output_path = os.path.join(config.TEMP_DIR, f"audio_from_video_{os.path.basename(file_path).replace('.', '_')}.mp3")
            if await video_processor.extract_audio_from_video(file_path, audio_output_path):
                processed_text = await audio_processor.transcribe_audio(audio_output_path)
                await asyncio.to_thread(os.remove, audio_output_path) # Удаляем временный аудиофайл
            else:
                processed_text = config.ERROR_UNSUPPORTED_FORMAT
            file_type = "video"
    elif content_type == "url":
        # Определяем тип URL (YouTube, TikTok и т.д.)
        if "youtube.com" in text_content or "youtu.be" in text_content:
            video_id_match = re.search(r"(?:v=|youtu\.be/|embed/|watch\?v=)([a-zA-Z0-9_-]{11})", text_content)
            video_id = video_id_match.group(1) if video_id_match else None

            subtitles = None
            if config.YOUTUBE_PREFER_SUBTITLES and video_id:
                subtitles = await video_processor.get_youtube_subtitles(video_id)
            
            if subtitles:
                processed_text = subtitles
                file_type = "youtube_subtitles"
            else:
                # Если субтитров нет или не предпочтительны, скачиваем видео и транскрибируем
                logger.info(config.ERROR_YOUTUBE_NO_SUBTITLES)
                temp_video_path = os.path.join(config.TEMP_DIR, f"youtube_video_{video_id}.mp4")
                try:
                    downloaded_path = await video_processor.download_youtube_video(text_content, temp_video_path)
                    if downloaded_path:
                        audio_output_path = os.path.join(config.TEMP_DIR, f"audio_from_youtube_{video_id}.mp3")
                        if await video_processor.extract_audio_from_video(downloaded_path, audio_output_path):
                            processed_text = await audio_processor.transcribe_audio(audio_output_path)
                            await asyncio.to_thread(os.remove, audio_output_path)
                        else:
                            processed_text = config.ERROR_UNSUPPORTED_FORMAT
                        await asyncio.to_thread(os.remove, downloaded_path)
                    else:
                        processed_text = config.ERROR_VIDEO_NOT_FOUND
                except Exception as e:
                    processed_text = str(e)
                file_type = "youtube_audio"
        elif any(platform in text_content for platform in ["tiktok.com", "rutube.ru", "instagram.com", "vimeo.com"]):
            temp_video_path = os.path.join(config.TEMP_DIR, f"platform_video_{int(time.time())}.mp4")
            try:
                downloaded_path = await video_processor.download_youtube_video(text_content, temp_video_path)
                if downloaded_path:
                    audio_output_path = os.path.join(config.TEMP_DIR, f"audio_from_platform_{int(time.time())}.mp3")
                    if await video_processor.extract_audio_from_video(downloaded_path, audio_output_path):
                        processed_text = await audio_processor.transcribe_audio(audio_output_path)
                        await asyncio.to_thread(os.remove, audio_output_path)
                    else:
                        processed_text = config.ERROR_UNSUPPORTED_FORMAT
                    await asyncio.to_thread(os.remove, downloaded_path)
                else:
                    processed_text = config.ERROR_VIDEO_NOT_FOUND
            except Exception as e:
                processed_text = str(e)
            file_type = "platform_video"
        else:
            processed_text = config.ERROR_INVALID_URL
            file_type = "url_error"
    elif content_type == "document":
        if file_path:
            mime_type, _ = mimetypes.guess_type(file_path)
            if mime_type == "application/pdf":
                processed_text = await document_processor.extract_text_from_pdf(file_path)
                file_type = "pdf"
            elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                processed_text = await document_processor.extract_text_from_docx(file_path)
                file_type = "docx"
            elif mime_type == "text/plain":
                processed_text = await document_processor.extract_text_from_txt(file_path)
                file_type = "txt"
            else:
                processed_text = config.ERROR_UNSUPPORTED_FORMAT
                file_type = "unsupported_document"
    else:
        processed_text = config.ERROR_UNSUPPORTED_FORMAT
        file_type = "unknown"

    if not original_text and processed_text and not processed_text.startswith("❌"):
        original_text = processed_text # Если был файл, то его текст становится оригиналом

    return processed_text, original_text, file_type


async def apply_correction(text: str, mode: str) -> str:
    """Применяет выбранный режим коррекции к тексту."""
    if mode == "basic":
        return await text_processor.basic_correction(text)
    elif mode == "premium":
        return await text_processor.premium_correction(text)
    elif mode == "summary":
        return await text_processor.summarize_text(text)
    else:
        return "Неизвестный режим коррекции."


async def get_status_info(groq_clients: list) -> Dict[str, Any]:
    """Собирает информацию о статусе бота."""
    groq_count = len(groq_clients)
    users_count = len(document_dialogues) # Используем глобальный document_dialogues для подсчета пользователей
    vision_status = "✅" if groq_client_manager._initialized and any(c for c in groq_client_manager._clients) else "❌"
    docx_status = "✅" if DOCX_AVAILABLE else "❌"
    vad_status = "✅" if PYDUB_AVAILABLE and config.VAD_ENABLED else "❌"
    s3_status = "✅" if config.S3_ACCESS_KEY_ID and config.S3_BUCKET_NAME else "❌"
    redis_status = "✅" if config.REDIS_URL else "❌"

    temp_files = 0
    if os.path.exists(config.TEMP_DIR):
        temp_files = len(os.listdir(config.TEMP_DIR))

    return {
        "groq_count": groq_count,
        "users_count": users_count,
        "vision_status": vision_status,
        "docx_status": docx_status,
        "vad_status": vad_status,
        "s3_status": s3_status,
        "redis_status": redis_status,
        "temp_files": temp_files,
    }


# Создаем временную директорию, если ее нет
os.makedirs(config.TEMP_DIR, exist_ok=True)
