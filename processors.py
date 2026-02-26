# processors.py
"""
Служба обработки: Groq, Whisper, Vision, VAD, PDF, DOCX, видео, диалог.
Версия 5.0 — добавлен AudioEngine (Silero VAD) для беспаузной транскрибации.
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

# ─── Опциональные зависимости ─────────────────────────────────────────────────

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

# Silero VAD (torch + torchaudio нужны отдельно)
try:
    import torch
    import torchaudio
    SILERO_AVAILABLE = True
except ImportError:
    SILERO_AVAILABLE = False

# WebRTC VAD (лёгкая альтернатива Silero, ~5 МБ)
try:
    import webrtcvad
    WEBRTCVAD_AVAILABLE = True
except ImportError:
    WEBRTCVAD_AVAILABLE = False

# Выбор бэкенда через переменную окружения:
#   VAD_BACKEND=silero   — Silero (точнее, ~250 МБ RAM, нужен torch)
#   VAD_BACKEND=webrtc   — webrtcvad (лёгкий, ~5 МБ RAM)  [DEFAULT]
#   VAD_BACKEND=none     — VAD отключён, аудио идёт в Whisper как есть
VAD_BACKEND = os.environ.get("VAD_BACKEND", "webrtc").lower()

logger = logging.getLogger(__name__)

# Хранилище для диалогов о документах
document_dialogues: Dict[int, Dict[int, Dict[str, Any]]] = {}


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ GROQ
# ============================================================================

async def _make_groq_request(groq_clients: list, func, *args, **kwargs):
    """Перебираем ключи с retry и rate-limit backoff"""
    if not groq_clients:
        raise Exception("Нет доступных Groq клиентов")

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
            if "429" in error_msg or "rate_limit" in error_msg.lower():
                wait_time = 5 + (attempt * 2)
                logger.info(f"Rate limit, ждем {wait_time}с...")
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(1 + (attempt % 3))

    raise Exception(f"Все клиенты недоступны: {'; '.join(errors[:3])}")


def _truncate_text_for_model(text: str, model_type: str) -> str:
    """Обрезает текст под лимиты конкретной модели"""
    model_limits = {
        "basic": 5000,
        "premium": 10000,
        "reasoning": 25000,
    }
    limit = model_limits.get(model_type, 5000)
    if len(text) > limit:
        logger.warning(f"Текст обрезан с {len(text)} до {limit} символов для {model_type}")
        return text[:limit] + "... [текст обрезан из-за лимитов API]"
    return text


# ============================================================================
# AUDIO ENGINE — MULTI-BACKEND VAD
# ============================================================================
#
# Бэкенды выбираются через переменную окружения VAD_BACKEND:
#
#   VAD_BACKEND=webrtc   (default) — webrtcvad, ~5 МБ RAM
#                                    подходит для Render Free (512 МБ)
#   VAD_BACKEND=silero              — Silero VAD, ~250 МБ RAM (torch)
#                                    подходит для Render Starter (2 ГБ+)
#   VAD_BACKEND=none                — VAD отключён, аудио идёт в Whisper как есть
#
# Во всех случаях: если что-то падает — graceful fallback на оригинальные байты.
# ============================================================================

class AudioEngine:
    """
    Предобработка аудио перед транскрибацией.

    Общая цепочка для обоих бэкендов:
        bytes (любой формат)
        → ffmpeg → WAV 16kHz mono PCM
        → VAD → вырезаем тишину, склеиваем речь с паузами 200мс
        → WAV-байты → Whisper (Groq)

    Экономия: 30-50% длины файла → быстрее и дешевле транскрибация.
    """

    SAMPLE_RATE = 16000       # Стандарт для обоих VAD бэкендов
    SPEECH_PAD_MS = 200       # Пауза между сегментами речи при склейке

    # ── Silero state ──────────────────────────────────────────────────────────
    _silero_model = None
    _silero_utils = None

    # ── ffmpeg: WAV 16kHz mono ────────────────────────────────────────────────

    @classmethod
    async def _to_wav16k(cls, audio_bytes: bytes) -> Optional[bytes]:
        """Конвертирует любой аудиоформат в WAV 16kHz mono PCM через ffmpeg."""
        def _run():
            try:
                result = subprocess.run(
                    [
                        "ffmpeg", "-i", "pipe:0",
                        "-f", "wav",
                        "-ar", str(cls.SAMPLE_RATE),
                        "-ac", "1",
                        "-acodec", "pcm_s16le",
                        "pipe:1",
                        "-loglevel", "error", "-y",
                    ],
                    input=audio_bytes,
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                logger.error(f"ffmpeg error: {result.stderr.decode()[:200]}")
                return None
            except Exception as e:
                logger.error(f"ffmpeg error: {e}")
                return None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)

    # ── Склейка сегментов ─────────────────────────────────────────────────────

    @classmethod
    def _merge_segments_wav(cls, wav_bytes: bytes, segments: list) -> Optional[bytes]:
        """
        Принимает WAV-байты и список сегментов [(start_ms, end_ms), ...].
        Возвращает WAV с вырезанной тишиной и паузами 200 мс между сегментами.
        """
        if not segments:
            return wav_bytes

        try:
            import struct
            import wave

            # Читаем WAV через stdlib — не требует soundfile/torchaudio
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                sr = wf.getframerate()
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                frames = wf.readframes(wf.getnframes())

            # PCM как bytes → работаем напрямую
            bytes_per_sample = sampwidth * n_channels
            bytes_per_ms = sr * bytes_per_sample // 1000
            pad_bytes = b"\x00" * (cls.SPEECH_PAD_MS * bytes_per_ms)

            chunks = []
            for i, (start_ms, end_ms) in enumerate(segments):
                s = start_ms * bytes_per_ms
                e = end_ms * bytes_per_ms
                chunks.append(frames[s:e])
                if i < len(segments) - 1:
                    chunks.append(pad_bytes)

            merged_frames = b"".join(chunks)

            out_buf = io.BytesIO()
            with wave.open(out_buf, "wb") as wf_out:
                wf_out.setnchannels(n_channels)
                wf_out.setsampwidth(sampwidth)
                wf_out.setframerate(sr)
                wf_out.writeframes(merged_frames)

            original_dur = len(frames) / (sr * bytes_per_sample)
            merged_dur = len(merged_frames) / (sr * bytes_per_sample)
            reduction = (1 - merged_dur / original_dur) * 100
            logger.info(
                f"VAD merge: {original_dur:.1f}s → {merged_dur:.1f}s "
                f"(-{reduction:.0f}%, {len(segments)} сегментов)"
            )

            return out_buf.getvalue()

        except Exception as e:
            logger.error(f"VAD merge error: {e}", exc_info=True)
            return None

    # ── WebRTC VAD бэкенд ─────────────────────────────────────────────────────

    @classmethod
    def _apply_webrtc(cls, wav_bytes: bytes) -> Optional[bytes]:
        """
        Применяет webrtcvad к WAV 16kHz.
        Aggressiveness=2: баланс между точностью и чувствительностью.
        Работает фреймами по 30 мс, собирает «речевые» окна в сегменты.
        """
        if not WEBRTCVAD_AVAILABLE:
            return None

        try:
            import wave

            with wave.open(io.BytesIO(wav_bytes)) as wf:
                sr = wf.getframerate()
                pcm = wf.readframes(wf.getnframes())
                sampwidth = wf.getsampwidth()

            vad = webrtcvad.Vad(2)        # aggressiveness 0-3
            frame_ms = 30                  # webrtcvad поддерживает 10/20/30 мс
            frame_bytes = int(sr * frame_ms / 1000) * sampwidth

            segments = []
            in_speech = False
            seg_start = 0
            offset = 0

            while offset + frame_bytes <= len(pcm):
                frame = pcm[offset : offset + frame_bytes]
                is_speech = vad.is_speech(frame, sr)
                ts_ms = offset // (sr * sampwidth // 1000)

                if is_speech and not in_speech:
                    in_speech = True
                    seg_start = max(0, ts_ms - cls.SPEECH_PAD_MS)
                elif not is_speech and in_speech:
                    in_speech = False
                    segments.append((seg_start, ts_ms + cls.SPEECH_PAD_MS))

                offset += frame_bytes

            if in_speech:
                segments.append((seg_start, len(pcm) // (sr * sampwidth // 1000)))

            if not segments:
                logger.warning("WebRTC VAD: речь не найдена, возвращаем оригинал")
                return wav_bytes

            return cls._merge_segments_wav(wav_bytes, segments)

        except Exception as e:
            logger.error(f"WebRTC VAD error: {e}", exc_info=True)
            return None

    # ── Silero VAD бэкенд ─────────────────────────────────────────────────────

    @classmethod
    def _load_silero(cls) -> bool:
        """Ленивая загрузка модели Silero (кэшируется на весь процесс)."""
        if cls._silero_model is not None:
            return True
        if not SILERO_AVAILABLE:
            logger.warning("Silero недоступен: установите torch и torchaudio")
            return False
        try:
            logger.info("⏳ Загружаю Silero VAD...")
            cls._silero_model, cls._silero_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            cls._silero_model.eval()
            logger.info("✅ Silero VAD готов")
            return True
        except Exception as e:
            logger.error(f"❌ Silero load error: {e}")
            return False

    @classmethod
    def _apply_silero(cls, wav_bytes: bytes) -> Optional[bytes]:
        """Применяет Silero VAD. Возвращает WAV с вырезанной тишиной."""
        if not cls._load_silero():
            return None
        try:
            wav_buf = io.BytesIO(wav_bytes)
            waveform, sr = torchaudio.load(wav_buf)

            if sr != cls.SAMPLE_RATE:
                waveform = torchaudio.transforms.Resample(sr, cls.SAMPLE_RATE)(waveform)

            audio_tensor = waveform.squeeze(0)
            get_speech_timestamps = cls._silero_utils[0]

            timestamps = get_speech_timestamps(
                audio_tensor,
                cls._silero_model,
                sampling_rate=cls.SAMPLE_RATE,
                threshold=0.5,
                min_speech_duration_ms=100,
                min_silence_duration_ms=300,
                speech_pad_ms=cls.SPEECH_PAD_MS,
                return_seconds=False,
            )

            if not timestamps:
                logger.warning("Silero VAD: речь не найдена, возвращаем оригинал")
                return wav_bytes

            # Конвертируем сэмплы → миллисекунды для _merge_segments_wav
            ms_segments = [
                (ts["start"] * 1000 // cls.SAMPLE_RATE, ts["end"] * 1000 // cls.SAMPLE_RATE)
                for ts in timestamps
            ]

            return cls._merge_segments_wav(wav_bytes, ms_segments)

        except Exception as e:
            logger.error(f"Silero VAD error: {e}", exc_info=True)
            return None

    # ── Публичный метод ───────────────────────────────────────────────────────

    @classmethod
    async def process_voice_vad(cls, audio_bytes: bytes) -> bytes:
        backend = backend or VAD_BACKEND
        """
        Полный пайплайн VAD-предобработки.

        Принимает байты любого формата (.ogg, .mp3, .m4a, .wav...).
        Возвращает WAV-байты с вырезанной тишиной — готовы для Whisper.

        На любом шаге при ошибке — graceful fallback на предыдущий результат,
        транскрибация не прерывается никогда.
        """
        backend = VAD_BACKEND

        # Шаг 1: конвертируем в WAV 16kHz (нужно для обоих бэкендов)
        wav_bytes = await cls._to_wav16k(audio_bytes)
        if not wav_bytes:
            logger.warning("VAD: ffmpeg не смог конвертировать, передаём оригинал")
            return audio_bytes

        # Шаг 2: применяем выбранный бэкенд
        if backend == "none":
            logger.debug("VAD отключён (VAD_BACKEND=none), передаём WAV без фильтрации")
            return wav_bytes

        if backend == "silero":
            result = cls._apply_silero(wav_bytes)
        else:
            # default: webrtc
            result = cls._apply_webrtc(wav_bytes)
            if result is None and backend == "webrtc":
                logger.warning("WebRTC VAD недоступен, передаём WAV без фильтрации")

        return result if result else wav_bytes


# Глобальный экземпляр (используется в transcribe_voice)
audio_engine = AudioEngine()


# ============================================================================
# VISION PROCESSOR (OCR)
# ============================================================================

class VisionProcessor:
    """Распознавание текста с изображений через Groq Vision"""

    def __init__(self):
        self.groq_clients = []

    def init_clients(self, groq_clients: list):
        self.groq_clients = groq_clients
        logger.info(f"VisionProcessor инициализирован с {len(groq_clients)} клиентами")

    async def extract_text(self, image_bytes: bytes) -> str:
        if not self.groq_clients:
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
                                },
                            },
                        ],
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
    """Обработка видеофайлов"""

    @staticmethod
    async def check_video_duration(filepath: str) -> Optional[float]:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    filepath,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"Error checking video duration: {e}")
        return None

    @staticmethod
    async def extract_audio_from_video(video_path: str, output_path: str) -> bool:
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-i", video_path,
                    "-vn",
                    "-acodec", "libmp3lame",
                    "-ab", "64k",
                    "-ar", str(config.AUDIO_SAMPLE_RATE),
                    "-ac", "1",
                    "-y",
                    output_path,
                ],
                capture_output=True,
                timeout=300,
            )
            return os.path.exists(output_path) and os.path.getsize(output_path) > 0
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
        url = url.strip()
        platforms = {
            "youtube": ["youtube.com", "youtu.be", "m.youtube.com", "youtube.com/shorts"],
            "tiktok": ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"],
            "rutube": ["rutube.ru"],
            "instagram": ["instagram.com", "instagr.am", "instagram.com/reel/"],
            "vimeo": ["vimeo.com"],
        }
        for platform, domains in platforms.items():
            if any(domain in url.lower() for domain in domains):
                return True, platform
        return False, None

    @staticmethod
    def _extract_youtube_video_id(url: str) -> Optional[str]:
        patterns = [
            r"(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]+)",
            r"youtube\.com\/embed\/([a-zA-Z0-9_-]+)",
            r"youtube\.com\/shorts\/([a-zA-Z0-9_-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _format_timecode(seconds: float) -> str:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"[{h:02d}:{m:02d}:{s:02d}]"
        return f"[{m:02d}:{s:02d}]"

    @staticmethod
    async def extract_youtube_subtitles(
        video_id: str, with_timecodes: bool = True
    ) -> Optional[str]:
        if not YT_TRANSCRIPT_API_AVAILABLE:
            return None
        try:
            for lang in config.YOUTUBE_SUBTITLES_LANGS:
                try:
                    api_lang = lang.replace("a.", "")
                    transcript = YouTubeTranscriptApi.get_transcript(
                        video_id, languages=[api_lang]
                    )
                    if with_timecodes:
                        lines = []
                        for item in transcript:
                            tc = VideoPlatformProcessor._format_timecode(item["start"])
                            text = item["text"].replace("\n", " ").strip()
                            if text:
                                lines.append(f"{tc} {text}")
                        return "\n".join(lines)
                    else:
                        return " ".join([item["text"] for item in transcript])
                except Exception:
                    continue
            return None
        except Exception as e:
            logger.error(f"Error extracting YouTube subtitles: {e}")
            return None

    @staticmethod
    async def download_audio_with_ytdlp(url: str, output_path: str) -> Optional[str]:
        if not YT_DLP_AVAILABLE:
            logger.error("yt-dlp not installed")
            return None
        try:
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": output_path,
                "quiet": config.YTDLP_QUIET,
                "no_warnings": config.YTDLP_NO_WARNINGS,
                "socket_timeout": config.YTDLP_SOCKET_TIMEOUT,
                "noplaylist": True,
                "retries": 10,
                "fragment_retries": 10,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "64",
                    }
                ],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web_creator", "ios", "android"],
                        "skip": ["hls", "dash"],
                    }
                },
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
                    ),
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                "cookiefile": (
                    "youtube_cookies.txt"
                    if os.path.exists("youtube_cookies.txt")
                    else None
                ),
                "extractor_retries": 5,
                "file_access_retries": 5,
                "throttledratelimit": 1000000,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: ydl.download([url]))

                for ext in [".mp3", ".m4a", ".webm", ".opus"]:
                    test_path = output_path + ext
                    if os.path.exists(test_path):
                        return test_path
            return None
        except Exception as e:
            logger.error(f"Error downloading audio: {e}")
            return None

    @staticmethod
    async def process_video_url(
        url: str, groq_clients: list, with_timecodes: bool = True
    ) -> str:
        is_valid, platform = VideoPlatformProcessor._validate_url(url)
        if not is_valid:
            return config.ERROR_INVALID_URL

        logger.info(f"Processing {platform} video: {url}")

        try:
            if platform == "youtube" and config.YOUTUBE_PREFER_SUBTITLES:
                video_id = VideoPlatformProcessor._extract_youtube_video_id(url)
                if video_id:
                    subtitles = await VideoPlatformProcessor.extract_youtube_subtitles(
                        video_id, with_timecodes=with_timecodes
                    )
                    if subtitles and len(subtitles.strip()) > config.MIN_TEXT_LENGTH:
                        return subtitles

            temp_audio_path = (
                f"{config.TEMP_DIR}/audio_{int(time.time())}_{os.getpid()}"
            )
            audio_path = await VideoPlatformProcessor.download_audio_with_ytdlp(
                url, temp_audio_path
            )

            if not audio_path or not os.path.exists(audio_path):
                return config.ERROR_VIDEO_NOT_FOUND

            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

            text = await transcribe_voice(
                audio_bytes, groq_clients, with_timecodes=with_timecodes
            )

            try:
                os.remove(audio_path)
            except Exception:
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
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _segments_to_timecoded_text(segments: list) -> str:
    lines = []
    for seg in segments:
        tc = _format_timecode(seg.get("start", 0))
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"{tc} {text}")
    return "\n".join(lines)


async def transcribe_voice(
    audio_bytes: bytes,
    groq_clients: list,
    with_timecodes: bool = False,
    vad_backend: Optional[str] = None,   # ← добавить
) -> str:
    processed_bytes = await audio_engine.process_voice_vad(
        audio_bytes,
        backend=vad_backend or VAD_BACKEND
    )

    async def transcribe(client):
        if with_timecodes:
            response = await client.audio.transcriptions.create(
                model=config.GROQ_MODELS["transcription"],
                file=("audio.wav", processed_bytes, "audio/wav"),
                language=config.AUDIO_LANGUAGE,
                response_format="verbose_json",
                temperature=config.MODEL_TEMPERATURES["transcription"],
            )
            segments = getattr(response, "segments", None)
            if segments:
                return _segments_to_timecoded_text(segments)
            return getattr(response, "text", str(response))
        else:
            response = await client.audio.transcriptions.create(
                model=config.GROQ_MODELS["transcription"],
                file=("audio.wav", processed_bytes, "audio/wav"),
                language=config.AUDIO_LANGUAGE,
                response_format="text",
                temperature=config.MODEL_TEMPERATURES["transcription"],
            )
            return response

    try:
        return await _make_groq_request(groq_clients, transcribe)
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return f"❌ Ошибка распознавания: {str(e)[:100]}"


# ============================================================================
# TEXT PROCESSING — CORRECTION
# ============================================================================

async def correct_text_basic(text: str, groq_clients: list) -> str:
    """Базовая коррекция: быстрая модель"""
    if not text.strip():
        return config.ERROR_EMPTY_TEXT

    text = _truncate_text_for_model(text, "basic")

    async def correct(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["basic"],
            messages=[
                {
                    "role": "user",
                    "content": config.BASIC_CORRECTION_PROMPT + f"\n\nТекст:\n{text}",
                }
            ],
            temperature=config.MODEL_TEMPERATURES["basic"],
        )
        return response.choices[0].message.content.strip()

    try:
        return await _make_groq_request(groq_clients, correct)
    except Exception as e:
        logger.error(f"Basic correction error: {e}")
        if "413" in str(e) or "rate_limit_exceeded" in str(e):
            shorter = text[:3000] + "... [сильно обрезано]"

            async def retry(client):
                response = await client.chat.completions.create(
                    model=config.GROQ_MODELS["basic"],
                    messages=[
                        {
                            "role": "user",
                            "content": config.BASIC_CORRECTION_PROMPT + f"\n\nТекст:\n{shorter}",
                        }
                    ],
                    temperature=config.MODEL_TEMPERATURES["basic"],
                )
                return response.choices[0].message.content.strip()

            return await _make_groq_request(groq_clients, retry)
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


async def correct_text_premium(text: str, groq_clients: list) -> str:
    """Премиум коррекция: мощная модель"""
    if not text.strip():
        return config.ERROR_EMPTY_TEXT

    text = _truncate_text_for_model(text, "premium")

    async def correct(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["premium"],
            messages=[
                {
                    "role": "user",
                    "content": config.PREMIUM_CORRECTION_PROMPT + f"\n\nТекст:\n{text}",
                }
            ],
            temperature=config.MODEL_TEMPERATURES["premium"],
        )
        return response.choices[0].message.content.strip()

    try:
        return await _make_groq_request(groq_clients, correct)
    except Exception as e:
        logger.error(f"Premium correction error: {e}")
        if "413" in str(e) or "rate_limit_exceeded" in str(e):
            shorter = text[:5000] + "... [сильно обрезано]"

            async def retry(client):
                response = await client.chat.completions.create(
                    model=config.GROQ_MODELS["premium"],
                    messages=[
                        {
                            "role": "user",
                            "content": config.PREMIUM_CORRECTION_PROMPT + f"\n\nТекст:\n{shorter}",
                        }
                    ],
                    temperature=config.MODEL_TEMPERATURES["premium"],
                )
                return response.choices[0].message.content.strip()

            return await _make_groq_request(groq_clients, retry)
        return f"❌ Ошибка коррекции: {str(e)[:100]}"


# ============================================================================
# TEXT PROCESSING — SUMMARIZATION
# ============================================================================

async def summarize_text(text: str, groq_clients: list) -> str:
    """Саммари через reasoning-модель"""
    if not text.strip():
        return config.ERROR_EMPTY_TEXT

    words_count = len(text.split())
    if (
        words_count < config.MIN_WORDS_FOR_SUMMARY
        or len(text) < config.MIN_CHARS_FOR_SUMMARY
    ):
        return config.ERROR_TEXT_TOO_SHORT_FOR_SUMMARY

    text = _truncate_text_for_model(text, "reasoning")

    async def summarize(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["reasoning"],
            messages=[
                {
                    "role": "user",
                    "content": config.SUMMARIZATION_PROMPT + f"\n\nТекст:\n{text}",
                }
            ],
            temperature=config.MODEL_TEMPERATURES["reasoning"],
        )
        return response.choices[0].message.content.strip()

    try:
        return await _make_groq_request(groq_clients, summarize)
    except Exception as e:
        logger.error(f"Summarization error: {e}")
        if "413" in str(e) or "rate_limit_exceeded" in str(e):
            shorter = text[:10000] + "... [сильно обрезано]"

            async def retry(client):
                response = await client.chat.completions.create(
                    model=config.GROQ_MODELS["reasoning"],
                    messages=[
                        {
                            "role": "user",
                            "content": config.SUMMARIZATION_PROMPT + f"\n\nТекст:\n{shorter}",
                        }
                    ],
                    temperature=config.MODEL_TEMPERATURES["reasoning"],
                )
                return response.choices[0].message.content.strip()

            return await _make_groq_request(groq_clients, retry)
        return f"❌ Ошибка создания саммари: {str(e)[:100]}"


# ============================================================================
# ДИАЛОГОВЫЙ РЕЖИМ
# ============================================================================

def save_document_for_dialog(
    user_id: int, msg_id: int, document_text: str, source: str = "unknown"
):
    """Сохраняем документ для диалогового режима"""
    if user_id not in document_dialogues:
        document_dialogues[user_id] = {}

    document_dialogues[user_id][msg_id] = {
        "full_text": document_text,
        "text": document_text,
        "original": document_text,
        "history": [],
        "timestamp": time.time(),
        "source": source,
    }
    logger.info(
        f"💾 Документ для диалога: user={user_id}, msg={msg_id}, длина={len(document_text)}"
    )
    return document_dialogues[user_id][msg_id]


def get_document_text(user_id: int, msg_id: int) -> Optional[str]:
    """Получить текст документа (пробуем разные ключи для совместимости)"""
    if user_id not in document_dialogues or msg_id not in document_dialogues[user_id]:
        return None
    doc_data = document_dialogues[user_id][msg_id]
    for key in ("full_text", "text", "original"):
        if key in doc_data and doc_data[key]:
            return doc_data[key]
    return None


async def answer_document_question(
    user_id: int, msg_id: int, question: str, groq_clients: list
) -> str:
    """Ответ на вопрос по документу (без стриминга)"""
    if user_id not in document_dialogues or msg_id not in document_dialogues[user_id]:
        return "❌ Документ не найден. Сначала загрузите документ."

    doc_data = document_dialogues[user_id][msg_id]
    full_text = get_document_text(user_id, msg_id)
    if not full_text:
        return "❌ Не удалось извлечь текст документа."

    history = doc_data.get("history", [])

    doc_preview = (
        full_text[:20000] + "... [документ обрезан]"
        if len(full_text) > 20000
        else full_text
    )

    dialog_context = ""
    if history:
        dialog_context = "Предыдущий диалог:\n"
        for turn in history[-config.MAX_DIALOG_HISTORY :]:
            q = turn.get("question") or turn.get("q", "")
            a = turn.get("answer") or turn.get("a", "")
            dialog_context += f"Пользователь: {q}\nАссистент: {a}\n\n"

    qa_prompt = (
        f"Ты — ассистент, который отвечает на вопросы по содержанию документа.\n\n"
        f"Документ:\n{doc_preview}\n\n{dialog_context}\n"
        f"Вопрос пользователя: {question}\n\n"
        f"Ответь на вопрос, используя только информацию из документа. "
        f"Если ответа нет — так и скажи."
    )

    async def answer(client):
        response = await client.chat.completions.create(
            model=config.GROQ_MODELS["reasoning"],
            messages=[{"role": "user", "content": qa_prompt}],
            temperature=config.MODEL_TEMPERATURES["reasoning"],
        )
        return response.choices[0].message.content.strip()

    try:
        answer_text = await _make_groq_request(groq_clients, answer)
        history.append(
            {
                "question": question,
                "answer": answer_text,
                "q": question,
                "a": answer_text,
                "timestamp": time.time(),
            }
        )
        doc_data["history"] = history[-config.MAX_DIALOG_HISTORY :]
        return answer_text
    except Exception as e:
        logger.error(f"QA error: {e}")
        return f"❌ Ошибка при ответе на вопрос: {str(e)[:100]}"


async def stream_document_answer(
    user_id: int, msg_id: int, question: str, groq_clients: list
) -> AsyncGenerator[str, None]:
    """Стриминг ответа на вопрос по документу"""
    if not groq_clients:
        yield "❌ Ошибка: нет доступных Groq клиентов"
        return

    if user_id not in document_dialogues or msg_id not in document_dialogues[user_id]:
        yield "❌ Документ не найден. Сначала загрузите документ."
        return

    doc_data = document_dialogues[user_id][msg_id]
    full_text = get_document_text(user_id, msg_id)

    if not full_text:
        yield "❌ Не удалось извлечь текст документа."
        return

    history = doc_data.get("history", [])
    context = ""
    for turn in history[-5:]:
        q = turn.get("question") or turn.get("q", "")
        a = turn.get("answer") or turn.get("a", "")
        if q and a:
            context += f"Вопрос: {q}\nОтвет: {a}\n\n"

    doc_preview = (
        full_text[:20000] + "... [документ обрезан]"
        if len(full_text) > 20000
        else full_text
    )

    prompt = (
        f"Ты — ассистент, который отвечает на вопросы по содержанию документа.\n\n"
        f"Документ:\n{doc_preview}\n\n{context}\nВопрос:\n{question}\n\n"
        f"Ответь на вопрос, используя только информацию из документа. "
        f"Если ответа нет — так и скажи."
    )

    client = groq_clients[0 % len(groq_clients)]

    try:
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

        logger.info(
            f"Stream completed: {chunk_count} chunks, {len(full_answer)} chars"
        )

        history.append(
            {
                "question": question,
                "answer": full_answer,
                "q": question,
                "a": full_answer,
                "timestamp": time.time(),
            }
        )
        doc_data["history"] = history[-config.MAX_DIALOG_HISTORY :]

    except Exception as e:
        logger.error(f"Ошибка в stream_document_answer: {e}", exc_info=True)
        yield f"❌ Ошибка при генерации ответа: {str(e)[:100]}"


# ============================================================================
# FILE PROCESSING
# ============================================================================

async def process_video_file(
    video_bytes: bytes, filename: str, groq_clients: list, with_timecodes: bool = False
) -> str:
    try:
        file_ext = filename.split(".")[-1] if "." in filename else "mp4"
        temp_video = f"{config.TEMP_DIR}/video_{int(time.time())}_{os.getpid()}.{file_ext}"
        temp_audio = f"{config.TEMP_DIR}/audio_{int(time.time())}_{os.getpid()}.mp3"

        with open(temp_video, "wb") as f:
            f.write(video_bytes)

        duration = await video_processor.check_video_duration(temp_video)
        if duration and duration > config.VIDEO_MAX_DURATION:
            os.remove(temp_video)
            return config.ERROR_VIDEO_TOO_LONG

        if not await video_processor.extract_audio_from_video(temp_video, temp_audio):
            os.remove(temp_video)
            return "❌ Ошибка извлечения звука из видео"

        with open(temp_audio, "rb") as f:
            audio_bytes = f.read()

        text = await transcribe_voice(audio_bytes, groq_clients, with_timecodes=with_timecodes)

        for path in (temp_video, temp_audio):
            try:
                os.remove(path)
            except Exception:
                pass

        return text

    except Exception as e:
        logger.error(f"Error processing video file: {e}")
        return f"❌ Ошибка обработки видеофайла: {str(e)[:100]}"


async def extract_text_from_pdf(pdf_bytes: bytes) -> str:
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
                    text += f"\n--- Страница {page_num} ---\n{page_text}\n"
                for table_idx, table in enumerate(page.find_tables(), 1):
                    text += f"\n[Таблица {table_idx} на странице {page_num}]\n"
                    for row in table.extract():
                        if row:
                            text += (
                                " | ".join(str(c) if c else "" for c in row) + "\n"
                            )
                page_count += 1
        if not text.strip():
            return "❌ Не удалось извлечь текст из PDF"
        logger.info(f"PDF: {page_count} страниц")
        return text.strip()
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return f"❌ Ошибка обработки PDF: {str(e)}"


async def extract_text_from_docx(docx_bytes: bytes) -> str:
    if not DOCX_AVAILABLE:
        return "❌ Для работы с DOCX требуется установить python-docx"
    try:
        doc_buffer = io.BytesIO(docx_bytes)
        doc = docx.Document(doc_buffer)
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        return text.strip() if text.strip() else "❌ Документ пуст"
    except Exception as e:
        logger.error(f"DOCX extraction error: {e}")
        return f"❌ Ошибка обработки DOCX: {str(e)}"


async def extract_text_from_txt(txt_bytes: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "koi8-r", "windows-1251"):
        try:
            return txt_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return txt_bytes.decode("utf-8", errors="ignore")


async def extract_text_from_file(
    file_bytes: bytes, filename: str, groq_clients: list
) -> str:
    mime_type, _ = mimetypes.guess_type(filename)
    file_ext = filename.lower().split(".")[-1] if "." in filename else ""

    if (mime_type and mime_type.startswith("image/")) or file_ext in (
        "jpg", "jpeg", "png", "bmp", "gif", "webp"
    ):
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)

    if file_ext in config.VIDEO_SUPPORTED_FORMATS:
        return await process_video_file(file_bytes, filename, groq_clients)

    if mime_type == "application/pdf" or file_ext == "pdf":
        return await extract_text_from_pdf(file_bytes)

    if (
        mime_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or file_ext == "docx"
    ):
        return await extract_text_from_docx(file_bytes)

    if mime_type == "text/plain" or file_ext == "txt":
        return await extract_text_from_txt(file_bytes)

    if file_ext == "doc":
        return config.ERROR_DOC_NOT_SUPPORTED

    return config.ERROR_UNSUPPORTED_FORMAT


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def get_available_modes(text: str) -> list:
    """Определяем доступные режимы обработки"""
    available = ["basic", "premium"]
    if (
        len(text.split()) >= config.MIN_WORDS_FOR_SUMMARY
        and len(text) >= config.MIN_CHARS_FOR_SUMMARY
    ):
        available.append("summary")
    return available


# ============================================================================
# ЭКСПОРТ
# ============================================================================

__all__ = [
    "AudioEngine",
    "audio_engine",
    "transcribe_voice",
    "correct_text_basic",
    "correct_text_premium",
    "summarize_text",
    "extract_text_from_file",
    "process_video_file",
    "get_available_modes",
    "vision_processor",
    "video_platform_processor",
    "save_document_for_dialog",
    "answer_document_question",
    "stream_document_answer",
    "get_document_text",
    "document_dialogues",
    "PDFPLUMBER_AVAILABLE",
    "DOCX_AVAILABLE",
    "YT_DLP_AVAILABLE",
    "SILERO_AVAILABLE",
]
