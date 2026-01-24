# bot.py
import os
import io
import logging
import asyncio
import sys
from dotenv import load_dotenv
from aiohttp import web
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F, html
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем наши сервисы
from groq_services import (
    transcribe_voice,
    correct_text_basic,
    correct_text_premium,
    summarize_text,
    check_text_length
)

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found! Exiting.")
    exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище контекста: {user_id: {type: "voice/text", original: "...", processed: {...}}}
user_context = {}

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def health_check(request):
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    try:
        app = web.Application()
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check)
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"✅ WEB SERVER STARTED ON PORT {port}")
    except Exception as e:
        logger.error(f"❌ Error starting web server: {e}")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def create_options_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создаем клавиатуру с вариантами обработки"""
    builder = InlineKeyboardBuilder()
    
    # Основные варианты
    builder.row(
        InlineKeyboardButton(text="📝 Как есть", callback_data=f"process_{user_id}_basic"),
        InlineKeyboardButton(text="✨ Красиво", callback_data=f"process_{user_id}_premium"),
    )
    
    # Саммари только для длинных текстов
    builder.row(
        InlineKeyboardButton(text="📊 Саммари", callback_data=f"process_{user_id}_summary"),
    )
    
    return builder.as_markup()

def create_export_keyboard(user_id: int, text_type: str) -> InlineKeyboardMarkup:
    """Создаем клавиатуру для экспорта"""
    builder = InlineKeyboardBuilder()
    
    # Простые варианты экспорта
    builder.row(
        InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{user_id}_{text_type}_txt"),
        InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{user_id}_{text_type}_pdf"),
    )
    
    return builder.as_markup()

async def save_to_file(user_id: int, text: str, format_type: str) -> str:
    """Сохраняем текст в файл"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"text_{user_id}_{timestamp}"
    
    if format_type == "txt":
        filepath = f"/tmp/{filename}.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)
        return filepath
    elif format_type == "pdf":
        # Простой PDF через reportlab (установите через requirements.txt)
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfgen import canvas
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            import textwrap
            
            filepath = f"/tmp/{filename}.pdf"
            
            # Создаем PDF
            c = canvas.Canvas(filepath, pagesize=letter)
            width, height = letter
            
            # Настройки
            margin = 50
            line_height = 14
            y = height - margin
            
            # Добавляем заголовок
            c.setFont("Helvetica", 16)
            c.drawString(margin, y, "Обработанный текст")
            y -= 30
            
            # Добавляем дату
            c.setFont("Helvetica", 10)
            c.drawString(margin, y, f"Создано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            y -= 30
            
            # Добавляем текст
            c.setFont("Helvetica", 12)
            lines = textwrap.wrap(text, width=80)
            
            for line in lines:
                if y < margin:
                    c.showPage()
                    y = height - margin
                    c.setFont("Helvetica", 12)
                
                c.drawString(margin, y, line)
                y -= line_height
            
            c.save()
            return filepath
            
        except ImportError:
            # Fallback: если reportlab не установлен, сохраняем как txt
            logger.warning("Reportlab not installed, using txt fallback")
            filepath = f"/tmp/{filename}.txt"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
    
    return None

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "👋 <b>Текст-редактор бот</b>\n\n"
        "Отправьте мне голосовое или текстовое сообщение, и я предложу варианты обработки:\n\n"
        "• <b>📝 Как есть</b> - исправление ошибок, пунктуация\n"
        "• <b>✨ Красиво</b> - уборка слов-паразитов, улучшение стиля\n"
        "• <b>📊 Саммари</b> - краткое содержание (для длинных текстов)\n\n"
        "После обработки можно экспортировать текст в файл.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(F.voice | F.audio)
async def voice_handler(message: types.Message):
    user_id = message.from_user.id
    msg = await message.answer("🎧 Распознаю голосовое сообщение...")
    
    try:
        # Скачиваем голосовое
        if message.voice:
            file_info = await bot.get_file(message.voice.file_id)
        else:
            file_info = await bot.get_file(message.audio.file_id)
        
        voice_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, voice_buffer)
        
        # Распознаем
        original_text = await transcribe_voice(voice_buffer.getvalue())
        
        if original_text.startswith("Ошибка"):
            await msg.edit_text(f"❌ {original_text}")
            return
        
        # Сохраняем контекст
        user_context[user_id] = {
            "type": "voice",
            "original": original_text,
            "message_id": msg.message_id,
            "chat_id": message.chat.id
        }
        
        # Предлагаем варианты
        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{original_text[:200]}...</i>\n\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id)
        )
        
        # Удаляем оригинальное сообщение
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await msg.edit_text("❌ Ошибка обработки голосового сообщения")

