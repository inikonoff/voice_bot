# bot.py (v3)
"""
Главный файл бота: хэндлеры, управление контекстом, видео-обработка
Версия 3.0 с поддержкой YouTube, TikTok, Rutube, Instagram, Vimeo
"""

import os
import io
import logging
import asyncio
import sys
from typing import Optional, List, Dict
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
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found! Exiting.")
    exit(1)

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_context = {}
groq_clients = []
current_client_index = 0

# Счётчик временных файлов
temp_files_count = 0


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ GROQ КЛИЕНТОВ
# ============================================================================

def init_groq_clients():
    """Инициализация клиентов Groq"""
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
            logger.error(f"❌ Error initializing client {key[:8]}...: {e}")
    
    logger.info(f"✅ Total Groq clients: {len(groq_clients)}")


# ============================================================================
# УПРАВЛЕНИЕ КЭШЕМ И КОНТЕКСТОМ
# ============================================================================

async def cleanup_old_contexts():
    """Фоновая задача: удаление контекстов старше CACHE_TIMEOUT_SECONDS"""
    while True:
        try:
            await asyncio.sleep(config.CACHE_CHECK_INTERVAL)
            
            current_time = datetime.now().timestamp()
            users_to_delete = []
            
            for user_id, ctx in user_context.items():
                context_age = current_time - ctx.get("created_at", current_time)
                
                if context_age > config.CACHE_TIMEOUT_SECONDS:
                    users_to_delete.append(user_id)
            
            if len(user_context) > config.MAX_CONTEXTS:
                contexts_by_age = sorted(
                    user_context.items(),
                    key=lambda x: x[1].get("created_at", 0)
                )
                users_to_delete.extend([uid for uid, _ in contexts_by_age[:len(user_context) - config.MAX_CONTEXTS]])
            
            for user_id in users_to_delete:
                if user_id in user_context:
                    del user_context[user_id]
                    logger.debug(f"Cleaned up context for user {user_id}")
            
            if users_to_delete:
                logger.info(f"Cache cleanup: removed {len(set(users_to_delete))} contexts. Current users: {len(user_context)}")
                
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")


async def cleanup_temp_files():
    """Фоновая задача: удаление старых временных файлов"""
    while True:
        try:
            await asyncio.sleep(config.TEMP_FILE_RETENTION)
            
            if not config.CLEANUP_TEMP_FILES:
                continue
            
            current_time = datetime.now().timestamp()
            temp_dir = config.TEMP_DIR
            
            if not os.path.exists(temp_dir):
                continue
            
            deleted_count = 0
            for filename in os.listdir(temp_dir):
                if filename.startswith('video_') or filename.startswith('audio_'):
                    filepath = os.path.join(temp_dir, filename)
                    
                    try:
                        file_age = current_time - os.path.getmtime(filepath)
                        if file_age > config.TEMP_FILE_RETENTION:
                            os.remove(filepath)
                            deleted_count += 1
                            logger.debug(f"Deleted temp file: {filename}")
                    except Exception as e:
                        logger.debug(f"Error deleting temp file {filename}: {e}")
            
            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} temporary files")
                
        except Exception as e:
            logger.error(f"Temp files cleanup error: {e}")


# ============================================================================
# СОЗДАНИЕ КЛАВИАТУР
# ============================================================================

def create_options_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с вариантами обработки"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="📝 Как есть", callback_data=f"process_{user_id}_basic"),
        InlineKeyboardButton(text="✨ Красиво", callback_data=f"process_{user_id}_premium"),
    )
    
    ctx = user_context.get(user_id)
    available_modes = ctx.get("available_modes", []) if ctx else []
    
    if "summary" in available_modes:
        builder.row(
            InlineKeyboardButton(text="📊 Саммари", callback_data=f"process_{user_id}_summary"),
        )
    
    return builder.as_markup()


