# bot.py
import os
import io
import logging
import asyncio
import sys
import json
import base64
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web
from openai import AsyncOpenAI
import random
import mimetypes

from aiogram import Bot, Dispatcher, types, F
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
                timeout=60.0,  # Увеличил таймаут для Vision
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
            await asyncio.sleep(1 + random.random())  # Увеличил паузу
    
    raise Exception(f"All clients failed: {'; '.join(errors[:3])}")

# --- VISION ПРОЦЕССОР (из vision.py) ---
class VisionProcessor:
    def __init__(self):
        pass
    
    async def check_content(self, image_bytes: bytes) -> tuple[bool, str]:
        """
        Проверка изображения на образовательный контент
        Возвращает: (is_educational, message)
        """
        
        # Базовые проверки
        if len(image_bytes) > 10 * 1024 * 1024:  # 10MB
            return False, "Изображение слишком большое. Попробуйте сфотографировать ближе."
        
        if not groq_clients:
            # Если нет клиентов Groq, пропускаем проверку
            return True, "OK"
        
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        async def analyze(client):
            response = await client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": """Analyze this image. Respond ONLY with JSON:
{
  "is_educational": true/false,
  "content_type": "homework/textbook/notes/diagram/inappropriate/unclear/other"
}

Educational content includes:
- Textbook pages, homework assignments
- Math problems, exercises, diagrams
- Handwritten notes, formulas
- Educational charts, tables
- Any text documents, letters, articles

Non-educational (but respond politely):
- Random photos, memes
- Screenshots of unrelated content
- Blurry/unclear images
- Inappropriate content (handle with care)"""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                temperature=0.2,
                max_tokens=150
            )
            return response.choices[0].message.content
        
        try:
            result = await make_groq_request(analyze)
            analysis = json.loads(result)
            
            is_educational = analysis.get("is_educational", False)
            content_type = analysis.get("content_type", "unclear")
            
            if not is_educational:
                # Вежливые ответы для разных случаев
                messages = {
                    "inappropriate": "Пожалуйста, отправляйте только текстовые материалы для обработки.",
                    "unclear": "Изображение нечёткое. Попробуйте сфотографировать ещё раз при хорошем освещении.",
                    "other": "Я вижу изображение, но не могу найти там текст. Отправьте фото с текстом или текстовый файл."
                }
                message = messages.get(content_type, "Отправьте, пожалуйста, фото с текстом.")
                return False, message
            
            return True, "OK"
            
        except Exception as e:
            # При ошибке - пропускаем изображение
            logger.warning(f"Vision check error: {e}")
            return True, "OK"
    
    async def extract_text(self, image_bytes: bytes) -> str:
        """OCR через Groq Vision"""
        
        if not groq_clients:
            return "❌ Для распознавания изображений нужны ключи Groq API. Добавьте GROQ_API_KEYS в .env файл."
        
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        async def extract(client):
            response = await client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": """Распознай и перепиши ВЕСЬ текст с этого изображения максимально точно.
ВАЖНО:
1. Сохрани ВСЕ слова, цифры, знаки препинания
2. Сохрани структуру текста (абзацы, списки, заголовки)
3. Сохрани математические формулы и выражения как есть
4. Сохрани нумерацию заданий
5. Если есть ошибки в оригинале - оставь их как есть
6. Не исправляй текст, просто перепиши его
7. Если текст на иностранном языке - сохрани его как есть

Верни ТОЛЬКО распознанный текст без комментариев и пояснений."""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                temperature=0.1,
                max_tokens=4000,
            )
            return response.choices[0].message.content
        
        try:
            return await make_groq_request(extract)
        except Exception as e:
            logger.error(f"Vision OCR error: {e}")
            return f"❌ Ошибка распознавания текста: {str(e)[:100]}"

# Создаем экземпляр VisionProcessor
vision_processor = VisionProcessor()

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
    
    prompt = """Ты — профессиональный редактор. Твоя задача отредактировать текст пользователя:
