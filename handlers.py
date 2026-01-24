# handlers.py
import io
from aiogram import types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardRemove, FSInputFile

from config import Config, logger, user_context
from services import TextProcessor, CacheManager
from keyboards import KeyboardFactory
from utils import TextAnalyzer, FileExporter


# ============================================================================
# ГРУППА 1: КОМАНДЫ
# ============================================================================
async def start_handler(message: types.Message):
    """Обработчик команды /start"""
    await message.answer(
        "👋 <b>Текст-редактор бот</b>\n\n"
        "Отправьте мне голосовое или текстовое сообщение, и я предложу варианты обработки:\n\n"
        "• <b>📝 Как есть</b> - исправление ошибок, пунктуация\n"
        "• <b>✨ Красиво</b> - уборка слов-паразитов, улучшение стиля\n"
        "• <b>📊 Саммари</b> - краткое содержание (для длинных текстов)\n\n"
        "После обработки можно переключаться между вариантами и экспортировать текст в файл.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


# ============================================================================
# ГРУППА 2: КОНТЕНТ (ТЕКСТ И ГОЛОС)
# ============================================================================
async def voice_handler(message: types.Message, bot):
    """Обработчик голосовых и аудио сообщений"""
    user_id = message.from_user.id
    
    try:
        # Удаляем предыдущий контекст
        CacheManager.clear_context(user_id)
        
        msg = await message.answer("🎧 Распознаю голосовое сообщение...")
        
        # Скачиваем голосовое
        if message.voice:
            file_info = await bot.get_file(message.voice.file_id)
        else:
            file_info = await bot.get_file(message.audio.file_id)
        
        voice_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, voice_buffer)
        
        # Распознаем
        original_text = await TextProcessor.transcribe_voice(voice_buffer.getvalue())
        
        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return
        
        # Сохраняем в контекст
        CacheManager.save_context(user_id, {
            "type": "voice",
            "original": original_text,
            "message_id": msg.message_id,
            "chat_id": message.chat.id,
            "cached_results": {},
            "available_modes": None  # Определится при первом запросе
        })
        
        # Предлагаем варианты обработки
        preview = TextAnalyzer.trim_text_preview(original_text)
        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.create_initial_keyboard(user_id)
        )
        
        # Удаляем оригинальное сообщение
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        await message.answer("❌ Ошибка обработки голосового сообщения")


async def text_handler(message: types.Message):
    """Обработчик текстовых сообщений"""
    user_id = message.from_user.id
    original_text = message.text.strip()
    
    # Игнорируем команды
    if original_text.startswith("/"):
        return
    
    try:
        # Удаляем предыдущий контекст
        CacheManager.clear_context(user_id)
        
        msg = await message.answer("📝 Анализирую текст...")
        
        # Сохраняем в контекст
        CacheManager.save_context(user_id, {
            "type": "text",
            "original": original_text,
            "message_id": msg.message_id,
            "chat_id": message.chat.id,
            "cached_results": {},
            "available_modes": None  # Определится при первом запросе
        })
        
        # Предлагаем варианты обработки
        preview = TextAnalyzer.trim_text_preview(original_text)
        await msg.edit_text(
            f"📝 <b>Полученный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.create_initial_keyboard(user_id)
        )
        
        # Удаляем оригинальное сообщение
        try:
            await message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Text handler error: {e}")
        await message.answer("❌ Ошибка обработки текста")


# ============================================================================
# ГРУППА 3: CALLBACK'И (СВЯЗАННАЯ ЛОГИКА)
# ============================================================================
async def process_callback(callback: types.CallbackQuery, bot):
    """Обработчик первичной обработки текста"""
    await callback.answer()
    
    try:
        # Парсим callback data: process_{user_id}_{mode}
        parts = callback.data.split("_")
        if len(parts) < 3:
            return
        
        target_user_id = int(parts[1])
        mode = parts[2]
        
        # Проверяем права
        if callback.from_user.id != target_user_id:
            await callback.message.answer("⚠️ Это не ваш запрос!")
            return
        
        # Получаем контекст
        ctx = CacheManager.get_context(target_user_id)
        if not ctx or "original" not in ctx:
            await callback.message.edit_text("❌ Время обработки истекло. Отправьте текст заново.")
            return
        
        original_text = ctx["original"]
        
        # Обновляем сообщение
        processing_msg = await callback.message.edit_text(f"⏳ Обрабатываю ({Config.MODES[mode]['text']})...")
        
        # Проверяем кэш
        cached_result = CacheManager.get_cached_result(target_user_id, mode)
        
        if cached_result:
            # Используем кэшированный результат
            result = cached_result
            logger.info(f"Using cached result for user {target_user_id}, mode {mode}")
        else:
            # Обрабатываем через Groq
            if mode == "basic":
                result = await TextProcessor.basic_correction(original_text)
            elif mode == "premium":
                result = await TextProcessor.premium_correction(original_text)
            elif mode == "summary":
                result = await TextProcessor.summarize(original_text)
            else:
                result = "❌ Неизвестный тип обработки"
            
            # Кэшируем результат
            if not result.startswith("❌"):
                CacheManager.cache_result(target_user_id, mode, result)
        
        # Отправляем результат
        await _send_processed_text(
            bot=bot,
            chat_id=callback.message.chat.id,
            message_id=processing_msg.message_id,
            user_id=target_user_id,
            text=result,
            mode=mode
        )
        
    except Exception as e:
        logger.error(f"Process callback error: {e}")
        await callback.message.edit_text("❌ Ошибка обработки запроса")


