# keyboards.py
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import Config
from services import CacheManager


class KeyboardFactory:
    
    @staticmethod
    def create_initial_keyboard(user_id: int) -> InlineKeyboardMarkup:
        """Создать клавиатуру после получения текста/голоса"""
        builder = InlineKeyboardBuilder()
        
        # Добавляем все режимы обработки
        for mode in Config.MODE_ORDER:
            mode_info = Config.MODES[mode]
            builder.add(
                InlineKeyboardButton(
                    text=mode_info["text"],
                    callback_data=f"process_{user_id}_{mode}"
                )
            )
        
        builder.adjust(2)  # 2 кнопки в ряд для первых двух режимов
        return builder.as_markup()
    
    @staticmethod
    def create_switch_keyboard(user_id: int) -> InlineKeyboardMarkup:
        """Создать клавиатуру для переключения режимов + экспорта"""
        builder = InlineKeyboardBuilder()
        
        # Получаем текущий режим и доступные режимы
        current_mode = CacheManager.get_current_mode(user_id)
        available_modes = CacheManager.get_available_modes(user_id)
        
        # Добавляем кнопки для всех доступных режимов, кроме текущего
        for mode in Config.MODE_ORDER:
            if mode in available_modes and mode != current_mode:
                mode_info = Config.MODES[mode]
                builder.add(
                    InlineKeyboardButton(
                        text=mode_info["text"],
                        callback_data=f"switch_{user_id}_{mode}"
                    )
                )
        
        # Добавляем кнопки экспорта (только если есть текущий режим)
        if current_mode:
            for fmt in ["txt", "pdf"]:
                fmt_info = Config.EXPORT_FORMATS[fmt]
                builder.add(
                    InlineKeyboardButton(
                        text=fmt_info["text"],
                        callback_data=f"export_{user_id}_{current_mode}_{fmt}"
                    )
                )
        
        # Автоматическая разметка: режимы в первом ряду, экспорт во втором
        mode_count = len([m for m in available_modes if m != current_mode])
        builder.adjust(mode_count, 2)  # режимы в одном ряду, экспорт в двух колонках
        
        return builder.as_markup()
    
    @staticmethod
    def create_export_only_keyboard(user_id: int, mode: str) -> InlineKeyboardMarkup:
        """Создать клавиатуру только с кнопками экспорта"""
        builder = InlineKeyboardBuilder()
        
        for fmt in ["txt", "pdf"]:
            fmt_info = Config.EXPORT_FORMATS[fmt]
            builder.add(
                InlineKeyboardButton(
                    text=fmt_info["text"],
                    callback_data=f"export_{user_id}_{mode}_{fmt}"
                )
            )
        
        builder.adjust(2)  # 2 кнопки в ряд
        return builder.as_markup()