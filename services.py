# services.py
import asyncio
import random
from openai import AsyncOpenAI
from config import Config, logger

# ============================================================================
# СЕКЦИЯ 1: GROQ КЛИЕНТ И РОТАЦИЯ КЛЮЧЕЙ
# ============================================================================
class GroqService:
    clients = []
    current_index = 0
    
    @classmethod
    def init(cls):
        """Инициализация клиентов Groq"""
        if not Config.GROQ_API_KEYS:
            logger.warning("GROQ_API_KEYS не настроены!")
            return
        
        keys = [key.strip() for key in Config.GROQ_API_KEYS.split(",") if key.strip()]
        
        for key in keys:
            try:
                client = AsyncOpenAI(
                    api_key=key,
                    base_url="https://api.groq.com/openai/v1",
                    timeout=30.0,
                )
                cls.clients.append(client)
                logger.info(f"✅ Groq client: {key[:8]}...")
            except Exception as e:
                logger.error(f"❌ Error client {key[:8]}: {e}")
        
        logger.info(f"✅ Total Groq clients: {len(cls.clients)}")
    
    @classmethod
    def _get_client(cls):
        """Получаем следующего клиента по кругу"""
        if not cls.clients:
            return None
        
        client = cls.clients[cls.current_index]
        cls.current_index = (cls.current_index + 1) % len(cls.clients)
        return client
    
    @classmethod
    async def make_request(cls, func, *args, **kwargs):
        """Делаем запрос с перебором ключей"""
        if not cls.clients:
            raise Exception("No Groq clients available")
        
        errors = []
        
        for _ in range(len(cls.clients) * 2):  # Пробуем каждый ключ 2 раза
            client = cls._get_client()
            if not client:
                break
            
            try:
                return await func(client, *args, **kwargs)
            except Exception as e:
                errors.append(str(e))
                logger.warning(f"Groq request error: {e}")
                await asyncio.sleep(0.5 + random.random())
        
        raise Exception(f"All Groq clients failed: {'; '.join(errors[:3])}")


# ============================================================================
# СЕКЦИЯ 2: ОБРАБОТКА ТЕКСТА
# ============================================================================
class TextProcessor:
    
    @staticmethod
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
            return await GroqService.make_request(transcribe)
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return f"❌ Ошибка распознавания: {str(e)[:100]}"
    
    @staticmethod
    async def basic_correction(text: str) -> str:
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
            return await GroqService.make_request(correct)
        except Exception as e:
            logger.error(f"Basic correction error: {e}")
            return f"❌ Ошибка коррекции: {str(e)[:100]}"
    
    @staticmethod
    async def premium_correction(text: str) -> str:
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
            return await GroqService.make_request(correct)
        except Exception as e:
            logger.error(f"Premium correction error: {e}")
            return f"❌ Ошибка коррекции: {str(e)[:100]}"
    
    @staticmethod
    async def summarize(text: str) -> str:
        """Создание саммари"""
        if not text.strip():
            return "❌ Пустой текст"
        
        from utils import TextAnalyzer
        if TextAnalyzer.is_short_text(text):
            return "📝 Текст слишком короткий для саммари. Используйте обычную коррекцию."
        
        prompt = """Сделай краткое содержательное саммари текста:
        1. Выдели основную мысль и ключевые моменты
        2. Дай только суть, без деталей и примеров
        3. Объем: примерно 10-20% от оригинала
        4. Сохрани важные факты и выводы
        5. Только саммари, без вступлений
        
        Текст для саммаризации:"""
        
        async def summarize_func(client):
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
            return await GroqService.make_request(summarize_func)
        except Exception as e:
            logger.error(f"Summarization error: {e}")
            return f"❌ Ошибка создания саммари: {str(e)[:100]}"


# ============================================================================
# СЕКЦИЯ 3: КЭШИРОВАНИЕ И УПРАВЛЕНИЕ КОНТЕКСТОМ
# ============================================================================
class CacheManager:
    
    @staticmethod
    def get_context(user_id: int) -> dict:
        """Получить контекст пользователя"""
        from config import user_context
        return user_context.get(user_id, {})
    
    @staticmethod
    def save_context(user_id: int, data: dict):
        """Сохранить контекст пользователя"""
        from config import user_context
        user_context[user_id] = data
    
    @staticmethod
    def cache_result(user_id: int, mode: str, text: str):
        """Кэшировать результат обработки"""
        from config import user_context
        
        if user_id not in user_context:
            user_context[user_id] = {}
        
        if "cached_results" not in user_context[user_id]:
            user_context[user_id]["cached_results"] = {}
        
        user_context[user_id]["cached_results"][mode] = text
        user_context[user_id]["current_mode"] = mode
    
    @staticmethod
    def get_cached_result(user_id: int, mode: str) -> str:
        """Получить кэшированный результат"""
        from config import user_context
        
        if user_id not in user_context:
            return None
        
        cached = user_context[user_id].get("cached_results", {})
        return cached.get(mode)
    
    @staticmethod
    def clear_context(user_id: int):
        """Очистить контекст пользователя (при новом сообщении)"""
        from config import user_context
        if user_id in user_context:
            del user_context[user_id]
    
    @staticmethod
    def get_available_modes(user_id: int) -> list:
        """Получить доступные режимы для пользователя"""
        from config import user_context
        
        if user_id not in user_context:
            return []
        
        # Если уже определены - возвращаем
        if "available_modes" in user_context[user_id]:
            return user_context[user_id]["available_modes"]
        
        # Определяем на основе текста
        from utils import TextAnalyzer
        
        original_text = user_context[user_id].get("original", "")
        available_modes = ["basic", "premium"]
        
        if not TextAnalyzer.is_short_text(original_text):
            available_modes.append("summary")
        
        # Сохраняем для будущего использования
        user_context[user_id]["available_modes"] = available_modes
        return available_modes
    
    @staticmethod
    def get_current_mode(user_id: int) -> str:
        """Получить текущий активный режим"""
        from config import user_context
        
        if user_id not in user_context:
            return None
        
        return user_context[user_id].get("current_mode")
    
    @staticmethod
    def set_current_mode(user_id: int, mode: str):
        """Установить текущий активный режим"""
        from config import user_context
        
        if user_id not in user_context:
            return
        
        user_context[user_id]["current_mode"] = mode