# bot.py
"""
Главный файл бота
Версия 4.0 с поддержкой диалога о документах и кружочков
"""

import os
import io
import sys
import signal
import logging
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web
from openai import AsyncOpenAI

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ContentType

import config
import processors

load_dotenv()

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found! Exiting.")
    exit(1)

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище контекста
user_context: Dict[int, Dict[int, Any]] = {}
groq_clients = []
shutdown_event = asyncio.Event()


# ============================================================================
# ОБРАБОТКА СИГНАЛОВ
# ============================================================================

def handle_sigterm(signum, frame):
    logger.info("📡 Received SIGTERM signal, initiating graceful shutdown...")
    asyncio.create_task(shutdown())


async def shutdown():
    logger.info("🛑 Starting graceful shutdown...")
    shutdown_event.set()
    await asyncio.sleep(30)
    await bot.session.close()
    logger.info("✅ Graceful shutdown complete")
    sys.exit(0)


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ GROQ
# ============================================================================

def init_groq_clients():
    global groq_clients
    
    if not GROQ_API_KEYS:
        logger.warning("GROQ_API_KEYS not configured!")
        return
    
    keys = [key.strip() for key in GROQ_API_KEYS.split(",") if key.strip()]
    
    for key in keys:
        try:
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=config.GROQ_TIMEOUT,
            )
            groq_clients.append(client)
            logger.info(f"✅ Groq client initialized: {key[:8]}...")
        except Exception as e:
            logger.error(f"❌ Error initializing client: {e}")
    
    logger.info(f"✅ Total Groq clients: {len(groq_clients)}")


# ============================================================================
# УПРАВЛЕНИЕ КОНТЕКСТОМ
# ============================================================================

def save_to_history(user_id: int, msg_id: int, text: str, mode: str = "basic", available_modes: list = None):
    if user_id not in user_context:
        user_context[user_id] = {}
    
    if len(user_context[user_id]) > config.MAX_CONTEXTS_PER_USER:
        oldest_msg = min(user_context[user_id].keys(), key=lambda k: user_context[user_id][k]['time'])
        user_context[user_id].pop(oldest_msg)
    
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
        "full_text": text,  # Для диалога
    }


async def cleanup_old_contexts():
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(config.CACHE_CHECK_INTERVAL)
            # Логика очистки (как в вашем коде)
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")


# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================

def create_options_keyboard(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с вариантами обработки"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="📝 Как есть", callback_data=f"process_{user_id}_basic_{msg_id}"),
        InlineKeyboardButton(text="✨ Красиво", callback_data=f"process_{user_id}_premium_{msg_id}"),
    )
    
    ctx_data = user_context.get(user_id, {}).get(msg_id, {})
    available_modes = ctx_data.get("available_modes", [])
    
    if "summary" in available_modes:
        builder.row(
            InlineKeyboardButton(text="📊 Саммари", callback_data=f"process_{user_id}_summary_{msg_id}"),
        )
    
    # Добавляем кнопку для диалога (если текст длинный)
    if ctx_data and len(ctx_data.get("original", "")) > config.MIN_CHARS_FOR_SUMMARY * 2:
        builder.row(
            InlineKeyboardButton(text="💬 Задать вопрос", callback_data=f"dialog_start_{user_id}_{msg_id}"),
        )
    
    return builder.as_markup()


def create_dialog_keyboard(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для режима диалога"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="❓ Задать вопрос", callback_data=f"dialog_ask_{user_id}_{msg_id}"),
        InlineKeyboardButton(text="📋 Показать саммари", callback_data=f"process_{user_id}_summary_{msg_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Назад к обработке", callback_data=f"back_to_modes_{user_id}_{msg_id}"),
    )
    
    return builder.as_markup()


