# bot.py
import os
import io
import logging
import asyncio
import sys
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web
from openai import AsyncOpenAI
import random

from aiogram import Bot, Dispatcher, types, F, html
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")

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

# Хранилище контекста
user_context = {}

# --- ИНИЦИАЛИЗАЦИЯ GROQ КЛИЕНТОВ ---
groq_clients = []
current_client_index = 0

def init_groq_clients():
    """Инициализация клиентов Groq"""
    global groq_clients
    
    if not GROQ_API_KEYS:
        logger.warning("GROQ_API_KEYS не настроены!")
        return
    
    keys = [key.strip() for key in GROQ_API_KEYS.split(",") if key.strip()]
    
    for key in keys:
        try:
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=30.0,
            )
            groq_clients.append(client)
            logger.info(f"✅ Groq client: {key[:8]}...")
        except Exception as e:
            logger.error(f"❌ Error client {key[:8]}: {e}")
    
    logger.info(f"✅ Total clients: {len(groq_clients)}")

def get_client():
    """Получаем следующего клиента по кругу"""
    if not groq_clients:
        return None
    
    global current_client_index
    client = groq_clients[current_client_index]
    current_client_index = (current_client_index + 1) % len(groq_clients)
    return client

async def make_groq_request(func, *args, **kwargs):
    """Делаем запрос с перебором ключей"""
    if not groq_clients:
        raise Exception("No Groq clients available")
    
    errors = []
    
    for _ in range(len(groq_clients) * 2):  # Пробуем каждый ключ 2 раза
        client = get_client()
        if not client:
            break
        
        try:
            return await func(client, *args, **kwargs)
        except Exception as e:
            errors.append(str(e))
            logger.warning(f"Request error: {e}")
            await asyncio.sleep(0.5 + random.random())
    
    raise Exception(f"All clients failed: {'; '.join(errors[:3])}")

# --- GROQ СЕРВИСЫ ---
async def transcribe_voice(audio_bytes: bytes) -> str:
    """Транскрибация голоса через Whisper v3"""
    async def transcribe(client):
        return await client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=("audio.ogg", audio_bytes, "audio/ogg"),
            language="ru",
            response_format="text",
        )
    
    try:
        return await make_groq_request(transcribe)
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return f"❌ Ошибка распознавания: {str(e)[:100]}"

async def correct_text_basic(text: str) -> str:
    """Базовая коррекция: ошибки и пунктуация"""
    if not text.strip():
        return "❌ Пустой текст"
    
    prompt = """Исправь орфографические и пунктуационные ошибки в тексте. 
    Сохрани оригинальный смысл и стиль. Только исправленный текст, без комментариев.
    
    Текст для исправления:"""
    
    async def correct(client):
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты редактор русского языка. Только исправляешь ошибки."},
                {"role": "user", "content": f"{prompt}\n\n{text}"}
            ],
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await make_groq_request(correct)
    except Exception as e:
        logger.error(f"Basic correction error: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"

async def correct_text_premium(text: str) -> str:
    """Премиум коррекция: стиль, паразиты, мат"""
    if not text.strip():
        return "❌ Пустой текст"
    
    prompt = """Отредактируй текст профессионально:
    1. Исправь все ошибки (орфография, пунктуация, грамматика)
    2. Удали слова-паразиты (ну, типа, короче, как бы, блин и т.д.)
    3. Замени матерные и грубые слова на литературные аналоги
    4. Улучши стиль, сделай текст более гладким и читаемым
    5. Разбей на логические абзацы если нужно
    6. Сохрани оригинальный смысл и тон
    
    Верни только отредактированный текст, без пояснений.
    
    Текст для редактирования:"""
    
    async def correct(client):
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты профессиональный редактор и стилист."},
                {"role": "user", "content": f"{prompt}\n\n{text}"}
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await make_groq_request(correct)
    except Exception as e:
        logger.error(f"Premium correction error: {e}")
        return f"❌ Ошибка коррекции: {str(e)[:100]}"

async def summarize_text(text: str) -> str:
    """Создание саммари"""
    if not text.strip():
        return "❌ Пустой текст"
    
    # Проверяем длину
    words = text.split()
    if len(words) < 50:
        return "📝 Текст слишком короткий для саммари. Используйте обычную коррекцию."
    
    prompt = """Сделай краткое содержательное саммари текста:
    1. Выдели основную мысль и ключевые моменты
    2. Дай только суть, без деталей и примеров
    3. Объем: примерно 10-20% от оригинала
    4. Сохрани важные факты и выводы
    5. Только саммари, без вступлений
    
    Текст для саммаризации:"""
    
    async def summarize(client):
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты создаешь краткие содержательные саммари."},
                {"role": "user", "content": f"{prompt}\n\n{text}"}
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await make_groq_request(summarize)
    except Exception as e:
        logger.error(f"Summarization error: {e}")
        return f"❌ Ошибка создания саммари: {str(e)[:100]}"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def create_options_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создаем клавиатуру с вариантами обработки"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="📝 Как есть", callback_data=f"process_{user_id}_basic"),
        InlineKeyboardButton(text="✨ Красиво", callback_data=f"process_{user_id}_premium"),
    )
    
    builder.row(
        InlineKeyboardButton(text="📊 Саммари", callback_data=f"process_{user_id}_summary"),
    )
    
    return builder.as_markup()