def create_switch_keyboard(user_id: int) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура для переключения между режимами"""
    ctx = user_context.get(user_id)
    if not ctx:
        return None
    
    current = ctx.get("current_mode")
    available = ctx.get("available_modes", [])
    
    builder = InlineKeyboardBuilder()
    
    mode_buttons = []
    if "basic" in available and current != "basic":
        mode_buttons.append(InlineKeyboardButton(text="📝 Как есть", callback_data=f"switch_{user_id}_basic"))
    if "premium" in available and current != "premium":
        mode_buttons.append(InlineKeyboardButton(text="✨ Красиво", callback_data=f"switch_{user_id}_premium"))
    if "summary" in available and current != "summary":
        mode_buttons.append(InlineKeyboardButton(text="📊 Саммари", callback_data=f"switch_{user_id}_summary"))
    
    for i in range(0, len(mode_buttons), 2):
        builder.row(*mode_buttons[i:i+2])
    
    if current:
        builder.row(
            InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{user_id}_{current}_txt"),
            InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{user_id}_{current}_pdf")
        )
    
    return builder.as_markup()


# ============================================================================
# СОХРАНЕНИЕ ФАЙЛОВ
# ============================================================================

async def save_to_file(user_id: int, text: str, format_type: str) -> Optional[str]:
    """Сохраняем текст в файл (TXT или PDF)"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"text_{user_id}_{timestamp}"
    
    if format_type == "txt":
        filepath = f"{config.TEMP_DIR}/{filename}.txt"
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            logger.debug(f"Saved TXT file: {filepath}")
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
            
            for paragraph in text.split('\n'):
                if not paragraph.strip():
                    y -= line_height
                    continue
                    
                lines = simpleSplit(paragraph, "Helvetica", 11, max_width)
                
                for line in lines:
                    if y < margin + 20:
                        c.showPage()
                        y = height - margin
                        c.setFont("Helvetica", 11)
                    c.drawString(margin, y, line)
                    y -= line_height
            
            c.save()
            logger.debug(f"Saved PDF file: {filepath}")
            return filepath
            
        except ImportError:
            logger.warning("Reportlab not installed, using txt fallback")
            filepath = f"{config.TEMP_DIR}/{filename}.txt"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
        except Exception as e:
            logger.error(f"Error saving PDF: {e}")
            return None
    
    return None


# ============================================================================
# ВЕБ-СЕРВЕР (для Render/Uptime Robot)
# ============================================================================

async def health_check(request):
    """Health check для Uptime Robot и Render"""
    return web.Response(text="Bot is alive!", status=200)


async def start_web_server():
    """Запуск фонового веб-сервера"""
    try:
        app = web.Application()
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check)
        app.router.add_get('/ping', health_check)
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"✅ WEB SERVER STARTED ON PORT {port}")
    except Exception as e:
        logger.error(f"❌ Error starting web server: {e}")


# ============================================================================
# ХЭНДЛЕРЫ БОТА
# ============================================================================

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    """Команда /start"""
    await message.answer(
        config.START_MESSAGE,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(Command("help"))
async def help_handler(message: types.Message):
    """Команда /help"""
    await message.answer(
        config.HELP_MESSAGE,
        parse_mode="HTML"
    )


@dp.message(Command("status"))
async def status_handler(message: types.Message):
    """Команда /status"""
    
    docx_status = "✅"
    try:
        import docx
    except ImportError:
        docx_status = "❌"
    
    temp_files = len([f for f in os.listdir(config.TEMP_DIR) 
                     if f.startswith('video_') or f.startswith('audio_')]) if os.path.exists(config.TEMP_DIR) else 0
    
    status_text = config.STATUS_MESSAGE.format(
        groq_count=len(groq_clients),
        users_count=len(user_context),
        vision_status="✅" if groq_clients else "❌",
        docx_status=docx_status,
        temp_files=temp_files
    )
    
    await message.answer(status_text, parse_mode="HTML")


@dp.message(F.voice)
async def voice_handler(message: types.Message):
    """Обработка голосовых сообщений и кружочков"""
    user_id = message.from_user.id
    msg = await message.answer(config.MSG_PROCESSING_VOICE)
    
    try:
        file_info = await bot.get_file(message.voice.file_id)
        
        voice_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, voice_buffer)
        
        original_text = await processors.transcribe_voice(voice_buffer.getvalue(), groq_clients)
        
        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return
        
        available_modes = processors.get_available_modes(original_text)
        
        user_context[user_id] = {
            "type": "voice",
            "original": original_text,
            "cached_results": {"basic": None, "premium": None, "summary": None},
            "current_mode": None,
            "available_modes": available_modes,
            "message_id": msg.message_id,
            "chat_id": message.chat.id,
            "created_at": datetime.now().timestamp()
        }
        
        preview = original_text[:config.PREVIEW_LENGTH]
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
            reply_markup=create_options_keyboard(user_id)
        )
        
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки голосового сообщения")


