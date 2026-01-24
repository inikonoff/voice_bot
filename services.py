import asyncio
import random
from openai import AsyncOpenAI

# Инициализация Groq клиентов
groq_clients = []
current_client_index = 0

def init_groq_clients(groq_api_keys: str):
    """Инициализация клиентов Groq"""
    global groq_clients
    
    if not groq_api_keys:
        print("⚠️ GROQ_API_KEYS не настроены!")
        return
    
    keys = [key.strip() for key in groq_api_keys.split(",") if key.strip()]
    
    for key in keys:
        try:
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=30.0,
            )
            groq_clients.append(client)
            print(f"✅ Groq client: {key[:8]}...")
        except Exception as e:
            print(f"❌ Error client {key[:8]}: {e}")
    
    print(f"✅ Total clients: {len(groq_clients)}")

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
        return f"❌ Ошибка создания саммари: {str(e)[:100]}"