# access.py
"""
Доступ и лимиты:
  • администраторы (ADMIN_IDS) — без лимитов, выбор модели, статистика;
  • остальные пользователи — суточный лимит обращений к ИИ, потолки на размер
    файла / длину голоса / длину текста, пауза между сообщениями;
  • выбор модели: личный (только у админа) и общий по умолчанию.

Учёт ведётся в памяти и, если подключён Supabase, дублируется в БД
(таблицы bot_settings и usage_daily). Без БД после рестарта счётчики
обнуляются — для личного бота это допустимо.
"""

import re
import os
import time
import asyncio
import logging
import contextvars
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import config
import database

logger = logging.getLogger(__name__)


# ============================================================================
# АДМИНИСТРАТОРЫ
# ============================================================================

def _parse_ids(raw: str) -> set:
    ids = set()
    for part in re.split(r"[,\s;]+", raw or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS: set = _parse_ids(os.environ.get("ADMIN_IDS") or os.environ.get("ADMIN_ID") or "")


def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


# Пользователь, чьё сообщение сейчас обрабатывается. Выставляется middleware и
# автоматически виден во всех await-цепочках (processors списывает лимит по нему).
current_user_id: contextvars.ContextVar = contextvars.ContextVar("current_user_id", default=None)


# ============================================================================
# ВРЕМЯ И СБРОС ЛИМИТА
# ============================================================================

def _make_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(config.LIMITS_TZ)
    except Exception:
        logger.warning(f"Часовой пояс {config.LIMITS_TZ!r} недоступен, использую UTC+3")
        return timezone(timedelta(hours=3))


_TZ = _make_tz()


def _now() -> datetime:
    return datetime.now(_TZ)


def today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def seconds_to_reset() -> int:
    now = _now()
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - now).total_seconds()))


def reset_in_text() -> str:
    s = seconds_to_reset()
    h, m = s // 3600, (s % 3600) // 60
    if h:
        return f"через {h} ч {m} мин"
    return f"через {max(m, 1)} мин"


# ============================================================================
# ЛИМИТЫ
# ============================================================================

class QuotaExceeded(Exception):
    """Суточный лимит исчерпан. user_message начинается с ❌ — так его понимают хендлеры."""

    def __init__(self, used: int, limit: int):
        self.used, self.limit = used, limit
        super().__init__(self.user_message)

    @property
    def user_message(self) -> str:
        return (f"❌ Дневной лимит исчерпан ({self.used}/{self.limit}). "
                f"Обновится в 00:00 ({config.LIMITS_TZ_LABEL}), {reset_in_text()}.")


# uid -> {"day": "YYYY-MM-DD", "n": int}
_usage: Dict[int, Dict] = {}
_loaded: set = set()                 # (uid, day), по которым уже читали БД
_limit_override: Dict[int, int] = {}
_limit_loaded: set = set()
_names: Dict[int, str] = {}
_last_msg: Dict[int, float] = {}


def remember_user(user) -> None:
    """Запоминает имя для статистики /admin."""
    try:
        name = f"@{user.username}" if getattr(user, "username", None) else (user.first_name or str(user.id))
        _names[user.id] = name
    except Exception:
        pass


def remember_name(entity_id: int, name: str) -> None:
    _names[entity_id] = name


def user_limit(user_id: int) -> int:
    """Личный лимит; для групп (отрицательный chat_id) — общий лимит чата."""
    default = config.GROUP_DAILY_LIMIT if user_id < 0 else config.USER_DAILY_LIMIT
    return _limit_override.get(user_id, default)


def _entry(user_id: int) -> Dict:
    day = today_str()
    e = _usage.get(user_id)
    if e is None or e["day"] != day:
        e = {"day": day, "n": 0}
        _usage[user_id] = e
        _loaded.add((user_id, day))      # новые сутки — в БД ещё ничего нет
    return e


def used_today(user_id: int) -> int:
    return _entry(user_id)["n"]


def remaining(user_id: int) -> Optional[int]:
    """None — безлимит (админ)."""
    if is_admin(user_id):
        return None
    return max(0, user_limit(user_id) - used_today(user_id))


