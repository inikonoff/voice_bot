# bot.py
import os
import io
import logging
import asyncio
import sys
from dotenv import load_dotenv
from aiohttp import web  # ВАЖНО для Render

from aiogram import Bot, Dispatcher, types, F, html
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardRemove

# Импортируем функции (убедитесь, что google_services.py лежит рядом)
from google_services import transcribe_voice_google, correct_text_with_gemini, explain_correction_gemini

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Логирование в stdout (чтобы Render видел логи сразу)
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

# История контекста
user_last_context = {}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_forward_label(message: types.Message) -> str:
    if not message.forward_origin:
        return ""
    origin = message.forward_origin
    label = ""
    if origin.type == "user":
        name = origin.sender_user.full_name
        label = f"↩️ от {html.bold(name)}:"
    elif origin.type == "hidden_user":
        name = origin.sender_user_name
        label = f"↩️ от {html.bold(name)}:"
    elif origin.type in ("chat", "channel"):
        title = origin.chat.title if origin.chat and origin.chat.title else "Чата"
        label = f"↩️ из {html.bold(title)}:"
    else:
        label = "↩️ Пересланное сообщение:"
    return label + "\n\n"

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (БЕССМЕРТИЕ) ---
async def health_check(request):
    """Render пингует этот адрес, чтобы проверить, жив ли бот"""
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    """Запуск фонового веб-сервера"""
    try:
        app = web.Application()
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check) # На всякий случай добавим и этот путь
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Render ОБЯЗАТЕЛЬНО передает порт через переменную PORT
        port = int(os.environ.get("PORT", 8080))
        
        # Важно: 0.0.0.0, чтобы слушать внешний мир
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"✅ WEB SERVER STARTED ON PORT {port}")
    except Exception as e:
        logger.error(f"❌ Error starting web server: {e}")

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "Здравствуйте.\n"
        "Превращаю устную речь, черновики и 'поток сознания' из чужих голосовых сообщений в грамотный текст.\n\n"
        "<b>Как это работает:</b>\n"
        "1. Отправьте или перешлите мне голосовое сообщение или текст.\n"
        "2. Получите откорректированную версию.\n\n"
        "Чтобы получить комментарий к исправлениям, используйте команду: <b>Объясни, почему</b>", 
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )

@dp.message(F.voice)
async def voice_handler(message: types.Message):
    user_id = message.from_user.id
    processing_msg = await message.answer("🎧 Слушаю...")

    try:
        header = get_forward_label(message)
        file_info = await bot.get_file(message.voice.file_id)
        voice_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, voice_buffer)
        
        raw_text = await transcribe_voice_google(voice_buffer.getvalue())

        if raw_text.startswith("Ошибка") or raw_text.startswith("Не удалось"):
            await processing_msg.edit_text(raw_text)
            return

        await processing_msg.edit_text("✍️ Исправляю...")
        corrected_text = await correct_text_with_gemini(raw_text)
        
        user_last_context[user_id] = {'raw': raw_text, 'corrected': corrected_text}
        final_text = header + corrected_text

        # 1. Удаляем сообщение "Слушаю/Исправляю"
        await processing_msg.delete()
        
        # 2. Разбивка длинных сообщений и отправка
        if len(final_text) > 4096:
            for x in range(0, len(final_text), 4096):
                await message.answer(final_text[x:x+4096], parse_mode="HTML")
        else:
            await message.answer(final_text, parse_mode="HTML")
        
        # 3. АВТОУДАЛЕНИЕ ИСХОДНИКА
        try:
            await message.delete()
        except Exception:
            pass # Если нет прав или уже удалено

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await processing_msg.edit_text("❌ Ошибка обработки.")

@dp.message(F.text)
async def text_handler(message: types.Message):
    text = message.text.strip()
    if text.startswith("/"): return
    user_id = message.from_user.id

    question_triggers = ["почему", "объясни", "зачем", "why"]
    if any(text.lower().startswith(t) for t in question_triggers) and user_id in user_last_context:
        ctx = user_last_context[user_id]
        wait_msg = await message.answer("🤔 Думаю...")
        explanation = await explain_correction_gemini(ctx['raw'], ctx['corrected'], text)
        await wait_msg.delete()
        await message.answer(explanation, parse_mode="HTML")
        return

    processing_msg = await message.answer("✍️ Редактирую...")
    try:
        header = get_forward_label(message)
        corrected_text = await correct_text_with_gemini(text)
        user_last_context[user_id] = {'raw': text, 'corrected': corrected_text}
        final_text = header + corrected_text
        
        # 1. Удаляем "Редактирую..."
        await processing_msg.delete()
        
        # 2. Отправляем результат
        await message.answer(final_text, parse_mode="HTML")

        # 3. АВТОУДАЛЕНИЕ ИСХОДНИКА
        try:
            await message.delete()
        except Exception:
            pass # Если нет прав или уже удалено

    except Exception as e:
        logger.error(f"Text error: {e}")
        await processing_msg.edit_text("❌ Ошибка.")

# --- ЗАПУСК ---
async def main():
    logger.info("Bot starting process...")

    # 1. Запускаем веб-сервер В ФОНЕ (через create_task), чтобы он не блокировал бота
    # И бот не блокировал сервер. Это критично для Render.
    asyncio.create_task(start_web_server())
    
    # 2. Удаляем вебхук и запускаем поллинг
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