@dp.message(F.audio)
async def audio_handler(message: types.Message):
    """Обработка аудиофайлов"""
    user_id = message.from_user.id
    msg = await message.answer(config.MSG_TRANSCRIBING)
    
    try:
        file_info = await bot.get_file(message.audio.file_id)
        
        audio_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, audio_buffer)
        
        original_text = await processors.transcribe_voice(audio_buffer.getvalue(), groq_clients)
        
        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return
        
        available_modes = processors.get_available_modes(original_text)
        
        user_context[user_id] = {
            "type": "audio",
            "original": original_text,
            "cached_results": {"basic": None, "premium": None, "summary": None},
            "current_mode": None,
            "available_modes": available_modes,
            "message_id": msg.message_id,
            "chat_id": message.chat.id,
            "created_at": datetime.now().timestamp()
        }
        
        preview = original_text[:config.PREVIEW_LENGTH]
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
            reply_markup=create_options_keyboard(user_id)
        )
        
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Audio handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки аудиофайла")


@dp.message(F.text)
async def text_handler(message: types.Message):
    """Обработка текстовых сообщений и ссылок"""
    user_id = message.from_user.id
    original_text = message.text.strip()
    
    if original_text.startswith("/"):
        return
    
    # Проверяем, не ссылка ли это на видеоплатформу
    is_valid, platform = processors.video_platform_processor._validate_url(original_text)
    
    if is_valid:
        # Обработка ссылки на видео
        msg = await message.answer(f"🔗 Обрабатываю {platform} видео...\n{config.MSG_LOOKING_FOR_SUBTITLES}")
        
        try:
            original_text = await processors.video_platform_processor.process_video_url(original_text, groq_clients)
            
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
        
        user_context[user_id] = {
            "type": "text" if not is_valid else f"video_{platform}",
            "original": original_text,
            "cached_results": {"basic": None, "premium": None, "summary": None},
            "current_mode": None,
            "available_modes": available_modes,
            "message_id": msg.message_id,
            "chat_id": message.chat.id,
            "created_at": datetime.now().timestamp()
        }
        
        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."
        
        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"
        
        msg_title = "🔗 <b>Извлеченный текст из видео:</b>" if is_valid else "📝 <b>Полученный текст:</b>"
        
        await msg.edit_text(
            f"{msg_title}\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id)
        )
        
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Text handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки текста")


@dp.message(F.photo | F.document)
async def file_handler(message: types.Message):
    """Обработка файлов и изображений"""
    user_id = message.from_user.id
    msg = await message.answer("📁 Обрабатываю файл...")
    
    try:
        file_info = None
        file_bytes = None
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
        
        # Определяем тип файла
        file_ext = filename.lower().split('.')[-1] if '.' in filename else ''
        
        # Видеофайлы
        if file_ext in processors.config.VIDEO_SUPPORTED_FORMATS:
            await msg.edit_text(config.MSG_EXTRACTING_AUDIO)
        else:
            await msg.edit_text("🔍 Извлекаю текст...")
        
        original_text = await processors.extract_text_from_file(file_bytes, filename, groq_clients)
        
        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return
        
        if not original_text.strip() or len(original_text.strip()) < config.MIN_TEXT_LENGTH:
            await msg.edit_text(config.ERROR_NO_TEXT_IN_FILE)
            return
        
        available_modes = processors.get_available_modes(original_text)
        
        user_context[user_id] = {
            "type": "file",
            "original": original_text,
            "cached_results": {"basic": None, "premium": None, "summary": None},
            "current_mode": None,
            "available_modes": available_modes,
            "message_id": msg.message_id,
            "chat_id": message.chat.id,
            "filename": filename,
            "created_at": datetime.now().timestamp()
        }
        
        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."
        
        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"
        
        file_type = "видео" if file_ext in processors.config.VIDEO_SUPPORTED_FORMATS else \
                   "изображения" if filename.startswith("photo_") or any(
            ext in filename.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']
        ) else "файла"
        
        await msg.edit_text(
            f"✅ <b>Извлеченный текст из {file_type}:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id)
        )
        
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"File handler error: {e}")
        await msg.edit_text(f"❌ Ошибка обработки файла: {str(e)[:100]}")


# ============================================================================
# CALLBACK ОБРАБОТЧИКИ
# ============================================================================