async def ensure_loaded(user_id: int) -> None:
    """Подтягивает расход за сегодня и индивидуальный лимит из БД (один раз)."""
    if is_admin(user_id) or not database.is_available():
        return
    day = today_str()
    if (user_id, day) not in _loaded:
        _loaded.add((user_id, day))
        db_n = await database.get_usage(user_id, day)
        e = _entry(user_id)
        e["n"] = max(e["n"], db_n)
    if user_id not in _limit_loaded:
        _limit_loaded.add(user_id)
        raw = await database.get_setting(f"limit:{user_id}")
        if raw and raw.lstrip("-").isdigit():
            _limit_override[user_id] = max(0, int(raw))


def charge(cost: int = 1) -> None:
    """
    Списывает cost единиц у текущего пользователя (по contextvar).
    Админ, фоновые задачи и API-эндпоинты без пользователя не списываются.
    Бросает QuotaExceeded, если лимит исчерпан.
    """
    uid = current_user_id.get()
    if uid is None or is_admin(uid):
        return
    e = _entry(uid)
    limit = user_limit(uid)
    if e["n"] + cost > limit:
        raise QuotaExceeded(e["n"], limit)
    e["n"] += cost
    if database.is_available():
        try:
            asyncio.get_running_loop().create_task(database.set_usage(uid, e["day"], e["n"]))
        except RuntimeError:
            pass


async def set_user_limit(user_id: int, value: Optional[int]) -> None:
    """value=None — вернуть общий лимит."""
    if value is None:
        _limit_override.pop(user_id, None)
        await database.set_setting(f"limit:{user_id}", None)
    else:
        _limit_override[user_id] = max(0, int(value))
        await database.set_setting(f"limit:{user_id}", str(max(0, int(value))))
    _limit_loaded.add(user_id)


# ---- проверки входящего сообщения (без списания) ---------------------------

def cooldown_left(user_id: int) -> float:
    """Сколько секунд ещё ждать. Если пауза прошла — фиксирует время и возвращает 0."""
    now = time.monotonic()
    wait = config.USER_COOLDOWN_SEC - (now - _last_msg.get(user_id, -1e9))
    if wait > 0:
        return wait
    _last_msg[user_id] = now
    return 0.0


def check_message_caps(message) -> Optional[str]:
    """Потолки на размер/длину для обычных пользователей. Возвращает текст отказа или None."""
    max_bytes = config.USER_MAX_FILE_MB * 1024 * 1024
    max_sec = config.USER_MAX_AUDIO_SEC

    for media, label in ((message.voice, "Голосовое"), (message.audio, "Аудио"),
                         (message.video_note, "Кружок")):
        if media is not None:
            dur = getattr(media, "duration", 0) or 0
            if dur > max_sec:
                return f"⚠️ {label} слишком длинное: {dur} с, максимум {max_sec} с ({max_sec // 60} мин)."
            size = getattr(media, "file_size", 0) or 0
            if size > max_bytes:
                return f"⚠️ Файл слишком большой, максимум {config.USER_MAX_FILE_MB} МБ."

    if message.document is not None:
        if (message.document.file_size or 0) > max_bytes:
            return f"⚠️ Файл слишком большой, максимум {config.USER_MAX_FILE_MB} МБ."
    if message.photo:
        if (message.photo[-1].file_size or 0) > max_bytes:
            return f"⚠️ Фото слишком большое, максимум {config.USER_MAX_FILE_MB} МБ."

    text = message.text or ""
    if len(text) > config.USER_MAX_TEXT_CHARS:
        return (f"⚠️ Текст слишком длинный: {len(text)} символов, "
                f"максимум {config.USER_MAX_TEXT_CHARS}. Разбейте его на части.")
    return None


def limit_exhausted_message(user_id: int) -> str:
    return (f"🚫 <b>Дневной лимит исчерпан</b> ({used_today(user_id)}/{user_limit(user_id)}).\n"
            f"Обновится в 00:00 ({config.LIMITS_TZ_LABEL}), {reset_in_text()}.\n\n"
            f"Остаток всегда виден по команде /limit")


def group_limits_text(chat_id: int) -> str:
    """Текст команды /limit в группе: лимит чата."""
    limit = user_limit(chat_id)
    used = used_today(chat_id)
    return (
        f"📊 <b>Лимит этого чата на сегодня</b>\n"
        f"Использовано: <b>{used}</b> из <b>{limit}</b>, осталось <b>{max(0, limit - used)}</b>\n"
        f"Сброс в 00:00 ({config.LIMITS_TZ_LABEL}), {reset_in_text()}.\n\n"
        f"<i>Лимит общий на всех участников группы. Сообщения администратора бота не списываются.</i>"
    )


