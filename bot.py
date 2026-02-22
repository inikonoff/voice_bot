# bot.py
"""
Главный файл бота: упрощенная версия
Поддерживает: голосовые, кружочки, текстовые файлы, вопросы по документам
"""

import os
import sys
import signal
import logging
import asyncio
import time
from typing import Dict, Any, Optional
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

import config
import processors

load_dotenv()

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")
PORT = int(os.environ.get("PORT", 10000))

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
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Флаг для graceful shutdown
shutdown_event = asyncio.Event()


# ============================================================================
# FSM СОСТОЯНИЯ
# ============================================================================

class DialogStates(StatesGroup):
    """Состояния для диалогов с документами"""
    normal = State()
    viewing_document = State()


# ============================================================================
# STARTUP/SHUTDOWN
# ============================================================================

async def on_startup(bot: Bot):
    """Действия при запуске"""
    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК БОТА (упрощенная версия)")
    logger.info("=" * 50)
    
    # Проверка подключения
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот @{me.username} запущен")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения: {e}")
        raise
    
    # Сброс вебхука
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Инициализация Groq клиентов
    try:
        await processors.groq_client_manager.initialize(GROQ_API_KEYS)
        logger.info(f"✅ Groq клиентов: {len(processors.groq_client_manager._clients)}")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Groq: {e}")
    
    # Создание временной директории
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    
    logger.info("=" * 50)
    logger.info("✅ БОТ ГОТОВ")
    logger.info("=" * 50)


async def on_shutdown(bot: Bot):
    """Действия при остановке"""
    logger.info("=" * 50)
    logger.info("👋 ОСТАНОВКА БОТА")
    logger.info("=" * 50)
    
    # Очистка временных файлов
    try:
        for filename in os.listdir(config.TEMP_DIR):
            filepath = os.path.join(config.TEMP_DIR, filename)
            if os.path.isfile(filepath):
                os.remove(filepath)
        logger.info("✅ Временные файлы удалены")
    except Exception as e:
        logger.error(f"❌ Ошибка очистки: {e}")
    
    await bot.session.close()
    logger.info("=" * 50)


dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


# ============================================================================
# ОБРАБОТКА СИГНАЛОВ
# ============================================================================

def handle_sigterm(signum, frame):
    """Обработчик SIGTERM от Render"""
    logger.info("📡 Получен сигнал SIGTERM")
    shutdown_event.set()


# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================

def get_correction_keyboard(message_id: int, current_mode: str) -> InlineKeyboardMarkup:
    """Клавиатура для выбора режима коррекции"""
    builder = InlineKeyboardBuilder()
    
    modes = [
        ("basic", "📝 Без ошибок"),
        ("premium", "✨ Литературный")
    ]
    
    for mode_key, mode_text in modes:
        text = f"✅ {mode_text}" if mode_key == current_mode else mode_text
        builder.button(text=text, callback_data=f"correct_{message_id}_{mode_key}")
    
    builder.button(text="💬 Задать вопрос", callback_data=f"ask_{message_id}")
    builder.button(text="📄 Скачать TXT", callback_data=f"export_{message_id}")
    builder.adjust(2, 1, 1)
    
    return builder.as_markup()


def get_dialog_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для режима диалога"""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Завершить диалог", callback_data=f"end_dialog_{message_id}")
    return builder.as_markup()


# ============================================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обработка /start"""
    await message.answer(config.START_MESSAGE, parse_mode="HTML")
    await state.set_state(DialogStates.normal)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Обработка /help"""
    await message.answer(config.HELP_MESSAGE, parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Обработка /status"""
    groq_count = len(processors.groq_client_manager._clients) if processors.groq_client_manager.is_initialized() else 0
    users_count = len(processors.dialogue_manager._store)
    
    await message.answer(
        f"🤖 <b>Статус:</b>\n"
        f"• Groq клиентов: {groq_count}\n"
        f"• Активных диалогов: {users_count}",
        parse_mode="HTML"
    )


# ============================================================================
# ОБРАБОТЧИК ГОЛОСОВЫХ И КРУЖОЧКОВ
# ============================================================================

