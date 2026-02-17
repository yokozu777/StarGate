#!/usr/bin/env python3
"""
Настройка логирования для worker
"""
import os
import json
import logging
from logging.handlers import RotatingFileHandler
from .config import LOG_FILE, DATA_DIR

# Функция для получения настроек логирования из execution_settings.json
def get_logging_settings():
    """Получает настройки логирования из execution_settings.json или переменных окружения"""
    execution_settings_file = DATA_DIR / 'execution_settings.json'
    
    # Сначала пытаемся загрузить из execution_settings.json
    try:
        if execution_settings_file.exists():
            with open(execution_settings_file, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                log_level = settings.get('log_level', 'DEBUG').upper()
                max_log_size_mb = settings.get('max_log_size_mb', 10)
                return log_level, max_log_size_mb
    except Exception:
        pass
    
    # Fallback: переменные окружения или значения по умолчанию
    log_level = os.environ.get('LOG_LEVEL', 'DEBUG').upper()
    max_log_size_mb = int(os.environ.get('MAX_LOG_SIZE_MB', '10'))
    return log_level, max_log_size_mb

# Валидация уровней логирования
VALID_LOG_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}

def validate_log_level(level_name: str) -> bool:
    """
    Валидирует уровень логирования
    
    Args:
        level_name: строка с именем уровня
        
    Returns:
        True если уровень валидный, False иначе
    """
    return level_name.upper() in VALID_LOG_LEVELS

# Получаем настройки логирования
LOG_LEVEL_ENV, MAX_LOG_SIZE_MB = get_logging_settings()

# Валидируем уровень логирования
if not validate_log_level(LOG_LEVEL_ENV):
    import warnings
    warnings.warn(f"Invalid log level '{LOG_LEVEL_ENV}', using DEBUG. Valid levels: {', '.join(VALID_LOG_LEVELS)}")
    LOG_LEVEL_ENV = 'DEBUG'

LOG_LEVEL_MAP = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}
LOG_LEVEL = LOG_LEVEL_MAP.get(LOG_LEVEL_ENV, logging.DEBUG)

# Унифицированный формат логирования: [TIMESTAMP] LEVEL: message
UNIFIED_LOG_FORMAT = '[%(asctime)s] %(levelname)s: %(message)s'

# Настройка handlers с ротацией для файла
file_handler = RotatingFileHandler(
    LOG_FILE,
    encoding='utf-8',
    maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024,  # Конвертируем MB в bytes
    backupCount=5  # Храним 5 резервных копий
)
file_handler.setLevel(LOG_LEVEL)
file_formatter = logging.Formatter(
    UNIFIED_LOG_FORMAT,
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(file_formatter)

# Консольный handler
console_handler = logging.StreamHandler()
console_handler.setLevel(LOG_LEVEL)
console_formatter = logging.Formatter(
    UNIFIED_LOG_FORMAT,
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(console_formatter)

# Настраиваем root logger
logging.basicConfig(
    level=LOG_LEVEL,
    format=UNIFIED_LOG_FORMAT,
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[file_handler, console_handler],
    force=True  # Переопределяем существующую конфигурацию
)

logger = logging.getLogger(__name__)

# Текущий уровень (для проверки изменений при reload)
_current_log_level = LOG_LEVEL

# Функция для динамического обновления уровня логирования
def reload_log_level():
    """
    Перезагружает уровень логирования из execution_settings.json
    и применяет его к handlers без перезапуска worker.
    Логирует только при реальном изменении уровня.
    """
    global _current_log_level
    try:
        log_level_str, max_log_size_mb = get_logging_settings()
        
        if not validate_log_level(log_level_str):
            logger.warning(f"Invalid log level '{log_level_str}', keeping current level")
            return False
        
        level = LOG_LEVEL_MAP.get(log_level_str.upper(), logging.DEBUG)
        
        if level == _current_log_level:
            return True  # Уровень не изменился — не спамим лог
        
        # Обновляем уровень для всех handlers
        file_handler.setLevel(level)
        console_handler.setLevel(level)
        logging.root.setLevel(level)
        _current_log_level = level
        
        logger.info(f"Log level reloaded to: {log_level_str.upper()}")
        return True
    except Exception as e:
        logger.error(f"Error reloading log level: {e}", exc_info=True)
        return False