def create_after_basic_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создаем клавиатуру для текста после базовой обработки"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="✨ Обработать красиво", callback_data=f"convert_{user_id}_basic_to_premium"),
    )
    
    builder.row(
        InlineKeyboardButton(text="📊 Сделать саммари", callback_data=f"convert_{user_id}_basic_to_summary"),
    )
    
    builder.row(
        InlineKeyboardButton(text="💾 Экспортировать", callback_data=f"export_{user_id}_basic_txt"),
    )
    
    return builder.as_markup()

def create_export_keyboard(user_id: int, text_type: str) -> InlineKeyboardMarkup:
    """Создаем клавиатуру для экспорта"""
    builder = InlineKeyboardBuilder()
    
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
        try:
            # Простой PDF без reportlab
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            import textwrap
            
            filepath = f"/tmp/{filename}.pdf"
            c = canvas.Canvas(filepath, pagesize=A4)
            width, height = A4
            
            margin = 50
            line_height = 14
            y = height - margin
            
            # Заголовок
            c.setFont("Helvetica-Bold", 14)
            c.drawString(margin, y, "Обработанный текст")
            y -= 30
            
            # Дата
            c.setFont("Helvetica", 10)
            c.drawString(margin, y, f"Создано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            y -= 40
            
            # Текст
            c.setFont("Helvetica", 11)
            lines = textwrap.wrap(text, width=90)
            
            for line in lines:
                if y < margin:
                    c.showPage()
                    y = height - margin
                    c.setFont("Helvetica", 11)
                c.drawString(margin, y, line)
                y -= line_height
            
            c.save()
            return filepath
            
        except ImportError:
            # Fallback на txt
            logger.warning("Reportlab not installed, using txt fallback")
            filepath = f"/tmp/{filename}.txt"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
    
    return None

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER/UPTIME ROBOT ---
async def health_check(request):
    """Uptime Robot и Render пингуют этот адрес, чтобы проверить, жив ли бот"""
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    """Запуск фонового веб-сервера для Uptime Robot"""
    try:
        app = web.Application()
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check) # Два пути для надежности
        app.router.add_get('/ping', health_check)   # Еще один путь для Uptime Robot
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Render передает порт через переменную PORT, локально используем 8080
        port = int(os.environ.get("PORT", 8080))
        
        # 0.0.0.0 - слушаем все интерфейсы
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"✅ WEB SERVER STARTED ON PORT {port}")
    except Exception as e:
        logger.error(f"❌ Error starting web server: {e}")