@dp.message(F.text)
async def text_handler(message: types.Message):
    user_id = message.from_user.id
    original_text = message.text.strip()
    
    if original_text.startswith("/"):
        return
    
    msg = await message.answer("📝 Анализирую текст...")
    
    try:
        # Сохраняем контекст
        user_context[user_id] = {
            "type": "text",
            "original": original_text,
            "message_id": msg.message_id,
            "chat_id": message.chat.id
        }
        
        # Предлагаем варианты
        preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
        
        await msg.edit_text(
            f"📝 <b>Полученный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id)
        )
        
        # Удаляем оригинальное сообщение
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Text error: {e}")
        await msg.edit_text("❌ Ошибка обработки текста")

@dp.callback_query(F.data.startswith("process_"))
async def process_callback(callback: types.CallbackQuery):
    await callback.answer()
    
    try:
        # Парсим callback data: process_{user_id}_{type}
        parts = callback.data.split("_")
        if len(parts) < 3:
            return
        
        target_user_id = int(parts[1])
        process_type = parts[2]
        
        # Проверяем права
        if callback.from_user.id != target_user_id:
            await callback.message.answer("⚠️ Это не ваш запрос!")
            return
        
        # Получаем контекст
        if target_user_id not in user_context:
            await callback.message.edit_text("❌ Время обработки истекло. Отправьте текст заново.")
            return
        
        ctx = user_context[target_user_id]
        original_text = ctx["original"]
        
        # Обновляем сообщение
        processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({process_type})...")
        
        # Обрабатываем в зависимости от типа
        if process_type == "basic":
            result = await correct_text_basic(original_text)
            result_type = "basic"
        elif process_type == "premium":
            result = await correct_text_premium(original_text)
            result_type = "premium"
        elif process_type == "summary":
            result = await summarize_text(original_text)
            result_type = "summary"
        else:
            result = "Неизвестный тип обработки"
            result_type = "error"
        
        # Сохраняем результат в контекст
        user_context[target_user_id]["processed"] = result
        user_context[target_user_id]["result_type"] = result_type
        
        # Отправляем результат
        if len(result) > 4000:
            # Если текст длинный, разбиваем
            await processing_msg.delete()
            
            # Первая часть
            await callback.message.answer(
                f"✅ <b>Результат ({process_type}):</b>\n\n{result[:4000]}",
                parse_mode="HTML"
            )
            
            # Остальные части
            for i in range(4000, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            
            # Добавляем кнопки экспорта к последнему сообщению
            last_msg = await callback.message.answer(
                "💾 <b>Экспортировать текст?</b>",
                parse_mode="HTML",
                reply_markup=create_export_keyboard(target_user_id, result_type)
            )
            
        else:
            # Если текст короткий
            await processing_msg.edit_text(
                f"✅ <b>Результат ({process_type}):</b>\n\n{result}",
                parse_mode="HTML",
                reply_markup=create_export_keyboard(target_user_id, result_type)
            )
            
    except Exception as e:
        logger.error(f"Process error: {e}")
        await callback.message.edit_text("❌ Ошибка обработки")

@dp.callback_query(F.data.startswith("export_"))
async def export_callback(callback: types.CallbackQuery):
    await callback.answer()
    
    try:
        # Парсим: export_{user_id}_{type}_{format}
        parts = callback.data.split("_")
        if len(parts) < 4:
            return
        
        target_user_id = int(parts[1])
        text_type = parts[2]
        export_format = parts[3]
        
        # Проверяем права
        if callback.from_user.id != target_user_id:
            return
        
        # Получаем контекст
        if target_user_id not in user_context or "processed" not in user_context[target_user_id]:
            await callback.message.answer("❌ Текст не найден. Обработайте текст заново.")
            return
        
        text = user_context[target_user_id]["processed"]
        
        # Создаем файл
        await callback.message.edit_text("📁 Создаю файл...")
        filepath = await save_to_file(target_user_id, text, export_format)
        
        if not filepath:
            await callback.message.edit_text("❌ Ошибка создания файла")
            return
        
        # Отправляем файл
        filename = os.path.basename(filepath)
        
        if export_format == "pdf":
            caption = "📊 PDF файл с текстом"
            mime_type = "application/pdf"
        else:
            caption = "📄 Текстовый файл"
            mime_type = "text/plain"
        
        document = FSInputFile(filepath, filename=filename)
        await callback.message.answer_document(
            document=document,
            caption=caption
        )
        
        # Удаляем временный файл
        try:
            os.remove(filepath)
        except:
            pass
        
        # Восстанавливаем предыдущее сообщение
        await callback.message.delete()
        
    except Exception as e:
        logger.error(f"Export error: {e}")
        await callback.message.edit_text("❌ Ошибка создания файла")

# --- ЗАПУСК ---
async def main():
    logger.info("Bot starting process...")
    
    # Запускаем веб-сервер
    asyncio.create_task(start_web_server())
    
    # Запускаем бота
    logger.info("🚀 Starting polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")