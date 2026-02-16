# bot.py
"""
Production Bot v6
+ Кнопка выхода из режима вопросов
+ Стриминг ответов
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

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found")
    exit(1)

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
    for key in GROQ_API_KEYS.split(","):
        key = key.strip()
        if not key:
            continue
        groq_clients.append(
            AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=config.GROQ_TIMEOUT,
            )
        )


# ==========================
# KEYBOARDS
# ==========================

def create_dialog_keyboard(user_id: int):
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
    user_id = message.from_user.id
    text = message.text.strip()

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
    await callback.answer()

    parts = callback.data.split("_")
    user_id = int(parts[2])
    msg_id = int(parts[3])

    if callback.from_user.id != user_id:
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
    await callback.answer()

    parts = callback.data.split("_")
    user_id = int(parts[2])

    if user_id in active_dialogs:
        del active_dialogs[user_id]

    await callback.message.edit_text("Вы вышли из режима вопросов.")


# ==========================
# STREAMING ANSWER
# ==========================

async def handle_streaming_answer(message, user_id, msg_id, question):
    placeholder = await message.answer("💭 Думаю...")

    accumulated = ""
    last_edit_length = 0

    try:
        async for chunk in processors.stream_document_answer(
            user_id,
            msg_id,
            question,
            groq_clients
        ):
            accumulated += chunk

            # Обновляем не чаще чем при приросте 30 символов
            if len(accumulated) - last_edit_length > 30:
                try:
                    await placeholder.edit_text(
                        accumulated + "▌",
                        reply_markup=create_dialog_keyboard(user_id)
                    )
                except:
                    pass
                last_edit_length = len(accumulated)

        # Финальный текст
        await placeholder.edit_text(
            accumulated,
            reply_markup=create_dialog_keyboard(user_id)
        )

    except Exception as e:
        await placeholder.edit_text(f"Ошибка: {str(e)}")


# ==========================
# MAIN
# ==========================

async def main():
    init_groq_clients()
    processors.vision_processor.init_clients(groq_clients)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())