Исправь орфографические, пунктуационные и грамматические ошибки.        
Верни ТОЛЬКО готовый исправленный текст без кавычек и вступлений.

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
    
    prompt = """Ты — профессиональный редактор. Твоя задача отредактировать текст пользователя:
1. Исправь орфографические, пунктуационные, речевые и грамматические ошибки.        
2. Удали слова-паразиты (ну, короче, типа, эээ), 'воду' и бессмысленные повторы. Оставь только минимум вводных и соединительных слов с полным сохранением смысла фразы.
3. Если есть матерные, бранные или грубые выражения — замени их на безобидные литературные аналоги, подходящие по смыслу, или смягчи тон.
Верни ТОЛЬКО готовый исправленный текст без кавычек и вступлений.

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

# --- ФУНКЦИИ ДЛЯ ФАЙЛОВ ---
async def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Извлечение текста из PDF"""
    try:
        from PyPDF2 import PdfReader
        pdf_buffer = io.BytesIO(pdf_bytes)
        reader = PdfReader(pdf_buffer)
        text = ""
        
        for page_num, page in enumerate(reader.pages, 1):
            page_text = page.extract_text()
            if page_text:
                text += f"\n--- Страница {page_num} ---\n"
                text += page_text + "\n"
        
        return text.strip() if text else "Не удалось извлечь текст из PDF"
    except ImportError:
        return "❌ Для работы с PDF требуется установить PyPDF2"
    except Exception as e:
        return f"❌ Ошибка обработки PDF: {str(e)}"

async def extract_text_from_docx(docx_bytes: bytes) -> str:
    """Извлечение текста из DOCX"""
    try:
        import docx
        doc_buffer = io.BytesIO(docx_bytes)
        doc = docx.Document(doc_buffer)
        text = ""
        
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                text += paragraph.text + "\n"
        
        return text.strip() if text else "Документ пуст"
    except ImportError:
        return "❌ Для работы с DOCX требуется установить python-docx"
    except Exception as e:
        return f"❌ Ошибка обработки DOCX: {str(e)}"

async def extract_text_from_txt(txt_bytes: bytes) -> str:
    """Извлечение текста из TXT"""
    try:
        # Пробуем разные кодировки
        encodings = ['utf-8', 'cp1251', 'koi8-r', 'windows-1251', 'iso-8859-1']
        
        for encoding in encodings:
            try:
                return txt_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        
        # Если ни одна кодировка не подошла
        return txt_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        return f"❌ Ошибка чтения текстового файла: {str(e)}"

async def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """Определяем тип файла и извлекаем текст"""
    
    # Определяем MIME тип по расширению
    mime_type, _ = mimetypes.guess_type(filename)
    
    if mime_type:
        if mime_type.startswith('image/'):
            # Это изображение - используем VisionProcessor
            # Сначала проверяем контент
            is_educational, message = await vision_processor.check_content(file_bytes)
            if not is_educational:
                return f"❌ {message}"
            
            # Затем распознаем текст
            status_msg = f"🔍 Распознаю текст с изображения..."
            logger.info(status_msg)
            return await vision_processor.extract_text(file_bytes)
        
        elif mime_type == 'application/pdf':
            # PDF файл
            return await extract_text_from_pdf(file_bytes)
        
        elif mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
            # DOCX файл
            return await extract_text_from_docx(file_bytes)
        
        elif mime_type == 'text/plain':
            # TXT файл
            return await extract_text_from_txt(file_bytes)
    
    # Если MIME тип не определился, пробуем по расширению
    file_ext = filename.lower().split('.')[-1] if '.' in filename else ''
    
    if file_ext in ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp']:
        # Изображение - используем VisionProcessor
        is_educational, message = await vision_processor.check_content(file_bytes)
        if not is_educational:
            return f"❌ {message}"
        
        status_msg = f"🔍 Распознаю текст с изображения..."
        logger.info(status_msg)
        return await vision_processor.extract_text(file_bytes)
    
    elif file_ext == 'pdf':
        return await extract_text_from_pdf(file_bytes)
    
    elif file_ext == 'docx':
        return await extract_text_from_docx(file_bytes)
    
    elif file_ext == 'txt':
        return await extract_text_from_txt(file_bytes)
    
    elif file_ext == 'doc':
        return "❌ DOC файлы (старый формат Word) не поддерживаются. Сохраните файл как DOCX."
    
    else:
        return f"❌ Неподдерживаемый формат файла: .{file_ext}\nПоддерживаются: изображения, PDF, DOCX, TXT"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_available_modes(text: str) -> list:
    """Определяем доступные режимы обработки"""
    words = text.split()
    if len(words) < 50 or len(text) < 300:
        return ["basic", "premium"]
    return ["basic", "premium", "summary"]

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

