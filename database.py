# database.py
"""
Supabase-слой с полным fallback.
Если SUPABASE_URL + SUPABASE_KEY не заданы — все функции молча возвращают None/[].
Если БД упала в рантайме — бот продолжает работать без сохранения.
"""

import os
import asyncio
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================================

_client = None
_available = False


def init_database() -> bool:
    """
    Попытка подключиться к Supabase.
    Возвращает True если успешно, False если недоступно.
    Вызывать один раз при старте приложения.
    """
    global _client, _available

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()

    if not url or not key:
        logger.info("📦 Supabase не настроен (нет SUPABASE_URL / SUPABASE_KEY). Работаем без БД.")
        _available = False
        return False

    try:
        from supabase import create_client
        _client = create_client(url, key)
        # Быстрая проверка соединения
        _client.table("users").select("id").limit(1).execute()
        _available = True
        logger.info("✅ Supabase подключен успешно")
        return True
    except ImportError:
        logger.warning("⚠️  Библиотека supabase не установлена. Работаем без БД.")
        _available = False
        return False
    except Exception as e:
        logger.warning(f"⚠️  Supabase недоступен: {e}. Работаем без БД.")
        _available = False
        return False


def is_available() -> bool:
    return _available


# ============================================================================
# ВНУТРЕННИЙ ХЕЛПЕР
# ============================================================================

def _safe_execute(func):
    """
    Выполняет синхронный Supabase-запрос.
    При любой ошибке — логирует и возвращает None (не роняет бот).
    """
    if not _available or _client is None:
        return None
    try:
        return func()
    except Exception as e:
        logger.warning(f"⚠️  Supabase ошибка: {e}")
        return None


async def _run(func):
    """Запускает синхронный Supabase-запрос в thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _safe_execute(func))


# ============================================================================
# KEEP-ALIVE
# ============================================================================

async def keep_alive_loop():
    """
    Фоновая задача: лёгкий SELECT каждые 10 минут.
    Держит Supabase-проект активным.
    """
    while True:
        await asyncio.sleep(600)
        if not _available or _client is None:
            continue
        try:
            await _run(lambda: _client.table("users").select("id").limit(1).execute())
            logger.debug("💓 Supabase keep-alive OK")
        except Exception as e:
            logger.debug(f"💓 Supabase keep-alive failed: {e}")


# ============================================================================
# ПОЛЬЗОВАТЕЛИ
# ============================================================================

async def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
    """Создать или обновить пользователя. Молча возвращает False если БД недоступна."""
    if not _available:
        return False
    result = await _run(lambda: _client.table("users").upsert({
        "id": user_id,
        "username": username,
        "first_name": first_name,
        "last_seen": datetime.utcnow().isoformat(),
    }, on_conflict="id").execute())
    return result is not None


# ============================================================================
# ТРАНСКРИПТЫ
# ============================================================================

async def save_transcript(
    user_id: int,
    source_type: str,
    original_text: str,
) -> Optional[int]:
    """
    Сохранить транскрипт. Возвращает transcript_id или None.
    source_type: 'voice' | 'audio' | 'video_note' | 'file' | 'text'
    """
    if not _available:
        return None
    result = await _run(lambda: _client.table("transcripts").insert({
        "user_id": user_id,
        "source_type": source_type,
        "original_text": original_text,
        "created_at": datetime.utcnow().isoformat(),
    }).execute())
    if result and result.data:
        return result.data[0].get("id")
    return None


async def save_result(transcript_id: Optional[int], mode: str, result_text: str) -> bool:
    """
    Сохранить результат обработки (basic / premium / summary).
    Молча возвращает False если transcript_id=None или БД недоступна.
    """
    if not _available or transcript_id is None:
        return False
    result = await _run(lambda: _client.table("results").insert({
        "transcript_id": transcript_id,
        "mode": mode,
        "result_text": result_text,
        "created_at": datetime.utcnow().isoformat(),
    }).execute())
    return result is not None


async def get_user_history(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Последние N транскриптов пользователя.
    Возвращает [] если БД недоступна.
    """
    if not _available:
        return []
    result = await _run(lambda: (
        _client.table("transcripts")
        .select("id, source_type, original_text, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    ))
    if result and result.data:
        return result.data
    return []
