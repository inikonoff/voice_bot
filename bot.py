# bot.py
"""
Главный файл бота: Версия 5.0
Реализовано: Статусы обработки, Новая языковая политика, Диалоги
"""

import os
import logging
import asyncio
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import AsyncOpenAI

import config
import processors

load_dotenv()

# Инициализация клиентов
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_KEYS = os.environ.get("GROQ_API_KEYS", "").split(",")
groq_clients = [
    AsyncOpenAI(api_key=k.strip(), base_url="https://api.groq.com/openai/v1") 
    for k in GROQ_KEYS if k.strip()
]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Временное хранилище текстов (в продакшене лучше Redis)
user_storage = {}

# --- УТИЛИТЫ ---

def get_mode_keyboard(modes: list):
    builder = InlineKeyboardBuilder()
    if "basic" in modes:
        builder.row(types.InlineKeyboardButton(text="📝 BASIC (Как есть)", callback_data="run_basic"))
    if "premium" in modes:
        builder.row(types.InlineKeyboardButton(text="💎 PREMIUM (Стиль)", callback_data="run_premium"))
    if "summary" in modes:
        builder.row(types.InlineKeyboardButton(text="📊 SUMMARY (Русский)", callback_data="run_summary"))
    return builder.as_markup()

# --- ХЭНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Пришли мне текст, файл (PDF/DOCX) или голосовое, и я обработаю его по новой политике.")

@dp.message(F.text | F.document)
async def handle_input(message: types.Message):
    status_msg = await message.answer("📥 Получаю данные...")
    
    text = ""
    if message.text:
        text = message.text
    elif message.document:
        await status_msg.edit_text("📄 Читаю документ...")
        file = await bot.get_file(message.document.file_id)
        content = await bot.download_file(file.file_path)
        text = await processors.extract_text_from_file(content.read(), message.document.file_name)

    if not text or len(text) < 5:
        await status_msg.edit_text("❌ Не удалось извлечь текст или он слишком короткий.")
        return

    # Сохраняем для обработки
    user_id = message.from_user.id
    user_storage[user_id] = {"text": text}
    
    # Режим диалога (QA) активируется автоматически
    processors.document_dialogues[user_id] = {"text": text}
    
    modes = processors.get_available_modes(text)
    await status_msg.edit_text(
        f"✅ Текст принят ({len(text)} зн.). Теперь вы можете задавать вопросы по нему или выбрать режим обработки:",
        reply_markup=get_mode_keyboard(modes)
    )

@dp.callback_query(F.data.startswith("run_"))
async def process_action(callback: types.CallbackQuery):
    mode = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    if user_id not in user_storage:
        await callback.answer("Ошибка: данные не найдены.", show_alert=True)
        return

    source_text = user_storage[user_id]["text"]
    
    # Визуализация процесса
    status_map = {
        "basic": "🛠 Исправляю пунктуацию и орфографию...",
        "premium": "✨ Выполняю литературную правку...",
        "summary": "📝 Анализирую и составляю резюме на русском..."
    }
    
    edit_msg = await callback.message.answer(status_map.get(mode, "Обработка..."))
    await callback.answer()

    try:
        if mode == "basic":
            res = await processors.correct_text_basic(source_text, groq_clients)
        elif mode == "premium":
            res = await processors.correct_text_premium(source_text, groq_clients)
        else:
            res = await processors.summarize_text(source_text, groq_clients)
        
        await edit_msg.delete()
        # Отправляем результат (разбивка если текст длинный)
        if len(res) > 4096:
            for x in range(0, len(res), 4096):
                await bot.send_message(user_id, res[x:x+4096])
        else:
            await bot.send_message(user_id, res)
            
    except Exception as e:
        await edit_msg.edit_text(f"❌ Ошибка LLM: {e}")

@dp.message(F.text)
async def handle_qa(message: types.Message):
    user_id = message.from_user.id
    if user_id in processors.document_dialogues:
        doc_text = processors.document_dialogues[user_id]["text"]
        
        # Индикация "печатает"
        await bot.send_chat_action(message.chat.id, "typing")
        
        sent_msg = await message.answer("🤔 Ищу ответ в документе...")
        full_response = ""
        
        try:
            counter = 0
            async for chunk in processors.stream_document_answer(doc_text, message.text, groq_clients):
                full_response += chunk
                counter += 1
                # Обновляем сообщение раз в 15 чанков, чтобы не ловить лимиты Telegram API
                if counter % 15 == 0:
                    try: await sent_msg.edit_text(full_response + " ▌")
                    except: pass
            
            await sent_msg.edit_text(full_response)
        except Exception as e:
            await sent_msg.edit_text(f"❌ Ошибка при поиске ответа: {e}")
    else:
        await message.answer("Сначала пришлите документ или текст.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