def create_switch_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создаем клавиатуру для переключения между режимами"""
    ctx = user_context.get(user_id)
    if not ctx:
        return None
    
    current = ctx.get("current_mode")
    available = ctx.get("available_modes", [])
    
    builder = InlineKeyboardBuilder()
    
    # Кнопки других режимов
    mode_buttons = []
    if "basic" in available and current != "basic":
        mode_buttons.append(InlineKeyboardButton(text="📝 Как есть", callback_data=f"switch_{user_id}_basic"))
    if "premium" in available and current != "premium":
        mode_buttons.append(InlineKeyboardButton(text="✨ Красиво", callback_data=f"switch_{user_id}_premium"))
    if "summary" in available and current != "summary":
        mode_buttons.append(InlineKeyboardButton(text="📊 Саммари", callback_data=f"switch_{user_id}_summary"))
    
    # Добавляем по 2 кнопки в ряд
    for i in range(0, len(mode_buttons), 2):
        builder.row(*mode_buttons[i:i+2])
    
    # Кнопки экспорта
    builder.row(
        InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{user_id}_{current}_txt"),
        InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{user_id}_{current}_pdf")
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
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.lib.utils import simpleSplit
            
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
            max_width = width - 2 * margin
            
            for paragraph in text.split('\n'):
                if not paragraph.strip():
                    y -= line_height
                    continue
                    
                lines = simpleSplit(paragraph, "Helvetica", 11, max_width)
                
                for line in lines:
                    if y < margin + 20:
                        c.showPage()
                        y = height - margin
                        c.setFont("Helvetica", 11)
                    c.drawString(margin, y, line)
                    y -= line_height
            
            c.save()
            return filepath
            
        except ImportError:
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
        app.router.add_get('/health', health_check)
        app.router.add_get('/ping', health_check)
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"✅ WEB SERVER STARTED ON PORT {port}")
    except Exception as e:
        logger.error(f"❌ Error starting web server: {e}")

# --- ХЭНДЛЕРЫ БОТА ---
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "👋 <b>Текст-редактор бот Грамотей</b>\n\n"
        "📁 <b>Что я умею:</b>\n"
        "• Распознавать текст с <b>изображений</b> (JPG, PNG и др.)\n"
        "• Читать текст из <b>файлов</b> (PDF, DOCX, TXT)\n"
        "• Транскрибировать <b>голосовые сообщения</b>\n"
        "• Обрабатывать <b>прямой текст</b>\n\n"
        "🔧 <b>Варианты обработки:</b>\n"
        "• <b>📝 Как есть</b> - исправление ошибок и пунктуация\n"
        "• <b>✨ Красиво</b> - уборка слов-паразитов, улучшение стиля\n"
        "• <b>📊 Саммари</b> - краткое содержание (для длинных текстов)\n\n"
        "💾 После обработки можно переключаться между вариантами и экспортировать в файлы.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(Command("help"))
async def help_handler(message: types.Message):
    await message.answer(
        "📋 <b>Как использовать бота:</b>\n\n"
        "1. <b>Отправьте любым способом:</b>\n"
        "   • Текст сообщением\n"
        "   • Голосовое сообщение\n"
        "   • Фото с текстом\n"
        "   • Файл (PDF, DOCX, TXT)\n\n"
        "2. <b>Выберите вариант обработки:</b>\n"
        "   • 📝 Как есть - быстрая коррекция\n"
        "   • ✨ Красиво - профессиональное редактирование\n"
        "   • 📊 Саммари - краткое содержание\n\n"
        "3. <b>После обработки можно:</b>\n"
        "   • Переключаться между вариантами\n"
        "   • Экспортировать в TXT или PDF\n\n"
        "📌 <b>Формат файлов:</b>\n"
        "• Изображения: JPG, PNG, GIF, BMP, WebP\n"
        "• Документы: PDF, DOCX, TXT\n"
        "• Максимальный размер: 10 MB",
        parse_mode="HTML"
    )

@dp.message(Command("status"))
async def status_handler(message: types.Message):
    """Показывает статус бота"""
    status_text = (
        f"🤖 <b>Статус бота:</b>\n"
        f"• Groq клиентов: {len(groq_clients)}\n"
        f"• Пользователей в памяти: {len(user_context)}\n"
        f"• Vision доступен: {'✅' if groq_clients else '❌'}\n"
        f"• PDF обработка: {'✅' if hasattr(__import__('PyPDF2'), 'PdfReader') else '❌'}\n"
        f"• DOCX обработка: {'✅' if hasattr(__import__('docx'), 'Document') else '❌'}\n"
    )
    await message.answer(status_text, parse_mode="HTML")

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
        
        # Определяем доступные режимы
        available_modes = get_available_modes(original_text)
        
        # Сохраняем контекст
        user_context[user_id] = {
            "type": "voice",
            "original": original_text,
            "cached_results": {"basic": None, "premium": None, "summary": None},
            "current_mode": None,
            "available_modes": available_modes,
            "message_id": msg.message_id,
            "chat_id": message.chat.id
        }
        
        # Предлагаем варианты
        preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
        
        # Формируем сообщение с учетом доступных режимов
        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"
        
        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
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
        # Определяем доступные режимы
        available_modes = get_available_modes(original_text)
        
        # Сохраняем контекст
        user_context[user_id] = {
            "type": "text",
            "original": original_text,
            "cached_results": {"basic": None, "premium": None, "summary": None},
            "current_mode": None,
            "available_modes": available_modes,
            "message_id": msg.message_id,
            "chat_id": message.chat.id
        }
        
        # Предлагаем варианты
        preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
        
        # Формируем сообщение с учетом доступных режимов
        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"
        
        await msg.edit_text(
            f"📝 <b>Полученный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
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

@dp.message(F.photo | F.document)
async def file_handler(message: types.Message):
    user_id = message.from_user.id
    msg = await message.answer("📁 Обрабатываю файл...")
    
    try:
        file_info = None
        file_bytes = None
        filename = ""
        
        # Получаем информацию о файле
        if message.photo:
            # Для фото берем максимальное качество
            file_info = await bot.get_file(message.photo[-1].file_id)
            filename = f"photo_{file_info.file_unique_id}.jpg"
        elif message.document:
            file_info = await bot.get_file(message.document.file_id)
            filename = message.document.file_name or f"file_{file_info.file_unique_id}"
        
        # Скачиваем файл
        file_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, file_buffer)
        file_bytes = file_buffer.getvalue()
        
        # Проверяем размер файла
        if len(file_bytes) > 10 * 1024 * 1024:  # 10 MB
            await msg.edit_text("❌ Файл слишком большой (максимум 10 MB)")
            return
        
        # Извлекаем текст из файла
        status_msg = await msg.edit_text("🔍 Извлекаю текст...")
        original_text = await extract_text_from_file(file_bytes, filename)
        
        if original_text.startswith("❌"):
            await status_msg.edit_text(original_text)
            return
        
        # Проверяем, не пустой ли текст
        if not original_text.strip() or len(original_text.strip()) < 10:
            await status_msg.edit_text(
                "❌ Не удалось найти текст в файле.\n"
                "Попробуйте:\n"
                "• Более четкое изображение\n"
                "• Файл с текстовым содержимым\n"
                "• Прямой текст сообщением"
            )
            return
        
        # Определяем доступные режимы
        available_modes = get_available_modes(original_text)
        
        # Сохраняем контекст
        user_context[user_id] = {
            "type": "file",
            "original": original_text,
            "cached_results": {"basic": None, "premium": None, "summary": None},
            "current_mode": None,
            "available_modes": available_modes,
            "message_id": msg.message_id,
            "chat_id": message.chat.id,
            "filename": filename
        }
        
        # Предлагаем варианты
        preview = original_text[:200] + "..." if len(original_text) > 200 else original_text
        
        # Формируем сообщение с учетом доступных режимов
        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"
        
        file_type = "изображения" if filename.startswith("photo_") or any(ext in filename.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']) else "файла"
        
        await status_msg.edit_text(
            f"✅ <b>Извлеченный текст из {file_type}:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
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
        logger.error(f"File error: {e}")
        await msg.edit_text(f"❌ Ошибка обработки файла: {str(e)[:100]}")

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
        available_modes = ctx.get("available_modes", [])
        
        # Проверяем доступность режима
        if process_type not in available_modes:
            await callback.answer("⚠️ Этот режим недоступен для данного текста", show_alert=True)
            return
        
        original_text = ctx["original"]
        
        # Обновляем сообщение
        processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({process_type})...")
        
        # Обрабатываем в зависимости от типа
        if process_type == "basic":
            result = await correct_text_basic(original_text)
        elif process_type == "premium":
            result = await correct_text_premium(original_text)
        elif process_type == "summary":
            result = await summarize_text(original_text)
        else:
            result = "Неизвестный тип обработки"
        
        # Сохраняем результат в кэш
        user_context[target_user_id]["cached_results"][process_type] = result
        user_context[target_user_id]["current_mode"] = process_type
        
        # Отправляем результат
        if len(result) > 4000:
            await processing_msg.delete()
            
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id)
            )
        else:
            await processing_msg.edit_text(
                result,
                reply_markup=create_switch_keyboard(target_user_id)
            )
            
    except Exception as e:
        logger.error(f"Process error: {e}")
        await callback.message.edit_text("❌ Ошибка обработки")

@dp.callback_query(F.data.startswith("switch_"))
async def switch_callback(callback: types.CallbackQuery):
    """Переключение между режимами обработки"""
    await callback.answer()
    
    try:
        # Парсим callback data: switch_{user_id}_{target_mode}
        parts = callback.data.split("_")
        if len(parts) < 3:
            return
        
        target_user_id = int(parts[1])
        target_mode = parts[2]
        
        # Проверяем права
        if callback.from_user.id != target_user_id:
            return
        
        # Получаем контекст
        if target_user_id not in user_context:
            await callback.message.answer("❌ Текст не найден. Обработайте текст заново.")
            return
        
        ctx = user_context[target_user_id]
        available_modes = ctx.get("available_modes", [])
        
        # Проверяем доступность режима
        if target_mode not in available_modes:
            await callback.answer("⚠️ Этот режим недоступен для данного текста", show_alert=True)
            return
        
        # Проверяем кэш
        cached = ctx["cached_results"].get(target_mode)
        
        if cached:
            # Берем из кэша
            result = cached
        else:
            # Обрабатываем
            processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({target_mode})...")
            
            original_text = ctx["original"]
            
            if target_mode == "basic":
                result = await correct_text_basic(original_text)
            elif target_mode == "premium":
                result = await correct_text_premium(original_text)
            elif target_mode == "summary":
                result = await summarize_text(original_text)
            else:
                result = "Неизвестный режим"
            
            # Сохраняем в кэш
            user_context[target_user_id]["cached_results"][target_mode] = result
        
        # Обновляем текущий режим
        user_context[target_user_id]["current_mode"] = target_mode
        
        # Показываем результат
        if len(result) > 4000:
            await callback.message.delete()
            
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i:i+4000])
            
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id)
            )
        else:
            await callback.message.edit_text(
                result,
                reply_markup=create_switch_keyboard(target_user_id)
            )
            
    except Exception as e:
        logger.error(f"Switch error: {e}")
        await callback.message.edit_text("❌ Ошибка переключения режима")

@dp.callback_query(F.data.startswith("export_"))
async def export_callback(callback: types.CallbackQuery):
    await callback.answer()
    
    try:
        # Парсим: export_{user_id}_{mode}_{format}
        parts = callback.data.split("_")
        if len(parts) < 4:
            return
        
        target_user_id = int(parts[1])
        mode = parts[2]
        export_format = parts[3]
        
        # Проверяем права
        if callback.from_user.id != target_user_id:
            return
        
        # Получаем контекст
        if target_user_id not in user_context:
            await callback.message.answer("❌ Текст не найден. Обработайте текст заново.")
            return
        
        ctx = user_context[target_user_id]
        text = ctx["cached_results"].get(mode)
        
        if not text:
            await callback.answer("⚠️ Текст не найден в кэше", show_alert=True)
            return
        
        # Создаем файл
        status_msg = await callback.message.answer("📁 Создаю файл...")
        filepath = await save_to_file(target_user_id, text, export_format)
        
        if not filepath:
            await status_msg.edit_text("❌ Ошибка создания файла")
            return
        
        # Отправляем файл
        filename = os.path.basename(filepath)
        
        if export_format == "pdf":
            caption = "📊 PDF файл с обработанным текстом"
        else:
            caption = "📄 Текстовый файл с обработанным текстом"
        
        document = FSInputFile(filepath, filename=filename)
        await callback.message.answer_document(document=document, caption=caption)
        
        # Удаляем сообщение о создании
        await status_msg.delete()
        
        # Удаляем временный файл
        try:
            os.remove(filepath)
        except:
            pass
        
    except Exception as e:
        logger.error(f"Export error: {e}")
        await callback.message.answer("❌ Ошибка создания файла")

# --- ЗАПУСК ---
async def main():
    logger.info("Bot starting process...")
    
    # Инициализируем Groq клиенты
    init_groq_clients()
    
    # Запускаем веб-сервер в фоне
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
