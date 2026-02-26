# handlers.py
"""
Интерфейс и навигация: все @dp.message, кнопки, инлайн-меню, диалоговый режим.
Версия 4.0 — выделен из bot.py, работает через set_shared_state()
"""

import os
import io
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable, Awaitable

from aiogram import Bot, Router, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramUnauthorizedError, TelegramNetworkError

import config
import processors

logger = logging.getLogger(__name__)

# ============================================================================
# РАЗДЕЛЯЕМОЕ СОСТОЯНИЕ (инициализируется из main.py)
# ============================================================================

_bot: Optional[Bot] = None
_groq_clients: list = []

# Хранилища состояния пользователей
user_context: Dict[int, Dict[int, Any]] = {}
active_dialogs: Dict[int, int] = {}


def set_shared_state(bot: Bot, groq_clients: list):
    """Вызывается из main.py при старте, передаёт bot и groq_clients"""
    global _bot, _groq_clients
    _bot = bot
    _groq_clients = groq_clients
    logger.info(f"Handlers initialized: {len(groq_clients)} Groq clients")


# ============================================================================
# ROUTER
# ============================================================================

router = Router()

# ============================================================================
# MIDDLEWARE ДЛЯ ОБРАБОТКИ ОШИБОК
# ============================================================================

class ErrorHandlingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramUnauthorizedError as e:
            logger.error(f"❌ Auth error in middleware: {e}")
            raise
        except TelegramNetworkError as e:
            logger.error(f"❌ Network error in middleware: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Unhandled error in middleware: {e}", exc_info=True)
            if hasattr(event, "message") and event.message:
                await event.message.answer("❌ Произошла внутренняя ошибка. Попробуйте позже.")
            elif hasattr(event, "callback_query") and event.callback_query:
                await event.callback_query.message.answer(
                    "❌ Произошла внутренняя ошибка. Попробуйте позже."
                )
            raise


router.message.middleware(ErrorHandlingMiddleware())
router.callback_query.middleware(ErrorHandlingMiddleware())


# ============================================================================
# УПРАВЛЕНИЕ КОНТЕКСТОМ
# ============================================================================

def save_to_history(
    user_id: int,
    msg_id: int,
    text: str,
    mode: str = "basic",
    available_modes: list = None,
):
    """Сохраняем текст, привязывая его к ID сообщения"""
    if user_id not in user_context:
        user_context[user_id] = {}

    if len(user_context[user_id]) > config.MAX_CONTEXTS_PER_USER:
        oldest = min(
            user_context[user_id].keys(),
            key=lambda k: user_context[user_id][k]["time"],
        )
        user_context[user_id].pop(oldest)

    user_context[user_id][msg_id] = {
        "text": text,
        "mode": mode,
        "time": datetime.now(),
        "available_modes": available_modes or ["basic"],
        "original": text,
        "cached_results": {"basic": None, "premium": None, "summary": None},
        "type": "text",
        "chat_id": None,
        "filename": None,
    }


# ============================================================================
# СОХРАНЕНИЕ ФАЙЛОВ
# ============================================================================