# --- ХЭНДЛЕРЫ БОТА ---
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
        
        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return
        
        # Сохраняем контекст
        user_context[user_id] = {
            "type": "voice",
            "original": original_text,
            "message_id": msg.message_id,
            "chat_id": message.chat.id
        }
        
        # Предлагаем варианты
        preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n"
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
        
        # Отправляем результат (БЕЗ заголовка "Результат...")
        if len(result) > 4000:
            # Если текст длинный, разбиваем
            await processing_msg.delete()
            
            # Первая часть
            await callback.message.answer(result[:4000])
            
            # Остальные части
            for i in range(4000, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            
            # Для базовой обработки добавляем кнопки дальнейших действий
            if result_type == "basic":
                await callback.message.answer(
                    "📝 <b>Текст исправлен. Что дальше?</b>",
                    parse_mode="HTML",
                    reply_markup=create_after_basic_keyboard(target_user_id)
                )
            else:
                # Для других типов добавляем кнопки экспорта к последнему сообщению
                await callback.message.answer(
                    "💾 <b>Экспортировать текст?</b>",
                    parse_mode="HTML",
                    reply_markup=create_export_keyboard(target_user_id, result_type)
                )
            
        else:
            # Если текст короткий
            if result_type == "basic":
                # Для базовой обработки сразу добавляем кнопки дальнейших действий
                await processing_msg.edit_text(
                    result,
                    reply_markup=create_after_basic_keyboard(target_user_id)
                )
            else:
                # Для других типов добавляем кнопки экспорта
                await processing_msg.edit_text(
                    result,
                    reply_markup=create_export_keyboard(target_user_id, result_type)
                )
            
    except Exception as e:
        logger.error(f"Process error: {e}")
        await callback.message.edit_text("❌ Ошибка обработки")

@dp.callback_query(F.data.startswith("convert_"))
async def convert_callback(callback: types.CallbackQuery):
    """Обработка конвертации из базовой обработки в другие форматы"""
    await callback.answer()
    
    try:
        # Парсим callback data: convert_{user_id}_{from}_to_{to}
        parts = callback.data.split("_")
        if len(parts) < 5:
            return
        
        target_user_id = int(parts[1])
        from_type = parts[2]
        to_type = parts[4]
        
        # Проверяем права
        if callback.from_user.id != target_user_id:
            return
        
        # Получаем контекст
        if target_user_id not in user_context or "processed" not in user_context[target_user_id]:
            await callback.message.answer("❌ Текст не найден. Обработайте текст заново.")
            return
        
        # Получаем уже обработанный текст
        current_text = user_context[target_user_id]["processed"]
        
        # Обновляем сообщение
        processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({to_type})...")
        
        # Обрабатываем в зависимости от типа
        if to_type == "premium":
            result = await correct_text_premium(current_text)
            result_type = "premium"
        elif to_type == "summary":
            result = await summarize_text(current_text)
            result_type = "summary"
        else:
            result = "Неизвестный тип обработки"
            result_type = "error"
        
        # Сохраняем результат в контекст
        user_context[target_user_id]["processed"] = result
        user_context[target_user_id]["result_type"] = result_type
        
        # Отправляем результат (БЕЗ заголовка)
        if len(result) > 4000:
            # Если текст длинный, разбиваем
            await processing_msg.delete()
            
            # Первая часть
            await callback.message.answer(result[:4000])
            
            # Остальные части
            for i in range(4000, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            
            # Добавляем кнопки экспорта к последнему сообщению
            await callback.message.answer(
                "💾 <b>Экспортировать текст?</b>",
                parse_mode="HTML",
                reply_markup=create_export_keyboard(target_user_id, result_type)
            )
            
        else:
            # Если текст короткий
            await processing_msg.edit_text(
                result,
                reply_markup=create_export_keyboard(target_user_id, result_type)
            )
            
    except Exception as e:
        logger.error(f"Convert error: {e}")
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
        await callback.message.answer_document(document=document, caption=caption)
        
        # Восстанавливаем предыдущее сообщение с текстом
        if len(text) <= 4000:
            await callback.message.delete()
            await callback.message.answer(
                text,
                reply_markup=create_export_keyboard(target_user_id, text_type)
            )
        else:
            # Для длинных текстов просто удаляем сообщение "Создаю файл"
            await callback.message.delete()
        
        # Удаляем временный файл
        try:
            os.remove(filepath)
        except:
            pass
        
    except Exception as e:
        logger.error(f"Export error: {e}")
        await callback.message.edit_text("❌ Ошибка создания файла")

# --- ЗАПУСК ---
async def main():
    logger.info("Bot starting process...")
    
    # Инициализируем Groq клиенты
    init_groq_clients()
    
    # Запускаем веб-сервер ДЛЯ UPTIME ROBOT (в фоне)
    # Важно: через create_task, чтобы не блокировать основной поток
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