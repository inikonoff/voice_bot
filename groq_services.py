# groq_services.py
import os
import logging
import asyncio
import random

logger = logging.getLogger(__name__)

# --- ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ GROQ ---
def init_groq_clients():
    """Инициализация клиентов Groq с несколькими ключами"""
    clients = []
    
    # Пробуем разные форматы ключей
    keys_str = os.environ.get("GROQ_API_KEYS", "") or os.environ.get("GROQ_API_KEY", "")
    
    if not keys_str:
        logger.error("GROQ_API_KEYS не найден!")
        return clients
    
    # Разделяем ключи
    from openai import AsyncOpenAI
    api_keys = [key.strip() for key in keys_str.split(",") if key.strip()]
    
    for key in api_keys:
        try:
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=30.0,
            )
            clients.append(client)
            logger.info(f"✅ Groq client: {key[:8]}...")
        except Exception as e:
            logger.error(f"❌ Error client {key[:8]}: {e}")
    
    logger.info(f"✅ Total clients: {len(clients)}")
    return clients

# Глобальные клиенты
groq_clients = init_groq_clients()
current_client_index = 0

def get_client():
    """Получаем следующего клиента по кругу"""
    if not groq_clients:
        return None
    
    global current_client_index
    client = groq_clients[current_client_index]
    current_client_index = (current_client_index + 1) % len(groq_clients)
    return client

async def make_request(func, *args, **kwargs):
    """Делаем запрос с перебором ключей"""
    if not groq_clients:
        raise Exception("No Groq clients")
    
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
    
    raise Exception(f"All clients failed: {errors}")

# --- ОСНОВНЫЕ ФУНКЦИИ ---
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
        return await make_request(transcribe)
    except Exception as e:
        return f"Ошибка распознавания: {str(e)[:100]}"

async def correct_text_basic(text: str) -> str:
    """Базовая коррекция: ошибки и пунктуация"""
    prompt = """Исправь орфографические и пунктуационные ошибки в тексте. 
    Сохрани оригинальный смысл и стиль. Не добавляй комментарии.
    
    Текст:"""
    
    async def correct(client):
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты редактор русского языка."},
                {"role": "user", "content": f"{prompt}\n\n{text}"}
            ],
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    
    try:
        return await make_request(correct)
    except Exception as e:
        return f"Ошибка коррекции: {str(e)[:100]}"

async def correct_text_premium(text: str) -> str:
    """Премиум коррекция: стиль, паразиты, мат"""
    prompt = """Отредактируй текст: исправь ошибки, убери слова-паразиты (ну, типа, короче, блин), 
    замени матерные слова на литературные аналоги, улучши стиль, разбей на логические абзацы.
    Сохрани смысл, но сделай текст красивым и читаемым. Не добавляй комментарии.
    
    Текст:"""
    
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
        return await make_request(correct)
    except Exception as e:
        return f"Ошибка коррекции: {str(e)[:100]}"

async def summarize_text(text: str) -> str:
    """Создание саммари"""
    # Проверяем длину
    if len(text.split()) < 50:  # Если меньше 50 слов
        return "❌ Текст слишком короткий для саммари. Используйте обычную коррекцию."
    
    prompt = """Сделай краткое содержательное саммари текста, выделив основную мысль и ключевые моменты.
    Дай только суть, без деталей. Объем: 10-20% от оригинала.
    
    Текст:"""
    
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
        return await make_request(summarize)
    except Exception as e:
        return f"Ошибка создания саммари: {str(e)[:100]}"

def check_text_length(text: str) -> dict:
    """Проверка длины текста для саммари"""
    words = text.split()
    chars = len(text)
    
    return {
        "words": len(words),
        "chars": chars,
        "needs_summary": len(words) > 100,  # Саммари для текстов >100 слов
        "is_short": len(words) < 30,
        "is_long": len(words) > 500,
    }