@dp.callback_query(F.data.startswith("process_"))
async def process_callback(callback: types.CallbackQuery):
    """Начальная обработка текста"""
    await callback.answer()
    
    try:
        parts = callback.data.split("_")
        if len(parts) < 3:
            return
        
        target_user_id = int(parts[1])
        process_type = parts[2]
        
        if callback.from_user.id != target_user_id:
            await callback.message.answer("⚠️ Это не ваш запрос!")
            return
        
        if target_user_id not in user_context:
            await callback.message.edit_text("❌ Время обработки истекло. Отправьте текст заново.")
            return
        
        ctx = user_context[target_user_id]
        available_modes = ctx.get("available_modes", [])
        
        if process_type not in available_modes:
            await callback.answer("⚠️ Этот режим недоступен для данного текста", show_alert=True)
            return
        
        original_text = ctx["original"]
        
        processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({process_type})...")
        
        if process_type == "basic":
            result = await processors.correct_text_basic(original_text, groq_clients)
        elif process_type == "premium":
            result = await processors.correct_text_premium(original_text, groq_clients)
        elif process_type == "summary":
            result = await processors.summarize_text(original_text, groq_clients)
        else:
            result = "❌ Неизвестный тип обработки"
        
        user_context[target_user_id]["cached_results"][process_type] = result
        user_context[target_user_id]["current_mode"] = process_type
        
        if len(result) > 4000:
            await processing_msg.delete()
            
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id)
            )
        else:
            await processing_msg.edit_text(
                result,
                reply_markup=create_switch_keyboard(target_user_id)
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
        if len(parts) < 3:
            return
        
        target_user_id = int(parts[1])
        target_mode = parts[2]
        
        if callback.from_user.id != target_user_id:
            return
        
        if target_user_id not in user_context:
            await callback.message.answer("❌ Текст не найден. Обработайте текст заново.")
            return
        
        ctx = user_context[target_user_id]
        available_modes = ctx.get("available_modes", [])
        
        if target_mode not in available_modes:
            await callback.answer("⚠️ Этот режим недоступен", show_alert=True)
            return
        
        cached = ctx["cached_results"].get(target_mode)
        
        if cached:
            result = cached
        else:
            processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({target_mode})...")
            
            original_text = ctx["original"]
            
            if target_mode == "basic":
                result = await processors.correct_text_basic(original_text, groq_clients)
            elif target_mode == "premium":
                result = await processors.correct_text_premium(original_text, groq_clients)
            elif target_mode == "summary":
                result = await processors.summarize_text(original_text, groq_clients)
            else:
                result = "❌ Неизвестный режим"
            
            user_context[target_user_id]["cached_results"][target_mode] = result
        
        user_context[target_user_id]["current_mode"] = target_mode
        
        if len(result) > 4000:
            await callback.message.delete()
            
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id)
            )
        else:
            await callback.message.edit_text(
                result,
                reply_markup=create_switch_keyboard(target_user_id)
            )
            
    except Exception as e:
        logger.error(f"Switch callback error: {e}")
        await callback.message.edit_text("❌ Ошибка переключения")


@dp.callback_query(F.data.startswith("export_"))
async def export_callback(callback: types.CallbackQuery):
    """Экспорт в файл"""
    await callback.answer()
    
    try:
        parts = callback.data.split("_")
        if len(parts) < 4:
            return
        
        target_user_id = int(parts[1])
        mode = parts[2]
        export_format = parts[3]
        
        if callback.from_user.id != target_user_id:
            return
        
        if target_user_id not in user_context:
            await callback.message.answer("❌ Текст не найден.")
            return
        
        ctx = user_context[target_user_id]
        text = ctx["cached_results"].get(mode)
        
        if not text:
            await callback.answer("⚠️ Текст не найден в кэше", show_alert=True)
            return
        
        status_msg = await callback.message.answer("📁 Создаю файл...")
        filepath = await save_to_file(target_user_id, text, export_format)
        
        if not filepath:
            await status_msg.edit_text("❌ Ошибка создания файла")
            return
        
        filename = os.path.basename(filepath)
        caption = "📊 PDF файл" if export_format == "pdf" else "📄 Текстовый файл"
        
        document = FSInputFile(filepath, filename=filename)
        await callback.message.answer_document(document=document, caption=caption)
        
        await status_msg.delete()
        
        try:
            os.remove(filepath)
        except:
            pass
        
    except Exception as e:
        logger.error(f"Export callback error: {e}")
        await callback.message.answer("❌ Ошибка создания файла")


# ============================================================================
# ЗАПУСК БОТА
# ============================================================================

async def main():
    logger.info("🚀 Bot v3.0 starting process...")
    
    init_groq_clients()
    processors.vision_processor.init_clients(groq_clients)
    
    asyncio.create_task(start_web_server())
    asyncio.create_task(cleanup_old_contexts())
    asyncio.create_task(cleanup_temp_files())
    
    logger.info("✅ Starting polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