async def save_to_file(user_id: int, text: str, format_type: str) -> Optional[str]:
    """Сохраняем текст в TXT или PDF"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"text_{user_id}_{timestamp}"

    if format_type == "txt":
        filepath = f"{config.TEMP_DIR}/{filename}.txt"
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
        except Exception as e:
            logger.error(f"Error saving TXT: {e}")
            return None

    elif format_type == "pdf":
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            from reportlab.lib.utils import simpleSplit

            filepath = f"{config.TEMP_DIR}/{filename}.pdf"
            c = canvas.Canvas(filepath, pagesize=A4)
            width, height = A4
            margin = 50
            line_height = 14
            y = height - margin

            c.setFont("Helvetica-Bold", 14)
            c.drawString(margin, y, "Обработанный текст")
            y -= 30

            c.setFont("Helvetica", 10)
            c.drawString(margin, y, f"Создано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            y -= 40

            c.setFont("Helvetica", 11)
            max_width = width - 2 * margin

            for paragraph in text.split("\n"):
                if not paragraph.strip():
                    y -= line_height
                    continue
                for line in simpleSplit(paragraph, "Helvetica", 11, max_width):
                    if y < margin + 20:
                        c.showPage()
                        y = height - margin
                        c.setFont("Helvetica", 11)
                    c.drawString(margin, y, line)
                    y -= line_height

            c.save()
            return filepath

        except ImportError:
            logger.warning("Reportlab not installed, falling back to TXT")
            filepath = f"{config.TEMP_DIR}/{filename}.txt"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
        except Exception as e:
            logger.error(f"Error saving PDF: {e}")
            return None

    return None


# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================

def create_dialog_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🚪 Выйти из режима вопросов",
            callback_data=f"dialog_exit_{user_id}",
        )
    )
    return builder.as_markup()


def create_keyboard(
    msg_id: int, current_mode: str, available_modes: list = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if available_modes is None:
        available_modes = ["basic", "premium"]

    mode_display = {
        "basic": "📝 Как есть",
        "premium": "✨ Красиво",
        "summary": "📊 Саммари",
    }

    mode_buttons = []
    for mode_code in available_modes:
        if mode_code in mode_display:
            prefix = "✅ " if mode_code == current_mode else ""
            mode_buttons.append(
                InlineKeyboardButton(
                    text=f"{prefix}{mode_display[mode_code]}",
                    callback_data=f"mode_{mode_code}_{msg_id}",
                )
            )

    for i in range(0, len(mode_buttons), 2):
        if i + 1 < len(mode_buttons):
            builder.row(mode_buttons[i], mode_buttons[i + 1])
        else:
            builder.row(mode_buttons[i])

    if current_mode:
        builder.row(
            InlineKeyboardButton(
                text="📄 TXT", callback_data=f"export_{current_mode}_{msg_id}_txt"
            ),
            InlineKeyboardButton(
                text="📊 PDF", callback_data=f"export_{current_mode}_{msg_id}_pdf"
            ),
        )
    return builder.as_markup()


def create_options_keyboard(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📝 Как есть", callback_data=f"process_{user_id}_basic_{msg_id}"
        ),
        InlineKeyboardButton(
            text="✨ Красиво", callback_data=f"process_{user_id}_premium_{msg_id}"
        ),
    )

    ctx_data = user_context.get(user_id, {}).get(msg_id)
    available_modes = ctx_data.get("available_modes", []) if ctx_data else []

    if "summary" in available_modes:
        builder.row(
            InlineKeyboardButton(
                text="📊 Саммари",
                callback_data=f"process_{user_id}_summary_{msg_id}",
            )
        )

    if ctx_data and len(ctx_data.get("original", "")) > 100:
        builder.row(
            InlineKeyboardButton(
                text="💬 Задать вопрос по тексту",
                callback_data=f"dialog_start_{user_id}_{msg_id}",
            )
        )

    return builder.as_markup()


def create_switch_keyboard(user_id: int, msg_id: int) -> Optional[InlineKeyboardMarkup]:
    ctx_data = user_context.get(user_id, {}).get(msg_id)
    if not ctx_data:
        return None

    current = ctx_data.get("mode", "basic")
    available = ctx_data.get("available_modes", ["basic", "premium"])

    builder = InlineKeyboardBuilder()
    mode_display = {
        "basic": "📝 Как есть",
        "premium": "✨ Красиво",
        "summary": "📊 Саммари",
    }

    mode_buttons = [
        InlineKeyboardButton(
            text=mode_display.get(m, m),
            callback_data=f"switch_{user_id}_{m}_{msg_id}",
        )
        for m in available
        if m != current
    ]

    for i in range(0, len(mode_buttons), 2):
        if i + 1 < len(mode_buttons):
            builder.row(mode_buttons[i], mode_buttons[i + 1])
        else:
            builder.row(mode_buttons[i])

    if len(ctx_data.get("original", "")) > 100:
        builder.row(
            InlineKeyboardButton(
                text="💬 Задать вопрос по тексту",
                callback_data=f"dialog_start_{user_id}_{msg_id}",
            )
        )

    if current:
        builder.row(
            InlineKeyboardButton(
                text="📄 TXT",
                callback_data=f"export_{user_id}_{current}_{msg_id}_txt",
            ),
            InlineKeyboardButton(
                text="📊 PDF",
                callback_data=f"export_{user_id}_{current}_{msg_id}_pdf",
            ),
        )

    return builder.as_markup()


# ============================================================================
# СТРИМИНГОВЫЙ ОТВЕТ НА ВОПРОС ПО ДОКУМЕНТУ
# ============================================================================

async def handle_streaming_answer(
    message: types.Message, user_id: int, msg_id: int, question: str
):
    placeholder = await message.answer("💭 Думаю...")
    accumulated = ""
    last_edit_length = 0
    edit_counter = 0

    try:
        if not _groq_clients:
            await placeholder.edit_text("❌ Ошибка: нет доступных Groq клиентов")
            return

        if user_id not in user_context or msg_id not in user_context[user_id]:
            await placeholder.edit_text("❌ Документ не найден. Начните заново.")
            active_dialogs.pop(user_id, None)
            return

        doc_text = user_context[user_id][msg_id].get("original", "")
        if not doc_text:
            await placeholder.edit_text("❌ Текст документа пуст")
            return

        if not hasattr(processors, "document_dialogues"):
            processors.document_dialogues = {}
        if user_id not in processors.document_dialogues:
            processors.document_dialogues[user_id] = {}

        processors.document_dialogues[user_id][msg_id] = {
            "text": doc_text,
            "history": [],
        }

        async for chunk in processors.stream_document_answer(
            user_id, msg_id, question, _groq_clients
        ):
            if chunk:
                accumulated += chunk
                if len(accumulated) - last_edit_length > 30:
                    try:
                        display_text = accumulated + "▌"
                        if len(display_text) > 4096:
                            display_text = display_text[:4093] + "..."
                        await placeholder.edit_text(
                            display_text,
                            reply_markup=create_dialog_keyboard(user_id),
                        )
                        edit_counter += 1
                    except Exception as edit_err:
                        logger.error(f"Edit error: {edit_err}")
                    last_edit_length = len(accumulated)

        final_text = accumulated if accumulated else "❌ Пустой ответ"
        if len(final_text) > 4096:
            final_text = final_text[:4093] + "..."

        await placeholder.edit_text(
            final_text, reply_markup=create_dialog_keyboard(user_id)
        )
        logger.debug(f"Streaming done: {edit_counter} edits, {len(accumulated)} chars")

        if (
            user_id in processors.document_dialogues
            and msg_id in processors.document_dialogues[user_id]
        ):
            history = processors.document_dialogues[user_id][msg_id].setdefault(
                "history", []
            )
            history.append(
                {
                    "question": question,
                    "answer": accumulated,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    except Exception as e:
        logger.error(f"Streaming error: {e}", exc_info=True)
        try:
            await placeholder.edit_text(
                f"❌ Ошибка при генерации ответа: {str(e)[:200]}"
            )
        except Exception:
            pass


# ============================================================================
# КОМАНДЫ
# ============================================================================

@router.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        config.START_MESSAGE,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("help"))
async def help_handler(message: types.Message):
    await message.answer(config.HELP_MESSAGE, parse_mode="HTML")


@router.message(Command("status"))
async def status_handler(message: types.Message):
    try:
        import docx as _docx
        docx_status = "✅"
    except ImportError:
        docx_status = "❌"

    temp_files = 0
    if os.path.exists(config.TEMP_DIR):
        temp_files = len(
            [
                f
                for f in os.listdir(config.TEMP_DIR)
                if f.startswith(("video_", "audio_", "text_"))
            ]
        )

    status_text = config.STATUS_MESSAGE.format(
        groq_count=len(_groq_clients),
        users_count=len(user_context),
        vision_status="✅" if _groq_clients else "❌",
        docx_status=docx_status,
        temp_files=temp_files,
    )
    status_text += f"\n\n💬 Активных диалогов: {len(active_dialogs)}"
    await message.answer(status_text, parse_mode="HTML")


@router.message(Command("exit"))
async def exit_dialog_handler(message: types.Message):
    user_id = message.from_user.id
    if user_id in active_dialogs:
        del active_dialogs[user_id]
        await message.answer("✅ Вы вышли из режима вопросов.")
    else:
        await message.answer("❌ Вы не находитесь в режиме вопросов.")


# ============================================================================
# МЕДИА-ХЭНДЛЕРЫ
# ============================================================================

@router.message(F.voice)
async def voice_handler(message: types.Message):
    user_id = message.from_user.id

    if user_id in active_dialogs:
        await message.answer(
            "⏳ Голосовые вопросы пока не поддерживаются. Напишите текст."
        )
        return

    msg = await message.answer(config.MSG_PROCESSING_VOICE)

    try:
        file_info = await _bot.get_file(message.voice.file_id)
        voice_buffer = io.BytesIO()
        await _bot.download_file(file_info.file_path, voice_buffer)

        original_text = await processors.transcribe_voice(
            voice_buffer.getvalue(), _groq_clients
        )

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, "basic", available_modes)

        ctx = user_context.get(user_id, {}).get(msg.message_id)
        if ctx:
            ctx["type"] = "voice"
            ctx["chat_id"] = message.chat.id

        preview = original_text[: config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id),
        )

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки голосового сообщения")


@router.message(F.video_note)
async def video_note_handler(message: types.Message):
    user_id = message.from_user.id

    if user_id in active_dialogs:
        await message.answer(
            "⏳ Голосовые вопросы пока не поддерживаются. Напишите текст."
        )
        return

    msg = await message.answer("🎥 Обрабатываю кружочек...")

    try:
        file_info = await _bot.get_file(message.video_note.file_id)
        buffer = io.BytesIO()
        await _bot.download_file(file_info.file_path, buffer)

        original_text = await processors.process_video_file(
            buffer.getvalue(), "video_note.mp4", _groq_clients, with_timecodes=False
        )

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, "basic", available_modes)

        ctx = user_context.get(user_id, {}).get(msg.message_id)
        if ctx:
            ctx["type"] = "video_note"
            ctx["chat_id"] = message.chat.id

        preview = original_text[: config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        await msg.edit_text(
            f"✅ <b>Распознанный текст из кружочка:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id),
        )

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Video note handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки кружочка")


@router.message(F.audio)
async def audio_handler(message: types.Message):
    user_id = message.from_user.id
    active_dialogs.pop(user_id, None)

    msg = await message.answer(config.MSG_TRANSCRIBING)

    try:
        file_info = await _bot.get_file(message.audio.file_id)
        audio_buffer = io.BytesIO()
        await _bot.download_file(file_info.file_path, audio_buffer)

        original_text = await processors.transcribe_voice(
            audio_buffer.getvalue(), _groq_clients
        )

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, "basic", available_modes)

        ctx = user_context.get(user_id, {}).get(msg.message_id)
        if ctx:
            ctx["type"] = "audio"
            ctx["chat_id"] = message.chat.id

        preview = original_text[: config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id),
        )

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Audio handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки аудиофайла")


@router.message(F.photo | F.document | F.video)
async def file_handler(message: types.Message):
    user_id = message.from_user.id
    active_dialogs.pop(user_id, None)

    msg = await message.answer("📁 Обрабатываю файл...")

    try:
        file_info = None
        filename = ""

        if message.photo:
            file_info = await _bot.get_file(message.photo[-1].file_id)
            filename = f"photo_{file_info.file_unique_id}.jpg"
        elif message.document:
            file_info = await _bot.get_file(message.document.file_id)
            filename = message.document.file_name or f"file_{file_info.file_unique_id}"
        elif message.video:
            file_info = await _bot.get_file(message.video.file_id)
            filename = message.video.file_name or f"video_{file_info.file_unique_id}.mp4"

        file_buffer = io.BytesIO()
        await _bot.download_file(file_info.file_path, file_buffer)
        file_bytes = file_buffer.getvalue()

        if len(file_bytes) > config.FILE_SIZE_LIMIT:
            await msg.edit_text(config.ERROR_FILE_TOO_LARGE)
            return

        file_ext = filename.lower().split(".")[-1] if "." in filename else ""

        if file_ext in config.VIDEO_SUPPORTED_FORMATS:
            await msg.edit_text(config.MSG_EXTRACTING_AUDIO)
        else:
            await msg.edit_text("🔍 Извлекаю текст...")

        original_text = await processors.extract_text_from_file(
            file_bytes, filename, _groq_clients
        )

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        if not original_text.strip() or len(original_text.strip()) < config.MIN_TEXT_LENGTH:
            await msg.edit_text(config.ERROR_NO_TEXT_IN_FILE)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, "basic", available_modes)

        ctx = user_context.get(user_id, {}).get(msg.message_id)
        if ctx:
            ctx["type"] = "file"
            ctx["chat_id"] = message.chat.id
            ctx["filename"] = filename
            ctx["original"] = original_text

        preview = original_text[: config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        file_type = (
            "видео"
            if file_ext in config.VIDEO_SUPPORTED_FORMATS
            else "изображения"
            if filename.startswith("photo_")
            or any(e in filename.lower() for e in [".jpg", ".jpeg", ".png", ".gif", ".bmp"])
            else "файла"
        )

        await msg.edit_text(
            f"✅ <b>Извлеченный текст из {file_type}:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id),
        )

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"File handler error: {e}")
        await msg.edit_text(f"❌ Ошибка обработки файла: {str(e)[:100]}")


@router.message(F.text)
async def text_handler(message: types.Message):
    user_id = message.from_user.id
    original_text = message.text.strip()

    # Диалоговый режим — пропускаем текст как вопрос
    if user_id in active_dialogs:
        msg_id = active_dialogs[user_id]
        await handle_streaming_answer(message, user_id, msg_id, message.text)
        return

    if original_text.startswith("/"):
        return

    is_valid, platform = processors.video_platform_processor._validate_url(original_text)

    if is_valid:
        msg = await message.answer(
            f"🔗 Обрабатываю {platform} видео...\n{config.MSG_LOOKING_FOR_SUBTITLES}"
        )
        try:
            original_text = await processors.video_platform_processor.process_video_url(
                original_text, _groq_clients, with_timecodes=True
            )
            if original_text.startswith("❌"):
                await msg.edit_text(original_text)
                return
        except Exception as e:
            logger.error(f"Video URL handler error: {e}")
            await msg.edit_text(f"❌ Ошибка обработки видеоссылки: {str(e)[:100]}")
            return
    else:
        msg = await message.answer("📝 Анализирую текст...")

    try:
        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, "basic", available_modes)

        ctx = user_context.get(user_id, {}).get(msg.message_id)
        if ctx:
            ctx["type"] = "text" if not is_valid else f"video_{platform}"
            ctx["chat_id"] = message.chat.id
            ctx["original"] = original_text

        preview = original_text[: config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        msg_title = (
            "🔗 <b>Извлеченный текст из видео:</b>"
            if is_valid
            else "📝 <b>Полученный текст:</b>"
        )

        await msg.edit_text(
            f"{msg_title}\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id),
        )

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Text handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки текста")


# ============================================================================
# ДИАЛОГОВЫЕ CALLBACK
# ============================================================================

@router.callback_query(F.data.startswith("dialog_start_"))
async def dialog_start_callback(callback: types.CallbackQuery):
    await callback.answer()

    parts = callback.data.split("_")
    if len(parts) < 4:
        return

    user_id = int(parts[2])
    msg_id = int(parts[3])

    if callback.from_user.id != user_id:
        await callback.answer("⚠️ Это не ваш запрос!", show_alert=True)
        return

    if user_id not in user_context or msg_id not in user_context[user_id]:
        await callback.message.edit_text("❌ Документ не найден. Попробуйте заново.")
        return

    doc_text = user_context[user_id][msg_id].get("original", "")

    if not hasattr(processors, "document_dialogues"):
        processors.document_dialogues = {}
    if user_id not in processors.document_dialogues:
        processors.document_dialogues[user_id] = {}

    processors.document_dialogues[user_id][msg_id] = {
        "text": doc_text,
        "history": [],
    }
    active_dialogs[user_id] = msg_id

    filename = user_context[user_id][msg_id].get("filename", "документ")

    await callback.message.edit_text(
        f"💬 <b>Режим вопросов активирован</b>\n\n"
        f"📄 Документ: {filename}\n"
        f"📊 Размер текста: {len(doc_text)} символов\n\n"
        f"Теперь вы можете задавать вопросы по содержимому документа.\n"
        f"Для выхода используйте /exit или кнопку ниже.",
        parse_mode="HTML",
        reply_markup=create_dialog_keyboard(user_id),
    )


@router.callback_query(F.data.startswith("dialog_exit_"))
async def dialog_exit_callback(callback: types.CallbackQuery):
    await callback.answer()

    parts = callback.data.split("_")
    if len(parts) < 3:
        return

    user_id = int(parts[2])
    if callback.from_user.id != user_id:
        return

    if user_id in active_dialogs:
        msg_id = active_dialogs.pop(user_id)
        if (
            hasattr(processors, "document_dialogues")
            and user_id in processors.document_dialogues
            and msg_id in processors.document_dialogues[user_id]
        ):
            history = processors.document_dialogues[user_id][msg_id].get("history", [])
            if len(history) > 10:
                processors.document_dialogues[user_id][msg_id]["history"] = history[-10:]

    await callback.message.edit_text(
        "✅ Вы вышли из режима вопросов. Можете загрузить новый документ."
    )


# ============================================================================
# ОБРАБОТКА РЕЖИМОВ (process_ / mode_ / switch_)
# ============================================================================

@router.callback_query(F.data.startswith("process_"))
async def process_callback(callback: types.CallbackQuery):
    await callback.answer()

    try:
        parts = callback.data.split("_")
        if len(parts) < 4:
            return

        target_user_id = int(parts[1])
        process_type = parts[2]
        msg_id = int(parts[3])

        if callback.from_user.id != target_user_id:
            await callback.message.answer("⚠️ Это не ваш запрос!")
            return

        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.edit_text(
                "❌ Время обработки истекло. Отправьте текст заново."
            )
            return

        available_modes = ctx_data.get("available_modes", ["basic", "premium"])
        if process_type not in available_modes:
            await callback.answer(
                "⚠️ Этот режим недоступен для данного текста", show_alert=True
            )
            return

        original_text = ctx_data.get("original", ctx_data.get("text", ""))
        await callback.message.edit_text(f"⏳ Обрабатываю ({process_type})...")

        if process_type == "basic":
            result = await processors.correct_text_basic(original_text, _groq_clients)
        elif process_type == "premium":
            result = await processors.correct_text_premium(original_text, _groq_clients)
        elif process_type == "summary":
            result = await processors.summarize_text(original_text, _groq_clients)
        else:
            result = "❌ Неизвестный тип обработки"

        user_context[target_user_id][msg_id]["cached_results"][process_type] = result
        user_context[target_user_id][msg_id]["mode"] = process_type

        if len(result) > 4000:
            await callback.message.delete()
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i : i + 4000])
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id, msg_id),
            )
        else:
            await callback.message.edit_text(
                result,
                reply_markup=create_switch_keyboard(target_user_id, msg_id),
            )

    except Exception as e:
        logger.error(f"Process callback error: {e}")
        await callback.message.edit_text("❌ Ошибка обработки")


@router.callback_query(F.data.startswith("mode_"))
async def mode_callback(callback: types.CallbackQuery):
    await callback.answer()

    try:
        parts = callback.data.split("_")
        if len(parts) < 3:
            return

        new_mode = parts[1]
        msg_id = int(parts[2])
        user_id = callback.from_user.id

        ctx_data = user_context.get(user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.answer(
                "❌ Данные устарели. Перешлите сообщение еще раз.", show_alert=True
            )
            return

        if ctx_data["mode"] == new_mode:
            return

        await callback.answer("Обрабатываю...")
        original_text = ctx_data.get("original", ctx_data.get("text", ""))

        if new_mode == "basic":
            processed = await processors.correct_text_basic(original_text, _groq_clients)
        elif new_mode == "premium":
            processed = await processors.correct_text_premium(original_text, _groq_clients)
        elif new_mode == "summary":
            processed = await processors.summarize_text(original_text, _groq_clients)
        else:
            processed = original_text

        user_context[user_id][msg_id]["mode"] = new_mode
        user_context[user_id][msg_id]["cached_results"][new_mode] = processed

        await callback.message.edit_text(
            processed,
            reply_markup=create_keyboard(
                msg_id, new_mode, ctx_data.get("available_modes", ["basic", "premium"])
            ),
        )

    except Exception as e:
        logger.error(f"Mode callback error: {e}")
        await callback.message.edit_text("❌ Ошибка переключения")


@router.callback_query(F.data.startswith("switch_"))
async def switch_callback(callback: types.CallbackQuery):
    await callback.answer()

    try:
        parts = callback.data.split("_")
        if len(parts) < 4:
            return

        target_user_id = int(parts[1])
        target_mode = parts[2]
        msg_id = int(parts[3])

        if callback.from_user.id != target_user_id:
            return

        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.answer("❌ Текст не найден. Обработайте текст заново.")
            return

        available_modes = ctx_data.get("available_modes", ["basic", "premium"])
        if target_mode not in available_modes:
            await callback.answer("⚠️ Этот режим недоступен", show_alert=True)
            return

        cached = ctx_data["cached_results"].get(target_mode)

        if cached:
            result = cached
        else:
            await callback.message.edit_text(f"⏳ Обрабатываю ({target_mode})...")
            original_text = ctx_data.get("original", ctx_data.get("text", ""))

            if target_mode == "basic":
                result = await processors.correct_text_basic(original_text, _groq_clients)
            elif target_mode == "premium":
                result = await processors.correct_text_premium(original_text, _groq_clients)
            elif target_mode == "summary":
                result = await processors.summarize_text(original_text, _groq_clients)
            else:
                result = "❌ Неизвестный режим"

            user_context[target_user_id][msg_id]["cached_results"][target_mode] = result

        user_context[target_user_id][msg_id]["mode"] = target_mode

        if len(result) > 4000:
            await callback.message.delete()
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i : i + 4000])
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id, msg_id),
            )
        else:
            await callback.message.edit_text(
                result,
                reply_markup=create_switch_keyboard(target_user_id, msg_id),
            )

    except Exception as e:
        logger.error(f"Switch callback error: {e}")
        await callback.message.edit_text("❌ Ошибка переключения")


# ============================================================================
# ЭКСПОРТ
# ============================================================================

@router.callback_query(F.data.startswith("export_"))
async def export_callback(callback: types.CallbackQuery):
    await callback.answer()

    try:
        parts = callback.data.split("_")

        if len(parts) == 4:
            # export_{mode}_{msg_id}_{format}
            mode = parts[1]
            msg_id = int(parts[2])
            export_format = parts[3]
            target_user_id = callback.from_user.id
        elif len(parts) == 5:
            # export_{user_id}_{mode}_{msg_id}_{format}
            target_user_id = int(parts[1])
            mode = parts[2]
            msg_id = int(parts[3])
            export_format = parts[4]
        else:
            return

        if callback.from_user.id != target_user_id:
            return

        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.answer("❌ Текст не найден.")
            return

        text = ctx_data["cached_results"].get(mode)
        if not text:
            text = ctx_data.get("original", ctx_data.get("text", ""))
        if not text:
            await callback.answer("⚠️ Текст не найден", show_alert=True)
            return

        status_msg = await callback.message.answer("📁 Создаю файл...")
        filepath = await save_to_file(target_user_id, text, export_format)

        if not filepath:
            await status_msg.edit_text("❌ Ошибка создания файла")
            return

        caption = "📊 PDF файл" if export_format == "pdf" else "📄 Текстовый файл"
        document = FSInputFile(filepath, filename=os.path.basename(filepath))
        await callback.message.answer_document(document=document, caption=caption)
        await status_msg.delete()

        try:
            os.remove(filepath)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Export callback error: {e}")
        await callback.message.answer("❌ Ошибка создания файла")
