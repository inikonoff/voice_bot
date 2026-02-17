# bot.py
"""
Production Bot v6.1
+ Кнопка выхода из режима вопросов
+ Стриминг ответов
+ Исправлены ошибки авторизации и обработки истории
"""

import os
import sys
import logging
import asyncio
from typing import Dict, Any
from datetime import datetime
from dotenv import load_dotenv
from openai import AsyncOpenAI

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import processors

# Загружаем переменные окружения
load_dotenv()

# Получаем токен с проверкой
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,  # Временно DEBUG для отладки
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# Проверка наличия токена
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в переменных окружения!")
    logger.error("Проверьте файл .env и наличие переменной BOT_TOKEN")
    exit(1)

# Проверка формата токена
if ":" not in BOT_TOKEN:
    logger.error("❌ Неверный формат токена! Должен быть в формате: 123456:ABCdef")
    exit(1)

logger.info(f"✅ Токен загружен: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}")
logger.info(f"✅ Groq ключей: {len(GROQ_API_KEYS.split(',')) if GROQ_API_KEYS else 0}")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==========================
# STORAGE
# ==========================

user_context: Dict[int, Dict[int, Any]] = {}
active_dialogs: Dict[int, int] = {}
groq_clients = []


# ==========================
# GROQ INIT
# ==========================

def init_groq_clients():
    """Инициализация клиентов Groq API"""
    global groq_clients
    groq_clients = []
    
    if not GROQ_API_KEYS:
        logger.warning("⚠️ GROQ_API_KEYS не найдены")
        return
    
    for key in GROQ_API_KEYS.split(","):
        key = key.strip()
        if not key:
            continue
        try:
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=config.GROQ_TIMEOUT,
            )
            groq_clients.append(client)
            logger.info(f"✅ Groq клиент добавлен: {key[:10]}...")
        except Exception as e:
            logger.error(f"❌ Ошибка создания Groq клиента: {e}")
    
    logger.info(f"✅ Всего Groq клиентов: {len(groq_clients)}")


# ==========================
# KEYBOARDS
# ==========================