def limits_text(user_id: int) -> str:
    """Текст команды /limit."""
    if user_id < 0:
        return group_limits_text(user_id)
    if is_admin(user_id):
        return "👑 <b>Вы администратор</b> — лимитов нет."
    limit = user_limit(user_id)
    used = used_today(user_id)
    left = max(0, limit - used)
    return (
        f"📊 <b>Ваш лимит на сегодня</b>\n"
        f"Использовано: <b>{used}</b> из <b>{limit}</b>, осталось <b>{left}</b>\n"
        f"Сброс в 00:00 ({config.LIMITS_TZ_LABEL}), {reset_in_text()}.\n\n"
        f"<i>1 единица = 1 обращение к ИИ: распознавание голоса или фото, "
        f"обработка текста, перевод, разбор правок, вопрос по документу. "
        f"Просмотр готовых результатов и экспорт бесплатны.</i>\n\n"
        f"<b>Ограничения на одно сообщение:</b>\n"
        f"• файл/фото — до {config.USER_MAX_FILE_MB} МБ\n"
        f"• голос, кружок, аудио — до {config.USER_MAX_AUDIO_SEC // 60} мин\n"
        f"• текст — до {config.USER_MAX_TEXT_CHARS} символов\n"
        f"• пауза между сообщениями — {config.USER_COOLDOWN_SEC} с"
    )


async def usage_snapshot(top: int = 5) -> Tuple[str, int, int, List[Tuple[int, str, int]]]:
    """(день, активных пользователей, всего единиц, топ[(uid, имя, n)])."""
    day = today_str()
    counts: Dict[int, int] = {u: e["n"] for u, e in _usage.items() if e["day"] == day and e["n"] > 0}
    if database.is_available():
        for row in await database.get_usage_day(day):
            uid, n = row.get("user_id"), int(row.get("count") or 0)
            if uid is not None:
                counts[uid] = max(counts.get(uid, 0), n)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return day, len(counts), sum(counts.values()), [(u, _names.get(u, str(u)), n) for u, n in ranked[:top]]


# ============================================================================
# ВЫБОР МОДЕЛИ
# ============================================================================

_global_profile: Optional[str] = None
_personal_profile: Dict[int, str] = {}


def _valid(key: Optional[str]) -> bool:
    return bool(key) and key in config.LLM_PROFILES


def global_profile_key() -> str:
    return _global_profile if _valid(_global_profile) else config.DEFAULT_LLM_PROFILE


def profile_key_for(user_id: Optional[int]) -> str:
    """Личный выбор есть только у админа; остальные всегда на общей модели."""
    if is_admin(user_id) and _valid(_personal_profile.get(user_id)):
        return _personal_profile[user_id]
    return global_profile_key()


def profile_for(user_id: Optional[int]) -> dict:
    return config.LLM_PROFILES[profile_key_for(user_id)]


async def load_settings() -> None:
    """Читает сохранённые профили из БД (вызывать после init_database)."""
    global _global_profile
    if not database.is_available():
        return
    g = await database.get_setting("llm_default")
    if _valid(g):
        _global_profile = g
    for uid in ADMIN_IDS:
        p = await database.get_setting(f"llm_user:{uid}")
        if _valid(p):
            _personal_profile[uid] = p
    logger.info(f"Профиль модели по умолчанию: {global_profile_key()}")


async def set_personal_profile(user_id: int, key: str) -> None:
    if not (is_admin(user_id) and _valid(key)):
        return
    _personal_profile[user_id] = key
    await database.set_setting(f"llm_user:{user_id}", key)


async def reset_personal_profile(user_id: int) -> None:
    _personal_profile.pop(user_id, None)
    await database.set_setting(f"llm_user:{user_id}", None)


async def set_global_profile(key: str) -> None:
    global _global_profile
    if not _valid(key):
        return
    _global_profile = key
    await database.set_setting("llm_default", key)


def personal_profile_key(user_id: int) -> Optional[str]:
    """Личный выбор админа (None, если не задан или пользователь не админ)."""
    if is_admin(user_id) and _valid(_personal_profile.get(user_id)):
        return _personal_profile[user_id]
    return None