@dp.message(F.voice | F.video_note, DialogStates.normal)
async def handle_voice(message: types.Message, state: FSMContext):
    """Обработка голосовых сообщений и кружочков"""
    user_id = message.from_user.id
    
    await message.answer("🎙️ Распознаю речь...", reply_markup=ReplyKeyboardRemove())
    
    try:
        # Получаем файл
        file_id = message.voice.file_id if message.voice else message.video_note.file_id
        file_info = await bot.get_file(file_id)
        
        file_path = os.path.join(config.TEMP_DIR, f"voice_{file_id}.ogg")
        await bot.download_file(file_info.file_path, file_path)
        
        # Читаем файл
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
        
        # Транскрибируем
        original_text = await processors.transcribe_audio(audio_bytes)
        
        # Удаляем временный файл
        os.remove(file_path)
        
        if original_text.startswith("❌"):
            await message.answer(original_text)
            return
        
        # Сохраняем в контекст
        processors.dialogue_manager.add_document_context(
            user_id, message.message_id, original_text
        )
        
        # Показываем результат
        preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
        
        await message.answer(
            f"📝 <b>Распознанный текст:</b>\n\n{preview}\n\n"
            f"<b>Выберите режим обработки:</b>",
            parse_mode="HTML",
            reply_markup=get_correction_keyboard(message.message_id, "basic")
        )
        
    except Exception as e:
        logger.error(f"Ошибка обработки голоса: {e}")
        await message.answer("❌ Ошибка при обработке голосового сообщения")


# ============================================================================
# ОБРАБОТЧИК ТЕКСТОВЫХ ФАЙЛОВ
# ============================================================================

@dp.message(F.document, DialogStates.normal)
async def handle_document(message: types.Message, state: FSMContext):
    """Обработка текстовых файлов (TXT, DOCX, PDF)"""
    user_id = message.from_user.id
    
    # Проверка размера
    if message.document.file_size > config.FILE_SIZE_LIMIT:
        await message.answer(config.ERROR_FILE_TOO_LARGE)
        return
    
    await message.answer("📄 Читаю файл...", reply_markup=ReplyKeyboardRemove())
    
    try:
        # Скачиваем файл
        file_info = await bot.get_file(message.document.file_id)
        filename = message.document.file_name or "document.txt"
        file_path = os.path.join(config.TEMP_DIR, filename)
        
        await bot.download_file(file_info.file_path, file_path)
        
        # Читаем файл
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        
        # Извлекаем текст
        original_text = await processors.extract_text_from_file(file_bytes, filename)
        
        # Удаляем временный файл
        os.remove(file_path)
        
        if original_text.startswith("❌"):
            await message.answer(original_text)
            return
        
        # Сохраняем в контекст
        processors.dialogue_manager.add_document_context(
            user_id, message.message_id, original_text
        )
        
        # Показываем результат
        preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
        
        await message.answer(
            f"📝 <b>Текст из файла:</b>\n\n{preview}\n\n"
            f"<b>Выберите режим обработки:</b>",
            parse_mode="HTML",
            reply_markup=get_correction_keyboard(message.message_id, "basic")
        )
        
    except Exception as e:
        logger.error(f"Ошибка обработки файла: {e}")
        await message.answer("❌ Ошибка при чтении файла")


# ============================================================================
# ОБРАБОТЧИК ТЕКСТА
# ============================================================================

@dp.message(F.text, DialogStates.normal)
async def handle_text(message: types.Message, state: FSMContext):
    """Обработка обычного текста"""
    user_id = message.from_user.id
    original_text = message.text.strip()
    
    if original_text.startswith("/"):
        return
    
    await message.answer("📝 Обрабатываю текст...", reply_markup=ReplyKeyboardRemove())
    
    # Сохраняем в контекст
    processors.dialogue_manager.add_document_context(
        user_id, message.message_id, original_text
    )
    
    # Показываем результат
    preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
    
    await message.answer(
        f"📝 <b>Ваш текст:</b>\n\n{preview}\n\n"
        f"<b>Выберите режим обработки:</b>",
        parse_mode="HTML",
        reply_markup=get_correction_keyboard(message.message_id, "basic")
    )


# ============================================================================
# ОБРАБОТЧИКИ КОРРЕКЦИИ
# ============================================================================

@dp.callback_query(F.data.startswith("correct_"))
async def callback_correct(callback: types.CallbackQuery, state: FSMContext):
    """Применение коррекции к тексту"""
    _, msg_id_str, mode = callback.data.split("_")
    msg_id = int(msg_id_str)
    user_id = callback.from_user.id
    
    await callback.answer()
    
    # Получаем контекст
    context = processors.dialogue_manager.get_document_context(user_id, msg_id)
    if not context:
        await callback.message.edit_text("❌ Текст не найден. Отправьте заново.")
        return
    
    original_text = context["text"]
    current_mode = context.get("mode", "basic")
    
    # Если тот же режим
    if mode == current_mode:
        await callback.answer(f"Текст уже в режиме {mode}")
        return
    
    await callback.message.edit_text(f"⏳ Применяю режим {mode}...")
    
    # Применяем коррекцию
    if mode == "basic":
        corrected = await processors.correct_text_basic(original_text)
    else:  # premium
        corrected = await processors.correct_text_premium(original_text)
    
    if corrected.startswith("❌"):
        await callback.message.edit_text(corrected)
        return
    
    # Обновляем контекст
    if user_id in processors.dialogue_manager._store and msg_id in processors.dialogue_manager._store[user_id]:
        processors.dialogue_manager._store[user_id][msg_id]["mode"] = mode
    
    # Отправляем результат
    await callback.message.edit_text(
        corrected,
        reply_markup=get_correction_keyboard(msg_id, mode)
    )