def create_dialog_keyboard(user_id: int):
    """Создание клавиатуры для режима диалога"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🚪 Выйти из режима вопросов",
            callback_data=f"dialog_exit_{user_id}"
        )
    )
    return builder.as_markup()


# ==========================
# TEXT HANDLER
# ==========================

@dp.message(F.text)
async def text_handler(message: types.Message):
    """Обработка текстовых сообщений"""
    user_id = message.from_user.id
    text = message.text.strip()
    
    logger.debug(f"Text message from user {user_id}: {text[:50]}...")

    if text.startswith("/"):
        return

    # === ЕСЛИ АКТИВЕН ДИАЛОГ → ВОПРОС ===
    if user_id in active_dialogs:
        doc_msg_id = active_dialogs[user_id]
        await handle_streaming_answer(message, user_id, doc_msg_id, text)
        return

    msg = await message.answer("📝 Анализирую текст...")

    available_modes = processors.get_available_modes(text)

    if user_id not in user_context:
        user_context[user_id] = {}

    user_context[user_id][msg.message_id] = {
        "original": text,
        "available_modes": available_modes,
        "time": datetime.now(),
    }

    await msg.edit_text(
        "Текст получен.\n\nНажмите 'Задать вопрос' для перехода в режим диалога.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Задать вопрос",
                        callback_data=f"dialog_start_{user_id}_{msg.message_id}"
                    )
                ]
            ]
        )
    )


# ==========================
# DIALOG START
# ==========================

@dp.callback_query(F.data.startswith("dialog_start_"))
async def dialog_start_callback(callback: types.CallbackQuery):
    """Начало диалога по документу"""
    await callback.answer()

    parts = callback.data.split("_")
    user_id = int(parts[2])
    msg_id = int(parts[3])

    if callback.from_user.id != user_id:
        return

    # Проверяем наличие контекста
    if user_id not in user_context or msg_id not in user_context[user_id]:
        await callback.message.edit_text("❌ Документ не найден. Попробуйте заново.")
        return

    processors.save_document_for_dialog(
        user_id,
        msg_id,
        user_context[user_id][msg_id]["original"]
    )

    active_dialogs[user_id] = msg_id

    await callback.message.edit_text(
        "💬 Режим вопросов активирован.\n\n"
        "Напишите ваш вопрос.",
        reply_markup=create_dialog_keyboard(user_id)
    )


# ==========================
# EXIT BUTTON
# ==========================

@dp.callback_query(F.data.startswith("dialog_exit_"))
async def dialog_exit_callback(callback: types.CallbackQuery):
    """Выход из режима диалога"""
    await callback.answer()

    parts = callback.data.split("_")
    user_id = int(parts[2])

    if user_id in active_dialogs:
        del active_dialogs[user_id]

    await callback.message.edit_text("✅ Вы вышли из режима вопросов.")


# ==========================
# STREAMING ANSWER
# ==========================

async def handle_streaming_answer(message, user_id, msg_id, question):
    """Обработка стримингового ответа на вопрос"""
    placeholder = await message.answer("💭 Думаю...")
    
    accumulated = ""
    last_edit_length = 0
    
    try:
        # Проверяем наличие Groq клиентов
        if not groq_clients:
            await placeholder.edit_text("❌ Ошибка: нет доступных Groq клиентов")
            return
        
        # Проверяем наличие документа
        if user_id not in processors.document_dialogues or msg_id not in processors.document_dialogues.get(user_id, {}):
            await placeholder.edit_text("❌ Документ не найден. Начните заново.")
            return
        
        # Получаем стриминг ответа
        async for chunk in processors.stream_document_answer(
            user_id,
            msg_id,
            question,
            groq_clients
        ):
            if chunk:
                accumulated += chunk
                
                # Обновляем сообщение при накоплении
                if len(accumulated) - last_edit_length > 30:
                    try:
                        display_text = accumulated + "▌"
                        if len(display_text) > 4096:
                            display_text = display_text[:4093] + "..."
                        
                        await placeholder.edit_text(
                            display_text,
                            reply_markup=create_dialog_keyboard(user_id)
                        )
                    except Exception as edit_error:
                        logger.error(f"Ошибка редактирования: {edit_error}")
                    last_edit_length = len(accumulated)
        
        # Финальный текст
        final_text = accumulated if accumulated else "❌ Пустой ответ"
        if len(final_text) > 4096:
            final_text = final_text[:4093] + "..."
        
        await placeholder.edit_text(
            final_text,
            reply_markup=create_dialog_keyboard(user_id)
        )
        
    except Exception as e:
        logger.error(f"Ошибка стриминга: {e}", exc_info=True)
        try:
            await placeholder.edit_text(f"❌ Ошибка: {str(e)[:200]}")
        except:
            pass


# ==========================
# COMMAND HANDLERS
# ==========================

@dp.message(F.text == "/start")
async def start_command(message: types.Message):
    """Обработка команды /start"""
    await message.answer(config.START_MESSAGE, parse_mode="HTML")


@dp.message(F.text == "/help")
async def help_command(message: types.Message):
    """Обработка команды /help"""
    await message.answer(config.HELP_MESSAGE, parse_mode="HTML")


@dp.message(F.text == "/status")
async def status_command(message: types.Message):
    """Обработка команды /status"""
    status_text = config.STATUS_MESSAGE.format(
        groq_count=len(groq_clients),
        users_count=len(user_context),
        vision_status="✅" if groq_clients else "❌",
        docx_status="✅" if processors.DOCX_AVAILABLE else "❌",
        temp_files=0
    )
    await message.answer(status_text, parse_mode="HTML")


# ==========================
# FILE HANDLERS
# ==========================

@dp.message(F.document | F.photo | F.video | F.voice | F.audio)
async def file_handler(message: types.Message):
    """Обработка файлов"""
    user_id = message.from_user.id
    
    # Определяем тип файла
    file_id = None
    file_name = "file"
    
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "document.bin"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = "photo.jpg"
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video.mp4"
    elif message.voice:
        file_id = message.voice.file_id
        file_name = "voice.ogg"
    elif message.audio:
        file_id = message.audio.file_id
        file_name = message.audio.file_name or "audio.mp3"
    
    if not file_id:
        await message.answer("❌ Не удалось определить тип файла")
        return
    
    # Отправляем статус
    status_msg = await message.answer("📥 Скачиваю файл...")
    
    try:
        # Скачиваем файл
        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        file_bytes = file_bytes.getvalue()
        
        await status_msg.edit_text("🔄 Обрабатываю файл...")
        
        # Извлекаем текст
        text = await processors.extract_text_from_file(file_bytes, file_name, groq_clients)
        
        if not text or text.startswith("❌"):
            await status_msg.edit_text(text)
            return
        
        # Сохраняем в контекст
        available_modes = processors.get_available_modes(text)
        
        if user_id not in user_context:
            user_context[user_id] = {}
        
        user_context[user_id][status_msg.message_id] = {
            "original": text,
            "available_modes": available_modes,
            "time": datetime.now(),
            "filename": file_name
        }
        
        # Показываем результат
        preview = text[:config.PREVIEW_LENGTH] + ("..." if len(text) > config.PREVIEW_LENGTH else "")
        
        await status_msg.edit_text(
            f"📄 Текст из файла:\n\n{preview}\n\n"
            f"Всего символов: {len(text)}\n\n"
            f"Нажмите 'Задать вопрос' для перехода в режим диалога.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💬 Задать вопрос",
                            callback_data=f"dialog_start_{user_id}_{status_msg.message_id}"
                        )
                    ]
                ]
            )
        )
        
    except Exception as e:
        logger.error(f"Ошибка обработки файла: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")


# ==========================
# MAIN
# ==========================

async def main():
    """Главная функция запуска бота"""
    logger.info("🚀 Запуск бота...")
    
    # Инициализация Groq клиентов
    init_groq_clients()
    if not groq_clients:
        logger.warning("⚠️ Бот работает без Groq клиентов! Некоторые функции будут недоступны.")
    
    # Инициализация Vision процессора
    processors.vision_processor.init_clients(groq_clients)
    
    # Проверка и удаление вебхука
    try:
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Текущий вебхук: {webhook_info.url}")
        
        if webhook_info.url:
            logger.info("Удаление вебхука...")
            await bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(1)
            logger.info("✅ Вебхук удален")
    except Exception as e:
        logger.error(f"Ошибка при работе с вебхуком: {e}")
    
    # Запуск поллинга
    logger.info("✅ Бот запущен и готов к работе")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}", exc_info=True)
