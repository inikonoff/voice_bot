# utils.py
import os
import asyncio
from datetime import datetime
from aiohttp import web
from config import Config, logger


# ============================================================================
# СЕКЦИЯ 1: РАБОТА С ФАЙЛАМИ
# ============================================================================
class FileExporter:
    
    @staticmethod
    async def save_to_txt(user_id: int, text: str) -> str:
        """Сохранить текст в TXT файл"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"text_{user_id}_{timestamp}.txt"
        filepath = f"/tmp/{filename}"
        
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
        except Exception as e:
            logger.error(f"Error saving TXT: {e}")
            return None
    
    @staticmethod
    async def save_to_pdf(user_id: int, text: str) -> str:
        """Сохранить текст в PDF файл"""
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            import textwrap
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"text_{user_id}_{timestamp}.pdf"
            filepath = f"/tmp/{filename}"
            
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
            lines = textwrap.wrap(text, width=90)
            
            for line in lines:
                if y < margin:
                    c.showPage()
                    y = height - margin
                    c.setFont("Helvetica", 11)
                c.drawString(margin, y, line)
                y -= line_height
            
            c.save()
            return filepath
            
        except ImportError:
            logger.warning("Reportlab not installed, using TXT fallback")
            # Fallback на TXT
            return await FileExporter.save_to_txt(user_id, text)
        except Exception as e:
            logger.error(f"Error saving PDF: {e}")
            return None


# ============================================================================
# СЕКЦИЯ 2: АНАЛИЗ ТЕКСТА
# ============================================================================
class TextAnalyzer:
    
    @staticmethod
    def is_short_text(text: str) -> bool:
        """Проверить, является ли текст коротким"""
        if not text:
            return True
        
        # Проверка по количеству слов
        words = text.split()
        if len(words) < Config.SHORT_TEXT_WORDS:
            return True
        
        # Проверка по длине символов
        if len(text) < Config.SHORT_TEXT_CHARS:
            return True
        
        return False
    
    @staticmethod
    def get_word_count(text: str) -> int:
        """Получить количество слов в тексте"""
        if not text:
            return 0
        return len(text.split())
    
    @staticmethod
    def trim_text_preview(text: str, max_len: int = 200) -> str:
        """Обрезать текст для предварительного просмотра"""
        if not text:
            return ""
        
        if len(text) <= max_len:
            return text
        
        # Обрезаем до последнего целого слова
        trimmed = text[:max_len]
        last_space = trimmed.rfind(' ')
        
        if last_space > max_len * 0.7:  # Если есть разумное место для обрезки
            trimmed = trimmed[:last_space]
        
        return trimmed + "..."
    
    @staticmethod
    def split_long_text(text: str, max_chunk: int = 4000) -> list:
        """Разбить длинный текст на части"""
        if len(text) <= max_chunk:
            return [text]
        
        chunks = []
        for i in range(0, len(text), max_chunk):
            chunk = text[i:i + max_chunk]
            
            # Пытаемся обрезать по границе предложения
            if i + max_chunk < len(text):
                last_period = chunk.rfind('. ')
                if last_period > max_chunk * 0.7:
                    chunk = chunk[:last_period + 1]
            
            chunks.append(chunk)
        
        return chunks


# ============================================================================
# СЕКЦИЯ 3: ВЕБ-СЕРВЕР ДЛЯ UPTIME ROBOT
# ============================================================================
class HealthServer:
    
    @staticmethod
    async def health_check(request):
        """Проверка здоровья для Uptime Robot"""
        return web.Response(text="Bot is alive!", status=200)
    
    @staticmethod
    async def start():
        """Запуск фонового веб-сервера"""
        try:
            app = web.Application()
            app.router.add_get('/', HealthServer.health_check)
            app.router.add_get('/health', HealthServer.health_check)
            app.router.add_get('/ping', HealthServer.health_check)
            
            runner = web.AppRunner(app)
            await runner.setup()
            
            site = web.TCPSite(runner, '0.0.0.0', Config.PORT)
            await site.start()
            logger.info(f"✅ Web server started on port {Config.PORT}")
            
            # Бесконечный цикл, чтобы сервер работал
            while True:
                await asyncio.sleep(3600)  # Sleep for 1 hour
            
        except Exception as e:
            logger.error(f"❌ Error starting web server: {e}")
            raise