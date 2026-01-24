# bot.py
import asyncio
import sys
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardRemove

from keyboards import KeyboardFactory
from utils import HealthServer
import handlers


async def main():
    """Основная функция запуска бота"""
    logger.info("🚀 Starting bot...")
    
    # Проверка токена
    if not Config.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not found! Exiting.")
        sys.exit(1)
    
    # Инициализация бота и диспетчера
    bot = Bot(token=Config.BOT_TOKEN)
    dp = Dispatcher()
    
    # Регистрация хэндлеров
    dp.message.register(handlers.start_handler, Command("start"))
    dp.message.register(lambda msg: handlers.text_handler(msg), F.text)
    dp.message.register(lambda msg: handlers.voice_handler(msg, bot), F.voice | F.audio)
    
    dp.callback_query.register(
        lambda cb: handlers.process_callback(cb, bot), 
        F.data.startswith("process_")
    )
    dp.callback_query.register(
        lambda cb: handlers.switch_callback(cb, bot), 
        F.data.startswith("switch_")
    )
    dp.callback_query.register(
        lambda cb: handlers.export_callback(cb, bot), 
        F.data.startswith("export_")
    )
    
    # Инициализация Groq клиентов
    GroqService.init()
    
    # Запуск веб-сервера для Uptime Robot (в фоне)
    asyncio.create_task(HealthServer.start())
    
    # Запуск бота
    logger.info("✅ Bot initialized. Starting polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}", exc_info=True)
        sys.exit(1)