async def switch_callback(callback: types.CallbackQuery, bot):
    """Обработчик переключения между режимами"""
    await callback.answer()
    
    try:
        # Парсим callback data: switch_{user_id}_{mode}
        parts = callback.data.split("_")
        if len(parts) < 3:
            return
        
        target_user_id = int(parts[1])
        target_mode = parts[2]
        
        # Проверяем права
        if callback.from_user.id != target_user_id:
            return
        
        # Получаем контекст
        ctx = CacheManager.get_context(target_user_id)
        if not ctx or "original" not in ctx:
            await callback.message.edit_text("❌ Время обработки истекло. Отправьте текст заново.")
            return
        
        original_text = ctx["original"]
        
        # Обновляем сообщение
        processing_msg = await callback.message.edit_text(
            f"⏳ Переключаю на {Config.MODES[target_mode]['text']}..."
        )
        
        # Проверяем кэш
        cached_result = CacheManager.get_cached_result(target_user_id, target_mode)
        
        if cached_result:
            # Используем кэшированный результат
            result = cached_result
            logger.info(f"Using cached result for switch, user {target_user_id}, mode {target_mode}")
        else:
            # Обрабатываем через Groq
            if target_mode == "basic":
                result = await TextProcessor.basic_correction(original_text)
            elif target_mode == "premium":
                result = await TextProcessor.premium_correction(original_text)
            elif target_mode == "summary":
                result = await TextProcessor.summarize(original_text)
            else:
                result = "❌ Неизвестный тип обработки"
            
            # Кэшируем результат
            if not result.startswith("❌"):
                CacheManager.cache_result(target_user_id, target_mode, result)
        
        # Отправляем результат
        await _send_processed_text(
            bot=bot,
            chat_id=callback.message.chat.id,
            message_id=processing_msg.message_id,
            user_id=target_user_id,
            text=result,
            mode=target_mode
        )
        
    except Exception as e:
        logger.error(f"Switch callback error: {e}")
        await callback.message.edit_text("❌ Ошибка переключения режима")


async def export_callback(callback: types.CallbackQuery, bot):
    """Обработчик экспорта текста в файл"""
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
        
        # Получаем текст из кэша
        text = CacheManager.get_cached_result(target_user_id, mode)
        if not text:
            await callback.message.edit_text("❌ Текст не найден. Обработайте текст заново.")
            return
        
        # Создаем файл
        await callback.message.edit_text(f"📁 Создаю {export_format.upper()} файл...")
        
        if export_format == "pdf":
            filepath = await FileExporter.save_to_pdf(target_user_id, text)
            caption = "📊 PDF файл с текстом"
            mime_type = "application/pdf"
        else:  # txt
            filepath = await FileExporter.save_to_txt(target_user_id, text)
            caption = "📄 Текстовый файл"
            mime_type = "text/plain"
        
        if not filepath:
            await callback.message.edit_text("❌ Ошибка создания файла")
            return
        
        # Отправляем файл
        filename = os.path.basename(filepath)
        document = FSInputFile(filepath, filename=filename)
        await bot.send_document(
            chat_id=callback.message.chat.id,
            document=document,
            caption=caption
        )
        
        # Восстанавливаем сообщение с текстом и кнопками
        if len(text) <= 4000:
            await callback.message.delete()
            await callback.message.answer(
                text,
                reply_markup=KeyboardFactory.create_switch_keyboard(target_user_id)
            )
        else:
            # Для длинных текстов просто удаляем сообщение "Создаю файл"
            await callback.message.delete()
        
        # Удаляем временный файл
        try:
            os.remove(filepath)
        except:
            pass
        
    except Exception as e:
        logger.error(f"Export callback error: {e}")
        await callback.message.edit_text("❌ Ошибка создания файла")


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================
async def _send_processed_text(bot, chat_id, message_id, user_id, text, mode):
    """Отправить обработанный текст с клавиатурой"""
    # Разбиваем длинный текст
    chunks = TextAnalyzer.split_long_text(text)
    
    if len(chunks) == 1:
        # Короткий текст - редактируем существующее сообщение
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=KeyboardFactory.create_switch_keyboard(user_id)
        )
    else:
        # Длинный текст - удаляем старое сообщение и отправляем частями
        await bot.delete_message(chat_id, message_id)
        
        # Отправляем все части кроме последней
        for chunk in chunks[:-1]:
            await bot.send_message(chat_id, chunk)
        
        # Последнюю часть отправляем с клавиатурой
        await bot.send_message(
            chat_id,
            chunks[-1],
            reply_markup=KeyboardFactory.create_switch_keyboard(user_id)
        )