def create_switch_keyboard(user_id: int, msg_id: int) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура для переключения режимов"""
    ctx_data = user_context.get(user_id, {}).get(msg_id)
    if not ctx_data:
        return None
    
    current = ctx_data.get("mode", "basic")
    available = ctx_data.get("available_modes", ["basic", "premium"])
    
    builder = InlineKeyboardBuilder()
    
    mode_display = {"basic": "📝 Как есть", "premium": "✨ Красиво", "summary": "📊 Саммари"}
    
    for mode in available:
        if mode != current:
            builder.add(InlineKeyboardButton(
                text=mode_display.get(mode, mode),
                callback_data=f"switch_{user_id}_{mode}_{msg_id}"
            ))
    
    builder.adjust(2)
    
    # Кнопка для диалога
    if len(ctx_data.get("original", "")) > config.MIN_CHARS_FOR_SUMMARY * 2:
        builder.row(
            InlineKeyboardButton(text="💬 Задать вопрос", callback_data=f"dialog_start_{user_id}_{msg_id}"),
        )
    
    builder.row(
        InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{user_id}_{current}_{msg_id}_txt"),
        InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{user_id}_{current}_{msg_id}_pdf")
    )
    
    return builder.as_markup()


# ============================================================================
# СОХРАНЕНИЕ ФАЙЛОВ
# ============================================================================

async def save_to_file(user_id: int, text: str, format_type: str) -> Optional[str]:
    """Сохраняем текст в файл"""
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
            y = height - margin
            
            c.setFont("Helvetica", 11)
            for paragraph in text.split('\n'):
                lines = simpleSplit(paragraph, "Helvetica", 11, width - 2*margin)
                for line in lines:
                    if y < margin + 20:
                        c.showPage()
                        y = height - margin
                    c.drawString(margin, y, line)
                    y -= 14
            
            c.save()
            return filepath
            
        except Exception as e:
            logger.error(f"Error saving PDF: {e}")
            return None
    
    return None


# ============================================================================
# ВЕБ-СЕРВЕР
# ============================================================================

async def health_check(request):
    return web.Response(text='{"status": "healthy"}', content_type="application/json")


async def start_web_server():
    try:
        app = web.Application()
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check)
        
        port = int(os.environ.get("PORT", 8080))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        logger.info(f"✅ Web server started on port {port}")
        await shutdown_event.wait()
        await runner.cleanup()
        
    except Exception as e:
        logger.error(f"❌ Error in web server: {e}")


# ============================================================================
# ХЭНДЛЕРЫ
# ============================================================================

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(config.START_MESSAGE, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())


@dp.message(Command("help"))
async def help_handler(message: types.Message):
    await message.answer(config.HELP_MESSAGE, parse_mode="HTML")


@dp.message(Command("status"))
async def status_handler(message: types.Message):
    status_text = config.STATUS_MESSAGE.format(
        groq_count=len(groq_clients),
        users_count=len(user_context),
        vision_status="✅" if groq_clients else "❌",
        docx_status="✅" if processors.DOCX_AVAILABLE else "❌",
        temp_files=0
    )
    await message.answer(status_text, parse_mode="HTML")


@dp.message(F.voice | F.video_note | F.audio)
async def media_handler(message: types.Message):
    """Обработка голосовых, кружочков и аудио"""
    user_id = message.from_user.id
    msg = await message.answer(config.MSG_PROCESSING_VOICE)
    
    try:
        file_id = None
        if message.voice:
            file_id = message.voice.file_id
        elif message.video_note:
            file_id = message.video_note.file_id
        elif message.audio:
            file_id = message.audio.file_id
        
        file_info = await bot.get_file(file_id)
        file_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, file_buffer)
        
        original_text = await processors.transcribe_voice(file_buffer.getvalue(), groq_clients)
        
        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return
        
        available_modes = processors.get_available_modes(original_text)
        
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)
        
        preview = original_text[:config.PREVIEW_LENGTH] + ("..." if len(original_text) > config.PREVIEW_LENGTH else "")
        
        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n<i>{preview}</i>\n\n<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        
    except Exception as e:
        logger.error(f"Media handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки")


@dp.message(F.text)
async def text_handler(message: types.Message):
    """Обработка текста и ссылок"""
    user_id = message.from_user.id
    original_text = message.text.strip()
    
    if original_text.startswith("/"):
        return
    
    # Проверяем, не ответ ли это на вопрос в диалоге
    if message.reply_to_message:
        # Ищем, есть ли активный диалог
        for msg_id, ctx in user_context.get(user_id, {}).items():
            if ctx.get("mode") == "dialog":
                # Это ответ на вопрос
                await handle_dialog_question(message, msg_id)
                return
    
    # Проверяем ссылку на видео
    is_valid, platform = processors.video_platform_processor._validate_url(original_text)
    
    if is_valid:
        msg = await message.answer(f"🔗 Обрабатываю {platform} видео...")
        try:
            original_text = await processors.video_platform_processor.process_video_url(original_text, groq_clients)
            if original_text.startswith("❌"):
                await msg.edit_text(original_text)
                return
        except Exception as e:
            logger.error(f"Video URL error: {e}")
            await msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
            return
    else:
        msg = await message.answer("📝 Анализирую текст...")
    
    try:
        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)
        
        preview = original_text[:config.PREVIEW_LENGTH] + ("..." if len(original_text) > config.PREVIEW_LENGTH else "")
        
        await msg.edit_text(
            f"📝 <b>Текст:</b>\n\n<i>{preview}</i>\n\n<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        
    except Exception as e:
        logger.error(f"Text handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки")


@dp.message(F.photo | F.document)
async def file_handler(message: types.Message):
    """Обработка файлов, изображений и видео"""
    user_id = message.from_user.id
    msg = await message.answer("📁 Обрабатываю файл...")
    
    try:
        file_info = None
        filename = ""
        
        if message.photo:
            file_info = await bot.get_file(message.photo[-1].file_id)
            filename = f"photo_{file_info.file_unique_id}.jpg"
        elif message.document:
            file_info = await bot.get_file(message.document.file_id)
            filename = message.document.file_name or f"file_{file_info.file_unique_id}"
        
        file_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, file_buffer)
        file_bytes = file_buffer.getvalue()
        
        if len(file_bytes) > config.FILE_SIZE_LIMIT:
            await msg.edit_text(config.ERROR_FILE_TOO_LARGE)
            return
        
        original_text = await processors.extract_text_from_file(file_bytes, filename, groq_clients)
        
        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return
        
        if not original_text.strip() or len(original_text.strip()) < config.MIN_TEXT_LENGTH:
            await msg.edit_text(config.ERROR_NO_TEXT_IN_FILE)
            return
        
        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)
        
        # Сохраняем полный текст для диалога
        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["full_text"] = original_text
        
        preview = original_text[:config.PREVIEW_LENGTH] + ("..." if len(original_text) > config.PREVIEW_LENGTH else "")
        
        file_type = "видео" if filename.split('.')[-1].lower() in config.VIDEO_SUPPORTED_FORMATS else "файла"
        
        await msg.edit_text(
            f"✅ <b>Извлеченный текст из {file_type}:</b>\n\n<i>{preview}</i>\n\n<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        
    except Exception as e:
        logger.error(f"File handler error: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")


# ============================================================================
# ОБРАБОТЧИКИ ДИАЛОГА
# ============================================================================

@dp.callback_query(F.data.startswith("dialog_start_"))
async def dialog_start_callback(callback: types.CallbackQuery):
    """Начало диалога по документу"""
    await callback.answer()
    
    try:
        parts = callback.data.split("_")
        target_user_id = int(parts[2])
        msg_id = int(parts[3])
        
        if callback.from_user.id != target_user_id:
            await callback.message.answer("⚠️ Это не ваш запрос!")
            return
        
        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.edit_text("❌ Данные устарели. Отправьте документ заново.")
            return
        
        # Сохраняем документ для диалога
        full_text = ctx_data.get("full_text", ctx_data.get("original", ""))
        processors.save_document_for_dialog(target_user_id, msg_id, full_text)
        
        # Обновляем режим
        user_context[target_user_id][msg_id]["mode"] = "dialog"
        
        await callback.message.edit_text(
            "💬 <b>Режим вопросов по документу</b>\n\n"
            "Теперь вы можете задавать вопросы по содержанию документа.\n"
            "Просто напишите вопрос в чат (ответом на это сообщение).\n\n"
            "Или нажмите кнопку ниже, чтобы вернуться к обработке.",
            parse_mode="HTML",
            reply_markup=create_dialog_keyboard(target_user_id, msg_id)
        )
        
    except Exception as e:
        logger.error(f"Dialog start error: {e}")


@dp.callback_query(F.data.startswith("dialog_ask_"))
async def dialog_ask_callback(callback: types.CallbackQuery):
    """Подготовка к вопросу"""
    await callback.answer()
    
    await callback.message.answer(
        "❓ Напишите ваш вопрос по документу (ответом на это сообщение)."
    )


async def handle_dialog_question(message: types.Message, doc_msg_id: int):
    """Обработка вопроса в диалоге"""
    user_id = message.from_user.id
    question = message.text.strip()
    
    if not question:
        await message.answer("❓ Пожалуйста, напишите вопрос.")
        return
    
    processing = await message.answer("💭 Думаю над ответом...")
    
    try:
        answer = await processors.answer_document_question(
            user_id, doc_msg_id, question, groq_clients
        )
        
        await processing.delete()
        await message.answer(
            f"💬 <b>Ответ:</b>\n\n{answer}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Dialog question error: {e}")
        await processing.edit_text("❌ Ошибка при ответе на вопрос")


@dp.callback_query(F.data.startswith("back_to_modes_"))
async def back_to_modes_callback(callback: types.CallbackQuery):
    """Возврат к режимам обработки"""
    await callback.answer()
    
    try:
        parts = callback.data.split("_")
        target_user_id = int(parts[3])
        msg_id = int(parts[4])
        
        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.edit_text("❌ Данные устарели.")
            return
        
        user_context[target_user_id][msg_id]["mode"] = "basic"
        
        preview = ctx_data["original"][:config.PREVIEW_LENGTH] + "..."
        
        await callback.message.edit_text(
            f"📝 <b>Текст:</b>\n\n<i>{preview}</i>\n\n<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(target_user_id, msg_id)
        )
        
    except Exception as e:
        logger.error(f"Back to modes error: {e}")


# ============================================================================
# ОБРАБОТЧИКИ ОБРАБОТКИ ТЕКСТА
# ============================================================================

@dp.callback_query(F.data.startswith("process_"))
async def process_callback(callback: types.CallbackQuery):
    """Начальная обработка текста"""
    await callback.answer()
    
    try:
        parts = callback.data.split("_")
        target_user_id = int(parts[1])
        process_type = parts[2]
        msg_id = int(parts[3])
        
        if callback.from_user.id != target_user_id:
            await callback.message.answer("⚠️ Это не ваш запрос!")
            return
        
        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.edit_text("❌ Время обработки истекло.")
            return
        
        available_modes = ctx_data.get("available_modes", ["basic", "premium"])
        if process_type not in available_modes:
            await callback.answer("⚠️ Режим недоступен", show_alert=True)
            return
        
        original_text = ctx_data.get("original", "")
        processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({process_type})...")
        
        if process_type == "basic":
            result = await processors.correct_text_basic(original_text, groq_clients)
        elif process_type == "premium":
            result = await processors.correct_text_premium(original_text, groq_clients)
        elif process_type == "summary":
            result = await processors.summarize_text(original_text, groq_clients)
        else:
            result = "❌ Неизвестный тип обработки"
        
        user_context[target_user_id][msg_id]["cached_results"][process_type] = result
        user_context[target_user_id][msg_id]["mode"] = process_type
        
        if len(result) > 4000:
            await processing_msg.delete()
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            await callback.message.answer(
                "💾 <b>Действия:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id, msg_id)
            )
        else:
            await processing_msg.edit_text(
                result,
                reply_markup=create_switch_keyboard(target_user_id, msg_id)
            )
            
    except Exception as e:
        logger.error(f"Process callback error: {e}")
        await callback.message.edit_text("❌ Ошибка обработки")


@dp.callback_query(F.data.startswith("switch_"))
async def switch_callback(callback: types.CallbackQuery):
    """Переключение между режимами"""
    await callback.answer()
    
    try:
        parts = callback.data.split("_")
        target_user_id = int(parts[1])
        target_mode = parts[2]
        msg_id = int(parts[3])
        
        if callback.from_user.id != target_user_id:
            return
        
        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.answer("❌ Текст не найден.")
            return
        
        cached = ctx_data["cached_results"].get(target_mode)
        
        if cached:
            result = cached
        else:
            processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({target_mode})...")
            original_text = ctx_data.get("original", "")
            
            if target_mode == "basic":
                result = await processors.correct_text_basic(original_text, groq_clients)
            elif target_mode == "premium":
                result = await processors.correct_text_premium(original_text, groq_clients)
            elif target_mode == "summary":
                result = await processors.summarize_text(original_text, groq_clients)
            else:
                result = "❌ Неизвестный режим"
            
            user_context[target_user_id][msg_id]["cached_results"][target_mode] = result
        
        user_context[target_user_id][msg_id]["mode"] = target_mode
        
        if len(result) > 4000:
            await callback.message.delete()
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            await callback.message.answer(
                "💾 <b>Действия:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id, msg_id)
            )
        else:
            # ИСПРАВЛЕНО: проверяем, изменился ли текст
            current_text = callback.message.text
            if current_text != result:
                await callback.message.edit_text(
                    result,
                    reply_markup=create_switch_keyboard(target_user_id, msg_id)
                )
            else:
                # Текст не изменился, просто обновляем клавиатуру
                await callback.message.edit_reply_markup(
                    reply_markup=create_switch_keyboard(target_user_id, msg_id)
                )
            
    except Exception as e:
        logger.error(f"Switch callback error: {e}")


@dp.callback_query(F.data.startswith("export_"))
async def export_callback(callback: types.CallbackQuery):
    """Экспорт в файл"""
    await callback.answer()
    
    try:
        parts = callback.data.split("_")
        target_user_id = int(parts[1])
        mode = parts[2]
        msg_id = int(parts[3])
        export_format = parts[4]
        
        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.answer("❌ Текст не найден.")
            return
        
        text = ctx_data["cached_results"].get(mode)
        if not text:
            text = ctx_data.get("original", "")
        
        if not text:
            await callback.answer("⚠️ Текст не найден", show_alert=True)
            return
        
        status_msg = await callback.message.answer("📁 Создаю файл...")
        filepath = await save_to_file(target_user_id, text, export_format)
        
        if not filepath:
            await status_msg.edit_text("❌ Ошибка создания файла")
            return
        
        document = FSInputFile(filepath, filename=os.path.basename(filepath))
        await callback.message.answer_document(
            document=document,
            caption="📄 Готово"
        )
        
        await status_msg.delete()
        os.remove(filepath)
        
    except Exception as e:
        logger.error(f"Export error: {e}")


# ============================================================================
# ЗАПУСК
# ============================================================================

async def main():
    logger.info("🚀 Bot v4.0 starting...")
    
    signal.signal(signal.SIGTERM, handle_sigterm)
    
    init_groq_clients()
    processors.vision_processor.init_clients(groq_clients)
    
    web_server_task = asyncio.create_task(start_web_server())
    cleanup_task = asyncio.create_task(cleanup_old_contexts())
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot)
    finally:
        web_server_task.cancel()
        cleanup_task.cancel()
        await asyncio.gather(web_server_task, cleanup_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")