# ============================================================================
# ОБРАБОТЧИК ВОПРОСОВ ПО ДОКУМЕНТУ
# ============================================================================

@dp.callback_query(F.data.startswith("ask_"))
async def callback_ask(callback: types.CallbackQuery, state: FSMContext):
    """Начало диалога по документу"""
    _, msg_id_str = callback.data.split("_")
    msg_id = int(msg_id_str)
    user_id = callback.from_user.id
    
    await callback.answer()
    
    # Проверяем контекст
    context = processors.dialogue_manager.get_document_context(user_id, msg_id)
    if not context:
        await callback.message.edit_text("❌ Документ не найден.")
        return
    
    # Переключаем состояние
    await state.set_state(DialogStates.viewing_document)
    await state.update_data(doc_msg_id=msg_id)
    
    await callback.message.edit_text(
        "💬 <b>Режим вопросов</b>\n\n"
        "Теперь вы можете задавать вопросы по тексту.\n"
        "Чтобы выйти, нажмите кнопку ниже.",
        parse_mode="HTML",
        reply_markup=get_dialog_keyboard(msg_id)
    )


@dp.message(F.text, DialogStates.viewing_document)
async def handle_question(message: types.Message, state: FSMContext):
    """Обработка вопроса по документу"""
    user_id = message.from_user.id
    data = await state.get_data()
    msg_id = data.get("doc_msg_id")
    
    if not msg_id:
        await message.answer("❌ Ошибка: документ не найден")
        await state.set_state(DialogStates.normal)
        return
    
    await message.answer("💭 Думаю...", reply_markup=ReplyKeyboardRemove())
    
    # Получаем ответ
    answer = await processors.dialogue_manager.answer_document_question(
        user_id, msg_id, message.text
    )
    
    if answer.startswith("❌"):
        await message.answer(answer)
        return
    
    # Отправляем ответ
    await message.answer(
        answer,
        reply_markup=get_dialog_keyboard(msg_id)
    )


@dp.callback_query(F.data.startswith("end_dialog_"), DialogStates.viewing_document)
async def callback_end_dialog(callback: types.CallbackQuery, state: FSMContext):
    """Завершение диалога"""
    await state.set_state(DialogStates.normal)
    await callback.message.edit_text("✅ Диалог завершен. Можете загрузить новый документ.")
    await callback.answer()


# ============================================================================
# ЭКСПОРТ В TXT
# ============================================================================

@dp.callback_query(F.data.startswith("export_"))
async def callback_export(callback: types.CallbackQuery):
    """Экспорт текста в файл"""
    _, msg_id_str = callback.data.split("_")
    msg_id = int(msg_id_str)
    user_id = callback.from_user.id
    
    await callback.answer()
    
    # Получаем контекст
    context = processors.dialogue_manager.get_document_context(user_id, msg_id)
    if not context:
        await callback.message.answer("❌ Текст не найден.")
        return
    
    # Текст для экспорта
    text_to_export = callback.message.text or context["text"]
    
    # Сохраняем в файл
    file_path = os.path.join(config.TEMP_DIR, f"export_{user_id}_{msg_id}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(text_to_export)
    
    # Отправляем
    await callback.message.answer_document(
        FSInputFile(file_path),
        caption="📄 Ваш текст"
    )
    
    # Удаляем файл
    os.remove(file_path)


# ============================================================================
# ВЕБ-СЕРВЕР
# ============================================================================

async def health_check(request):
    """Проверка здоровья для Render"""
    return web.Response(text="Bot is alive!")


async def start_web_server():
    """Запуск веб-сервера"""
    app = web.Application()
    app.router.add_get("/health", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    
    await site.start()
    logger.info(f"🌐 Веб-сервер на порту {PORT}")
    
    await shutdown_event.wait()
    await runner.cleanup()


# ============================================================================
# ЗАПУСК
# ============================================================================

async def main():
    """Главная функция"""
    # Обработка сигналов
    signal.signal(signal.SIGTERM, handle_sigterm)
    
    # Запуск веб-сервера (он запустит бота внутри)
    await start_web_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
