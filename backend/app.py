#!/usr/bin/env python3
"""
Веб-сервис для конфигурации Ansible переменных
"""
import os
import yaml
import re
import subprocess
import threading
import uuid
import time
import logging
from pathlib import Path
from io import StringIO, BytesIO
import fcntl
from flask import Flask, request, jsonify, Response, stream_with_context, send_file
import zipfile
import tarfile
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, Tuple

# Import GitSourceManager
try:
    from .git_source_manager import GitSourceManager, GitSourceError
except ImportError:
    from git_source_manager import GitSourceManager, GitSourceError

# Import Playbook modules
try:
    from .playbook_storage import PlaybookStorage
    from .playbook_parser import PlaybookParser, PlaybookParseError
    from .playbook_generator import PlaybookGenerator
    from .playbook_validator import PlaybookValidator
except ImportError:
    from playbook_storage import PlaybookStorage
    from playbook_parser import PlaybookParser, PlaybookParseError
    from playbook_generator import PlaybookGenerator
    from playbook_validator import PlaybookValidator

try:
    from .vault_utils import (
        is_ansible_vault_encrypted,
        parse_vault_id_from_header,
        ansible_vault_decrypt,
        ansible_vault_encrypt
    )
except ImportError:
    from vault_utils import (
        is_ansible_vault_encrypted,
        parse_vault_id_from_header,
        ansible_vault_decrypt,
        ansible_vault_encrypt
    )

app = Flask(__name__)

# Настройка CORS для работы фронтенда на отдельном порту
try:
    from flask_cors import CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)
except ImportError:
    # Если flask-cors не установлен, выводим предупреждение
    import warnings
    warnings.warn("flask-cors not installed. CORS support disabled. Install with: pip install flask-cors")

# Путь к корню проекта (в Docker: app в /app → parent=/app; иначе backend/ → parent.parent=корень)
_app_dir = Path(__file__).resolve().parent
BASE_DIR = _app_dir.parent if _app_dir.name == 'backend' else _app_dir

# Базовая директория для всех данных (в Docker задаётся DATA_DIR=/app/data)
DATA_DIR = Path(os.environ.get('DATA_DIR', str(BASE_DIR / 'data')))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Настройка логирования в файл
LOG_DIR = DATA_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'backend.log'

# Унифицированный формат логирования: [TIMESTAMP] LEVEL: message
UNIFIED_LOG_FORMAT = '[%(asctime)s] %(levelname)s: %(message)s'

# Функция для получения настроек логирования из execution_settings.json
# (Определяется после EXECUTION_SETTINGS_FILE, но вызывается позже)
def get_logging_settings():
    """Получает настройки логирования из execution_settings.json или переменных окружения"""
    # Сначала пытаемся загрузить из execution_settings.json
    try:
        execution_settings_file = DATA_DIR / 'execution_settings.json'
        if execution_settings_file.exists():
            with open(execution_settings_file, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                log_level = settings.get('log_level', 'INFO').upper()
                max_log_size_mb = settings.get('max_log_size_mb', 10)
                return log_level, max_log_size_mb
    except Exception:
        pass
    
    # Fallback: переменные окружения или значения по умолчанию
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    max_log_size_mb = int(os.environ.get('MAX_LOG_SIZE_MB', '10'))
    return log_level, max_log_size_mb

# Получаем настройки логирования
LOG_LEVEL_ENV, MAX_LOG_SIZE_MB = get_logging_settings()
LOG_LEVEL_MAP = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}
LOG_LEVEL = LOG_LEVEL_MAP.get(LOG_LEVEL_ENV, logging.DEBUG)

# Настройка файлового handler для логирования с ротацией
from logging.handlers import RotatingFileHandler
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

# Настройка консольного handler
console_handler = logging.StreamHandler()
# Консоль использует INFO по умолчанию, но можно переопределить через LOG_LEVEL
console_level = LOG_LEVEL if LOG_LEVEL >= logging.INFO else logging.INFO
console_handler.setLevel(console_level)
console_formatter = logging.Formatter(
    UNIFIED_LOG_FORMAT,
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(console_formatter)

# Добавляем handlers к logger приложения
app.logger.setLevel(LOG_LEVEL)
app.logger.addHandler(file_handler)
app.logger.addHandler(console_handler)

# Настраиваем root logger для всех модулей (включая source_sync_service)
root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Отключаем логирование Werkzeug по умолчанию (будет логироваться через наш handler)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Настройка logger для frontend логов
FRONTEND_LOG_FILE = LOG_DIR / 'frontend.log'
frontend_file_handler = RotatingFileHandler(
    FRONTEND_LOG_FILE,
    encoding='utf-8',
    maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024,  # Конвертируем MB в bytes
    backupCount=5  # Храним 5 резервных копий
)
frontend_file_handler.setLevel(LOG_LEVEL)
frontend_file_handler.setFormatter(file_formatter)

frontend_logger = logging.getLogger('frontend')
frontend_logger.setLevel(LOG_LEVEL)
frontend_logger.addHandler(frontend_file_handler)
frontend_logger.propagate = False  # Не пропагируем в root logger


def safe_log_error(message, exception=None, context=None):
    """
    Безопасное логирование ошибок без чувствительных данных.
    Фильтрует пароли, приватные ключи и другие секреты из сообщений об ошибках.
    """
    # Список ключей, которые содержат чувствительные данные
    SENSITIVE_KEYS = ['password', 'privateKey', 'private_key', 'passphrase', 'secret', 
                      'api_key', 'apiKey', 'token', 'access_token', 'refresh_token']
    
    safe_message = str(message)
    
    # Если есть контекст (например, request.json), фильтруем чувствительные данные
    if context and isinstance(context, dict):
        safe_context = {}
        for key, value in context.items():
            if any(sensitive in key.lower() for sensitive in SENSITIVE_KEYS):
                safe_context[key] = '***REDACTED***'
            else:
                safe_context[key] = value
        # Не логируем контекст напрямую, только используем для фильтрации сообщения
        safe_message = safe_message.replace(str(context), str(safe_context))
    
    # Фильтруем чувствительные данные из сообщения об ошибке
    for sensitive_key in SENSITIVE_KEYS:
        # Ищем паттерны типа "password: xxx" или "privateKey=xxx"
        import re
        patterns = [
            rf'{sensitive_key}\s*[:=]\s*["\']?([^"\'\s,}}]+)["\']?',
            rf'"{sensitive_key}"\s*:\s*"([^"]+)"',
            rf"'{sensitive_key}'\s*:\s*'([^']+)'"
        ]
        for pattern in patterns:
            safe_message = re.sub(pattern, f'{sensitive_key}=***REDACTED***', safe_message, flags=re.IGNORECASE)
    
    if exception:
        # Логируем только тип и сообщение исключения, без traceback
        error_type = type(exception).__name__
        error_msg = str(exception)
        # Фильтруем чувствительные данные из сообщения об ошибке
        for sensitive_key in SENSITIVE_KEYS:
            error_msg = re.sub(rf'{sensitive_key}\s*[:=]\s*["\']?([^"\'\s,}}]+)["\']?', 
                              f'{sensitive_key}=***REDACTED***', error_msg, flags=re.IGNORECASE)
        return f"{safe_message}: {error_type}: {error_msg}"
    
    return safe_message


def safe_write_json_with_lock(file_path, data, timeout=5):
    """
    Безопасная запись JSON файла с файловой блокировкой.
    Предотвращает race condition при одновременной записи.
    
    Args:
        file_path: Path к файлу
        data: Данные для записи (dict)
        timeout: Таймаут ожидания блокировки в секундах
        
    Returns:
        bool: True если запись успешна, False в случае ошибки
        
    Raises:
        IOError: Если не удалось получить блокировку в течение timeout
    """
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    
    lock_file_path = file_path.with_suffix(file_path.suffix + '.lock')
    start_time = time.time()
    
    # Пытаемся получить эксклюзивную блокировку
    lock_acquired = False
    lock_file = None
    
    try:
        # Создаем lock файл если его нет
        lock_file_path.touch(exist_ok=True)
        
        # Открываем lock файл для записи
        lock_file = open(lock_file_path, 'w')
        
        # Пытаемся получить эксклюзивную блокировку с таймаутом
        while time.time() - start_time < timeout:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_acquired = True
                break
            except IOError:
                # Блокировка занята, ждем немного и пробуем снова
                time.sleep(0.1)
        
        if not lock_acquired:
            raise IOError(f"Could not acquire lock for {file_path} within {timeout} seconds")
        
        # Блокировка получена, записываем данные
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        return True
        
    except Exception as e:
        app.logger.error(f"Error writing file with lock {file_path}: {e}")
        raise
    finally:
        # Освобождаем блокировку
        if lock_file and lock_acquired:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except:
                pass
            lock_file.close()


def safe_read_json_with_lock(file_path, timeout=5):
    """
    Безопасное чтение JSON файла с файловой блокировкой.
    Предотвращает чтение файла во время его записи.
    
    Args:
        file_path: Path к файлу
        timeout: Таймаут ожидания блокировки в секундах
        
    Returns:
        dict: Данные из файла или None в случае ошибки
    """
    file_path = Path(file_path)
    
    if not file_path.exists():
        return None
    
    lock_file_path = file_path.with_suffix(file_path.suffix + '.lock')
    start_time = time.time()
    
    lock_acquired = False
    lock_file = None
    
    try:
        # Если lock файл существует, пытаемся получить shared lock
        if lock_file_path.exists():
            lock_file = open(lock_file_path, 'r')
            
            # Пытаемся получить shared блокировку с таймаутом
            while time.time() - start_time < timeout:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    lock_acquired = True
                    break
                except IOError:
                    # Блокировка занята, ждем немного и пробуем снова
                    time.sleep(0.1)
        
        # Читаем файл
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        return data
        
    except Exception as e:
        app.logger.error(f"Error reading file with lock {file_path}: {e}")
        return None
    finally:
        # Освобождаем блокировку
        if lock_file and lock_acquired:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except:
                pass
            lock_file.close()


def validate_ssh_private_key(private_key):
    """
    Валидация формата SSH приватного ключа.
    Проверяет наличие BEGIN/END маркеров и базовую структуру.
    
    Args:
        private_key: Строка с приватным ключом
        
    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    if not private_key or not isinstance(private_key, str):
        return False, "Private key must be a non-empty string"
    
    private_key = private_key.strip()
    
    # Минимальная длина для валидного ключа (даже самый короткий ключ должен быть больше)
    if len(private_key) < 100:
        return False, "Private key is too short to be valid"
    
    # Проверка наличия BEGIN маркера
    begin_markers = [
        '-----BEGIN RSA PRIVATE KEY-----',
        '-----BEGIN OPENSSH PRIVATE KEY-----',
        '-----BEGIN EC PRIVATE KEY-----',
        '-----BEGIN DSA PRIVATE KEY-----',
        '-----BEGIN PRIVATE KEY-----'
    ]
    
    has_begin = any(marker in private_key for marker in begin_markers)
    if not has_begin:
        return False, "Private key must start with '-----BEGIN ... PRIVATE KEY-----'"
    
    # Проверка наличия END маркера
    end_markers = [
        '-----END RSA PRIVATE KEY-----',
        '-----END OPENSSH PRIVATE KEY-----',
        '-----END EC PRIVATE KEY-----',
        '-----END DSA PRIVATE KEY-----',
        '-----END PRIVATE KEY-----'
    ]
    
    has_end = any(marker in private_key for marker in end_markers)
    if not has_end:
        return False, "Private key must end with '-----END ... PRIVATE KEY-----'"
    
    # Проверка, что BEGIN и END маркеры соответствуют друг другу
    # Находим BEGIN маркер
    begin_marker = None
    for marker in begin_markers:
        if marker in private_key:
            begin_marker = marker
            break
    
    # Находим соответствующий END маркер
    if begin_marker:
        # Извлекаем тип ключа из BEGIN маркера
        key_type = begin_marker.replace('-----BEGIN ', '').replace('-----', '')
        expected_end = f'-----END {key_type}-----'
        
        if expected_end not in private_key:
            return False, f"Private key END marker does not match BEGIN marker. Expected '{expected_end}'"
    
    # Проверка базовой структуры: BEGIN должен быть в начале (или после whitespace)
    # и END должен быть в конце (или перед whitespace)
    begin_pos = private_key.find('-----BEGIN')
    end_pos = private_key.find('-----END')
    
    if begin_pos == -1 or end_pos == -1:
        return False, "Private key structure is invalid"
    
    if end_pos <= begin_pos:
        return False, "Private key END marker must come after BEGIN marker"
    
    # Проверка, что между BEGIN и END есть содержимое
    content_start = private_key.find('-----', begin_pos + 1)
    if content_start == -1 or content_start >= end_pos:
        return False, "Private key must have content between BEGIN and END markers"
    
    # Проверка на наличие базовых символов base64 между маркерами
    # (ключ должен содержать base64-encoded данные)
    content = private_key[content_start + 5:end_pos].strip()
    if not content:
        return False, "Private key content between markers is empty"
    
    # Базовая проверка на base64 символы (не строгая, но помогает отсечь явно неверные ключи)
    base64_pattern = re.compile(r'^[A-Za-z0-9+/=\s]+$')
    if not base64_pattern.match(content):
        return False, "Private key content contains invalid characters (expected base64)"
    
    return True, None


def normalize_pem_key_for_file(content):
    """
    Нормализует содержимое PEM ключа перед записью в файл.
    OpenSSH/libcrypto даёт "error in libcrypto" при Windows-переводах строк или лишних символах.
    """
    if not content or not isinstance(content, str):
        return content or ''
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    content = content.rstrip('\n')
    if content and not content.endswith('\n'):
        content = content + '\n'
    return content


# Путь к корню проекта (в Docker: app в /app → parent=/app; иначе backend/ → parent.parent=корень)
_app_dir = Path(__file__).resolve().parent
BASE_DIR = _app_dir.parent if _app_dir.name == 'backend' else _app_dir

# Базовая директория для всех данных (в Docker задаётся DATA_DIR=/app/data)
DATA_DIR = Path(os.environ.get('DATA_DIR', str(BASE_DIR / 'data')))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Конфигурационные файлы
EXECUTION_SETTINGS_FILE = DATA_DIR / 'execution_settings.json'
ROLES_CONFIG_FILE = DATA_DIR / 'roles-config.json'
PROJECTS_CONFIG_FILE = DATA_DIR / 'projects.json'
BACKUP_SETTINGS_FILE = DATA_DIR / 'backup_settings.json'

# ==================== Host status cache (in-memory, TTL-based, per-project) ====================
HOST_STATUS_TTL_DEFAULT = 300  # seconds (5 minutes)

# Кэш статуса хостов в памяти процесса backend:
# ключ: (project_id, host_name) → значение: {'status': str, 'last_checked_ts': float, 'expires_at_ts': float}
HOST_STATUS_CACHE = {}


def get_host_status_ttl_seconds(project_id: Optional[str] = None) -> int:
    """
    Возвращает TTL для статуса хоста в секундах.
    Приоритет:
      1) project.json конкретного проекта: host_status.ttl_seconds или host_status_ttl_seconds
      2) переменная окружения HOST_STATUS_TTL
      3) значение по умолчанию HOST_STATUS_TTL_DEFAULT
    """
    # 1) Пытаемся прочитать настройки проекта
    if project_id:
        try:
            config = load_project_config(project_id) or {}
            host_status_cfg = config.get('host_status', {})
            ttl = host_status_cfg.get('ttl_seconds')
            if ttl is None:
                ttl = config.get('host_status_ttl_seconds')
            if isinstance(ttl, (int, float)) and ttl > 0:
                return int(ttl)
        except Exception:
            # Не критично, просто переходим к окружению/дефолту
            pass

    # 2) Переменная окружения (глобальный дефолт)
    env_ttl = os.environ.get('HOST_STATUS_TTL')
    if env_ttl:
        try:
            ttl_env = int(env_ttl)
            if ttl_env > 0:
                return ttl_env
        except ValueError:
            pass

    # 3) Значение по умолчанию
    return HOST_STATUS_TTL_DEFAULT


def set_host_check_status(project_id: str, host: str, status: str):
    """
    Сохраняет результат проверки хоста в in-memory кэш с учетом TTL.

    status:
      - 'online'
      - 'offline'
      - 'checking'
      - 'unknown'
    """
    if not project_id or not host:
        return

    ttl = get_host_status_ttl_seconds(project_id)
    now_ts = time.time()
    expires_at_ts = now_ts + ttl if ttl > 0 else now_ts

    key = (str(project_id), str(host))
    HOST_STATUS_CACHE[key] = {
        'status': status,
        'last_checked_ts': now_ts,
        'expires_at_ts': expires_at_ts,
    }

    # Также сохраняем в файл host_status.json, чтобы переживать рестарты
    try:
        status_file = get_project_host_status_file(project_id)
        existing = {}
        if status_file.exists():
            try:
                with open(status_file, 'r', encoding='utf-8') as f:
                    existing = json.load(f) or {}
            except Exception:
                existing = {}
        hosts_map = existing.get('hosts') or {}
        hosts_map[host] = {
            'status': status,
            'last_checked_at': datetime.utcfromtimestamp(now_ts).isoformat() + 'Z',
            'status_expires_at': datetime.utcfromtimestamp(expires_at_ts).isoformat() + 'Z',
        }
        data_to_save = {'hosts': hosts_map}
        # Используем безопасную запись с блокировкой
        safe_write_json_with_lock(status_file, data_to_save)
    except Exception as e:
        app.logger.warning(f"Failed to persist host status for project {project_id}, host {host}: {e}")


def get_host_check_status(project_id: str, host: str):
    """
    Возвращает сериализованный статус хоста. Данные берутся из кэша/файла (то же хранилище, куда пишет set_host_check_status).
    При истёкшем TTL всё равно возвращаем последний сохранённый статус (online/offline), чтобы после рефреша он не пропадал;
    поле expired=True даёт понять фронту, что статус устарел.

    Формат:
    {
        'status': 'online' | 'offline' | 'checking' | 'unknown',
        'last_checked_at': ISO-строка UTC,
        'status_expires_at': ISO-строка UTC,
        'expired': True если TTL истёк (опционально)
    }
    """
    if not project_id or not host:
        return None

    key = (str(project_id), str(host))
    data = HOST_STATUS_CACHE.get(key)

    # Если в памяти данных нет, пробуем загрузить из host_status.json
    if not data:
        try:
            status_file = get_project_host_status_file(project_id)
            if status_file.exists():
                with open(status_file, 'r', encoding='utf-8') as f:
                    file_data = json.load(f) or {}
                host_entry = (file_data.get('hosts') or {}).get(host)
                if host_entry:
                    # Преобразуем ISO-времена в timestamp
                    last_checked_at_str = host_entry.get('last_checked_at')
                    status_expires_at_str = host_entry.get('status_expires_at')
                    last_checked_ts = None
                    expires_at_ts = None
                    if last_checked_at_str:
                        try:
                            last_checked_ts = datetime.fromisoformat(last_checked_at_str.replace('Z', '+00:00')).timestamp()
                        except Exception:
                            last_checked_ts = None
                    if status_expires_at_str:
                        try:
                            expires_at_ts = datetime.fromisoformat(status_expires_at_str.replace('Z', '+00:00')).timestamp()
                        except Exception:
                            expires_at_ts = None
                    if last_checked_ts is None:
                        last_checked_ts = time.time()
                    if expires_at_ts is None:
                        ttl = get_host_status_ttl_seconds(project_id)
                        expires_at_ts = last_checked_ts + ttl

                    data = {
                        'status': host_entry.get('status', 'unknown'),
                        'last_checked_ts': last_checked_ts,
                        'expires_at_ts': expires_at_ts,
                    }
                    HOST_STATUS_CACHE[key] = data
        except Exception as e:
            app.logger.warning(f"Failed to load host status from file for project {project_id}, host {host}: {e}")

    if not data:
        return None

    now_ts = time.time()
    expires_at_ts = data.get('expires_at_ts') or 0
    expired = expires_at_ts <= now_ts

    last_checked_ts = data.get('last_checked_ts') or now_ts
    last_checked_at = datetime.utcfromtimestamp(last_checked_ts).isoformat() + 'Z'
    status_expires_at = datetime.utcfromtimestamp(expires_at_ts).isoformat() + 'Z'

    out = {
        'status': data.get('status', 'unknown'),
        'last_checked_at': last_checked_at,
        'status_expires_at': status_expires_at,
    }
    if expired:
        out['expired'] = True
    return out

# Директории для хранения данных
PROJECTS_DIR = DATA_DIR / 'projects'  # Проекты хранятся в data/projects/
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Кэш, временные файлы, логи, бэкапы и глобальные настройки в data/
GIT_CACHE_DIR = DATA_DIR / 'cache' / 'git'
GIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

TEMP_DIR = DATA_DIR / 'temp'
TEMP_DIR.mkdir(parents=True, exist_ok=True)

BACKUPS_DIR = DATA_DIR / 'backups'
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
# Полные архивы проектов (tar.gz) для Backup Now / Restore
BACKUP_ARCHIVES_DIR = BACKUPS_DIR / 'archives'
BACKUP_ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

GLOBAL_SECRETS_DIR = DATA_DIR / 'global' / 'secrets'
GLOBAL_SECRETS_DIR.mkdir(parents=True, exist_ok=True)

EXECUTIONS_LEGACY_DIR = DATA_DIR / 'executions' / 'legacy'
EXECUTIONS_LEGACY_DIR.mkdir(parents=True, exist_ok=True)

# Initialize GitSourceManager (pass DATA_DIR for global secrets)
git_source_manager = GitSourceManager(GIT_CACHE_DIR, PROJECTS_DIR, DATA_DIR)

# Initialize SourceSyncService
try:
    from .source_sync_service import SourceSyncService
except ImportError:
    from source_sync_service import SourceSyncService
source_sync_service = SourceSyncService(PROJECTS_DIR, git_source_manager)

# Initialize Playbook modules
playbook_storage = PlaybookStorage(PROJECTS_DIR)
playbook_parser = PlaybookParser()
playbook_generator = PlaybookGenerator()

# Initialize AutosyncScheduler (will be initialized after load_project_config is defined)
autosync_scheduler = None

# Feature flag for Project Sources
PROJECT_SOURCES_ENABLED = os.environ.get('PROJECT_SOURCES_ENABLED', 'true').lower() == 'true'

# Инициализация ruamel.yaml для сохранения комментариев
yaml_loader = YAML()
yaml_loader.preserve_quotes = True
yaml_loader.width = 4096

# Initialize Global Secrets Manager
try:
    from global_secrets_manager import GlobalSecretsManager, GlobalSecretError
    global_secrets_manager = GlobalSecretsManager(DATA_DIR)
except ImportError:
    # If module not available, set to None (endpoints will handle gracefully)
    global_secrets_manager = None
    GlobalSecretError = Exception

# ============================================================================
# AUTHENTICATION & RBAC INITIALIZATION
# ============================================================================

# Import authentication modules
try:
    from .auth import generate_token, add_token_to_blacklist, verify_token
    from .auth_middleware import require_auth, require_optional_auth, set_data_dir, set_access_control_service
    from .user_service import UserService
    from .role_service import RoleService, PermissionService
    from .permission_service import AccessControlService
    from .auth_validators import validate_username, validate_password, validate_email, validate_role_name
    from .auth_seed import seed_default_user, seed_default_roles
except ImportError:
    from auth import generate_token, add_token_to_blacklist, verify_token
    from auth_middleware import require_auth, require_optional_auth, set_data_dir, set_access_control_service
    from user_service import UserService
    from role_service import RoleService, PermissionService
    from permission_service import AccessControlService
    from auth_validators import validate_username, validate_password, validate_email, validate_role_name
    from auth_seed import seed_default_user, seed_default_roles

# Set DATA_DIR in auth_middleware
set_data_dir(DATA_DIR)

# Initialize authentication services
user_service = UserService(DATA_DIR)
role_service = RoleService(DATA_DIR)
permission_service = PermissionService(DATA_DIR)
access_control_service = AccessControlService(DATA_DIR)

# Set access_control_service in auth_middleware for @require_permission decorator
set_access_control_service(access_control_service)

# ============================================================================
# RBAC PLACEHOLDER FUNCTIONS
# ============================================================================

def can_read_global_secrets():
    """
    Check if current user can read global secrets.
    
    Returns:
        bool: True if user has read permission, False otherwise
        
    Note: This is a placeholder. In production, implement proper RBAC checks.
    """
    # TODO: Implement proper RBAC check
    # For now, return True (all authenticated users can read)
    return True

def can_write_global_secrets():
    """
    Check if current user can create/update/delete global secrets.
    
    Returns:
        bool: True if user has write permission, False otherwise
        
    Note: This is a placeholder. In production, implement proper RBAC checks.
    """
    # TODO: Implement proper RBAC check
    # For now, return True (all authenticated users can write)
    # In production, check user permissions: global_secrets:create, global_secrets:update, global_secrets:delete
    return True


def validate_yaml_content(content):
    """
    Валидирует YAML содержимое перед сохранением.
    
    Args:
        content: строка с YAML содержимым или словарь/объект для сериализации
        
    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    try:
        # Если content - строка, парсим её
        if isinstance(content, str):
            if not content.strip():
                return True, None  # Пустая строка считается валидной
            
            # Пробуем распарсить через ruamel.yaml
            test_loader = YAML()
            test_loader.preserve_quotes = True
            test_loader.width = 4096
            
            # Парсим в StringIO для проверки
            test_loader.load(StringIO(content))
            
            return True, None
        else:
            # Если content - объект (dict, list), пробуем сериализовать
            test_loader = YAML()
            test_loader.preserve_quotes = True
            test_loader.width = 4096
            
            # Пробуем сериализовать в StringIO
            stream = StringIO()
            test_loader.dump(content, stream)
            
            # Пробуем распарсить обратно для проверки
            stream.seek(0)
            test_loader.load(stream)
            
            return True, None
    except Exception as e:
        # ruamel.yaml и PyYAML выбрасывают разные исключения
        # Обрабатываем оба случая
        error_msg = str(e)
        
        # Пробуем извлечь информацию о позиции ошибки
        if hasattr(e, 'problem_mark'):
            mark = e.problem_mark
            error_msg = f"YAML syntax error at line {mark.line + 1}, column {mark.column + 1}: {error_msg}"
        elif hasattr(e, 'context_mark'):
            mark = e.context_mark
            error_msg = f"YAML syntax error at line {mark.line + 1}, column {mark.column + 1}: {error_msg}"
        elif 'line' in error_msg.lower() or 'column' in error_msg.lower():
            # Сообщение уже содержит информацию о позиции
            pass
        else:
            error_msg = f"YAML validation error: {error_msg}"
        
        return False, error_msg


def parse_yaml_with_comments(file_path):
    """Парсит YAML файл с сохранением комментариев"""
    try:
        if not file_path.exists():
            return {}
        with open(file_path, 'r', encoding='utf-8') as f:
            data = yaml_loader.load(f)
        return data if data is not None else {}
    except Exception as e:
        return {'_error': str(e)}


def get_variable_descriptions():
    """Загружает описания переменных из эталонного файла-шаблона"""
    descriptions = {}
    try:
        # Путь к файлу с описаниями (эталонный шаблон)
        descriptions_file = Path(__file__).parent / 'variable_descriptions.yml'
        
        if not descriptions_file.exists():
            app.logger.warning(f"Descriptions file not found: {descriptions_file}")
            return descriptions
        
        # Загружаем описания из YAML файла
        with open(descriptions_file, 'r', encoding='utf-8') as f:
            data = yaml_loader.load(f)
        
        if data:
            # Преобразуем многострочные строки в обычные строки
            for key, value in data.items():
                if isinstance(value, str):
                    descriptions[key] = value.strip()
                else:
                    descriptions[key] = str(value).strip()
        
        return descriptions
    except Exception as e:
        app.logger.error(f"Error loading descriptions: {e}", exc_info=True)
        return {}


# ============================================================================
# PROJECT MANAGEMENT
# ============================================================================

def load_projects():
    """Загружает список проектов из файла"""
    try:
        if not PROJECTS_CONFIG_FILE.exists():
            return []
        with open(PROJECTS_CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('projects', [])
    except Exception as e:
        app.logger.error(f"Error loading projects: {e}", exc_info=True)
        return []


def save_projects(projects):
    """Сохраняет список проектов в файл"""
    try:
        data = {'projects': projects}
        with open(PROJECTS_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        app.logger.error(f"Error saving projects: {e}", exc_info=True)
        return False


def get_default_project_id():
    """Возвращает ID проекта по умолчанию"""
    projects = load_projects()
    default_project = next((p for p in projects if p.get('name') == 'Default Project'), None)
    if default_project:
        return default_project['id']
    return None


def _initialize_projects():
    """Инициализирует проекты при старте приложения
    
    НЕ создаёт проекты автоматически. Только создаёт директории для существующих проектов.
    """
    try:
        projects = load_projects()
        
        # Инициализируем директории для всех существующих проектов
        for project in projects:
            project_id = project.get('id')
            if not project_id:
                continue
            
            project_dir = get_project_dir(project_id)
            project_dir.mkdir(exist_ok=True)
            
            # Создаем поддиректории для каждого проекта (новая структура)
            # repo/ - Git-synced workspace
            repo_dir = project_dir / 'repo'
            repo_dir.mkdir(exist_ok=True)
            (repo_dir / 'roles').mkdir(exist_ok=True)
            (repo_dir / 'playbooks').mkdir(exist_ok=True)
            (repo_dir / 'inventories').mkdir(exist_ok=True)
            (repo_dir / 'group_vars').mkdir(exist_ok=True)  # optional shared
            (repo_dir / 'host_vars').mkdir(exist_ok=True)    # optional shared
            (repo_dir / 'scripts').mkdir(exist_ok=True)
            
            # НЕ создаем папку prod автоматически - она должна создаваться только пользователем или из Git
            # prod_env_dir = repo_dir / 'inventories' / 'prod'
            # prod_env_dir.mkdir(parents=True, exist_ok=True)
            # (prod_env_dir / 'group_vars').mkdir(exist_ok=True)
            # (prod_env_dir / 'host_vars').mkdir(exist_ok=True)
            
            # ui/ - UI definitions
            ui_dir = project_dir / 'ui'
            ui_dir.mkdir(exist_ok=True)
            (ui_dir / 'playbooks').mkdir(exist_ok=True)
            
            # runtime/ - generated / ephemeral
            runtime_dir = project_dir / 'runtime'
            runtime_dir.mkdir(exist_ok=True)
            (runtime_dir / 'generated_playbooks').mkdir(exist_ok=True)
            (runtime_dir / 'inventory_snapshots').mkdir(exist_ok=True)
            (runtime_dir / 'artifacts').mkdir(exist_ok=True)
            
            # history/ - immutable execution history
            history_dir = project_dir / 'history'
            history_dir.mkdir(exist_ok=True)
            (history_dir / 'executions').mkdir(exist_ok=True)
            (history_dir / 'logs').mkdir(exist_ok=True)
            
            # secrets/ - credentials store
            secrets_dir = project_dir / 'secrets'
            secrets_dir.mkdir(exist_ok=True)
            (secrets_dir / 'ssh_keys').mkdir(exist_ok=True)
            (secrets_dir / 'vault').mkdir(exist_ok=True)
            (secrets_dir / 'vault_keys').mkdir(exist_ok=True)
            (secrets_dir / 'git_auth').mkdir(exist_ok=True)
        
        # Проверяем и создаем папку ansible-config для всех существующих проектов
        _ensure_ansible_config_directory_for_all_projects()
        
        projects_count = len([p for p in projects if not p.get('isArchived', False)])
        app.logger.info(f"Projects initialized. Total active projects: {projects_count}")
    except Exception as e:
        app.logger.error(f"Error initializing projects: {e}", exc_info=True)


def _ensure_ansible_config_directory_for_all_projects():
    """Проверяет и создает папку ansible-config для всех существующих проектов"""
    try:
        projects = load_projects()
        created_count = 0
        
        for project in projects:
            project_id = project.get('id')
            if not project_id:
                continue
            
            project_dir = get_project_dir(project_id)
            ansible_config_dir = project_dir / 'ansible-config'
            
            if not ansible_config_dir.exists():
                ansible_config_dir.mkdir(parents=True, exist_ok=True)
                created_count += 1
                app.logger.debug(f"Created ansible-config directory for project {project_id} ({project.get('name', 'Unknown')})")
        
        if created_count > 0:
            app.logger.debug(f"Created ansible-config directory for {created_count} project(s)")
    except Exception as e:
        app.logger.error(f"Error ensuring ansible-config directory for all projects: {e}", exc_info=True)


def get_project_dir(project_id):
    """Возвращает путь к директории проекта"""
    return PROJECTS_DIR / project_id


def resolve_ansible_config_path(project_id, selected_ansible_config):
    """Единственное место хранения: ansible-config/ansible.cfg (и другие .cfg в ansible-config/)."""
    project_dir = get_project_dir(project_id)
    if selected_ansible_config.startswith('ansible-config/'):
        return project_dir / selected_ansible_config
    return project_dir / 'ansible-config' / (selected_ansible_config or 'ansible.cfg')


def create_minimal_ansible_config(config_path):
    """Создает минимальный ansible config файл, если его нет"""
    try:
        # Создаем директорию, если её нет
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Минимальный конфиг для запуска
        minimal_config = """[defaults]
executable = /bin/bash
allow_world_readable_tmpfiles = true
stdout_callback = default
roles_path = roles
inventory = inventory.yml
host_key_checking = false
forks = 50
gather_facts = False
gathering = explicit
interpreter_python = /usr/bin/python3
interpreter_discovery = false

[ssh_connection]
pipelining = True
ssh_args = -C -o ControlMaster=auto -o ControlPersist=30m
"""
        
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(minimal_config)
        
        app.logger.debug(f"Created minimal ansible config at {config_path}")
        return True
    except Exception as e:
        app.logger.error(f"Error creating minimal ansible config at {config_path}: {e}", exc_info=True)
        return False


def get_repo_layout(project_id: str) -> dict:
    """
    Get repo layout configuration with defaults.
    
    Returns layout paths for Ansible entities. If repoLayout is not configured,
    returns default values.
    
    Args:
        project_id: Project ID
    
    Returns:
        dict with keys: 'playbooks', 'roles', 'inventories', 'vars'
        Default values: 'playbooks', 'roles', 'inventories', 'vars'
    """
    config = load_project_config(project_id)
    layout = config.get('repoLayout', {})
    
    return {
        'playbooks': layout.get('playbooks', 'playbooks'),
        'roles': layout.get('roles', 'roles'),
        'inventories': layout.get('inventories', 'inventories')
    }


def validate_repo_layout_path(path: str) -> Tuple[bool, Optional[str]]:
    """
    Validate repo layout path.
    
    Rules:
    - Must be relative (not absolute)
    - Cannot contain path traversal (..)
    - Cannot start or end with /
    - Cannot be empty
    
    Args:
        path: Path string to validate
    
    Returns:
        (is_valid, error_message)
        is_valid: True if path is valid, False otherwise
        error_message: Error description if invalid, None if valid
    """
    if not path:
        return False, 'Path cannot be empty'
    
    # Reject absolute paths
    if Path(path).is_absolute():
        return False, 'Path must be relative, not absolute'
    
    # Reject path traversal
    if '..' in path:
        return False, 'Path cannot contain .. (path traversal)'
    
    # Reject leading/trailing slashes (normalize)
    if path.startswith('/') or path.endswith('/'):
        return False, 'Path cannot start or end with /'
    
    # Reject Windows-style absolute paths
    if len(path) >= 2 and path[1] == ':':
        return False, 'Path must be relative, not absolute (Windows path detected)'
    
    return True, None


def resolve_repo_path(project_id: str, entity_type: str) -> Path:
    """
    Resolve absolute path to entity directory in Project Storage.
    
    CRITICAL: Always returns LOCAL Project Storage path (e.g., 'inventories'),
    NOT the Git repository path from repoLayout. The repoLayout paths are only
    used during Git sync to map between Git and local paths.
    
    Takes into account:
    1. Base path: data/projects/<project_id>/repo/
    2. Subdir from sources.repo.git.subdir (if specified)
    3. Local entity path (always uses default: 'playbooks', 'roles', 'inventories')
    
    Args:
        project_id: Project ID
        entity_type: One of 'playbooks', 'roles', 'inventories'
    
    Returns:
        Absolute Path to entity directory in Project Storage (local path)
    
    Raises:
        ValueError: If entity_type is invalid
    """
    valid_entity_types = ['playbooks', 'roles', 'inventories']
    if entity_type not in valid_entity_types:
        raise ValueError(f"Invalid entity_type: {entity_type}. Must be one of {valid_entity_types}")
    
    project_dir = get_project_dir(project_id)
    repo_base = project_dir / 'repo'
    
    # IMPORTANT: subdir используется только для поиска сущностей в Git, НЕ для локального пути
    # Локально файлы всегда хранятся в repo/<entity_type>/, без subdir
    # subdir указывает только базовый путь в Git, где искать playbooks, roles, inventories
    
    # IMPORTANT: Always use LOCAL Project Storage path, not Git path from repoLayout
    # The repoLayout paths are only used during Git sync for mapping.
    # Locally, files are always stored in standard folders: playbooks, roles, inventories
    entity_path = entity_type  # Use default local path, not Git path from repoLayout
    
    return repo_base / entity_path


def get_project_inventories_dir(project_id: str) -> Path:
    """Get inventories directory using repoLayout"""
    return resolve_repo_path(project_id, 'inventories')


def ensure_inventory_dirs(inventory_file_path: Path):
    """
    Создает папки group_vars и host_vars рядом с inventory файлом, если их нет.
    
    Args:
        inventory_file_path: Path к inventory файлу
    """
    try:
        inventory_dir = inventory_file_path.parent
        group_vars_dir = inventory_dir / 'group_vars'
        host_vars_dir = inventory_dir / 'host_vars'
        
        if not group_vars_dir.exists():
            group_vars_dir.mkdir(parents=True, exist_ok=True)
            app.logger.info(f"Created group_vars directory: {group_vars_dir}")
        
        if not host_vars_dir.exists():
            host_vars_dir.mkdir(parents=True, exist_ok=True)
            app.logger.info(f"Created host_vars directory: {host_vars_dir}")
    except Exception as e:
        app.logger.warning(f"Failed to create group_vars/host_vars directories for {inventory_file_path}: {e}")


def ensure_all_inventory_dirs(project_id: str):
    """
    Создаёт group_vars и host_vars рядом с inventory файлами в папке inventories.
    Ищет только в inventories: inventory.yaml, inventory.yml, hosts.yaml, hosts.yml, hosts (без расширения).
    """
    try:
        inventories_dir = get_project_inventories_dir(project_id)
        inventory_names = ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']
        inventory_files = []
        if inventories_dir.exists():
            for name in inventory_names:
                p = inventories_dir / name
                if p.exists() and p.is_file():
                    inventory_files.append(p)
            for inv_file in inventories_dir.rglob('*'):
                if inv_file.is_file() and inv_file.name in inventory_names:
                    rel = inv_file.relative_to(inventories_dir)
                    if 'group_vars' not in rel.parts and 'host_vars' not in rel.parts:
                        if inv_file not in inventory_files:
                            inventory_files.append(inv_file)
        for inventory_file in inventory_files:
            ensure_inventory_dirs(inventory_file)
        if inventory_files:
            app.logger.info(f"Checked {len(inventory_files)} inventory files and ensured group_vars/host_vars directories")
    except Exception as e:
        app.logger.error(f"Error ensuring inventory directories: {e}", exc_info=True)


def ensure_group_vars_host_vars_from_inventory(project_id: str, inventory_files_full: list):
    """Создаёт group_vars и host_vars рядом с inventory при обнаружении новых inventory-файлов.
    Вызывается бэкендом при загрузке данных (get_all_data), чтобы файлы создавались автоматически.
    """
    if not inventory_files_full:
        return
    try:
        ensure_all_inventory_dirs(project_id)
        project_dir = get_project_dir(project_id)
        repo_dir = project_dir / 'repo'
        inventories_dir = get_project_inventories_dir(project_id)
        for full_path_str in inventory_files_full:
            full_path = Path(full_path_str)
            if not full_path.exists():
                continue
            try:
                if _is_ini_inventory_file(full_path):
                    inventory_data = _parse_ini_inventory(full_path) or {}
                else:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        inventory_data = yaml_loader.load(f) or {}
                inventory_dir = full_path.parent
                group_vars_dir = inventory_dir / 'group_vars'
                group_vars_dir.mkdir(parents=True, exist_ok=True)
                all_children = inventory_data.get('all', {}).get('children', {})
                groups_in_this_inventory = set()
                for group_name in all_children.keys():
                    if isinstance(group_name, str) and not group_name.startswith('#'):
                        groups_in_this_inventory.add(group_name)
                if inventory_data.get('all', {}).get('vars'):
                    groups_in_this_inventory.add('all')
                groups_in_this_inventory.add('all')
                for group_name in groups_in_this_inventory:
                    group_file = group_vars_dir / f"{group_name}.yml"
                    if not group_file.exists():
                        with open(group_file, 'w', encoding='utf-8') as f:
                            yaml_loader.dump({}, f)
                        app.logger.info(f"Auto-created group_vars for {group_name} at {group_file}")
            except Exception as e:
                app.logger.debug(f"ensure_group_vars_host_vars skip {full_path}: {e}")
        hosts_from_inv = get_inventory_hosts(inventory_files_full)
        for h in hosts_from_inv:
            host_name = h.get('name')
            if not host_name:
                continue
            inventory_file = h.get('inventory_file', '')
            host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file)
            host_vars_dir.mkdir(parents=True, exist_ok=True)
            host_file = host_vars_dir / f"{host_name}.yml"
            if not host_file.exists():
                with open(host_file, 'w', encoding='utf-8') as f:
                    yaml_loader.dump({}, f)
                app.logger.info(f"Auto-created host_vars for {host_name} at {host_file}")
    except Exception as e:
        app.logger.warning(f"ensure_group_vars_host_vars_from_inventory: {e}", exc_info=True)


def get_project_roles_dir(project_id: str) -> Path:
    """Get roles directory using repoLayout"""
    return resolve_repo_path(project_id, 'roles')


def get_project_playbooks_dir(project_id: str) -> Path:
    """Get playbooks directory using repoLayout"""
    return resolve_repo_path(project_id, 'playbooks')


def get_project_inventory_file(project_id):
    """Возвращает путь к inventory файлу проекта.
    
    Ищем только в папке inventories: inventory.yaml, inventory.yml, hosts.yaml, hosts.yml, hosts (без расширения).
    Fallback не используется — если в inventories ничего нет, возвращается несуществующий путь
    (inventories_dir / 'inventory.yml'), чтобы .exists() давал False, а .parent — inventories_dir.
    """
    inventories_dir = get_project_inventories_dir(project_id)
    inventory_names = ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']
    
    # В корне inventories
    for name in inventory_names:
        root_inventory = inventories_dir / name
        if root_inventory.exists():
            return root_inventory
    
    # Рекурсивно в подпапках inventories (исключая group_vars, host_vars)
    if inventories_dir.exists():
        for inv_file in inventories_dir.rglob('*'):
            if inv_file.is_file() and inv_file.name in inventory_names:
                rel = inv_file.relative_to(inventories_dir)
                if 'group_vars' not in rel.parts and 'host_vars' not in rel.parts:
                    return inv_file
    
    return inventories_dir / 'inventory.yml'


def get_project_group_vars_dir(project_id):
    """Возвращает путь к директории group_vars проекта (fallback)
    
    Путь: inventories/group_vars/ (рядом с inventories)
    """
    inventories_dir = get_project_inventories_dir(project_id)
    return inventories_dir / 'group_vars'


def get_project_host_vars_dir(project_id):
    """Возвращает путь к директории host_vars проекта (fallback)
    
    Путь: inventories/host_vars/ (рядом с inventories)
    """
    inventories_dir = get_project_inventories_dir(project_id)
    return inventories_dir / 'host_vars'


def get_host_vars_dir_for_inventory(project_id: str, inventory_file_path: str = None) -> Path:
    """
    Определяет путь к директории host_vars на основе inventory файла.
    
    Если указан inventory_file_path, возвращает путь рядом с inventory файлом.
    Иначе возвращает общую директорию inventories/host_vars (fallback).
    
    Args:
        project_id: ID проекта
        inventory_file_path: Путь к inventory файлу (относительно repo/ или полный путь)
    
    Returns:
        Path к директории host_vars
    """
    if inventory_file_path:
        project_dir = get_project_dir(project_id)
        repo_dir = project_dir / 'repo'
        
        app.logger.debug(f"[get_host_vars_dir_for_inventory] Looking for inventory file: {inventory_file_path} in project {project_id}")
        
        # Пробуем разные варианты путей
        inventory_path = None
        
        # 1. Путь относительно repo/
        repo_path = repo_dir / inventory_file_path
        if repo_path.exists():
            inventory_path = repo_path
            app.logger.debug(f"[get_host_vars_dir_for_inventory] Found via repo_path: {repo_path}")
        else:
            # 2. Если путь уже полный, используем его
            full_path = Path(inventory_file_path)
            if full_path.exists():
                try:
                    if full_path.is_relative_to(project_dir):
                        inventory_path = full_path
                        app.logger.debug(f"[get_host_vars_dir_for_inventory] Found via full_path: {full_path}")
                except ValueError:
                    # full_path не является подпутем project_dir
                    pass
            
            if not inventory_path:
                # 3. Ищем файл рекурсивно в repo/ по имени файла
                file_name = Path(inventory_file_path).name
                found_files = list(repo_dir.rglob(file_name))
                if found_files:
                    # Если несколько файлов с таким именем, выбираем тот, который ближе всего к указанному пути
                    # Сортируем по совпадению пути
                    best_match = None
                    path_parts = Path(inventory_file_path).parts
                    for found_file in found_files:
                        # Проверяем, совпадает ли структура пути
                        try:
                            rel_path = found_file.relative_to(repo_dir)
                            if str(rel_path) == inventory_file_path or rel_path.parts[-len(path_parts):] == path_parts:
                                best_match = found_file
                                break
                        except ValueError:
                            pass
                    
                    inventory_path = best_match or found_files[0]
                    app.logger.debug(f"[get_host_vars_dir_for_inventory] Found via rglob: {inventory_path}")
        
        if inventory_path and inventory_path.exists():
            # Возвращаем путь рядом с inventory файлом
            host_vars_dir = inventory_path.parent / 'host_vars'
            app.logger.info(f"[get_host_vars_dir_for_inventory] Using host_vars dir: {host_vars_dir} for inventory: {inventory_path}")
            return host_vars_dir
        else:
            app.logger.warning(f"[get_host_vars_dir_for_inventory] Inventory file not found: {inventory_file_path}, using fallback")
    
    # Fallback: общая директория vars/host_vars
    fallback_dir = get_project_host_vars_dir(project_id)
    app.logger.debug(f"[get_host_vars_dir_for_inventory] Using fallback dir: {fallback_dir}")
    return fallback_dir


def get_group_vars_dir_for_inventory(project_id: str, inventory_file_path: str = None) -> Path:
    """
    Определяет путь к директории group_vars на основе inventory файла.
    
    Если указан inventory_file_path, возвращает путь рядом с inventory файлом.
    Иначе возвращает общую директорию inventories/group_vars (fallback).
    
    Args:
        project_id: ID проекта
        inventory_file_path: Путь к inventory файлу (относительно repo/ или полный путь)
    
    Returns:
        Path к директории group_vars
    """
    if inventory_file_path:
        project_dir = get_project_dir(project_id)
        repo_dir = project_dir / 'repo'
        
        # Пробуем разные варианты путей
        inventory_path = None
        
        # 1. Путь относительно repo/
        repo_path = repo_dir / inventory_file_path
        if repo_path.exists():
            inventory_path = repo_path
        else:
            # 2. Если путь уже полный, используем его
            full_path = Path(inventory_file_path)
            if full_path.exists() and full_path.is_relative_to(project_dir):
                inventory_path = full_path
            else:
                # 3. Ищем файл рекурсивно в repo/
                found_files = list(repo_dir.rglob(Path(inventory_file_path).name))
                if found_files:
                    inventory_path = found_files[0]
        
        if inventory_path and inventory_path.exists():
            # Возвращаем путь рядом с inventory файлом
            return inventory_path.parent / 'group_vars'
    
    # Fallback: общая директория vars/group_vars
    return get_project_group_vars_dir(project_id)


def find_inventory_file_for_group(project_id: str, group_name: str) -> str:
    """
    Находит inventory файл, в котором определена группа.
    
    Args:
        project_id: ID проекта
        group_name: Имя группы
        
    Returns:
        str: Относительный путь к inventory файлу (относительно repo/) или пустая строка
    """
    project_dir = get_project_dir(project_id)
    repo_dir = project_dir / 'repo'
    inventories_dir = get_project_inventories_dir(project_id)
    
    # Собираем все inventory файлы (.yaml, .yml, .ini, hosts)
    inventory_files = []
    if inventories_dir.exists():
        for inv_file in inventories_dir.rglob('*.yaml'):
            if inv_file.is_file():
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    inventory_files.append(str(inv_file.relative_to(repo_dir)))
        for inv_file in inventories_dir.rglob('*.yml'):
            if inv_file.is_file():
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    inventory_files.append(str(inv_file.relative_to(repo_dir)))
        for inv_file in inventories_dir.rglob('*.ini'):
            if inv_file.is_file():
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    inventory_files.append(str(inv_file.relative_to(repo_dir)))
        for inv_file in inventories_dir.rglob('*'):
            if inv_file.is_file() and inv_file.name == 'hosts' and inv_file.suffix == '':
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    inventory_files.append(str(inv_file.relative_to(repo_dir)))
    
    root_inventory = repo_dir / 'inventory.yml'
    if root_inventory.exists():
        inventory_files.append('inventory.yml')
    
    for inv_file_path in inventory_files:
        full_path = repo_dir / inv_file_path
        if full_path.exists():
            try:
                if _is_ini_inventory_file(full_path):
                    inventory_data = _parse_ini_inventory(full_path) or {}
                else:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        inventory_data = yaml_loader.load(f) or {}
                all_children = inventory_data.get('all', {}).get('children', {})
                if group_name in all_children:
                    return inv_file_path
            except Exception as e:
                app.logger.debug(f"Error reading inventory {inv_file_path} for group search: {e}")
                continue
    return ''


def find_inventory_files_for_playbook(project_id: str, playbook: dict) -> list:
    """
    Находит inventory файлы, которые содержат хосты и группы из плейбука.
    
    Args:
        project_id: ID проекта
        playbook: Словарь с данными плейбука
        
    Returns:
        list: Список относительных путей к inventory файлам (относительно repo/)
    """
    app.logger.info(f"[find_inventory_files_for_playbook] ========== Starting inventory file search ==========")
    app.logger.info(f"[find_inventory_files_for_playbook] Project ID: {project_id}")
    app.logger.info(f"[find_inventory_files_for_playbook] Playbook: {playbook.get('name', 'Unknown')} (ID: {playbook.get('id', 'Unknown')})")
    
    project_dir = get_project_dir(project_id)
    repo_dir = project_dir / 'repo'
    inventories_dir = get_project_inventories_dir(project_id)
    
    app.logger.debug(f"[find_inventory_files_for_playbook] Project dir: {project_dir}")
    app.logger.debug(f"[find_inventory_files_for_playbook] Repo dir: {repo_dir}")
    app.logger.debug(f"[find_inventory_files_for_playbook] Inventories dir: {inventories_dir}")
    
    # Собираем все хосты и группы из плейбука
    hosts_to_find = set()
    groups_to_find = set()
    
    app.logger.info(f"[find_inventory_files_for_playbook] Analyzing playbook with {len(playbook.get('plays', []))} plays")
    
    for play in playbook.get('plays', []):
        hosts_value = play.get('hosts', '')
        app.logger.debug(f"[find_inventory_files_for_playbook] Play hosts value: {hosts_value} (type: {type(hosts_value).__name__})")
        
        if isinstance(hosts_value, str):
            # Может быть группа или IP адрес
            if hosts_value and hosts_value != 'all':
                # Проверяем, это IP адрес или группа
                import re
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', hosts_value):
                    # Это IP адрес (хост)
                    hosts_to_find.add(hosts_value)
                    app.logger.debug(f"[find_inventory_files_for_playbook] Added host: {hosts_value}")
                else:
                    # Это группа
                    groups_to_find.add(hosts_value)
                    app.logger.debug(f"[find_inventory_files_for_playbook] Added group: {hosts_value}")
        elif isinstance(hosts_value, list):
            for host in hosts_value:
                if host and host != 'all':
                    import re
                    if re.match(r'^\d+\.\d+\.\d+\.\d+$', str(host)):
                        hosts_to_find.add(str(host))
                        app.logger.debug(f"[find_inventory_files_for_playbook] Added host from list: {host}")
                    else:
                        groups_to_find.add(str(host))
                        app.logger.debug(f"[find_inventory_files_for_playbook] Added group from list: {host}")
    
    app.logger.info(f"[find_inventory_files_for_playbook] Hosts to find: {hosts_to_find}, Groups to find: {groups_to_find}")
    
    if not hosts_to_find and not groups_to_find:
        # Если нет хостов/групп, возвращаем пустой список (будет использован default)
        app.logger.warning(f"[find_inventory_files_for_playbook] No hosts or groups found in playbook, returning empty list")
        return []
    
    # Собираем все inventory файлы (.yaml, .yml, .ini, hosts)
    inventory_files = []
    if inventories_dir.exists():
        for inv_file in inventories_dir.rglob('*.yaml'):
            if inv_file.is_file():
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    repo_rel_path = str(inv_file.relative_to(repo_dir))
                    inventory_files.append(repo_rel_path)
                    app.logger.debug(f"[find_inventory_files_for_playbook] Found inventory file: {repo_rel_path}")
        for inv_file in inventories_dir.rglob('*.yml'):
            if inv_file.is_file():
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    repo_rel_path = str(inv_file.relative_to(repo_dir))
                    inventory_files.append(repo_rel_path)
                    app.logger.debug(f"[find_inventory_files_for_playbook] Found inventory file: {repo_rel_path}")
        for inv_file in inventories_dir.rglob('*.ini'):
            if inv_file.is_file():
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    repo_rel_path = str(inv_file.relative_to(repo_dir))
                    inventory_files.append(repo_rel_path)
                    app.logger.debug(f"[find_inventory_files_for_playbook] Found inventory file: {repo_rel_path}")
        for inv_file in inventories_dir.rglob('*'):
            if inv_file.is_file() and inv_file.name == 'hosts' and inv_file.suffix == '':
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                    repo_rel_path = str(inv_file.relative_to(repo_dir))
                    inventory_files.append(repo_rel_path)
                    app.logger.debug(f"[find_inventory_files_for_playbook] Found inventory file: {repo_rel_path}")
    else:
        app.logger.warning(f"[find_inventory_files_for_playbook] Inventories directory does not exist: {inventories_dir}")
    
    root_inventory = repo_dir / 'inventory.yml'
    if root_inventory.exists():
        inventory_files.append('inventory.yml')
        app.logger.debug(f"[find_inventory_files_for_playbook] Found root inventory file: inventory.yml")
    
    app.logger.info(f"[find_inventory_files_for_playbook] Total inventory files found: {len(inventory_files)}")
    app.logger.info(f"[find_inventory_files_for_playbook] Inventory files: {inventory_files}")
    
    found_inventory_files = set()
    
    for inv_file_path in inventory_files:
        full_path = repo_dir / inv_file_path
        app.logger.debug(f"[find_inventory_files_for_playbook] Checking inventory file: {inv_file_path} (full path: {full_path})")
        if not full_path.exists():
            app.logger.warning(f"[find_inventory_files_for_playbook] Inventory file not found: {full_path}")
            continue
            
        try:
            if _is_ini_inventory_file(full_path):
                inventory_data = _parse_ini_inventory(full_path) or {}
            else:
                with open(full_path, 'r', encoding='utf-8') as f:
                    inventory_data = yaml_loader.load(f) or {}
            
            app.logger.debug(f"[find_inventory_files_for_playbook] Loaded inventory data from {inv_file_path}")
            if inventory_data.get('all'):
                all_children = inventory_data.get('all', {}).get('children', {})
                app.logger.debug(f"[find_inventory_files_for_playbook] Inventory has 'all' section with children: {list(all_children.keys())}")
                # Логируем хосты в каждой группе
                for group_name, group_data in all_children.items():
                    if isinstance(group_data, dict) and 'hosts' in group_data:
                        hosts_in_group = group_data.get('hosts', {})
                        if isinstance(hosts_in_group, dict):
                            app.logger.debug(f"[find_inventory_files_for_playbook] Group {group_name} has hosts (dict): {list(hosts_in_group.keys())}")
                        elif isinstance(hosts_in_group, list):
                            app.logger.debug(f"[find_inventory_files_for_playbook] Group {group_name} has hosts (list): {hosts_in_group}")
            else:
                app.logger.warning(f"[find_inventory_files_for_playbook] Inventory {inv_file_path} does not have 'all' section")
            
            found_hosts = False
            found_groups = False
            
            # Проверяем хосты
            if hosts_to_find:
                for host_name in hosts_to_find:
                    app.logger.debug(f"[find_inventory_files_for_playbook] Searching for host {host_name} in inventory {inv_file_path}")
                    host_result = find_host_in_inventory(inventory_data, host_name)
                    if host_result is not None:
                        found_hosts = True
                        app.logger.info(f"[find_inventory_files_for_playbook] ✓ Found host {host_name} in inventory {inv_file_path}")
                        break
                    else:
                        app.logger.warning(f"[find_inventory_files_for_playbook] ✗ Host {host_name} NOT found in inventory {inv_file_path}")
                        # Логируем структуру inventory для отладки
                        all_children = inventory_data.get('all', {}).get('children', {})
                        app.logger.debug(f"[find_inventory_files_for_playbook] Inventory structure - groups: {list(all_children.keys())}")
                        for group_name, group_data in all_children.items():
                            if isinstance(group_data, dict) and 'hosts' in group_data:
                                group_hosts = group_data.get('hosts', {})
                                if isinstance(group_hosts, dict):
                                    app.logger.debug(f"[find_inventory_files_for_playbook] Group {group_name} hosts (dict): {list(group_hosts.keys())}")
                                elif isinstance(group_hosts, list):
                                    app.logger.debug(f"[find_inventory_files_for_playbook] Group {group_name} hosts (list): {group_hosts}")
            
            # Проверяем группы
            if groups_to_find:
                all_children = inventory_data.get('all', {}).get('children', {})
                for group_name in groups_to_find:
                    if group_name in all_children:
                        found_groups = True
                        app.logger.info(f"[find_inventory_files_for_playbook] ✓ Found group {group_name} in inventory {inv_file_path}")
                        break
                    # Также проверяем вложенные группы
                    for child_group_name, child_data in all_children.items():
                        if isinstance(child_data, dict):
                            nested_children = child_data.get('children', {})
                            if group_name in nested_children:
                                found_groups = True
                                app.logger.info(f"[find_inventory_files_for_playbook] ✓ Found nested group {group_name} in inventory {inv_file_path}")
                                break
            
            # Если нашли хосты или группы, добавляем файл
            if found_hosts or found_groups:
                found_inventory_files.add(inv_file_path)
                app.logger.info(f"[find_inventory_files_for_playbook] ✓ Added matching inventory file: {inv_file_path} (hosts: {found_hosts}, groups: {found_groups})")
            else:
                app.logger.debug(f"[find_inventory_files_for_playbook] No match in inventory {inv_file_path}")
                
        except Exception as e:
            app.logger.warning(f"[find_inventory_files_for_playbook] Error reading inventory {inv_file_path} for playbook search: {e}", exc_info=True)
            continue
    
    app.logger.info(f"[find_inventory_files_for_playbook] Final inventory files: {list(found_inventory_files)}")
    return list(found_inventory_files)



def get_project_executions_dir(project_id):
    """Возвращает путь к директории executions проекта (новая структура: history/executions/)"""
    project_dir = get_project_dir(project_id)
    # Новая структура: history/executions/
    return project_dir / 'history' / 'executions'


def get_project_logs_dir(project_id):
    """Возвращает путь к директории логов проекта (новая структура: history/logs/)"""
    project_dir = get_project_dir(project_id)
    # Новая структура: history/logs/
    return project_dir / 'history' / 'logs'


def get_project_roles_config_file(project_id):
    """Возвращает путь к файлу конфигурации ролей проекта"""
    project_dir = get_project_dir(project_id)
    return project_dir / 'data' / 'roles-config.json'


def get_project_secrets_dir(project_id):
    """Возвращает путь к директории secrets проекта (новая структура: secrets/)"""
    project_dir = get_project_dir(project_id)
    # Новая структура: secrets/
    secrets_dir = project_dir / 'secrets'
    secrets_dir.mkdir(parents=True, exist_ok=True)
    return secrets_dir


def get_project_vault_dir(project_id):
    """Возвращает путь к директории vault проекта (secrets/vault/)"""
    secrets_dir = get_project_secrets_dir(project_id)
    vault_dir = secrets_dir / 'vault'
    vault_dir.mkdir(parents=True, exist_ok=True)
    return vault_dir


def get_project_vault_keys_dir(project_id):
    """Возвращает путь к директории vault_keys проекта (secrets/vault_keys/)"""
    secrets_dir = get_project_secrets_dir(project_id)
    vault_keys_dir = secrets_dir / 'vault_keys'
    vault_keys_dir.mkdir(parents=True, exist_ok=True)
    return vault_keys_dir


def get_project_vaults_file(project_id):
    """Возвращает путь к файлу vaults (secrets/vault/vaults.json)"""
    vault_dir = get_project_vault_dir(project_id)
    return vault_dir / 'vaults.json'


def get_project_config_file(project_id):
    """Возвращает путь к файлу конфигурации проекта (project.json)"""
    project_dir = get_project_dir(project_id)
    return project_dir / 'project.json'


def get_project_host_status_file(project_id):
    """Возвращает путь к файлу статусов хостов проекта (host_status.json)"""
    project_dir = get_project_dir(project_id)
    return project_dir / 'host_status.json'


def get_project_generated_playbook_path(project_id, execution_id):
    """Возвращает путь к сгенерированному playbook файлу (новая структура: runtime/generated_playbooks/<execution_id>.yml)"""
    project_dir = get_project_dir(project_id)
    # Новая структура: runtime/generated_playbooks/
    playbook_path = project_dir / 'runtime' / 'generated_playbooks' / f'{execution_id}.yml'
    playbook_path.parent.mkdir(parents=True, exist_ok=True)
    return playbook_path


def _copy_keys_to_generated_playbooks_dedup(temp_key_files, generated_playbooks_dir):
    """
    Копирует SSH ключи в generated_playbooks с дедупликацией по содержимому.
    Один физический .pem на уникальный ключ. Возвращает {old_path: new_path}.
    """
    import shutil
    key_path_map = {}
    content_hash_to_new_path = {}
    for old_key_path in temp_key_files:
        try:
            with open(old_key_path, 'rb') as f:
                content = f.read()
            content_hash = hashlib.sha256(content).hexdigest()
            if content_hash in content_hash_to_new_path:
                key_path_map[old_key_path] = content_hash_to_new_path[content_hash]
            else:
                new_key_name = f'key_{uuid.uuid4().hex[:8]}.pem'
                new_key_path = generated_playbooks_dir / new_key_name
                shutil.copy2(old_key_path, new_key_path)
                os.chmod(new_key_path, 0o600)
                new_path_str = str(new_key_path.resolve())
                content_hash_to_new_path[content_hash] = new_path_str
                key_path_map[old_key_path] = new_path_str
        except Exception as e:
            app.logger.warning(f"Failed to copy key to generated_playbooks: {e}")
    return key_path_map


def slugify_secret_name(name):
    """
    Преобразует имя секрета в безопасное для файловой системы имя.
    Защита от path traversal и специальных символов.
    """
    if not name or not isinstance(name, str):
        raise ValueError("Secret name must be a non-empty string")
    
    # Удаляем опасные символы
    import re
    # Разрешаем только буквы, цифры, дефисы и подчеркивания
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    
    # Проверка на path traversal
    if '..' in safe_name or '/' in safe_name or '\\' in safe_name:
        raise ValueError("Secret name contains invalid characters")
    
    # Проверка на пустое имя после очистки
    if not safe_name:
        raise ValueError("Secret name cannot be empty after sanitization")
    
    # Ограничение длины
    if len(safe_name) > 100:
        raise ValueError("Secret name is too long (max 100 characters)")
    
    return safe_name


def get_secret_file_path(project_id, secret_name, secret_type=None):
    """Возвращает путь к файлу секрета с валидацией
    
    Новая структура:
    - secrets/ssh_keys/<key_id>.json - для SSH ключей
    - secrets/vault/vault_pass - для vault паролей
    - secrets/git_auth/<auth_id>.json - для git аутентификации
    """
    safe_name = slugify_secret_name(secret_name)
    secrets_dir = get_project_secrets_dir(project_id)
    
    # Новая структура: определяем путь по типу секрета
    if secret_type == 'ssh_key' or (not secret_type):
        # По умолчанию SSH ключи
        ssh_keys_dir = secrets_dir / 'ssh_keys'
        ssh_keys_dir.mkdir(parents=True, exist_ok=True)
        return ssh_keys_dir / f"{safe_name}.json"
    elif secret_type == 'vault_password':
        vault_dir = secrets_dir / 'vault'
        vault_dir.mkdir(parents=True, exist_ok=True)
        return vault_dir / 'vault_pass'
    elif secret_type == 'git_auth':
        git_auth_dir = secrets_dir / 'git_auth'
        git_auth_dir.mkdir(parents=True, exist_ok=True)
        return git_auth_dir / f"{safe_name}.json"
    else:
        # По умолчанию SSH ключи
        ssh_keys_dir = secrets_dir / 'ssh_keys'
        ssh_keys_dir.mkdir(parents=True, exist_ok=True)
        return ssh_keys_dir / f"{safe_name}.json"


def get_project_id_from_request():
    """Получает projectId из запроса или возвращает Default Project
    
    WARNING: This function falls back to Default Project.
    For project-scoped endpoints, use require_project_id_from_request() instead.
    """
    project_id = request.args.get('project_id')
    if not project_id and request.is_json:
        json_data = request.get_json(silent=True)
        if json_data:
            project_id = json_data.get('project_id')
    
    if not project_id:
        # Возвращаем Default Project
        project_id = get_default_project_id()
    
    return project_id


def require_project_id_from_request():
    """Получает projectId из запроса, БЕЗ fallback на Default Project
    
    Используется для project-scoped endpoints (inventory, vars, roles, secrets, ansible_config).
    Возвращает project_id или None, если не указан.
    """
    project_id = request.args.get('project_id')
    if not project_id and request.is_json:
        json_data = request.get_json(silent=True)
        if json_data:
            project_id = json_data.get('project_id')
    
    return project_id


def create_backup(file_path, force=False):
    """Создает бэкап файла с timestamp в data/backups/
    
    Args:
        file_path: путь к файлу для бэкапа
        force: если True, создает бэкап независимо от настроек проекта
    """
    try:
        if not file_path.exists():
            return False
        
        # Проверяем, нужно ли создавать бэкап (если не force)
        if not force:
            project_id = None
            file_path_str = str(file_path)
            projects_match = re.search(r'/projects/([^/]+)/', file_path_str)
            if projects_match:
                project_id = projects_match.group(1)
            
            if project_id and not should_create_backup(file_path, project_id):
                return False
        
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Определяем тип бэкапа по пути файла
        file_path_str = str(file_path)
        backup_subdir = None
        project_id = None
        
        # Проверяем, является ли это файлом проекта
        projects_match = re.search(r'/projects/([^/]+)/', file_path_str)
        if projects_match:
            project_id = projects_match.group(1)
        
        # Определяем тип файла
        if 'group_vars' in file_path_str:
            if project_id:
                backup_subdir = BACKUPS_DIR / 'group_vars' / project_id
            else:
                backup_subdir = BACKUPS_DIR / 'group_vars'
        elif 'host_vars' in file_path_str:
            if project_id:
                backup_subdir = BACKUPS_DIR / 'host_vars' / project_id
            else:
                backup_subdir = BACKUPS_DIR / 'host_vars'
        elif 'inventory' in file_path_str:
            if project_id:
                backup_subdir = BACKUPS_DIR / 'inventory' / project_id
            else:
                backup_subdir = BACKUPS_DIR / 'inventory'
        elif 'playbooks' in file_path_str or file_path.suffix == '.json':
            # Playbooks или JSON файлы
            if project_id:
                backup_subdir = BACKUPS_DIR / 'playbooks' / project_id
            else:
                backup_subdir = BACKUPS_DIR / 'playbooks'
        else:
            # Для других файлов используем общую директорию
            backup_subdir = BACKUPS_DIR / 'other'
        
        backup_subdir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_subdir / f"{file_path.stem}_{timestamp}{file_path.suffix}"
        
        import shutil
        shutil.copy2(file_path, backup_file)
        
        # Очищаем старые бэкапы если нужно
        if project_id:
            settings = load_backup_settings()
            max_depth = settings.get('max_depth', 100)
            cleanup_old_backups(backup_subdir, max_depth)
        
        return True
    except Exception as e:
        app.logger.error(f"Error creating backup for {file_path}: {e}")
        return False


def save_yaml_with_comments(file_path, data, create_backup_file=None):
    """Сохраняет YAML файл с сохранением структуры комментариев
    
    Args:
        file_path: путь к файлу
        data: данные для сохранения
        create_backup_file: если True - создает бэкап, если False - не создает,
                           если None - проверяет настройки проекта автоматически
    """
    try:
        # Если данных нет, создаем пустой словарь
        if not data:
            data = {}
        
        # Удаляем служебные ключи
        clean_data = {k: v for k, v in data.items() if not k.startswith('_')}
        
        # Валидируем YAML перед сохранением
        is_valid, error_msg = validate_yaml_content(clean_data)
        if not is_valid:
            app.logger.error(f"YAML validation failed for {file_path}: {error_msg}")
            raise ValueError(f"Invalid YAML: {error_msg}")
        
        # Определяем, нужно ли создавать бэкап
        should_backup = False
        if create_backup_file is True:
            should_backup = True
        elif create_backup_file is None:
            # Автоматически проверяем настройки проекта
            should_backup = should_create_backup(file_path)
        # Если create_backup_file is False, should_backup остается False
        
        # Создаем бэкап если нужно
        if should_backup and file_path.exists():
            create_backup(file_path)
        
        # Создаем директорию если не существует
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Сохраняем
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml_loader.dump(clean_data, f)
        
        return True
    except ValueError as e:
        # Ошибка валидации - пробрасываем дальше
        raise
    except Exception as e:
        app.logger.error(f"Error saving {file_path}: {e}")
        return False


def extract_hosts_from_group(group_data, hosts_set, hosts_list, parent_path='', inventory_file=None):
    """Рекурсивно извлекает хосты из группы inventory
    
    Args:
        group_data: данные группы (может содержать 'hosts' или 'children')
        hosts_set: множество уже найденных хостов (для избежания дубликатов)
        hosts_list: список для добавления хостов
        parent_path: путь к родительской группе (для отладки)
        inventory_file: имя inventory файла, из которого извлекаются хосты
    """
    if not isinstance(group_data, dict):
        return
    
    # Обрабатываем прямые хосты в группе
    if 'hosts' in group_data:
        hosts = group_data['hosts']
        if isinstance(hosts, dict):
            # hosts: { host1: { vars: ... }, host2: None, host3: ... }
            for host, config in hosts.items():
                # Пропускаем закомментированные хосты
                if isinstance(host, str) and host.startswith('#'):
                    continue
                # Пропускаем не-строковые ключи
                if not isinstance(host, str):
                    continue
                # Если хост уже есть, обновляем его inventory_file на последний обработанный файл
                # Это позволяет видеть, из какого файла был загружен хост (приоритет последнему файлу)
                if host not in hosts_set:
                    vars_file = None
                    inline_vars = {}
                    if isinstance(config, dict):
                        vars_file = config.get('vars_file')
                        inline_vars = {k: v for k, v in config.items() if k != 'vars_file' and v is not None}
                    if not vars_file:
                        vars_file = f'host_vars/{host}.yml'
                    entry = {'name': host, 'vars_file': vars_file, 'inventory_file': inventory_file}
                    if inline_vars:
                        entry['vars'] = inline_vars
                    hosts_list.append(entry)
                    hosts_set.add(host)
                else:
                    for existing_host in hosts_list:
                        if existing_host['name'] == host:
                            existing_host['inventory_file'] = inventory_file
                            if isinstance(config, dict):
                                if config.get('vars_file'):
                                    existing_host['vars_file'] = config['vars_file']
                                inline_vars = {k: v for k, v in config.items() if k != 'vars_file' and v is not None}
                                if inline_vars:
                                    existing_host['vars'] = inline_vars
                            break
        elif isinstance(hosts, list):
            # hosts: [ host1, host2, ... ]
            for host in hosts:
                if isinstance(host, str) and not host.startswith('#'):
                    if host not in hosts_set:
                        hosts_list.append({
                            'name': host,
                            'vars_file': f'host_vars/{host}.yml',
                            'inventory_file': inventory_file
                        })
                        hosts_set.add(host)
                    else:
                        # Хост уже есть, обновляем inventory_file на текущий файл (приоритет последнему)
                        for existing_host in hosts_list:
                            if existing_host['name'] == host:
                                old_file = existing_host['inventory_file']
                                existing_host['inventory_file'] = inventory_file
                                app.logger.debug(f"Updated host {host} inventory_file from {old_file} to {inventory_file}")
                                break
    
    # Рекурсивно обрабатываем дочерние группы
    if 'children' in group_data:
        children = group_data['children']
        if isinstance(children, dict):
            for child_name, child_data in children.items():
                if isinstance(child_data, dict):
                    extract_hosts_from_group(child_data, hosts_set, hosts_list, f"{parent_path}.{child_name}" if parent_path else child_name, inventory_file)


def extract_groups_from_inventory(group_data, groups_dict, parent_path='', inventory_file=None):
    """Рекурсивно извлекает структуру групп из inventory
    
    Args:
        group_data: данные группы (может содержать 'hosts', 'children', 'vars')
        groups_dict: словарь для сохранения групп {group_name: {'hosts': [...], 'children': [...], 'inventory_file': ..., 'vars': {...}}}
        parent_path: путь к родительской группе
        inventory_file: путь к inventory файлу, из которого извлекаются группы
    """
    if not isinstance(group_data, dict):
        return
    current_group = parent_path if parent_path else 'all'
    if 'vars' in group_data and isinstance(group_data['vars'], dict) and group_data['vars']:
        if current_group not in groups_dict:
            groups_dict[current_group] = {'hosts': [], 'children': [], 'inventory_file': inventory_file}
        groups_dict[current_group]['vars'] = group_data['vars']
    # Обрабатываем прямые хосты в группе
    if 'hosts' in group_data:
        hosts = group_data['hosts']
        host_list = []
        if isinstance(hosts, dict):
            # hosts: { host1: { vars: ... }, host2: None, host3: ... }
            for host, config in hosts.items():
                if isinstance(host, str) and not host.startswith('#'):
                    host_list.append(host)
        elif isinstance(hosts, list):
            # hosts: [ host1, host2, ... ]
            for host in hosts:
                if isinstance(host, str) and not host.startswith('#'):
                    host_list.append(host)
        
        if host_list:
            current_group = parent_path if parent_path else 'all'
            if current_group not in groups_dict:
                groups_dict[current_group] = {'hosts': [], 'children': [], 'inventory_file': inventory_file}
            groups_dict[current_group]['hosts'].extend(host_list)
            # Сохраняем inventory_file, если еще не сохранен
            if inventory_file and not groups_dict[current_group].get('inventory_file'):
                groups_dict[current_group]['inventory_file'] = inventory_file
    
    # Рекурсивно обрабатываем дочерние группы
    if 'children' in group_data:
        children = group_data['children']
        children_list = []
        
        if isinstance(children, dict):
            # Если children это словарь, извлекаем имена дочерних групп
            for child_name, child_data in children.items():
                if isinstance(child_name, str) and not child_name.startswith('#'):
                    children_list.append(child_name)
                    if isinstance(child_data, dict):
                        child_path = child_name
                        if child_path not in groups_dict:
                            groups_dict[child_path] = {'hosts': [], 'children': [], 'inventory_file': inventory_file}
                        extract_groups_from_inventory(child_data, groups_dict, child_path, inventory_file)
        elif isinstance(children, list):
            # Если children это список, обрабатываем каждый элемент
            for child_item in children:
                if isinstance(child_item, str) and not child_item.startswith('#'):
                    children_list.append(child_item)
                elif isinstance(child_item, dict):
                    # Если элемент словарь, извлекаем имя и данные
                    for child_name, child_data in child_item.items():
                        if isinstance(child_name, str) and not child_name.startswith('#'):
                            children_list.append(child_name)
                            if isinstance(child_data, dict):
                                child_path = child_name
                                if child_path not in groups_dict:
                                    groups_dict[child_path] = {'hosts': [], 'children': [], 'inventory_file': inventory_file}
                                extract_groups_from_inventory(child_data, groups_dict, child_path, inventory_file)
        
        # Сохраняем список children для текущей группы
        if children_list:
            current_group = parent_path if parent_path else 'all'
            if current_group not in groups_dict:
                groups_dict[current_group] = {'hosts': [], 'children': [], 'inventory_file': inventory_file}
            groups_dict[current_group]['children'].extend(children_list)
            # Сохраняем inventory_file, если еще не сохранен
            if inventory_file and not groups_dict[current_group].get('inventory_file'):
                groups_dict[current_group]['inventory_file'] = inventory_file


def _is_ini_inventory_file(file_path):
    """Проверяет, что файл считается INI-инвентарём (hosts.ini, hosts без расширения, *.ini)."""
    p = Path(file_path) if not isinstance(file_path, Path) else file_path
    return p.suffix == '.ini' or p.name == 'hosts'


def _parse_ini_key_value(line):
    """Парсит строку key=value. Значение может быть в кавычках '...' или \"...\" или без."""
    eq = line.find('=')
    if eq <= 0:
        return None, None
    key = line[:eq].strip()
    val = line[eq + 1:].strip()
    if len(val) >= 2 and ((val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"'))):
        val = val[1:-1]
    return key, val


def _parse_ini_inventory(file_path):
    """Парсит Ansible INI inventory (hosts.ini / hosts). Возвращает структуру как у YAML.
    Поддерживает: [group], [group:children], [all:vars], [group:vars]; хосты с инлайновыми vars (host k=v k2=v2).
    """
    path = Path(file_path)
    if not path.exists():
        return None
    groups_raw = {}  # section_name -> {'hosts': [(name, vars_dict)|str], 'children': [], 'vars': {}}
    current_section = None
    current_kind = None  # None | 'children' | 'vars'
    all_vars = {}
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith('#'):
                continue
            if line_stripped.startswith('[') and line_stripped.endswith(']'):
                inner = line_stripped[1:-1].strip()
                if ':' in inner:
                    name, kind = inner.split(':', 1)
                    name, kind = name.strip(), kind.strip().lower()
                    current_section = name
                    current_kind = kind  # 'children' or 'vars'
                    groups_raw.setdefault(name, {'hosts': [], 'children': [], 'vars': {}})
                    if kind == 'vars':
                        if name == 'all':
                            all_vars = {}
                        else:
                            groups_raw[name]['vars'] = {}
                    elif kind == 'children':
                        groups_raw[name]['children'] = []
                else:
                    current_section = inner
                    current_kind = None
                    groups_raw.setdefault(inner, {'hosts': [], 'children': [], 'vars': {}})
                continue
            if current_section is None:
                continue
            if current_kind == 'vars':
                key, val = _parse_ini_key_value(line_stripped)
                if key is not None:
                    if current_section == 'all':
                        all_vars[key] = val
                    else:
                        groups_raw[current_section]['vars'][key] = val
                continue
            if current_kind == 'children':
                group_name = line_stripped.split()[0] if line_stripped else ''
                if group_name and not group_name.startswith('#'):
                    groups_raw[current_section]['children'].append(group_name)
                continue
            # Обычная секция группы — строка хоста, возможно с key=value
            parts = line_stripped.split()
            if not parts or parts[0].startswith('#'):
                continue
            host_name = parts[0]
            if ':' in host_name and not host_name.startswith('['):
                host_name = host_name.split(':')[0]
            host_vars = {}
            for part in parts[1:]:
                if '=' in part:
                    k, v = _parse_ini_key_value(part)
                    if k is not None:
                        host_vars[k] = v
            if host_vars:
                groups_raw[current_section]['hosts'].append((host_name, host_vars))
            else:
                groups_raw[current_section]['hosts'].append(host_name)
    # Добавляем [all:vars] в группу all (создаём all только если есть all_vars)
    if all_vars:
        groups_raw.setdefault('all', {'hosts': [], 'children': [], 'vars': {}})
        groups_raw['all']['vars'] = all_vars
    def build_group(name, raw):
        hosts = {}
        for h in raw['hosts']:
            if isinstance(h, tuple):
                hosts[h[0]] = h[1]
            else:
                hosts[h] = None
        children = {}
        for child_name in raw.get('children') or []:
            if child_name in groups_raw and child_name != 'all':
                children[child_name] = build_group(child_name, groups_raw[child_name])
        result = {'hosts': hosts, 'children': children}
        if raw.get('vars'):
            result['vars'] = raw['vars']
        return result
    all_children = {name: build_group(name, raw) for name, raw in groups_raw.items() if name != 'all'}
    root = {'children': all_children}
    if all_vars:
        root['vars'] = all_vars
    return {'all': root}


def get_inventory_groups(inventory_files=None):
    """Получает структуру групп из inventory файлов
    
    Args:
        inventory_files: список путей к inventory файлам (полные пути или относительные)
                        Если None, возвращает пустой dict (не читает из BASE_DIR)
    
    Returns:
        dict: {group_name: {'hosts': [host1, host2, ...], 'children': [child_group1, ...]}}
    """
    groups = {}
    
    # Если не указаны файлы, возвращаем пустой dict (не читаем из BASE_DIR)
    if inventory_files is None or len(inventory_files) == 0:
        return groups
    
    for file_name in inventory_files:
        # Поддерживаем как полные пути, так и относительные имена файлов
        if Path(file_name).is_absolute() or '/' in file_name or '\\' in file_name:
            file_path = Path(file_name)
        else:
            # Относительные пути должны быть полными путями к файлам проекта
            # Не используем BASE_DIR fallback для новых проектов
            app.logger.warning(f"Inventory file {file_name} specified as relative path without full path. Skipping.")
            continue
        
        if not file_path.exists():
            app.logger.warning(f"Inventory file {file_name} not found")
            continue

        # Относительный путь для сохранения в группах
        rel_file_path = None
        try:
            parts = file_path.parts
            if 'projects' in parts:
                project_idx = parts.index('projects')
                if project_idx + 1 < len(parts):
                    project_dir = Path(*parts[:project_idx + 2])
                    repo_dir = project_dir / 'repo'
                    try:
                        rel_file_path = file_path.relative_to(repo_dir)
                    except ValueError:
                        rel_file_path = Path(file_path.name)
            if rel_file_path is None:
                rel_file_path = Path(file_path.name)
        except Exception:
            rel_file_path = Path(file_path.name)
        inventory_file_str = str(rel_file_path) if rel_file_path else file_path.name

        try:
            if _is_ini_inventory_file(file_path):
                inventory = _parse_ini_inventory(file_path)
            else:
                with open(file_path, 'r', encoding='utf-8') as f:
                    inventory = yaml.safe_load(f)
            
            if not inventory:
                app.logger.warning(f"Inventory file {file_name} is empty or invalid")
                continue
            
            if 'all' in inventory:
                all_section = inventory['all']
                if isinstance(all_section, dict):
                    extract_groups_from_inventory(all_section, groups, 'all', inventory_file_str)
            else:
                extract_groups_from_inventory(inventory, groups, 'root', inventory_file_str)
                
        except yaml.YAMLError as e:
            app.logger.error(f"Error parsing YAML in file {file_name}: {e}")
            continue
        except Exception as e:
            app.logger.warning(f"Error reading inventory file {file_name}: {e}")
            continue
    
    if 'all' in groups and not groups['all']['hosts'] and not groups['all'].get('vars'):
        del groups['all']
    
    app.logger.info(f"Found {len(groups)} groups from {len(inventory_files)} inventory files")
    return groups


def get_inventory_hosts(inventory_files=None):
    """Получает список хостов из inventory файлов
    Универсальный парсер, поддерживающий:
    - Плоскую структуру: all.hosts
    - Структуру с группами: all.children
    - Рекурсивные вложенные группы
    
    Args:
        inventory_files: список путей к inventory файлам (полные пути или относительные)
                        Если None, возвращает пустой список (не читает из BASE_DIR)
    """
    hosts = []
    hosts_set = set()  # Для избежания дубликатов
    
    # Если не указаны файлы, возвращаем пустой список (не читаем из BASE_DIR)
    if inventory_files is None or len(inventory_files) == 0:
        return hosts
    
    for file_name in inventory_files:
        # Поддерживаем как полные пути, так и относительные имена файлов
        if Path(file_name).is_absolute() or '/' in file_name or '\\' in file_name:
            file_path = Path(file_name)
        else:
            # Относительные пути должны быть полными путями к файлам проекта
            # Не используем BASE_DIR fallback для новых проектов
            app.logger.warning(f"Inventory file {file_name} specified as relative path without full path. Skipping.")
            continue
        
        if not file_path.exists():
            app.logger.warning(f"Inventory file {file_name} not found")
            continue

        file_path_str = str(file_path)
        try:
            if _is_ini_inventory_file(file_path):
                inventory = _parse_ini_inventory(file_path)
            else:
                with open(file_path, 'r', encoding='utf-8') as f:
                    inventory = yaml.safe_load(f)
            
            if not inventory:
                app.logger.warning(f"Inventory file {file_name} is empty or invalid")
                continue

            hosts_before = len(hosts)
            if 'all' in inventory:
                all_section = inventory['all']
                if isinstance(all_section, dict):
                    extract_hosts_from_group(all_section, hosts_set, hosts, 'all', file_path_str)
            else:
                extract_hosts_from_group(inventory, hosts_set, hosts, 'root', file_path_str)
            
            hosts_added = len(hosts) - hosts_before
            app.logger.info(f"Loaded {hosts_added} hosts from inventory file: {file_path_str} (total: {len(hosts)})")
                
        except yaml.YAMLError as e:
            app.logger.error(f"YAML parsing error in file {file_name}: {e}")
            continue
        except Exception as e:
            app.logger.warning(f"Error reading inventory file {file_name}: {e}")
            continue
    
    app.logger.info(f"Found {len(hosts)} total hosts from {len(inventory_files)} inventory files")
    return hosts


def find_host_in_inventory(inv_data, host_name):
    """Рекурсивно ищет хост в inventory структуре"""
    if not inv_data or 'all' not in inv_data:
        app.logger.debug(f"[find_host_in_inventory] No 'all' section in inventory data")
        return None
    
    all_section = inv_data['all']
    
    # Проверяем прямое размещение в all.hosts
    if 'hosts' in all_section and isinstance(all_section['hosts'], dict):
        if host_name in all_section['hosts']:
            app.logger.debug(f"[find_host_in_inventory] Found host {host_name} directly in all.hosts")
            # Возвращаем значение хоста, или пустой dict если значение None (хост без переменных)
            host_value = all_section['hosts'][host_name]
            return host_value if host_value is not None else {}
    
    # Проверяем в children (группы)
    # CommentedMap является подклассом dict, поэтому isinstance должен работать
    if 'children' in all_section and isinstance(all_section['children'], dict):
        for group_name, group_data in all_section['children'].items():
            app.logger.debug(f"[find_host_in_inventory] Checking group {group_name}, type: {type(group_data)}")
            if isinstance(group_data, dict) and 'hosts' in group_data:
                hosts_in_group = group_data['hosts']
                app.logger.debug(f"[find_host_in_inventory] Group {group_name} has hosts, type: {type(hosts_in_group)}")
                if isinstance(hosts_in_group, dict):
                    app.logger.debug(f"[find_host_in_inventory] Group {group_name} hosts is dict, keys: {list(hosts_in_group.keys())[:10]}")
                    if host_name in hosts_in_group:
                        app.logger.info(f"[find_host_in_inventory] ✓ Found host {host_name} in group {group_name}")
                        # Возвращаем значение хоста, или пустой dict если значение None (хост без переменных)
                        host_value = hosts_in_group[host_name]
                        return host_value if host_value is not None else {}
                    else:
                        app.logger.debug(f"[find_host_in_inventory] Host {host_name} not in group {group_name} hosts dict (keys: {list(hosts_in_group.keys())[:10]})")
                elif isinstance(hosts_in_group, list):
                    if host_name in hosts_in_group:
                        app.logger.info(f"[find_host_in_inventory] ✓ Found host {host_name} in group {group_name} (list)")
                        return {}
                    else:
                        app.logger.debug(f"[find_host_in_inventory] Host {host_name} not in group {group_name} hosts list: {hosts_in_group}")
            # Рекурсивно проверяем вложенные children
            if isinstance(group_data, dict) and 'children' in group_data:
                result = find_host_in_inventory({'all': group_data}, host_name)
                if result is not None:
                    app.logger.info(f"[find_host_in_inventory] ✓ Found host {host_name} in nested children of group {group_name}")
                    return result
    
    app.logger.debug(f"[find_host_in_inventory] Host {host_name} not found in inventory")
    return None


def get_host_vars_file(host, project_id, inventory_file_path=None):
    """Получает путь к файлу host_vars для хоста из Project Storage
    
    Args:
        host: имя хоста
        project_id: ID проекта (REQUIRED)
        inventory_file_path: Путь к inventory файлу (опционально, для определения правильной директории)
    
    Returns:
        Path: путь к файлу host_vars в Project Storage
    """
    if not project_id:
        raise ValueError("project_id is required")
    
    # Определяем директорию host_vars на основе inventory файла
    if inventory_file_path:
        host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file_path)
    else:
        # Пытаемся найти inventory файл хоста
        hosts_list = get_inventory_hosts()
        for h in hosts_list:
            if h['name'] == host:
                host_inventory_file = h.get('inventory_file', '')
                if host_inventory_file:
                    host_vars_dir = get_host_vars_dir_for_inventory(project_id, host_inventory_file)
                    break
        else:
            # Fallback: общая директория
            host_vars_dir = get_project_host_vars_dir(project_id)
    
    # Определяем имя файла
    hosts_list = get_inventory_hosts()
    for h in hosts_list:
        if h['name'] == host:
            vars_file = h.get('vars_file', '')
            if vars_file.startswith('host_vars/'):
                vars_file = vars_file.replace('host_vars/', '')
                return host_vars_dir / vars_file
    return host_vars_dir / f'{host}.yml'


# ============================================================================
# AUTHENTICATION API ENDPOINTS
# ============================================================================

@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """API: Аутентификация пользователя и получение JWT токена"""
    try:
        data = request.json or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')
        
        # Валидация входных данных
        is_valid, error_msg = validate_username(username)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 400
        
        is_valid, error_msg = validate_password(password)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 400
        
        # Аутентификация пользователя
        user = user_service.authenticate(username, password)
        if not user:
            app.logger.warning(f"Failed login attempt for username: {username}")
            return jsonify({
                'success': False,
                'error': 'Incorrect username or password'
            }), 401
        
        # Получаем имена ролей пользователя
        role_names = []
        for role_id in user.roles:
            role = role_service.get_role_by_id(role_id)
            if role:
                role_names.append(role.name)
        
        # Генерируем токены
        access_token = generate_token(
            user_id=user.id,
            username=user.username,
            roles=role_names,
            data_dir=DATA_DIR,
            token_type='access'
        )
        
        refresh_token = generate_token(
            user_id=user.id,
            username=user.username,
            roles=role_names,
            data_dir=DATA_DIR,
            token_type='refresh'
        )
        
        app.logger.info(f"User {username} logged in successfully")
        
        return jsonify({
            'success': True,
            'access_token': access_token,
            'refresh_token': refresh_token,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'roles': role_names
            }
        })
    except Exception as e:
        app.logger.error(f"Error in login: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Login error'
        }), 500


@app.route('/api/auth/logout', methods=['POST'])
@require_auth
def api_auth_logout():
    """API: Выход из системы (добавление токена в blacklist)"""
    try:
        token = request.token
        if token:
            add_token_to_blacklist(DATA_DIR, token)
            app.logger.info(f"User {request.current_user.get('username')} logged out")
        
        return jsonify({
            'success': True,
            'message': 'Выход выполнен успешно'
        })
    except Exception as e:
        app.logger.error(f"Error in logout: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Logout error'
        }), 500


@app.route('/api/auth/refresh', methods=['POST'])
def api_auth_refresh():
    """API: Обновление access токена с помощью refresh токена"""
    try:
        data = request.json or {}
        refresh_token = data.get('refresh_token', '').strip()
        
        if not refresh_token:
            return jsonify({
                'success': False,
                'error': 'Refresh token обязателен'
            }), 400
        
        # Проверяем refresh токен
        payload = verify_token(refresh_token, DATA_DIR, token_type='refresh')
        
        if not payload:
            return jsonify({
                'success': False,
                'error': 'Невалидный или истекший refresh token'
            }), 401
        
        # Получаем пользователя
        user_id = payload.get('user_id')
        user = user_service.get_user_by_id(user_id)
        
        if not user or not user.is_active:
            return jsonify({
                'success': False,
                'error': 'Пользователь не найден или неактивен'
            }), 401
        
        # Получаем имена ролей пользователя
        role_names = []
        for role_id in user.roles:
            role = role_service.get_role_by_id(role_id)
            if role:
                role_names.append(role.name)
        
        # Генерируем новый access токен
        access_token = generate_token(
            user_id=user.id,
            username=user.username,
            roles=role_names,
            data_dir=DATA_DIR,
            token_type='access'
        )
        
        return jsonify({
            'success': True,
            'access_token': access_token
        })
    except Exception as e:
        app.logger.error(f"Error in refresh: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Token refresh error'
        }), 500


@app.route('/api/auth/me', methods=['GET'])
@require_auth
def api_auth_me():
    """API: Получить информацию о текущем пользователе"""
    try:
        user_id = request.current_user.get('user_id')
        user = user_service.get_user_by_id(user_id)
        
        if not user:
            return jsonify({
                'success': False,
                'error': 'User not found'
            }), 404
        
        # Получаем имена ролей пользователя
        role_names = []
        role_details = []
        for role_id in user.roles:
            role = role_service.get_role_by_id(role_id)
            if role:
                role_names.append(role.name)
                role_details.append({
                    'id': role.id,
                    'name': role.name,
                    'description': role.description
                })
        
        # Получаем все права доступа пользователя
        permissions = access_control_service.get_user_permissions(user_id)
        
        return jsonify({
            'success': True,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'roles': role_names,
                'role_details': role_details,
                'permissions': list(permissions),
                'is_active': user.is_active,
                'created_at': user.created_at,
                'last_login': user.last_login
            }
        })
    except Exception as e:
        app.logger.error(f"Error in /api/auth/me: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting user information'
        }), 500


# ============================================================================
# END AUTHENTICATION API ENDPOINTS
# ============================================================================

# ============================================================================
# USERS API ENDPOINTS (CRUD)
# ============================================================================

@app.route('/api/users', methods=['GET'])
@require_auth
def api_list_users():
    """API: Получить список всех пользователей"""
    try:
        users = user_service.get_all_users()
        # Преобразуем в словари без паролей
        users_data = [user.to_dict() for user in users]
        
        # Добавляем информацию о ролях для каждого пользователя
        for user_data in users_data:
            role_names = []
            role_details = []
            for role_id in user_data.get('roles', []):
                role = role_service.get_role_by_id(role_id)
                if role:
                    role_names.append(role.name)
                    role_details.append({
                        'id': role.id,
                        'name': role.name,
                        'description': role.description
                    })
            user_data['role_names'] = role_names
            user_data['role_details'] = role_details
        
        return jsonify({
            'success': True,
            'users': users_data
        })
    except Exception as e:
        app.logger.error(f"Error listing users: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting users list'
        }), 500


@app.route('/api/users', methods=['POST'])
@require_auth
def api_create_user():
    """API: Создать нового пользователя"""
    try:
        data = request.json or {}
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        email = (data.get('email') or '').strip() or None
        roles = data.get('roles') or []  # Список ID ролей
        
        # Валидация входных данных
        is_valid, error_msg = validate_username(username)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 400
        
        is_valid, error_msg = validate_password(password)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 400
        
        if email:
            is_valid, error_msg = validate_email(email)
            if not is_valid:
                return jsonify({
                    'success': False,
                    'error': error_msg
                }), 400
        
        # Валидация ролей: ожидаем список ID (строк), приводим к строке на случай числа/объекта
        role_ids = []
        if roles:
            for r in roles:
                rid = r.get('id', r) if isinstance(r, dict) else r
                rid = str(rid).strip() if rid is not None else None
                if not rid:
                    continue
                role = role_service.get_role_by_id(rid)
                if not role:
                    return jsonify({
                        'success': False,
                        'error': f'Role with ID {rid} not found'
                    }), 400
                role_ids.append(rid)
        
        # Создаем пользователя
        user = user_service.create_user(
            username=username,
            password=password,
            email=email,
            roles=role_ids
        )
        
        app.logger.info(f"User {username} created by {request.current_user.get('username')}")
        
        return jsonify({
            'success': True,
            'user': user.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
    except Exception as e:
        app.logger.error(f"Error creating user: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error creating user: ' + str(e)
        }), 500


@app.route('/api/users/<user_id>', methods=['GET'])
@require_auth
def api_get_user(user_id):
    """API: Получить пользователя по ID"""
    try:
        user = user_service.get_user_by_id(user_id)
        
        if not user:
            return jsonify({
                'success': False,
                'error': 'User not found'
            }), 404
        
        user_data = user.to_dict()
        
        # Добавляем информацию о ролях
        role_names = []
        role_details = []
        for role_id in user.roles:
            role = role_service.get_role_by_id(role_id)
            if role:
                role_names.append(role.name)
                role_details.append({
                    'id': role.id,
                    'name': role.name,
                    'description': role.description
                })
        user_data['role_names'] = role_names
        user_data['role_details'] = role_details
        
        return jsonify({
            'success': True,
            'user': user_data
        })
    except Exception as e:
        app.logger.error(f"Error getting user: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting user'
        }), 500


@app.route('/api/users/<user_id>', methods=['PUT'])
@require_auth
def api_update_user(user_id):
    """API: Обновить пользователя"""
    try:
        data = request.json or {}
        username = (data.get('username') or '').strip() or None
        email = (data.get('email') or '').strip() or None
        roles = data.get('roles')
        is_active = data.get('is_active')
        current_password = data.get('current_password')
        new_password = data.get('password')

        # Password change: self-service (current + new) or admin set (new only for another user)
        current_user_id = request.current_user.get('user_id')
        if current_password is not None and new_password is not None:
            if not new_password or len(new_password) < 6:
                return jsonify({
                    'success': False,
                    'error': 'New password must be at least 6 characters'
                }), 400
            ok = user_service.change_password(user_id, current_password, new_password)
            if not ok:
                return jsonify({
                    'success': False,
                    'error': 'Current password is incorrect'
                }), 400
        elif new_password is not None:
            # Only new password: allow for admin editing another user; require current password when editing self
            if user_id == current_user_id:
                return jsonify({
                    'success': False,
                    'error': 'To change your own password, use Account settings and provide your current password'
                }), 400
            if not new_password or len(new_password) < 6:
                return jsonify({
                    'success': False,
                    'error': 'New password must be at least 6 characters'
                }), 400
            ok = user_service.set_password(user_id, new_password)
            if not ok:
                return jsonify({
                    'success': False,
                    'error': 'User not found'
                }), 404

        # Валидация username, если он изменяется
        if username is not None:
            is_valid, error_msg = validate_username(username)
            if not is_valid:
                return jsonify({
                    'success': False,
                    'error': error_msg
                }), 400
        
        # Валидация email, если он изменяется
        if email is not None:
            is_valid, error_msg = validate_email(email)
            if not is_valid:
                return jsonify({
                    'success': False,
                    'error': error_msg
                }), 400
        
        # Валидация ролей, если они изменяются
        if roles is not None:
            for role_id in roles:
                role = role_service.get_role_by_id(role_id)
                if not role:
                    return jsonify({
                        'success': False,
                        'error': f'Role with ID {role_id} not found'
                    }), 400
        
        # Обновляем пользователя
        user = user_service.update_user(
            user_id=user_id,
            username=username,
            email=email,
            roles=roles,
            is_active=is_active
        )
        
        if not user:
            return jsonify({
                'success': False,
                'error': 'User not found'
            }), 404
        
        app.logger.info(f"User {user.username} updated by {request.current_user.get('username')}")
        
        user_data = user.to_dict()
        
        # Добавляем информацию о ролях
        role_names = []
        role_details = []
        for role_id in user.roles:
            role = role_service.get_role_by_id(role_id)
            if role:
                role_names.append(role.name)
                role_details.append({
                    'id': role.id,
                    'name': role.name,
                    'description': role.description
                })
        user_data['role_names'] = role_names
        user_data['role_details'] = role_details
        
        return jsonify({
            'success': True,
            'user': user_data
        })
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
    except Exception as e:
        app.logger.error(f"Error updating user: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error updating user'
        }), 500


@app.route('/api/users/<user_id>', methods=['DELETE'])
@require_auth
def api_delete_user(user_id):
    """API: Удалить пользователя"""
    try:
        # Нельзя удалить самого себя
        current_user_id = request.current_user.get('user_id')
        if user_id == current_user_id:
            return jsonify({
                'success': False,
                'error': 'You cannot delete your own account'
            }), 400
        
        deleted = user_service.delete_user(user_id)
        
        if not deleted:
            return jsonify({
                'success': False,
                'error': 'User not found'
            }), 404
        
        app.logger.info(f"User {user_id} deleted by {request.current_user.get('username')}")
        
        return jsonify({
            'success': True,
            'message': 'User deleted successfully'
        })
    except Exception as e:
        app.logger.error(f"Error deleting user: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error deleting user'
        }), 500


@app.route('/api/users/<user_id>/roles', methods=['POST'])
@require_auth
def api_assign_roles_to_user(user_id):
    """API: Назначить роли пользователю"""
    try:
        data = request.json or {}
        role_ids = data.get('roles', [])  # Список ID ролей для назначения
        
        if not isinstance(role_ids, list):
            return jsonify({
                'success': False,
                'error': 'Roles must be an array'
            }), 400
        
        # Проверяем существование пользователя
        user = user_service.get_user_by_id(user_id)
        if not user:
            return jsonify({
                'success': False,
                'error': 'User not found'
            }), 404
        
        # Валидация ролей
        for role_id in role_ids:
            role = role_service.get_role_by_id(role_id)
            if not role:
                return jsonify({
                    'success': False,
                    'error': f'Role with ID {role_id} not found'
                }), 400
        
        # Обновляем роли пользователя
        user = user_service.update_user(user_id=user_id, roles=role_ids)
        
        if not user:
            return jsonify({
                'success': False,
                'error': 'Error updating roles'
            }), 500
        
        app.logger.info(f"Roles assigned to user {user.username} by {request.current_user.get('username')}")
        
        # Получаем информацию о ролях
        role_names = []
        role_details = []
        for role_id in user.roles:
            role = role_service.get_role_by_id(role_id)
            if role:
                role_names.append(role.name)
                role_details.append({
                    'id': role.id,
                    'name': role.name,
                    'description': role.description
                })
        
        return jsonify({
            'success': True,
            'user': user.to_dict(),
            'role_names': role_names,
            'role_details': role_details
        })
    except Exception as e:
        app.logger.error(f"Error assigning roles to user: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error assigning roles'
        }), 500


# ============================================================================
# ROLES API ENDPOINTS (CRUD)
# ============================================================================

@app.route('/api/roles', methods=['GET'])
@require_auth
def api_list_roles():
    """API: Получить список всех ролей"""
    try:
        roles = role_service.get_all_roles()
        roles_data = []
        
        for role in roles:
            role_dict = role.to_dict()
            
            # Добавляем информацию о правах доступа
            permission_names = []
            permission_details = []
            for perm_id in role.permissions:
                perm = permission_service.get_permission_by_id(perm_id)
                if perm:
                    permission_names.append(perm.name)
                    permission_details.append({
                        'id': perm.id,
                        'name': perm.name,
                        'description': perm.description,
                        'resource': perm.resource,
                        'action': perm.action
                    })
            
            role_dict['permission_names'] = permission_names
            role_dict['permission_details'] = permission_details
            roles_data.append(role_dict)
        
        return jsonify({
            'success': True,
            'roles': roles_data
        })
    except Exception as e:
        app.logger.error(f"Error listing roles: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting roles list'
        }), 500


@app.route('/api/roles', methods=['POST'])
@require_auth
def api_create_role():
    """API: Создать новую роль"""
    try:
        data = request.json or {}
        name = (data.get('name') or '').strip()
        description = (data.get('description') or '').strip() or ''
        raw_permissions = data.get('permissions') or []
        permissions = [str(p.get('id', p) if isinstance(p, dict) else p).strip() for p in raw_permissions if p is not None]
        permissions = [p for p in permissions if p]
        
        # Валидация входных данных
        is_valid, error_msg = validate_role_name(name)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 400
        
        # Валидация прав доступа (проверяем, что все права существуют)
        if permissions:
            for perm_id in permissions:
                perm = permission_service.get_permission_by_id(str(perm_id))
                if not perm:
                    return jsonify({
                        'success': False,
                        'error': f'Право доступа с ID {perm_id} не найдено'
                    }), 400
        
        # Создаем роль
        role = role_service.create_role(
            name=name,
            description=description,
            permissions=permissions
        )
        
        app.logger.info(f"Role {name} created by {request.current_user.get('username')}")
        
        role_dict = role.to_dict()
        
        # Добавляем информацию о правах доступа
        permission_names = []
        permission_details = []
        for perm_id in role.permissions:
            perm = permission_service.get_permission_by_id(perm_id)
            if perm:
                permission_names.append(perm.name)
                permission_details.append({
                    'id': perm.id,
                    'name': perm.name,
                    'description': perm.description,
                    'resource': perm.resource,
                    'action': perm.action
                })
        
        role_dict['permission_names'] = permission_names
        role_dict['permission_details'] = permission_details
        
        return jsonify({
            'success': True,
            'role': role_dict
        }), 201
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
    except Exception as e:
        app.logger.error(f"Error creating role: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error creating role'
        }), 500


@app.route('/api/roles/<role_id>', methods=['GET'])
@require_auth
def api_get_role(role_id):
    """API: Получить роль по ID"""
    try:
        role = role_service.get_role_by_id(role_id)
        
        if not role:
            return jsonify({
                'success': False,
                'error': 'Роль не найдена'
            }), 404
        
        role_dict = role.to_dict()
        
        # Добавляем информацию о правах доступа
        permission_names = []
        permission_details = []
        for perm_id in role.permissions:
            perm = permission_service.get_permission_by_id(perm_id)
            if perm:
                permission_names.append(perm.name)
                permission_details.append({
                    'id': perm.id,
                    'name': perm.name,
                    'description': perm.description,
                    'resource': perm.resource,
                    'action': perm.action
                })
        
        role_dict['permission_names'] = permission_names
        role_dict['permission_details'] = permission_details
        
        return jsonify({
            'success': True,
            'role': role_dict
        })
    except Exception as e:
        app.logger.error(f"Error getting role: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting role'
        }), 500


@app.route('/api/roles/<role_id>', methods=['PUT'])
@require_auth
def api_update_role(role_id):
    """API: Обновить роль"""
    try:
        data = request.json or {}
        name = data.get('name', '').strip() if data.get('name') else None
        description = data.get('description', '').strip() if data.get('description') else None
        permissions = data.get('permissions')  # Список ID прав доступа
        
        # Валидация имени, если оно изменяется
        if name is not None:
            is_valid, error_msg = validate_role_name(name)
            if not is_valid:
                return jsonify({
                    'success': False,
                    'error': error_msg
                }), 400
        
        # Валидация прав доступа, если они изменяются
        if permissions is not None:
            for perm_id in permissions:
                perm = permission_service.get_permission_by_id(perm_id)
                if not perm:
                    return jsonify({
                        'success': False,
                        'error': f'Право доступа с ID {perm_id} не найдено'
                    }), 400
        
        # Обновляем роль
        role = role_service.update_role(
            role_id=role_id,
            name=name,
            description=description,
            permissions=permissions
        )
        
        if not role:
            return jsonify({
                'success': False,
                'error': 'Роль не найдена'
            }), 404
        
        app.logger.info(f"Role {role.name} updated by {request.current_user.get('username')}")
        
        role_dict = role.to_dict()
        
        # Добавляем информацию о правах доступа
        permission_names = []
        permission_details = []
        for perm_id in role.permissions:
            perm = permission_service.get_permission_by_id(perm_id)
            if perm:
                permission_names.append(perm.name)
                permission_details.append({
                    'id': perm.id,
                    'name': perm.name,
                    'description': perm.description,
                    'resource': perm.resource,
                    'action': perm.action
                })
        
        role_dict['permission_names'] = permission_names
        role_dict['permission_details'] = permission_details
        
        return jsonify({
            'success': True,
            'role': role_dict
        })
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
    except Exception as e:
        app.logger.error(f"Error updating role: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error updating role'
        }), 500


@app.route('/api/roles/<role_id>', methods=['DELETE'])
@require_auth
def api_delete_role(role_id):
    """API: Удалить роль"""
    try:
        deleted = role_service.delete_role(role_id)
        
        if not deleted:
            return jsonify({
                'success': False,
                'error': 'Роль не найдена'
            }), 404
        
        app.logger.info(f"Role {role_id} deleted by {request.current_user.get('username')}")
        
        return jsonify({
            'success': True,
            'message': 'Роль успешно удалена'
        })
    except Exception as e:
        app.logger.error(f"Error deleting role: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error deleting role'
        }), 500


# ============================================================================
# PERMISSIONS API ENDPOINTS (CRUD)
# ============================================================================

@app.route('/api/permissions', methods=['GET'])
@require_auth
def api_list_permissions():
    """API: Получить список всех прав доступа"""
    try:
        permissions = permission_service.get_all_permissions()
        permissions_data = [perm.to_dict() for perm in permissions]
        
        return jsonify({
            'success': True,
            'permissions': permissions_data
        })
    except Exception as e:
        app.logger.error(f"Error listing permissions: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting permissions list'
        }), 500


@app.route('/api/permissions', methods=['POST'])
@require_auth
def api_create_permission():
    """API: Создать новое право доступа"""
    try:
        data = request.json or {}
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        resource = data.get('resource', '').strip()
        action = data.get('action', '').strip()
        
        # Валидация входных данных
        is_valid, error_msg = validate_permission_name(name)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 400
        
        if not resource:
            return jsonify({
                'success': False,
                'error': 'Ресурс обязателен'
            }), 400
        
        if not action:
            return jsonify({
                'success': False,
                'error': 'Действие обязательно'
            }), 400
        
        # Создаем право доступа
        permission = permission_service.create_permission(
            name=name,
            description=description,
            resource=resource,
            action=action
        )
        
        app.logger.info(f"Permission {name} created by {request.current_user.get('username')}")
        
        return jsonify({
            'success': True,
            'permission': permission.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
    except Exception as e:
        app.logger.error(f"Error creating permission: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error creating permission'
        }), 500


@app.route('/api/permissions/<perm_id>', methods=['GET'])
@require_auth
def api_get_permission(perm_id):
    """API: Получить право доступа по ID"""
    try:
        permission = permission_service.get_permission_by_id(perm_id)
        
        if not permission:
            return jsonify({
                'success': False,
                'error': 'Право доступа не найдено'
            }), 404
        
        return jsonify({
            'success': True,
            'permission': permission.to_dict()
        })
    except Exception as e:
        app.logger.error(f"Error getting permission: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting permission'
        }), 500


@app.route('/api/permissions/<perm_id>', methods=['PUT'])
@require_auth
def api_update_permission(perm_id):
    """API: Обновить право доступа"""
    try:
        data = request.json or {}
        name = data.get('name', '').strip() if data.get('name') else None
        description = data.get('description', '').strip() if data.get('description') else None
        resource = data.get('resource', '').strip() if data.get('resource') else None
        action = data.get('action', '').strip() if data.get('action') else None
        
        # Валидация имени, если оно изменяется
        if name is not None:
            is_valid, error_msg = validate_permission_name(name)
            if not is_valid:
                return jsonify({
                    'success': False,
                    'error': error_msg
                }), 400
        
        # Обновляем право доступа
        permission = permission_service.update_permission(
            perm_id=perm_id,
            name=name,
            description=description,
            resource=resource,
            action=action
        )
        
        if not permission:
            return jsonify({
                'success': False,
                'error': 'Право доступа не найдено'
            }), 404
        
        app.logger.info(f"Permission {permission.name} updated by {request.current_user.get('username')}")
        
        return jsonify({
            'success': True,
            'permission': permission.to_dict()
        })
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
    except Exception as e:
        app.logger.error(f"Error updating permission: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error updating permission'
        }), 500


@app.route('/api/permissions/<perm_id>', methods=['DELETE'])
@require_auth
def api_delete_permission(perm_id):
    """API: Удалить право доступа"""
    try:
        deleted = permission_service.delete_permission(perm_id)
        
        if not deleted:
            return jsonify({
                'success': False,
                'error': 'Право доступа не найдено'
            }), 404
        
        app.logger.info(f"Permission {perm_id} deleted by {request.current_user.get('username')}")
        
        return jsonify({
            'success': True,
            'message': 'Право доступа успешно удалено'
        })
    except Exception as e:
        app.logger.error(f"Error deleting permission: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error deleting permission'
        }), 500


@app.route('/api/permissions/by-resource/<resource>', methods=['GET'])
@require_auth
def api_get_permissions_by_resource(resource):
    """API: Получить все права доступа для определенного ресурса"""
    try:
        permissions = permission_service.get_permissions_by_resource(resource)
        permissions_data = [perm.to_dict() for perm in permissions]
        
        return jsonify({
            'success': True,
            'permissions': permissions_data
        })
    except Exception as e:
        app.logger.error(f"Error getting permissions by resource: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Error getting permissions'
        }), 500


# ============================================================================
# END USERS, ROLES, PERMISSIONS API ENDPOINTS
# ============================================================================


@app.route('/api/all_data')
@require_auth
def get_all_data():
    """Получить все данные: group_vars и host_vars для всех хостов"""
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'error': 'Project ID is required'}), 400
        
        # Получаем выбранные inventory файлы из запроса
        selected_files = request.args.getlist('inventory_files')
        # Если не указаны файлы, возвращаем пустой список хостов
        # (пользователь должен явно выбрать inventory файлы)
        
        # Используем пути проекта
        project_group_vars_dir = get_project_group_vars_dir(project_id)
        project_host_vars_dir = get_project_host_vars_dir(project_id)
        project_inventory_file = get_project_inventory_file(project_id)
        
        # Загружаем описания переменных из эталонного файла-шаблона
        descriptions = get_variable_descriptions()
        
        # Парсим всю папку inventories: все .yml и .yaml (в т.ч. test.yaml и др.), чтобы хосты из любых inventory отображались
        inventory_files_to_use = []
        project_dir = get_project_dir(project_id)
        inventories_dir = get_project_inventories_dir(project_id)
        if inventories_dir.exists():
            for inv_file in inventories_dir.rglob('*'):
                if not inv_file.is_file():
                    continue
                if not (inv_file.suffix in ('.yml', '.yaml', '.ini') or inv_file.name in ('hosts', 'hosts.ini')):
                    continue
                rel = inv_file.relative_to(inventories_dir)
                if 'group_vars' in rel.parts or 'host_vars' in rel.parts:
                    continue
                path_str = str(inv_file)
                if path_str not in inventory_files_to_use:
                    inventory_files_to_use.append(path_str)
        if not inventory_files_to_use and project_inventory_file.exists():
            inventory_files_to_use = [str(project_inventory_file)]
        
        if inventory_files_to_use:
            ensure_group_vars_host_vars_from_inventory(project_id, inventory_files_to_use)
        
        hosts = get_inventory_hosts(inventory_files_to_use) if inventory_files_to_use else []
        
        # Преобразуем полные пути к inventory файлам в относительные пути для отображения
        # Создаем маппинг полных путей к относительным
        inventory_path_mapping = {}
        for full_path in inventory_files_to_use:
            full_path_obj = Path(full_path)
            # Пытаемся найти относительный путь от project_dir
            try:
                rel_path = full_path_obj.relative_to(project_dir)
                # Преобразуем в формат, который используется в UI (например, test-inv/invent.yaml)
                # Убираем префиксы repo/ и inventory/ для совместимости
                rel_path_str = str(rel_path)
                if rel_path_str.startswith('repo/'):
                    rel_path_str = rel_path_str[5:]  # Убираем 'repo/'
                elif rel_path_str.startswith('inventory/'):
                    rel_path_str = rel_path_str[10:]  # Убираем 'inventory/'
                inventory_path_mapping[full_path] = rel_path_str
            except ValueError:
                # Если не удалось вычислить относительный путь, используем имя файла
                inventory_path_mapping[full_path] = full_path_obj.name
        
        # Обновляем inventory_file в hosts на относительные пути
        for host in hosts:
            if host.get('inventory_file'):
                # inventory_file содержит полный путь, нужно найти соответствующий относительный путь
                host_inventory_file = host['inventory_file']
                # Проверяем, есть ли точное совпадение
                if host_inventory_file in inventory_path_mapping:
                    host['inventory_file'] = inventory_path_mapping[host_inventory_file]
                else:
                    # Ищем по совпадению пути
                    for full_path, rel_path in inventory_path_mapping.items():
                        if full_path == host_inventory_file or full_path.endswith(host_inventory_file) or Path(full_path).name == Path(host_inventory_file).name:
                            host['inventory_file'] = rel_path
                            break
                    else:
                        # Если не нашли, используем имя файла
                        host['inventory_file'] = Path(host_inventory_file).name
        
        # Загружаем структуру групп из inventory
        groups = get_inventory_groups(inventory_files_to_use) if inventory_files_to_use else {}
        
        # Обновляем inventory_file в группах на относительные пути
        for group_name, group_data in groups.items():
            if isinstance(group_data, dict) and group_data.get('inventory_file'):
                group_inventory_file = group_data['inventory_file']
                # Проверяем, есть ли точное совпадение
                if group_inventory_file in inventory_path_mapping:
                    group_data['inventory_file'] = inventory_path_mapping[group_inventory_file]
                else:
                    # Ищем по совпадению пути
                    for full_path, rel_path in inventory_path_mapping.items():
                        if full_path == group_inventory_file or full_path.endswith(group_inventory_file) or Path(full_path).name == Path(group_inventory_file).name:
                            group_data['inventory_file'] = rel_path
                            break
                    else:
                        # Если не нашли, используем имя файла или исходное значение
                        if Path(group_inventory_file).is_absolute():
                            group_data['inventory_file'] = Path(group_inventory_file).name
                        else:
                            # Уже относительный путь, оставляем как есть
                            pass
        
        # Загружаем group_vars для всех групп из inventory
        # Ищем group_vars рядом с inventory файлами, а не только в общей директории
        group_vars = {}
        group_vars_by_group = {}  # Структурированные данные по группам
        
        # Сначала загружаем all.yml из общей директории
        all_group_vars_file = project_group_vars_dir / 'all.yml'
        all_group_vars = parse_yaml_with_comments(all_group_vars_file)
        group_vars.update(all_group_vars)  # Объединяем в общий объект
        group_vars_by_group['all'] = all_group_vars
        
        # Собираем все group_vars файлы из всех директорий рядом с inventory файлами
        repo_dir = project_dir / 'repo'
        inventories_dir = get_project_inventories_dir(project_id)
        
        # Сканируем все директории group_vars рядом с inventory файлами
        if inventories_dir.exists():
            for group_vars_dir in inventories_dir.rglob('group_vars'):
                if group_vars_dir.is_dir():
                    # Загружаем все .yml и .yaml файлы из этой директории
                    for ext in ['*.yml', '*.yaml']:
                        for group_file in group_vars_dir.glob(ext):
                            if group_file.is_file():
                                group_name = group_file.stem  # Имя файла без расширения
                                if group_name == 'all':
                                    continue  # Уже загрузили
                                
                                # Загружаем данные из файла
                                group_vars_data = parse_yaml_with_comments(group_file)
                                if group_vars_data:
                                    # Если для этой группы уже есть данные, объединяем (приоритет у файлов ближе к inventory)
                                    if group_name in group_vars_by_group:
                                        # Объединяем, но новые значения перезаписывают старые
                                        group_vars_by_group[group_name].update(group_vars_data)
                                    else:
                                        group_vars_by_group[group_name] = group_vars_data
                                    group_vars.update(group_vars_data)
                                    app.logger.debug(f"Loaded group_vars for {group_name} from {group_file}")
        
        # Также загружаем group_vars из общей директории для групп, которых еще нет
        for group_name in groups.keys():
            if group_name == 'all':
                continue  # Уже загрузили
            
            # Если для группы еще нет данных, пробуем загрузить из общей директории
            if group_name not in group_vars_by_group:
                fallback_file = project_group_vars_dir / f"{group_name}.yml"
                if fallback_file.exists():
                    group_vars_data = parse_yaml_with_comments(fallback_file)
                    if group_vars_data:
                        group_vars.update(group_vars_data)
                        group_vars_by_group[group_name] = group_vars_data
                        app.logger.debug(f"Loaded group_vars for {group_name} from fallback: {fallback_file}")
                else:
                    # Создаем пустой объект для группы, если файла нет
                    group_vars_by_group[group_name] = {}
        
        # Загружаем host_vars для каждого хоста из проекта
        # Ищем host_vars рядом с inventory файлами, а не только в общей директории
        host_vars_data = {}
        for host in hosts:
            host_name = host['name']
            inventory_file = host.get('inventory_file', '')
            
            # Определяем директорию host_vars на основе inventory файла
            host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file)
            host_file = host_vars_dir / f"{host_name}.yml"
            
            # Если файл не найден рядом с inventory, пробуем общую директорию (для обратной совместимости)
            if not host_file.exists():
                fallback_file = project_host_vars_dir / f"{host_name}.yml"
                if fallback_file.exists():
                    host_file = fallback_file
                    app.logger.debug(f"Using fallback host_vars file for {host_name}: {fallback_file}")
            
            host_vars_data[host_name] = parse_yaml_with_comments(host_file)
        
        # Переменные из INI inventory: [all:vars], [group:vars], инлайн хоста
        for group_name, group_data in groups.items():
            if isinstance(group_data, dict) and group_data.get('vars'):
                gvars = group_data['vars']
                group_vars_by_group.setdefault(group_name, {}).update(gvars)
                group_vars.update(gvars)
        for h in hosts:
            if h.get('vars'):
                hn = h['name']
                host_vars_data.setdefault(hn, {})
                host_vars_data[hn].update(h['vars'])
        
        # Собираем все уникальные ключи из всех источников
        all_keys = set(group_vars.keys())
        for group_vars_data in group_vars_by_group.values():
            all_keys.update(group_vars_data.keys())
        for host_vars in host_vars_data.values():
            all_keys.update(host_vars.keys())
        
        # Удаляем служебные ключи
        all_keys = {k for k in all_keys if not k.startswith('_')}
        
        # Создаем словарь для быстрого доступа к информации о хостах (включая inventory_file)
        hosts_info = {h['name']: {'inventory_file': h.get('inventory_file'), 'vars_file': h.get('vars_file')} for h in hosts}

        # Добавляем информацию о статусе хостов из in-memory кэша
        host_statuses = {}
        for h in hosts:
            host_name = h['name']
            status_data = get_host_check_status(project_id, host_name)
            if status_data:
                host_statuses[host_name] = status_data
                # Обогащаем hosts_info полями статуса
                host_info = hosts_info.get(host_name, {})
                host_info['status'] = status_data.get('status', 'unknown')
                host_info['last_checked_at'] = status_data.get('last_checked_at')
                host_info['status_expires_at'] = status_data.get('status_expires_at')
                hosts_info[host_name] = host_info
            else:
                # Если данных нет или TTL истёк — считаем статус неизвестным
                host_info = hosts_info.get(host_name, {})
                host_info.setdefault('status', 'unknown')
                host_info.setdefault('last_checked_at', None)
                host_info.setdefault('status_expires_at', None)
                hosts_info[host_name] = host_info
        
        return jsonify({
            'success': True,  # Указываем успешность запроса
            'group_vars': group_vars,  # Объединенные group_vars (для обратной совместимости)
            'group_vars_by_group': group_vars_by_group,  # Структурированные данные по группам
            'host_vars': host_vars_data,
            'hosts': [h['name'] for h in hosts],
            # Информация о хостах (inventory_file, vars_file, а также статус и таймстампы проверки)
            'hosts_info': hosts_info,
            # Отдельный объект со статусами хостов для будущего расширения API
            'hosts_status': host_statuses,
            'groups': groups,  # Добавляем структуру групп
            'all_keys': sorted(list(all_keys)),
            'descriptions': descriptions,
            'project_id': project_id  # Возвращаем project_id для frontend
        })
    except Exception as e:
        app.logger.error(f"Error in get_all_data: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preview', methods=['POST'])
@require_auth
def preview_data():
    """Предпросмотр сгенерированных YAML файлов"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Request must be JSON'}), 400
        
        data = request.json
        if data is None:
            return jsonify({'success': False, 'error': 'Empty request body'}), 400
        
        group_vars = data.get('group_vars', {}) or {}
        host_vars = data.get('host_vars', {}) or {}
        
        # Удаляем служебные ключи
        clean_group_vars = {k: v for k, v in group_vars.items() if not k.startswith('_')}
        
        # Генерируем YAML для group_vars
        stream = StringIO()
        yaml_loader.dump(clean_group_vars, stream)
        group_vars_yaml = stream.getvalue()
        
        # Генерируем YAML для host_vars
        host_vars_yaml = {}
        for host, vars_data in host_vars.items():
            if vars_data:
                clean_host_vars = {k: v for k, v in vars_data.items() if not k.startswith('_')}
                stream = StringIO()
                yaml_loader.dump(clean_host_vars, stream)
                host_vars_yaml[host] = stream.getvalue()
        
        return jsonify({
            'success': True,
            'group_vars': group_vars_yaml,
            'host_vars': host_vars_yaml
        })
    except Exception as e:
        app.logger.error(f'Error in preview_data: {str(e)}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/yaml/parse', methods=['POST'])
@require_auth
def api_yaml_parse():
    """API: Парсить YAML в JSON"""
    try:
        data = request.json or {}
        yaml_content = data.get('yaml', '')
        
        if not yaml_content:
            return jsonify({'success': False, 'error': 'YAML content is required'}), 400
        
        # Парсим YAML
        parsed_data = yaml.safe_load(yaml_content)
        
        if parsed_data is None:
            return jsonify({'success': False, 'error': 'YAML is empty or invalid'}), 400
        
        return jsonify({
            'success': True,
            'data': parsed_data
        })
        
    except yaml.YAMLError as e:
        return jsonify({'success': False, 'error': f'Invalid YAML syntax: {str(e)}'}), 400
    except Exception as e:
        app.logger.error(f"Error parsing YAML: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/yaml/validate', methods=['POST'])
@require_auth
def api_yaml_validate():
    """
    Реальная валидация YAML синтаксиса (parser-based) с line/column.

    Request JSON:
      { content: "<yaml text>", context: "...", filename?: "..." }

    Response:
      - { success: true, valid: true }
      - { success: true, valid: false, error: { message, line, column } }
      - { success: false, error: "..." }
    """
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Request must be JSON'}), 400

        data = request.get_json(silent=True) or {}
        content = data.get('content', '')
        context = data.get('context', 'generic')
        filename = data.get('filename', None)

        # Простейшая защита от "пустого документа" (YAML technically allows null, но UI требует non-empty)
        if content is None or not str(content).strip():
            return jsonify({
                'success': True,
                'valid': False,
                'error': {
                    'message': 'YAML cannot be empty',
                    'line': 1,
                    'column': 1
                }
            })

        # Используем ruamel.yaml для более точных координат ошибок
        yaml_validator = YAML(typ='safe')

        try:
            yaml_validator.load(content)
        except Exception as e:
            # ruamel.yaml exceptions often provide .problem_mark/.context_mark
            mark = getattr(e, 'problem_mark', None) or getattr(e, 'context_mark', None)
            line = None
            column = None
            if mark is not None:
                try:
                    # ruamel uses 0-based indices
                    line = int(mark.line) + 1
                    column = int(mark.column) + 1
                except Exception:
                    line = None
                    column = None

            problem = getattr(e, 'problem', None)
            msg = str(problem) if problem else str(e)

            # Фиксируем контекст только в логах, UI получает message/line/column
            app.logger.info(
                f"YAML validate failed (context={context}, filename={filename}): {type(e).__name__}: {msg}"
            )

            return jsonify({
                'success': True,
                'valid': False,
                'error': {
                    'message': msg,
                    'line': line or 1,
                    'column': column or 1
                }
            })

        return jsonify({'success': True, 'valid': True})

    except Exception as e:
        app.logger.error(f"Error in api_yaml_validate: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/save', methods=['POST'])
@require_auth
def save_all_data():
    """Сохранить все данные"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json
        group_vars = data.get('group_vars', {})
        host_vars = data.get('host_vars', {})
        backup_settings = data.get('backup_settings', {})
        
        backup_group_vars = backup_settings.get('group_vars', False)
        backup_host_vars = backup_settings.get('host_vars', False)
        
        project_dir = get_project_dir(project_id)
        repo_dir = project_dir / 'repo'
        
        # Используем пути проекта
        project_group_vars_dir = get_project_group_vars_dir(project_id)
        project_host_vars_dir = get_project_host_vars_dir(project_id)
        
        # Логируем действие пользователя
        app.logger.info(f"User saving variables to project {project_id}: group_vars ({len(group_vars)} variables), host_vars ({len(host_vars)} hosts), backup_group_vars={backup_group_vars}, backup_host_vars={backup_host_vars}")
        
        # Получаем структуру групп из inventory для определения inventory файлов
        hosts_list = get_inventory_hosts()
        inventory_files_full = []
        for h in hosts_list:
            inv_file = h.get('inventory_file', '')
            if inv_file:
                # Преобразуем относительный путь в полный
                full_path = repo_dir / inv_file
                if full_path.exists():
                    inventory_files_full.append(str(full_path))
        groups = get_inventory_groups(inventory_files_full)
        
        # Сохраняем group_vars для каждой группы рядом с inventory файлом
        # group_vars приходит в формате: {group_name: {var_name: var_value}}
        # Но также может быть общий объект group_vars для всех групп (старый формат)
        # Проверяем формат данных
        is_dict_of_dicts = group_vars and all(isinstance(v, dict) for v in group_vars.values())
        
        if not is_dict_of_dicts:
            # Старый формат: group_vars это общий объект переменных
            # Сохраняем в общую директорию для обратной совместимости
            group_vars_file = project_group_vars_dir / 'all.yml'
            project_group_vars_dir.mkdir(parents=True, exist_ok=True)
            try:
                backup_param = backup_group_vars if backup_group_vars is not None else None
                if not save_yaml_with_comments(group_vars_file, group_vars, create_backup_file=backup_param):
                    app.logger.error("Error saving group_vars")
                    return jsonify({'success': False, 'message': 'Error saving group_vars'}), 500
            except ValueError as e:
                app.logger.error(f"YAML validation error for group_vars: {e}")
                return jsonify({'success': False, 'message': f'Invalid YAML in group_vars: {str(e)}'}), 400
        else:
            # Новый формат: group_vars это словарь {group_name: {var_name: var_value}}
            # Сохраняем для каждой группы рядом с её inventory файлом
            for group_name, vars_data in group_vars.items():
                if not isinstance(vars_data, dict):
                    continue
                
                # Находим inventory файл для группы
                inventory_file = find_inventory_file_for_group(project_id, group_name)
                
                # Определяем директорию group_vars на основе inventory файла
                group_vars_dir = get_group_vars_dir_for_inventory(project_id, inventory_file)
                group_vars_dir.mkdir(parents=True, exist_ok=True)
                
                # Создаем папки рядом с inventory файлом, если нужно
                if inventory_file:
                    project_dir = get_project_dir(project_id)
                    repo_dir = project_dir / 'repo'
                    inventory_path = repo_dir / inventory_file
                    if inventory_path.exists():
                        ensure_inventory_dirs(inventory_path)
                
                group_file = group_vars_dir / f'{group_name}.yml'
                app.logger.info(f"Saving group_vars for {group_name} to {group_file} (inventory_file: {inventory_file})")
                
                try:
                    backup_param = backup_group_vars if backup_group_vars is not None else None
                    if not save_yaml_with_comments(group_file, vars_data, create_backup_file=backup_param):
                        app.logger.error(f"Error saving group_vars for {group_name}")
                        return jsonify({'success': False, 'message': f'Error saving group_vars for {group_name}'}), 500
                except ValueError as e:
                    app.logger.error(f"YAML validation error for group_vars/{group_name}: {e}")
                    return jsonify({'success': False, 'message': f'Invalid YAML in group_vars for {group_name}: {str(e)}'}), 400
        
        # Сохраняем host_vars для каждого хоста в проект
        # Получаем информацию о хостах для определения inventory файлов
        hosts_list = get_inventory_hosts()
        hosts_info_dict = {h['name']: h for h in hosts_list}
        
        for host, vars_data in host_vars.items():
            # Определяем inventory файл для хоста
            host_info = hosts_info_dict.get(host, {})
            inventory_file = host_info.get('inventory_file', '')
            
            # Определяем директорию host_vars на основе inventory файла
            host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file)
            host_vars_dir.mkdir(parents=True, exist_ok=True)
            
            host_file = host_vars_dir / f'{host}.yml'
            app.logger.info(f"Saving host_vars for {host} to {host_file} (inventory_file: {inventory_file})")
            
            try:
                backup_param = backup_host_vars if backup_host_vars is not None else None
                if not save_yaml_with_comments(host_file, vars_data, create_backup_file=backup_param):
                    app.logger.error(f"Error saving host_vars for {host}")
                    return jsonify({'success': False, 'message': f'Error saving host_vars for {host}'}), 500
            except ValueError as e:
                # Ошибка валидации YAML
                app.logger.error(f"YAML validation error for host_vars/{host}: {e}")
                return jsonify({'success': False, 'message': f'Invalid YAML in host_vars for {host}: {str(e)}'}), 400
        
        app.logger.info("Variables saved successfully")
        return jsonify({
            'success': True,
            'message': 'All variables saved successfully'
        })
    except Exception as e:
        app.logger.error(f"Error saving variables: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/hosts')
@require_auth
def get_hosts():
    """Получить список хостов из inventory"""
    hosts = get_inventory_hosts()
    return jsonify({'hosts': [h['name'] for h in hosts]})

@app.route('/api/inventory/hosts', methods=['GET'])
@require_auth
def api_get_inventory_hosts():
    """API: Получить список хостов из inventory проекта
    
    Поддерживает новую структуру: repo/inventories/<env>/hosts.yml
    И обратную совместимость: inventory/inventory.yml
    Также поддерживает выбранные inventory файлы через параметр inventory_files
    """
    try:
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        # Получаем выбранные inventory файлы из запроса
        selected_files = request.args.getlist('inventory_files')
        project_dir = get_project_dir(project_id)
        project_inventory_file = get_project_inventory_file(project_id)
        
        inventory_files_to_use = []
        
        if selected_files:
            # Если указаны файлы, используем их из директории проекта
            # Получаем inventories_dir для поддержки кастомных путей через repoLayout
            inventories_dir = get_project_inventories_dir(project_id)
            layout = get_repo_layout(project_id)
            inventories_layout_path = layout.get('inventories', 'inventories')
            
            for file_path_param in selected_files:
                file_path = None
                # Путь может начинаться с кастомного пути inventories (например, ansible/inventories/)
                if file_path_param.startswith(f'{inventories_layout_path}/'):
                    # Путь вида inventories/prod/hosts.yml или ansible/inventories/prod/hosts.yml
                    # Вычисляем относительный путь от inventories_dir
                    rel_path = file_path_param[len(inventories_layout_path) + 1:]  # Убираем префикс
                    file_path = inventories_dir / rel_path
                elif file_path_param.startswith('inventories/'):
                    rel_path = file_path_param[len('inventories/'):]
                    file_path = inventories_dir / rel_path
                elif file_path_param in ('inventory.yml', 'inventory.yaml', 'hosts.yml', 'hosts.yaml', 'hosts', 'hosts.ini'):
                    # Сначала в папке inventories
                    file_path = inventories_dir / file_path_param
                    if not file_path.exists():
                        file_path = project_dir / 'repo' / file_path_param
                elif '/' in file_path_param and not file_path_param.startswith('inventory/'):
                    # Путь вида prod/hosts.yml - добавляем inventories_dir
                    file_path = inventories_dir / file_path_param
                else:
                    # Если просто имя файла, ищем в inventories_dir
                    file_path = inventories_dir / file_path_param
                
                if file_path and file_path.exists():
                    inventory_files_to_use.append(str(file_path))
        else:
            # Файлы не указаны — собираем все inventory из папки inventories
            inventories_dir = get_project_inventories_dir(project_id)
            inventory_names = ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']
            if inventories_dir.exists():
                for name in inventory_names:
                    p = inventories_dir / name
                    if p.exists():
                        inventory_files_to_use.append(str(p))
                for inv_file in inventories_dir.rglob('*'):
                    if inv_file.is_file() and inv_file.name in inventory_names:
                        rel = inv_file.relative_to(inventories_dir)
                        if 'group_vars' not in rel.parts and 'host_vars' not in rel.parts:
                            path_str = str(inv_file)
                            if path_str not in inventory_files_to_use:
                                inventory_files_to_use.append(path_str)
            if not inventory_files_to_use and project_inventory_file.exists():
                inventory_files_to_use = [str(project_inventory_file)]
        
        hosts = get_inventory_hosts(inventory_files_to_use) if inventory_files_to_use else []
        host_names = [h['name'] for h in hosts]
        
        return jsonify({
            'success': True,
            'hosts': host_names
        })
    except Exception as e:
        app.logger.error(f"Error getting inventory hosts: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/dashboard_stats')
@require_auth
def get_dashboard_stats():
    """Получить статистику для дашборда (автоматически пересчитывается при каждом запросе)"""
    try:
        # Количество хостов (только активные, не закомментированные)
        hosts = get_inventory_hosts()
        hosts_count = len(hosts)
        
        # Количество ролей (директории в roles/, исключая disabled и служебные)
        roles_dir = BASE_DIR / 'roles'
        roles_count = 0
        if roles_dir.exists():
            for item in roles_dir.iterdir():
                if item.is_dir() and not item.name.startswith('_') and item.name != 'disabled':
                    # Проверяем наличие tasks/main.yaml или tasks/main.yml
                    tasks_dir = item / 'tasks'
                    if tasks_dir.exists():
                        if (tasks_dir / 'main.yaml').exists() or (tasks_dir / 'main.yml').exists():
                            roles_count += 1
        
        # Количество тасков (все файлы main.yaml/yml в tasks/, исключая disabled)
        # Считаем каждый файл отдельно, так как в одной роли может быть несколько файлов тасков
        tasks_count = 0
        if roles_dir.exists():
            for task_file in roles_dir.rglob('tasks/main.*'):
                # Исключаем disabled роли
                if 'disabled' not in str(task_file):
                    # Проверяем, что это действительно файл main.yaml или main.yml
                    if task_file.name in ['main.yaml', 'main.yml']:
                        tasks_count += 1
        
        # Количество переменных (из group_vars/all.yml, исключая служебные)
        # Получаем project_id для Project Storage
        project_id = get_project_id_from_request()
        variables_count = 0
        if project_id:
            project_group_vars_dir = get_project_group_vars_dir(project_id)
            group_vars_file = project_group_vars_dir / 'all.yml'
            if group_vars_file.exists():
                group_vars = parse_yaml_with_comments(group_vars_file)
                variables_count = len([k for k in group_vars.keys() if not k.startswith('_')])
        
        return jsonify({
            'success': True,
            'stats': {
                'hosts': hosts_count,
                'roles': roles_count,
                'tasks': tasks_count,
                'variables': variables_count
            }
        })
    except Exception as e:
        app.logger.error(f'Error in get_dashboard_stats: {str(e)}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# Глобальная переменная для хранения вывода Ansible
ansible_output = {'output': '', 'status': 'idle', 'process': None}

def generate_dynamic_playbook(selected_roles):
    """Генерирует динамический playbook на основе выбранных ролей"""
    # Маппинг тегов на имена ролей
    role_mapping = {
        '00_init': '00_init',
        '01_backup_etc': '01_backup_etc',
        '02_init_sshd': '02_init_sshd',
        '03_configure_users': '03_configure_users',
        '04_configure_hostname': '04_configure_hostname',
        '05_configure_sysctl_limits': '05_configure_sysctl_limits',
        '06_configure_kernel': '06_configure_kernel',
        '07_remove_unwanted_services': '07_remove_unwanted_services',
        '08_configure_security': '08_configure_security',
        '09_configure_locales': '09_configure_locales',
        '10_manage_services': '10_manage_services',
        '11_certificates': '11_certificates',
        '12_date_timezone': '12_date_timezone',
        '13_configure_repo': '13_configure_repo',
        '14_install_software': '14_install_software',
        '15_configure_bash': '15_configure_bash',
        '16_configure_network': '16_configure_network',
        '17_update_reboot': '17_update_reboot'
    }
    
    # Описания ролей для комментариев
    role_descriptions = {
        '00_init': 'System initialization',
        '01_backup_etc': 'Backup must be FIRST - create a backup before making changes',
        '02_init_sshd': 'SSH and user configuration',
        '03_configure_users': 'SSH and user configuration',
        '04_configure_hostname': 'Basic system configuration',
        '05_configure_sysctl_limits': 'Basic system configuration',
        '06_configure_kernel': 'Basic system configuration',
        '07_remove_unwanted_services': 'Basic system configuration',
        '08_configure_security': 'Basic system configuration',
        '09_configure_locales': 'Basic system configuration',
        '10_manage_services': 'Basic system configuration',
        '11_certificates': 'Network settings and certificates',
        '12_date_timezone': 'Network settings and certificates',
        '13_configure_repo': 'Network settings and certificates',
        '14_install_software': 'Network settings and certificates',
        '15_configure_bash': 'Additional configuration',
        '16_configure_network': 'Additional configuration',
        '17_update_reboot': 'Final actions - update and reboot must be LAST'
    }
    
    playbook_content = """- hosts: all
  become: True
  remote_user: root
  vars:
    ansible_ssh_user: root
  roles:
"""
    
    # Сортируем роли по номерам для правильного порядка выполнения
    # Фильтруем только роли из roles-storage (с префиксом pack/), исключаем старые роли
    filtered_roles = [r for r in selected_roles if '/' in r]
    sorted_roles = sorted(filtered_roles, key=lambda x: (
        int(x.split('/')[-1].split('_')[0]) if x.split('/')[-1].split('_')[0].isdigit() else 999
    ))
    
    # Добавляем только выбранные роли в правильном порядке
    for role_tag in sorted_roles:
        # Роли приходят в формате "core-roles/00_init"
        # Извлекаем имя роли и путь
        if '/' in role_tag:
            pack_id, role_name = role_tag.split('/', 1)
            role_path = f"roles-storage/{pack_id}/{role_name}"
            # Извлекаем описание из role_descriptions по имени роли (без префикса)
            description = role_descriptions.get(role_name, '')
            if description:
                playbook_content += f"\n    # {description}\n"
            playbook_content += f"    - role: {role_path}\n"
            playbook_content += f"      tags: [{role_tag}]\n"
    
    return playbook_content


@app.route('/api/run_ansible', methods=['POST'])
@require_auth
def run_ansible():
    """Запуск Ansible playbook"""
    global ansible_output
    
    try:
        # Проверяем, не запущен ли уже процесс
        if ansible_output['status'] == 'running':
            app.logger.warning("Attempt to start Ansible while process is already running")
            return jsonify({'success': False, 'error': 'Ansible is already running'}), 400
        
        # Получаем project_id - REQUIRED, no fallback
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        # Получаем список выбранных хостов, ролей и inventory файлов из запроса
        data = request.json or {}
        selected_hosts = data.get('hosts', [])
        selected_roles = data.get('roles', [])
        selected_inventory_files = data.get('inventory_files', [])
        selected_ansible_config = data.get('ansible_config')
        if not selected_ansible_config:
            app.logger.error(f"[run_ansible] ansible_config not provided in request")
            return jsonify({'success': False, 'error': 'ansible_config is required'}), 400
        # Логируем действие пользователя
        app.logger.info(f"User starting Ansible playbook: project={project_id}, hosts={selected_hosts}, roles={selected_roles}, inventory={selected_inventory_files}")
        
        # Создаем execution record
        execution_id = None
        settings = load_execution_settings()
        if settings.get('save_history', True):
            # Получаем snapshot inventory
            inventory_snapshot = {'groups': []}
            try:
                groups = get_inventory_groups(selected_inventory_files if selected_inventory_files else None)
                for group_name, group_data in groups.items():
                    group_hosts = group_data.get('hosts', [])
                    inventory_snapshot['groups'].append({
                        'groupId': group_name,
                        'groupName': group_name,
                        'hosts': [{'hostId': host, 'ip': host} for host in group_hosts]
                    })
            except Exception as e:
                app.logger.warning(f"Error creating inventory snapshot: {e}")
            
            # Создаем snapshot selection
            selection_snapshot = {
                'selectedHostIds': selected_hosts,
                'mandatoryRoleIds': ['00_init', '02_init_sshd', '08_configure_security']
            }
            
            # Вычисляем effective roles by host (все хосты получают одинаковые роли)
            effective_roles_by_host = {}
            for host in selected_hosts:
                effective_roles_by_host[host] = selected_roles
            
            execution_data = {
                'playbookName': 'dynamic_playbook',
                'mode': 'ALL_SELECTED_HOSTS',
                'inventorySnapshot': inventory_snapshot,
                'selectionSnapshot': selection_snapshot,
                'stats': {
                    'hostsTargeted': len(selected_hosts),
                    'totalRoleExecutions': len(selected_roles) * len(selected_hosts)
                },
                'warnings': []
            }
            
            execution_id = create_execution_record(execution_data, project_id=project_id)
            if execution_id:
                ansible_output['execution_id'] = execution_id
                ansible_output['project_id'] = project_id
                app.logger.debug(f"Created execution record: {execution_id} for project {project_id}")
        
        if not selected_hosts:
            app.logger.warning("Attempt to run Ansible without selected hosts")
            return jsonify({'success': False, 'error': 'No hosts selected'}), 400
        
        if not selected_roles:
            app.logger.warning("Attempt to run Ansible without selected roles")
            return jsonify({'success': False, 'error': 'No roles selected'}), 400
        
        # Если не указаны inventory файлы, используем стандартный
        if not selected_inventory_files:
            selected_inventory_files = ['inventory.yml']
        
        # Генерируем динамический playbook
        playbook_content = generate_dynamic_playbook(selected_roles)
        
        # Сохраняем сгенерированный playbook в runtime/generated_playbooks/<execution_id>.yml
        temp_playbook = None
        if execution_id:
            generated_playbook_path = get_project_generated_playbook_path(project_id, execution_id)
            with open(generated_playbook_path, 'w', encoding='utf-8') as f:
                f.write(playbook_content)
            temp_playbook = generated_playbook_path
        else:
            # Fallback: если execution_id не создан, используем временный файл
            temp_playbook = TEMP_DIR / f'playbook_{uuid.uuid4().hex[:8]}.yaml'
            temp_playbook.parent.mkdir(exist_ok=True)
            with open(temp_playbook, 'w', encoding='utf-8') as f:
                f.write(playbook_content)
        
        # Get group_vars from Project Storage
        project_group_vars_dir = get_project_group_vars_dir(project_id)
        group_vars_file = project_group_vars_dir / 'all.yml'
        
        # Get inventory directory from Project Storage
        project_inventory_dir = get_project_inventory_file(project_id).parent
        
        # Объединяем несколько inventory файлов в один временный файл
        temp_inventory = None
        inventory_args = []
        
        if len(selected_inventory_files) == 1:
            # Если один файл, используем его напрямую из Project Storage
            inv_path = project_inventory_dir / selected_inventory_files[0]
            if inv_path.exists():
                inventory_args = ['-i', str(inv_path)]
            else:
                # Fallback to default inventory.yml in project storage
                default_inv = get_project_inventory_file(project_id)
                if default_inv.exists():
                    inventory_args = ['-i', str(default_inv)]
                else:
                    return jsonify({'success': False, 'error': f'Inventory file not found in project storage: {selected_inventory_files[0]}'}), 400
        elif len(selected_inventory_files) > 1:
            # Если несколько файлов, объединяем их из Project Storage
            temp_inventory = TEMP_DIR / f'inventory_{uuid.uuid4().hex[:8]}.yml'
            temp_inventory.parent.mkdir(exist_ok=True)
            
            combined_inventory = {'all': {'hosts': {}}}
            
            for inv_file in selected_inventory_files:
                inv_path = project_inventory_dir / inv_file
                if inv_path.exists():
                    try:
                        with open(inv_path, 'r', encoding='utf-8') as f:
                            inv_data = yaml.safe_load(f)
                            if inv_data and 'all' in inv_data and 'hosts' in inv_data['all']:
                                # Объединяем хосты из всех файлов
                                for host, config in inv_data['all']['hosts'].items():
                                    if host not in combined_inventory['all']['hosts']:
                                        combined_inventory['all']['hosts'][host] = config
                    except Exception as e:
                        app.logger.warning(f"Error reading inventory file {inv_file}: {e}")
            
            # Сохраняем объединенный inventory
            with open(temp_inventory, 'w', encoding='utf-8') as f:
                yaml.dump(combined_inventory, f, default_flow_style=False, allow_unicode=True)
            
            inventory_args = ['-i', str(temp_inventory)]
            ansible_output['temp_inventory'] = str(temp_inventory)  # Сохраняем для удаления
        else:
            # Если нет файлов, используем стандартный из Project Storage
            default_inv = get_project_inventory_file(project_id)
            if not default_inv.exists():
                return jsonify({'success': False, 'error': 'Default inventory.yml not found in project storage'}), 400
            inventory_args = ['-i', str(default_inv)]
        
        # Команда для запуска
        cmd = [
            'ansible-playbook',
            str(temp_playbook),
            *inventory_args,
            '-e', f'project_root={BASE_DIR}',
            '--limit', ','.join(selected_hosts)
        ]
        
        # Добавляем group_vars/all.yml если он существует
        if group_vars_file.exists():
            cmd.extend(['-e', f'@{group_vars_file}'])
        
        # Устанавливаем переменные окружения
        env = os.environ.copy()
        env['GIT_SSH_COMMAND'] = 'ssh -o StrictHostKeyChecking=no'
        env['JOB_NAME'] = 'redis_prepare'
        # Get ansible config (для ansible.cfg — корень проекта)
        project_dir = get_project_dir(project_id)
        ansible_config_path = resolve_ansible_config_path(project_id, selected_ansible_config)
        ansible_config_path.parent.mkdir(parents=True, exist_ok=True)
        if not ansible_config_path.exists() or not ansible_config_path.is_file():
            app.logger.debug(f"[run_ansible] Config file not found: {ansible_config_path}, creating minimal config")
            if not create_minimal_ansible_config(ansible_config_path):
                app.logger.warning(f"[run_ansible] Failed to create minimal config, continuing without ANSIBLE_CONFIG")
        
        ansible_config_used = None
        app.logger.debug(f"[run_ansible] === Ansible Config Resolution ===")
        app.logger.debug(f"[run_ansible] Selected config file: {selected_ansible_config}")
        app.logger.debug(f"[run_ansible] Project directory: {project_dir}")
        app.logger.debug(f"[run_ansible] Resolved config path: {ansible_config_path}")
        
        if ansible_config_path.exists() and ansible_config_path.is_file():
            env['ANSIBLE_CONFIG'] = str(ansible_config_path)
            ansible_config_used = str(ansible_config_path)
            app.logger.debug(f"[run_ansible] ✓ Using Ansible config: {ansible_config_used}")
        else:
            app.logger.warning(f"[run_ansible] ✗ Ansible config {selected_ansible_config} not found (requested: {ansible_config_path})")
            if not ansible_config_path.parent.exists():
                app.logger.warning(f"[run_ansible] Directory {ansible_config_path.parent} does not exist")
            app.logger.warning(f"[run_ansible] Using default Ansible config")
            ansible_config_used = "default (system default)"
        
        # Добавляем путь к roles-playbooks в roles_path
        # Роли находятся в roles-playbooks внутри проекта
        project_roles_path = project_dir / 'roles-playbooks'
        if project_roles_path.exists():
            # ANSIBLE_ROLES_PATH может содержать несколько путей через двоеточие
            existing_roles_path = env.get('ANSIBLE_ROLES_PATH', '')
            if existing_roles_path:
                env['ANSIBLE_ROLES_PATH'] = f"{str(project_roles_path)}:{existing_roles_path}"
            else:
                env['ANSIBLE_ROLES_PATH'] = str(project_roles_path)
            app.logger.debug(f"[run_ansible] Set ANSIBLE_ROLES_PATH: {env['ANSIBLE_ROLES_PATH']}")
        else:
            app.logger.warning(f"[run_ansible] Directory roles-playbooks not found: {project_roles_path}")
        
        # Детальное логирование команды ansible-playbook
        cmd_str = ' '.join([f'"{arg}"' if ' ' in str(arg) else str(arg) for arg in cmd])
        
        # Логируем все важные параметры
        log_details = [
            f"=== Ansible Playbook Execution Details ===",
            f"Project ID: {project_id}",
            f"Ansible Config: {ansible_config_used}",
            f"Selected Config File: {selected_ansible_config}",
            f"Selected Hosts: {', '.join(selected_hosts)}",
            f"Selected Roles: {', '.join(selected_roles)}",
            f"Inventory Files: {', '.join(selected_inventory_files) if selected_inventory_files else 'default inventory.yml'}",
            f"Playbook File: {temp_playbook}",
            f"Group Vars File: {group_vars_file if group_vars_file.exists() else 'not used'}",
            f"Working Directory: {BASE_DIR}",
            f"",
            f"Command: {cmd_str}",
            f"",
            f"Environment Variables:"
        ]
        
        # Логируем все переменные окружения, связанные с Ansible
        ansible_env_vars = ['ANSIBLE_CONFIG', 'ANSIBLE_ROLES_PATH', 'GIT_SSH_COMMAND', 'JOB_NAME', 'ANSIBLE_HOST_KEY_CHECKING', 
                           'ANSIBLE_SSH_ARGS', 'ANSIBLE_FORCE_COLOR', 'ANSIBLE_NOCOLOR']
        for key in ansible_env_vars:
            if key in env:
                log_details.append(f"  {key}={env[key]}")
        
        # Логируем все детали (DEBUG для детальной отладки)
        full_log_message = '\n'.join(log_details)
        app.logger.debug(full_log_message)
        
        # Также сохраняем в execution record для отображения в UI
        if execution_id:
            append_execution_log(execution_id, full_log_message, project_id=project_id)
        
        # Сбрасываем вывод
        ansible_output['output'] = ''
        ansible_output['status'] = 'running'
        ansible_output['start_time'] = time.time()
        ansible_output['temp_playbook'] = str(temp_playbook)  # Сохраняем путь для удаления
        
        # Запускаем процесс в отдельном потоке
        def run_ansible_process():
            global ansible_output
            temp_file_path = Path(ansible_output.get('temp_playbook', ''))
            try:
                # Логируем начало выполнения и полную команду (DEBUG для детальной отладки)
                cmd_str = ' '.join([f'"{arg}"' if ' ' in str(arg) else str(arg) for arg in cmd])
                app.logger.debug(f"[run_ansible_process] ========== Starting ansible-playbook ==========")
                app.logger.debug(f"[run_ansible_process] Full command: {cmd_str}")
                app.logger.debug(f"[run_ansible_process] Working directory (cwd): {project_dir}")
                app.logger.debug(f"[run_ansible_process] ANSIBLE_CONFIG: {env.get('ANSIBLE_CONFIG', 'not set')}")
                app.logger.debug(f"[run_ansible_process] ===========================================")
                
                # Используем project_dir как рабочую директорию, чтобы Ansible мог найти group_vars и host_vars
                # Путь к playbook должен быть абсолютным, так как мы меняем рабочую директорию
                cmd_with_abs_paths = []
                i = 0
                while i < len(cmd):
                    arg = cmd[i]
                    # Если это опция -i, следующий аргумент - путь к inventory
                    if arg == '-i' and i + 1 < len(cmd):
                        cmd_with_abs_paths.append(arg)
                        inv_path = Path(cmd[i + 1])
                        cmd_with_abs_paths.append(str(inv_path.resolve()))
                        i += 2
                    elif arg == str(temp_playbook):
                        # Преобразуем путь к playbook в абсолютный
                        cmd_with_abs_paths.append(str(temp_playbook.resolve()))
                        i += 1
                    else:
                        cmd_with_abs_paths.append(arg)
                        i += 1
                
                process = subprocess.Popen(
                    cmd_with_abs_paths,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    cwd=str(project_dir),
                    env=env
                )
                
                ansible_output['process'] = process
                app.logger.debug(f"[run_ansible_process] Process started, PID: {process.pid}")
                
                # Читаем вывод построчно
                output_lines = []
                for line in process.stdout:
                    line = line.rstrip()
                    output_lines.append(line)
                    ansible_output['output'] = '\n'.join(output_lines)
                    
                    # Сохраняем лог в execution record
                    if ansible_output.get('execution_id'):
                        append_execution_log(ansible_output['execution_id'], line, project_id=project_id)
                
                process.wait()
                
                # Логируем завершение процесса
                return_code = process.returncode
                app.logger.debug(f"[run_ansible_process] Process completed with return code: {return_code}")
                
                # Сохраняем код возврата в execution record
                if ansible_output.get('execution_id'):
                    status_message = f"\n=== Execution completed with return code: {return_code} ==="
                    if return_code == 0:
                        status_message += " (SUCCESS)"
                    else:
                        status_message += f" (FAILED)"
                    append_execution_log(ansible_output['execution_id'], status_message, project_id=project_id)
                
                # Сохраняем финальное время выполнения
                if ansible_output.get('start_time'):
                    ansible_output['end_time'] = time.time()
                    ansible_output['elapsed_time'] = int(ansible_output['end_time'] - ansible_output['start_time'])
                
                if process.returncode == 0:
                    ansible_output['status'] = 'completed'
                    final_status = 'SUCCESS'
                else:
                    ansible_output['status'] = 'failed'
                    final_status = 'FAILED'
                
                # Обновляем execution record
                if ansible_output.get('execution_id'):
                    finished_at = time.time()
                    update_data = {
                        'status': final_status,
                        'finishedAt': finished_at
                    }
                    # Добавляем duration если он был вычислен
                    if ansible_output.get('elapsed_time'):
                        update_data['duration'] = ansible_output['elapsed_time']
                    update_execution_record(ansible_output['execution_id'], update_data, project_id=project_id)
                    # Применяем retention policy после завершения
                    apply_retention_policy()
                    
            except Exception as e:
                ansible_output['status'] = 'error'
                error_msg = f'\nError: {str(e)}'
                ansible_output['output'] += error_msg
                
                # Сохраняем ошибку в лог
                if ansible_output.get('execution_id'):
                    append_execution_log(ansible_output['execution_id'], error_msg, project_id=project_id)
                    update_execution_record(ansible_output['execution_id'], {
                        'status': 'FAILED',
                        'finishedAt': time.time()
                    }, project_id=project_id)
                    apply_retention_policy()
                
                # Сохраняем время даже при ошибке
                if ansible_output.get('start_time'):
                    ansible_output['end_time'] = time.time()
                    ansible_output['elapsed_time'] = int(ansible_output['end_time'] - ansible_output['start_time'])
            finally:
                ansible_output['process'] = None
                # Удаляем временные файлы
                try:
                    if temp_file_path and temp_file_path.exists():
                        temp_file_path.unlink()
                except Exception:
                    pass
                try:
                    temp_inv_path = ansible_output.get('temp_inventory')
                    if temp_inv_path:
                        inv_path = Path(temp_inv_path)
                        if inv_path.exists():
                            inv_path.unlink()
                except Exception:
                    pass
                ansible_output['temp_playbook'] = None
                ansible_output['temp_inventory'] = None
        
        thread = threading.Thread(target=run_ansible_process, daemon=True)
        thread.start()
        
        response_data = {
            'success': True,
            'message': f'Ansible started on hosts: {", ".join(selected_hosts)}'
        }
        
        if execution_id:
            response_data['executionId'] = execution_id
        
        return jsonify(response_data)
        
    except Exception as e:
        ansible_output['status'] = 'error'
        ansible_output['output'] = str(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ansible_status', methods=['GET'])
@require_auth
def get_ansible_status():
    """Получить статус выполнения Ansible"""
    global ansible_output
    elapsed_time = 0
    
    # Если есть сохраненное финальное время, используем его
    if ansible_output.get('elapsed_time') is not None:
        elapsed_time = ansible_output['elapsed_time']
    elif ansible_output.get('start_time') and ansible_output['status'] == 'running':
        # Если выполняется, вычисляем текущее время
        elapsed_time = int(time.time() - ansible_output['start_time'])
    
    return jsonify({
        'success': True,
        'status': ansible_output['status'],
        'output': ansible_output['output'],
        'elapsed_time': elapsed_time
    })


@app.route('/api/stop_ansible', methods=['POST'])
@require_auth
def stop_ansible():
    """Остановить выполнение Ansible"""
    global ansible_output
    
    try:
        if ansible_output['process']:
            ansible_output['process'].terminate()
            ansible_output['status'] = 'stopped'
            ansible_output['output'] += '\n\n[Stopped by user]'
            return jsonify({'success': True, 'message': 'Ansible stopped'})
        else:
            return jsonify({'success': False, 'error': 'Process is not running'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/list', methods=['GET'])
@require_auth
def list_inventory_files():
    """Получить список inventory файлов в папке inventories.
    
    Включает все .yml и .yaml файлы в inventories (и подпапках), кроме host_vars/ и group_vars/,
    чтобы отображались и стандартные имена (inventory.yml, hosts.yml), и добавленные вручную (например test.yaml).
    """
    try:
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        inventory_files = []
        project_dir = get_project_dir(project_id)
        inventories_dir = get_project_inventories_dir(project_id)
        repo_dir = project_dir / 'repo'
        
        inventory_full_paths = []
        if inventories_dir.exists():
            for inv_file in inventories_dir.rglob('*'):
                if not inv_file.is_file():
                    continue
                if not (inv_file.suffix in ('.yml', '.yaml', '.ini') or inv_file.name in ('hosts', 'hosts.ini')):
                    continue
                rel_path = inv_file.relative_to(inventories_dir)
                if 'host_vars' in rel_path.parts or 'group_vars' in rel_path.parts:
                    continue
                inventory_full_paths.append(str(inv_file))
                try:
                    repo_rel_path = inv_file.relative_to(repo_dir)
                except ValueError:
                    repo_rel_path = rel_path
                env = rel_path.parts[0] if len(rel_path.parts) > 1 else None
                inventory_files.append({
                    'name': str(rel_path),
                    'path': str(repo_rel_path),
                    'env': env
                })
        for name in ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']:
            root_inv = repo_dir / name
            if root_inv.is_file():
                path_str = name
                if not any(f.get('path') == path_str or f.get('path', '').endswith('/' + name) for f in inventory_files):
                    inventory_files.append({'name': name, 'path': path_str, 'env': None})
                    inventory_full_paths.append(str(root_inv))
        
        if inventory_full_paths:
            ensure_group_vars_host_vars_from_inventory(project_id, inventory_full_paths)
        
        inventory_files.sort(key=lambda x: x['path'])
        return jsonify({'success': True, 'files': inventory_files})
    except Exception as e:
        app.logger.error(f"Error listing inventory files: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/get', methods=['GET'])
@require_auth
def get_inventory():
    """Получить содержимое inventory файла из Project Storage
    
    Поддерживает любые пути к inventory файлам, включая кастомные пути через repoLayout.
    Рекурсивно ищет файлы в папке inventories и её подпапках.
    
    CRITICAL: No fallback to BASE_DIR. Returns 404 if file doesn't exist in project storage.
    """
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_path_param = request.args.get('file', 'inventories/inventory.yml')
        project_dir = get_project_dir(project_id)
        file_path = None
        
        # Получаем inventories_dir для поддержки кастомных путей через repoLayout
        inventories_dir = get_project_inventories_dir(project_id)
        
        # Получаем layout для определения пути inventories относительно repo
        layout = get_repo_layout(project_id)
        inventories_layout_path = layout.get('inventories', 'inventories')
        
        # Приоритет поиска:
        # 1. Путь начинается с пути inventories (с учетом repoLayout)
        if file_path_param.startswith(f'{inventories_layout_path}/'):
            # Путь вида inventories/prod/hosts.yml или ansible/inventories/prod/hosts.yml
            # Вычисляем относительный путь от inventories_dir
            rel_path = file_path_param[len(inventories_layout_path) + 1:]  # Убираем префикс "inventories/" или "ansible/inventories/"
            file_path = inventories_dir / rel_path
        elif file_path_param == 'inventory.yml':
            # 2. Простой файл в корне: repo/inventory.yml
            file_path = project_dir / 'repo' / 'inventory.yml'
        elif '/' in file_path_param and not file_path_param.startswith('inventory/'):
            # Путь вида prod/hosts.yml - добавляем inventories_dir
            file_path = inventories_dir / file_path_param
        else:
            # Если просто имя файла, ищем в inventories_dir
            file_path = inventories_dir / file_path_param
        
        # Fallback: если файл не найден по inventories_dir, пробуем прямой путь repo/<file_path_param>
        if not file_path or not file_path.exists():
            if file_path_param.startswith('inventories/'):
                direct_path = project_dir / 'repo' / file_path_param
                if direct_path.exists() and direct_path.is_file():
                    file_path = direct_path
            if not file_path or not file_path.exists():
                if file_path_param != 'inventory.yml':
                    root_inventory = project_dir / 'repo' / 'inventory.yml'
                    if root_inventory.exists():
                        file_path = root_inventory
                    else:
                        return jsonify({'success': False, 'error': f'File {file_path_param} not found in Project Storage'}), 404
                else:
                    return jsonify({'success': False, 'error': f'File {file_path_param} not found in Project Storage'}), 404
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        vault_id = request.args.get('vault_id') or request.args.get('vaultId')
        resp = {'success': True, 'content': content, 'file': file_path_param}
        decrypted, was_encrypted, vault_id_used, vault_id_required = _decrypt_content_if_encrypted(project_id, content, vault_id)
        if was_encrypted:
            if vault_id_required:
                vaults = _load_vaults(project_id)
                return jsonify({
                    'success': True,
                    'encrypted': True,
                    'vaultIdRequired': True,
                    'vaults': vaults,
                    'file': file_path_param
                })
            if decrypted is None:
                return jsonify({'success': False, 'error': 'Failed to decrypt vault-encrypted file. Check vault key.'}), 500
            resp['content'] = decrypted
            resp['encrypted'] = True
            resp['vaultId'] = vault_id_used
        return jsonify(resp)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/save', methods=['POST'])
@require_auth
def save_inventory():
    """Сохранить inventory файл
    
    Новая структура: inventories/<env>/hosts.yml
    """
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_path_param = data.get('file', 'inventories/inventory.yml')
        content = data.get('content', '')
        env = data.get('env', None)  # Окружение опционально
        vault_id = data.get('vaultId') or data.get('vault_id')
        
        app.logger.info(f"User saving inventory file: {file_path_param} to project {project_id}")
        
        if not content:
            return jsonify({'success': False, 'error': 'File content cannot be empty'}), 400
        
        # Валидация YAML на backend (до шифрования)
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            error_msg = str(e)
            if hasattr(e, 'problem_mark'):
                mark = e.problem_mark
                error_msg = f"YAML error at line {mark.line + 1}, column {mark.column + 1}: {error_msg}"
            return jsonify({'success': False, 'error': f'YAML validation error: {error_msg}'}), 400
        
        # Ansible-vault: шифрование при наличии vault_id
        if vault_id:
            encrypted_content, err = _encrypt_content_if_requested(project_id, content, vault_id)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            content = encrypted_content
        
        project_dir = get_project_dir(project_id)
        # Получаем inventories_dir для поддержки кастомных путей через repoLayout
        inventories_dir = get_project_inventories_dir(project_id)
        layout = get_repo_layout(project_id)
        inventories_layout_path = layout.get('inventories', 'inventories')
        
        file_path = None
        
        # Путь может начинаться с кастомного пути inventories (например, ansible/inventories/)
        if file_path_param.startswith(f'{inventories_layout_path}/'):
            # Путь вида inventories/prod/hosts.yml или ansible/inventories/prod/hosts.yml
            # Вычисляем относительный путь от inventories_dir
            rel_path = file_path_param[len(inventories_layout_path) + 1:]  # Убираем префикс
            file_path = inventories_dir / rel_path
        elif file_path_param.startswith('inventories/'):
            # Обратная совместимость: старые пути с хардкодом inventories/
            file_path = project_dir / 'repo' / file_path_param
        elif file_path_param == 'inventory.yml':
            # Простой файл в корне inventories: inventories/inventory.yml
            file_path = inventories_dir / 'inventory.yml'
        elif env:
            # Если указано окружение, сохраняем в структуру с окружением
            file_path = inventories_dir / env / 'hosts.yml'
        elif '/' in file_path_param:
            # Путь вида subdir/hosts.yml — относительно inventories_dir
            if file_path_param.startswith('inventories/'):
                rel_path = file_path_param[len('inventories/'):]
                file_path = inventories_dir / rel_path
            else:
                file_path = inventories_dir / file_path_param
        else:
            # Если просто имя файла, создаем в корне inventories
            file_path = inventories_dir / file_path_param
        
        # Создаем директорию если не существует
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Создаем бэкап если файл существует
        if file_path.exists():
            create_backup(file_path)
        
        # Сохраняем файл
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # Создаем папки group_vars и host_vars рядом с inventory файлом, если их нет
        ensure_inventory_dirs(file_path)
        
        return jsonify({'success': True, 'message': f'File {file_path_param} saved'})
    except Exception as e:
        app.logger.error(f"Error saving inventory file: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/create-folder', methods=['POST'])
@require_auth
def create_inventory_folder():
    """Создать папку в inventories директории
    
    Args:
        folder_path: Путь к папке (например, 'prod', 'stage/dev')
    """
    try:
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        folder_path_param = data.get('folder_path', '').strip()
        
        if not folder_path_param:
            return jsonify({'success': False, 'error': 'Folder path is required'}), 400
        
        # Валидация пути
        if '..' in folder_path_param or folder_path_param.startswith('/'):
            return jsonify({'success': False, 'error': 'Invalid folder path'}), 400
        
        project_dir = get_project_dir(project_id)
        inventories_dir = get_project_inventories_dir(project_id)
        
        # Создаем путь к папке
        folder_path = inventories_dir / folder_path_param
        
        # Проверяем, что папка не существует
        if folder_path.exists():
            return jsonify({'success': False, 'error': f'Folder "{folder_path_param}" already exists'}), 400
        
        # Создаем папку
        folder_path.mkdir(parents=True, exist_ok=True)
        
        app.logger.info(f"Created inventory folder: {folder_path} in project {project_id}")
        
        return jsonify({
            'success': True,
            'message': f'Folder "{folder_path_param}" created successfully',
            'path': f'inventories/{folder_path_param}'
        })
    except Exception as e:
        app.logger.error(f"Error creating inventory folder: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/download', methods=['GET'])
@require_auth
def download_inventory():
    """Скачать inventory файл
    
    Новая структура: inventories/<env>/hosts.yml
    """
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_path_param = request.args.get('file', 'inventories/inventory.yml')
        project_dir = get_project_dir(project_id)
        # Получаем inventories_dir для поддержки кастомных путей через repoLayout
        inventories_dir = get_project_inventories_dir(project_id)
        layout = get_repo_layout(project_id)
        inventories_layout_path = layout.get('inventories', 'inventories')
        
        file_path = None
        
        # Путь может начинаться с кастомного пути inventories (например, ansible/inventories/)
        if file_path_param.startswith(f'{inventories_layout_path}/'):
            # Путь вида inventories/prod/hosts.yml или ansible/inventories/prod/hosts.yml
            # Вычисляем относительный путь от inventories_dir
            rel_path = file_path_param[len(inventories_layout_path) + 1:]  # Убираем префикс
            file_path = inventories_dir / rel_path
        elif file_path_param.startswith('inventories/'):
            # Обратная совместимость: старые пути с хардкодом inventories/
            file_path = project_dir / 'repo' / file_path_param
        elif file_path_param == 'inventory.yml':
            # Простой файл в корне: repo/inventory.yml
            file_path = project_dir / 'repo' / 'inventory.yml'
        elif '/' in file_path_param:
            # Путь вида subdir/hosts.yml — относительно inventories_dir
            file_path = inventories_dir / file_path_param
        else:
            # Просто имя файла — ищем в корне inventories
            file_path = inventories_dir / file_path_param
        
        if not file_path or not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_path_param} not found'}), 404
        
        return send_file(
            str(file_path),
            mimetype='text/yaml',
            as_attachment=True,
            download_name=file_path.name
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/environments', methods=['GET'])
@require_auth
def list_inventory_environments():
    """Список подпапок в inventories, в которых есть файл инвентаря (опционально).
    
    Подпапки типа pgsql/, stage/ и т.д., где лежат invent.yaml / inventory.yml / hosts.yml.
    """
    try:
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        environments = []
        inventory_names = ['invent.yaml', 'invent.yml', 'inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']
        inventories_dir = get_project_inventories_dir(project_id)
        if inventories_dir.exists():
            for env_dir in inventories_dir.iterdir():
                if not env_dir.is_dir() or env_dir.name in ('group_vars', 'host_vars'):
                    continue
                env_name = env_dir.name
                has_inventory = any((env_dir / name).exists() for name in inventory_names)
                environments.append({
                    'name': env_name,
                    'has_inventory': has_inventory,
                    'has_group_vars': (env_dir / 'group_vars').exists(),
                    'has_host_vars': (env_dir / 'host_vars').exists()
                })
        
        environments.sort(key=lambda x: x['name'])
        return jsonify({'success': True, 'environments': environments})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/export', methods=['GET'])
@require_auth
def export_inventory():
    """Экспортировать все inventory файлы проекта в ZIP архив
    
    Поддерживает новую структуру: repo/inventories/<env>/hosts.yml
    И обратную совместимость: inventory/inventory.yml
    """
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        project_dir = get_project_dir(project_id)
        
        # Создаем ZIP архив в памяти: все файлы инвентаря (из Git или локальные)
        zip_buffer = BytesIO()
        inventory_names = ['invent.yaml', 'invent.yml', 'inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            inventories_dir = get_project_inventories_dir(project_id)
            if inventories_dir.exists():
                for inv_file in inventories_dir.rglob('*'):
                    if not inv_file.is_file() or inv_file.name not in inventory_names:
                        continue
                    rel = inv_file.relative_to(inventories_dir)
                    if 'group_vars' in rel.parts or 'host_vars' in rel.parts:
                        continue
                    arc_name = f'inventories/{rel}'
                    zip_file.write(str(inv_file), arc_name)
        
        zip_buffer.seek(0)
        
        # Генерируем имя файла с timestamp
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        zip_filename = f'inventory_export_{timestamp}.zip'
        
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=zip_filename
        )
    except Exception as e:
        app.logger.error(f"Error exporting inventory: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/add_host', methods=['POST'])
@require_auth
def add_host_to_inventory():
    """Добавить хост в inventory файл"""
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        host_name = data.get('host_name', '').strip()
        inventory_file = data.get('inventory_file', 'inventory.yml')
        group_name = data.get('group_name', 'all')  # Группа, в которую добавить хост
        host_ip = data.get('host_ip', '')  # Опциональный IP адрес
        vars_file = data.get('vars_file', '')  # Опциональный путь к vars_file
        
        if not host_name:
            return jsonify({'success': False, 'error': 'Host name not specified'}), 400
        
        # Валидация имени хоста
        if not re.match(r'^[a-zA-Z0-9._-]+$', host_name):
            return jsonify({'success': False, 'error': 'Host name contains invalid characters'}), 400
        
        # Получаем путь к inventory файлу проекта (с учетом repoLayout)
        project_dir = get_project_dir(project_id)
        # Получаем inventories_dir для поддержки кастомных путей через repoLayout
        inventories_dir = get_project_inventories_dir(project_id)
        layout = get_repo_layout(project_id)
        inventories_layout_path = layout.get('inventories', 'inventories')
        
        # Путь может начинаться с кастомного пути inventories (например, ansible/inventories/)
        if inventory_file.startswith(f'{inventories_layout_path}/'):
            # Путь вида inventories/prod/hosts.yml или ansible/inventories/prod/hosts.yml
            # Вычисляем относительный путь от inventories_dir
            rel_path = inventory_file[len(inventories_layout_path) + 1:]  # Убираем префикс
            file_path = inventories_dir / rel_path
        elif inventory_file.startswith('inventories/'):
            # Обратная совместимость: старые пути с хардкодом inventories/
            file_path = project_dir / 'repo' / inventory_file
        elif inventory_file == 'inventory.yml':
            # Простой файл в корне inventories: inventories/inventory.yml
            file_path = inventories_dir / 'inventory.yml'
        elif '/' in inventory_file:
            # Путь вида prod/hosts.yml или inventories/inventory.yml - добавляем inventories_dir
            if inventory_file.startswith('inventories/'):
                # Убираем префикс inventories/ и добавляем к inventories_dir
                rel_path = inventory_file[len('inventories/'):]
                file_path = inventories_dir / rel_path
            else:
                # Путь вида prod/hosts.yml - добавляем inventories_dir
                file_path = inventories_dir / inventory_file
        else:
            # Если просто имя файла, создаем в корне inventories
            file_path = inventories_dir / inventory_file
        
        # No fallback to BASE_DIR - new projects should be empty
        # Загружаем существующий inventory из Project Storage
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                inventory_data = yaml_loader.load(f) or {}
        else:
            inventory_data = {'all': {}}
        
        # Инициализируем структуру если нужно
        if 'all' not in inventory_data:
            inventory_data['all'] = {}
        
        # Определяем vars_file
        if not vars_file:
            vars_file = f'host_vars/{host_name}.yml'
        
        # Добавляем хост в указанную группу
        if group_name == 'all':
            # Добавляем в all.hosts
            if 'hosts' not in inventory_data['all']:
                inventory_data['all']['hosts'] = {}
            
            # Проверяем, не существует ли уже такой хост
            if isinstance(inventory_data['all']['hosts'], dict):
                if host_name in inventory_data['all']['hosts']:
                    return jsonify({'success': False, 'error': f'Host {host_name} already exists in inventory'}), 400
                
                # Добавляем хост с vars_file
                inventory_data['all']['hosts'][host_name] = {
                    'vars_file': vars_file
                }
                
                # Если указан IP, добавляем ansible_host
                if host_ip:
                    inventory_data['all']['hosts'][host_name]['ansible_host'] = host_ip
            elif isinstance(inventory_data['all']['hosts'], list):
                if host_name in inventory_data['all']['hosts']:
                    return jsonify({'success': False, 'error': f'Host {host_name} already exists in inventory'}), 400
                inventory_data['all']['hosts'].append(host_name)
        else:
            # Добавляем в группу через children
            if 'children' not in inventory_data['all']:
                inventory_data['all']['children'] = {}
            
            if group_name not in inventory_data['all']['children']:
                inventory_data['all']['children'][group_name] = {'hosts': {}}
            
            group_data = inventory_data['all']['children'][group_name]
            if 'hosts' not in group_data:
                group_data['hosts'] = {}
            
            # Проверяем, не существует ли уже такой хост
            if isinstance(group_data['hosts'], dict):
                if host_name in group_data['hosts']:
                    return jsonify({'success': False, 'error': f'Host {host_name} already exists in group {group_name}'}), 400
                
                # Добавляем хост с vars_file
                group_data['hosts'][host_name] = {
                    'vars_file': vars_file
                }
                
                # Если указан IP, добавляем ansible_host
                if host_ip:
                    group_data['hosts'][host_name]['ansible_host'] = host_ip
            elif isinstance(group_data['hosts'], list):
                if host_name in group_data['hosts']:
                    return jsonify({'success': False, 'error': f'Host {host_name} already exists in group {group_name}'}), 400
                group_data['hosts'].append(host_name)
        
        # Создаем бэкап если файл существует
        if file_path.exists():
            create_backup(file_path)
        
        # Сохраняем обновленный inventory
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml_loader.dump(inventory_data, f)
        
        app.logger.info(f"Host {host_name} added to {inventory_file}, group: {group_name}")
        
        return jsonify({
            'success': True,
            'message': f'Host {host_name} successfully added to group {group_name}',
            'host_name': host_name,
            'group_name': group_name,
            'inventory_file': inventory_file
        })
    except Exception as e:
        app.logger.error(f"Error adding host: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/import', methods=['POST'])
@require_auth
def import_inventory():
    """Импортировать inventory файл(ы) из ZIP архива или одиночного файла"""
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File not uploaded'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'File not selected'}), 400
        
        project_inventory_dir = get_project_inventory_file(project_id).parent
        project_inventory_dir.mkdir(parents=True, exist_ok=True)
        
        imported_files = []
        errors = []
        
        # Проверяем, это ZIP архив или одиночный файл
        if file.filename.endswith('.zip'):
            # Обрабатываем ZIP архив
            try:
                zip_buffer = BytesIO(file.read())
                with zipfile.ZipFile(zip_buffer, 'r') as zip_file:
                    for file_info in zip_file.namelist():
                        # Пропускаем директории и служебные файлы
                        if file_info.endswith('/') or '__MACOSX' in file_info:
                            continue
                        
                        # Извлекаем только .yml и .yaml файлы
                        if not (file_info.endswith('.yml') or file_info.endswith('.yaml')):
                            continue
                        
                        try:
                            # Читаем содержимое файла из архива
                            content = zip_file.read(file_info)
                            
                            # Валидируем YAML
                            try:
                                yaml.safe_load(content)
                            except yaml.YAMLError as e:
                                errors.append(f"{file_info}: invalid YAML - {str(e)}")
                                continue
                            
                            # Сохраняем файл
                            file_name = Path(file_info).name
                            target_path = project_inventory_dir / file_name
                            
                            # Создаем директорию если не существует
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            
                            with open(target_path, 'wb') as f:
                                f.write(content)
                            
                            # Создаем папки group_vars и host_vars рядом с inventory файлом, если их нет
                            ensure_inventory_dirs(target_path)
                            
                            imported_files.append(file_name)
                            app.logger.info(f"Imported inventory file: {file_name}")
                        except Exception as e:
                            errors.append(f"{file_info}: {str(e)}")
                            app.logger.warning(f"Error importing file {file_info}: {e}")
            except zipfile.BadZipFile:
                return jsonify({'success': False, 'error': 'Invalid ZIP archive'}), 400
        else:
            # Обрабатываем одиночный файл
            if not (file.filename.endswith('.yml') or file.filename.endswith('.yaml')):
                return jsonify({'success': False, 'error': 'Only .yml and .yaml files are supported'}), 400
            
            try:
                content = file.read()
                
                # Валидируем YAML
                try:
                    yaml.safe_load(content)
                except yaml.YAMLError as e:
                    return jsonify({'success': False, 'error': f'Invalid YAML: {str(e)}'}), 400
                
                # Сохраняем файл
                file_name = file.filename
                target_path = project_inventory_dir / file_name
                
                # Создаем директорию если не существует
                target_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(target_path, 'wb') as f:
                    f.write(content)
                
                # Создаем папки group_vars и host_vars рядом с inventory файлом, если их нет
                ensure_inventory_dirs(target_path)
                
                imported_files.append(file_name)
                app.logger.info(f"Imported inventory file: {file_name}")
            except Exception as e:
                return jsonify({'success': False, 'error': f'File import error: {str(e)}'}), 500
        
        if not imported_files:
            return jsonify({
                'success': False,
                'error': 'Failed to import any files',
                'errors': errors
            }), 400
        
        return jsonify({
            'success': True,
            'imported_files': imported_files,
            'errors': errors if errors else None,
            'message': f'Imported files: {len(imported_files)}'
        })
    except Exception as e:
        app.logger.error(f"Error importing inventory: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def update_hosts_vars_file(group_data, updated=False):
    """Рекурсивно обновляет структуру inventory, добавляя vars_file для хостов, у которых его нет
    
    Args:
        group_data: данные группы (может содержать 'hosts' или 'children')
        updated: флаг, указывающий, были ли внесены изменения
    
    Returns:
        tuple: (обновленные данные группы, флаг изменений)
    """
    if not isinstance(group_data, dict):
        return group_data, updated
    
    # Создаем копию для безопасной модификации
    updated_group = group_data.copy()
    
    # Обрабатываем прямые хосты в группе
    if 'hosts' in updated_group:
        hosts = updated_group['hosts']
        if isinstance(hosts, dict):
            # hosts: { host1: { vars: ... }, host2: None, host3: ... }
            updated_hosts = {}
            for host, config in hosts.items():
                # Пропускаем закомментированные хосты
                if isinstance(host, str) and host.startswith('#'):
                    updated_hosts[host] = config
                    continue
                # Пропускаем не-строковые ключи
                if not isinstance(host, str):
                    updated_hosts[host] = config
                    continue
                
                # Если config это None, создаем пустой dict
                if config is None:
                    config = {}
                    updated = True
                
                # Если config это dict, проверяем наличие vars_file
                if isinstance(config, dict):
                    if 'vars_file' not in config:
                        # Добавляем vars_file по умолчанию
                        config['vars_file'] = f'host_vars/{host}.yml'
                        updated = True
                    updated_hosts[host] = config
                else:
                    updated_hosts[host] = config
            updated_group['hosts'] = updated_hosts
        elif isinstance(hosts, list):
            # hosts: [ host1, host2, ... ] - преобразуем в dict с vars_file
            updated_hosts = {}
            for host in hosts:
                if isinstance(host, str) and not host.startswith('#'):
                    # Преобразуем в dict формат с vars_file
                    updated_hosts[host] = {'vars_file': f'host_vars/{host}.yml'}
                    updated = True
                else:
                    # Сохраняем как есть (закомментированные хосты)
                    updated_hosts[host] = None
            updated_group['hosts'] = updated_hosts
    
    # Рекурсивно обрабатываем дочерние группы
    if 'children' in updated_group:
        children = updated_group['children']
        if isinstance(children, dict):
            updated_children = {}
            for child_name, child_data in children.items():
                if isinstance(child_data, dict):
                    updated_child, child_updated = update_hosts_vars_file(child_data, updated)
                    updated_children[child_name] = updated_child
                    if child_updated:
                        updated = True
                else:
                    updated_children[child_name] = child_data
            updated_group['children'] = updated_children
    
    return updated_group, updated


@app.route('/api/inventory/auto_update_vars_file', methods=['POST'])
@require_auth
def auto_update_inventory_vars_file():
    """Автоматически достраивает vars_file для хостов в inventory файлах"""
    try:
        data = request.json or {}
        file_names = data.get('files', [])
        
        if not file_names:
            return jsonify({'success': False, 'error': 'File list not specified'}), 400
        
        updated_files = []
        
        for file_name in file_names:
            file_path = BASE_DIR / file_name
            
            if not file_path.exists():
                app.logger.warning(f"Inventory file {file_name} not found, skipping")
                continue
            
            try:
                # Читаем inventory файл с помощью ruamel.yaml для сохранения комментариев
                with open(file_path, 'r', encoding='utf-8') as f:
                    inventory = yaml_loader.load(f)
                
                if not inventory:
                    app.logger.warning(f"Inventory file {file_name} is empty or invalid, skipping")
                    continue
                
                # Обновляем структуру
                updated_inventory = inventory
                updated = False
                
                # Обрабатываем секцию 'all'
                if 'all' in updated_inventory:
                    all_section = updated_inventory['all']
                    if isinstance(all_section, dict):
                        updated_all, all_updated = update_hosts_vars_file(all_section, False)
                        updated_inventory['all'] = updated_all
                        if all_updated:
                            updated = True
                else:
                    # Если нет секции 'all', обрабатываем корневой уровень
                    updated_root, root_updated = update_hosts_vars_file(updated_inventory, False)
                    if root_updated:
                        updated_inventory = updated_root
                        updated = True
                
                # Если были изменения, сохраняем файл
                if updated:
                    # Создаем бэкап перед изменением
                    create_backup(file_path)
                    
                    # Сохраняем обновленный файл с помощью ruamel.yaml для сохранения форматирования
                    with open(file_path, 'w', encoding='utf-8') as f:
                        yaml_loader.dump(updated_inventory, f)
                    
                    app.logger.info(f"Automatically updated inventory file {file_name}: added vars_file for hosts")
                    updated_files.append(file_name)
                
            except yaml.YAMLError as e:
                app.logger.error(f"YAML parsing error in file {file_name}: {e}")
                continue
            except Exception as e:
                app.logger.error(f"Error processing inventory file {file_name}: {e}")
                continue
        
        return jsonify({
            'success': True,
            'message': f'Updated files: {len(updated_files)}',
            'updated_files': updated_files
        })
    except Exception as e:
        app.logger.error(f"Error automatically updating vars_file: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/inventory/delete', methods=['POST'])
@require_auth
def delete_inventory():
    """Удалить inventory файл"""
    try:
        data = request.json or {}
        file_name = data.get('file')
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        file_path = BASE_DIR / file_name
        
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found'}), 404
        
        # Создаем бэкап перед удалением
        create_backup(file_path)
        
        # Удаляем файл
        file_path.unlink()
        
        return jsonify({'success': True, 'message': f'File {file_name} successfully deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ansible_config/list', methods=['GET'])
@require_auth
def list_ansible_config_files():
    """Получить список всех .cfg файлов из Project Storage"""
    try:
        # Получаем project_id - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        config_files = []
        project_dir = get_project_dir(project_id)
        ansible_config_dir = project_dir / 'ansible-config'
        ansible_config_dir.mkdir(parents=True, exist_ok=True)
        for file_path in sorted(ansible_config_dir.glob('*.cfg')):
            config_files.append({
                'name': file_path.name,
                'path': f'ansible-config/{file_path.name}',
                'is_primary': file_path.name == 'ansible.cfg'
            })
        if not config_files:
            config_files.append({
                'name': 'ansible.cfg',
                'path': 'ansible-config/ansible.cfg',
                'is_primary': True
            })
        
        # No on-the-fly migration from BASE_DIR - new projects should be empty
        # Migration should be explicit via migration endpoint only
        
        # Сортируем по имени
        config_files.sort(key=lambda x: x['name'])
        
        selected_config = None
        selection_file = project_dir / 'data' / 'ansible-config-selection.json'
        if selection_file.exists():
            try:
                with open(selection_file, 'r', encoding='utf-8') as f:
                    selection_data = json.load(f)
                    selected_config = selection_data.get('selected_config')
            except Exception as e:
                pass
        if not selected_config and config_files:
            selected_config = 'ansible-config/ansible.cfg'
        return jsonify({
            'success': True,
            'files': config_files,
            'selected_config': selected_config
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ansible_config/get', methods=['GET'])
@require_auth
def get_ansible_config():
    """Получить содержимое ansible config файла из Project Storage"""
    try:
        # Получаем project_id - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_name = request.args.get('file', 'ansible.cfg')
        project_dir = get_project_dir(project_id)
        
        if '..' in file_name or file_name.startswith('/'):
            return jsonify({'success': False, 'error': 'Invalid file path'}), 400
        file_path = resolve_ansible_config_path(project_id, file_name)
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content, 'file': file_name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ansible_config/save', methods=['POST'])
@require_auth
def save_ansible_config():
    """Сохранить содержимое ansible config файла в Project Storage"""
    try:
        # Получаем project_id - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400
        
        file_name = data.get('file', 'ansible.cfg')
        content = data.get('content', '')
        
        app.logger.info(f"User saving Ansible config file: {file_name} to project {project_id}")
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        # Validate file name (prevent path traversal)
        if '..' in file_name or file_name.startswith('/'):
            return jsonify({'success': False, 'error': 'Invalid file path'}), 400
        
        project_dir = get_project_dir(project_id)
        file_path = resolve_ansible_config_path(project_id, file_name)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Создаем бэкап если файл существует
        if file_path.exists():
            create_backup(file_path)
        
        # Сохраняем файл в Project Storage
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        app.logger.info(f"Ansible config saved to Project Storage: {file_path}")
        return jsonify({'success': True, 'message': f'File {file_name} successfully saved to Project Storage'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ansible_config/download', methods=['GET'])
@require_auth
def download_ansible_config():
    """Скачать ansible config файл из Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_name = request.args.get('file', 'ansible.cfg')
        if '..' in file_name or file_name.startswith('/'):
            return jsonify({'success': False, 'error': 'Invalid file path'}), 400
        file_path = resolve_ansible_config_path(project_id, file_name)
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        return send_file(
            str(file_path),
            mimetype='text/plain',
            as_attachment=True,
            download_name=file_name
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ansible_config/delete', methods=['POST'])
@require_auth
def delete_ansible_config():
    """Удалить ansible config файл из Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file')
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        # Validate file name (prevent path traversal)
        if '..' in file_name or file_name.startswith('/'):
            return jsonify({'success': False, 'error': 'Invalid file path'}), 400
        
        project_dir = get_project_dir(project_id)
        file_path = resolve_ansible_config_path(project_id, file_name)
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        # Создаем бэкап перед удалением
        create_backup(file_path)
        
        # Удаляем файл
        file_path.unlink()
        
        # Если удаляемый файл был выбран, очищаем выбор
        selection_file = project_dir / 'data' / 'ansible-config-selection.json'
        if selection_file.exists():
            try:
                with open(selection_file, 'r', encoding='utf-8') as f:
                    selection_data = json.load(f)
                selected_config = selection_data.get('selected_config')
                # Сравниваем по полному пути или по имени файла
                if selected_config and (selected_config == file_name or 
                                       selected_config == file_path.name or
                                       selected_config.endswith('/' + file_name) or
                                       selected_config.endswith('/' + file_path.name)):
                    selection_data['selected_config'] = None
                    selection_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(selection_file, 'w', encoding='utf-8') as f:
                        json.dump(selection_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                app.logger.warning(f"Failed to update selection after deletion: {e}")
        
        app.logger.info(f"Ansible config deleted from Project Storage: {file_path}")
        return jsonify({'success': True, 'message': f'File {file_name} successfully deleted from Project Storage'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ansible_config/select', methods=['POST'])
@require_auth
def select_ansible_config():
    """Сохранить выбранный ansible config файл в проектной папке"""
    try:
        # Получаем project_id - REQUIRED
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_path = data.get('file')
        
        if not file_path:
            return jsonify({'success': False, 'error': 'File path not specified'}), 400
        
        # Validate file path (prevent path traversal)
        if '..' in file_path or file_path.startswith('/'):
            return jsonify({'success': False, 'error': 'Invalid file path'}), 400
        
        project_dir = get_project_dir(project_id)
        selection_file = project_dir / 'data' / 'ansible-config-selection.json'
        
        # Сохраняем выбор
        selection_data = {
            'selected_config': file_path,
            'updated_at': datetime.now().isoformat()
        }
        
        selection_file.parent.mkdir(parents=True, exist_ok=True)
        with open(selection_file, 'w', encoding='utf-8') as f:
            json.dump(selection_data, f, indent=2, ensure_ascii=False)
        
        app.logger.info(f"Selected ansible config saved for project {project_id}: {file_path}")
        return jsonify({'success': True, 'message': 'Selection saved', 'selected_config': file_path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/group_vars/list', methods=['GET'])
@require_auth
def list_group_vars_files():
    """Получить список всех group_vars файлов из Project Storage и автоматически создать файлы для групп из inventory
    
    Собирает файлы из всех возможных мест:
    - Общая директория vars/group_vars/
    - Директории рядом с inventory файлами: inventories/*/group_vars/
    """
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        project_dir = get_project_dir(project_id)
        repo_dir = project_dir / 'repo'
        
        # Используем Project Storage
        project_group_vars_dir = get_project_group_vars_dir(project_id)
        project_group_vars_dir.mkdir(parents=True, exist_ok=True)
        
        ensure_all_inventory_dirs(project_id)
        
        groups_from_inventory = set()
        inventories_dir = get_project_inventories_dir(project_id)
        
        inventory_files = []
        if inventories_dir.exists():
            for inv_file in inventories_dir.rglob('*.yaml'):
                if inv_file.is_file():
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        inventory_files.append(str(inv_file.relative_to(repo_dir)))
            for inv_file in inventories_dir.rglob('*.yml'):
                if inv_file.is_file():
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        inventory_files.append(str(inv_file.relative_to(repo_dir)))
            for inv_file in inventories_dir.rglob('*.ini'):
                if inv_file.is_file():
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        inventory_files.append(str(inv_file.relative_to(repo_dir)))
            for inv_file in inventories_dir.rglob('*'):
                if inv_file.is_file() and inv_file.name in ('hosts',) and inv_file.suffix == '':
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        path_str = str(inv_file.relative_to(repo_dir))
                        if path_str not in inventory_files:
                            inventory_files.append(path_str)
        
        root_inventory = repo_dir / 'inventory.yml'
        if root_inventory.exists():
            inventory_files.append('inventory.yml')
        
        for inv_file_path in inventory_files:
            full_path = repo_dir / inv_file_path
            if full_path.exists():
                try:
                    if _is_ini_inventory_file(full_path):
                        inventory_data = _parse_ini_inventory(full_path) or {}
                    else:
                        with open(full_path, 'r', encoding='utf-8') as f:
                            inventory_data = yaml_loader.load(f) or {}
                    
                    inventory_dir = full_path.parent
                    group_vars_dir = inventory_dir / 'group_vars'
                    group_vars_dir.mkdir(parents=True, exist_ok=True)
                    
                    groups_in_this_inventory = set()
                    all_children = inventory_data.get('all', {}).get('children', {})
                    for group_name in all_children.keys():
                        if isinstance(group_name, str) and not group_name.startswith('#'):
                            groups_in_this_inventory.add(group_name)
                            groups_from_inventory.add(group_name)
                    
                    if inventory_data.get('all', {}).get('vars'):
                        groups_in_this_inventory.add('all')
                        groups_from_inventory.add('all')
                    else:
                        groups_in_this_inventory.add('all')
                        groups_from_inventory.add('all')
                    
                    for group_name in groups_in_this_inventory:
                        group_file = group_vars_dir / f"{group_name}.yml"
                        if not group_file.exists():
                            try:
                                with open(group_file, 'w', encoding='utf-8') as f:
                                    yaml_loader.dump({}, f)
                                app.logger.info(f"Auto-created group_vars file for group: {group_name} at {group_file}")
                            except Exception as e:
                                app.logger.error(f"Error creating group_vars file for {group_name} at {group_file}: {e}")
                except Exception as e:
                    app.logger.warning(f"Error reading inventory {inv_file_path} for group_vars sync: {e}")
        
        # Собираем все group_vars файлы из всех возможных мест
        group_vars_files_dict = {}  # Используем dict для избежания дубликатов по имени файла
        
        # 1. Файлы из общей директории vars/group_vars/
        for ext in ['*.yml', '*.yaml']:
            for file_path in project_group_vars_dir.glob(ext):
                # Skip backup directory
                if 'backups' in str(file_path):
                    continue
                if file_path.is_file():
                    file_name = file_path.name
                    if file_name not in group_vars_files_dict:
                        group_vars_files_dict[file_name] = {
                            'name': file_name,
                            'path': f'group_vars/{file_name}'
                        }
        
        # 2. Файлы из директорий рядом с inventory файлами: inventories/*/group_vars/
        if inventories_dir.exists():
            for group_vars_dir in inventories_dir.rglob('group_vars'):
                if group_vars_dir.is_dir():
                    for ext in ['*.yml', '*.yaml']:
                        for file_path in group_vars_dir.glob(ext):
                            # Skip backup directory
                            if 'backups' in str(file_path):
                                continue
                            if file_path.is_file():
                                file_name = file_path.name
                                # Вычисляем относительный путь от repo
                                repo_rel_path = file_path.relative_to(repo_dir)
                                if file_name not in group_vars_files_dict:
                                    group_vars_files_dict[file_name] = {
                                        'name': file_name,
                                        'path': str(repo_rel_path)
                                    }
        
        # 3. Файлы рядом с корневым inventory.yml (если есть)
        root_group_vars_dir = repo_dir / 'group_vars'
        if root_group_vars_dir.exists() and root_group_vars_dir.is_dir():
            for ext in ['*.yml', '*.yaml']:
                for file_path in root_group_vars_dir.glob(ext):
                    if 'backups' in str(file_path):
                        continue
                    if file_path.is_file():
                        file_name = file_path.name
                        if file_name not in group_vars_files_dict:
                            group_vars_files_dict[file_name] = {
                                'name': file_name,
                                'path': f'group_vars/{file_name}'
                            }
        
        # Преобразуем dict в список и сортируем по имени
        group_vars_files = list(group_vars_files_dict.values())
        group_vars_files.sort(key=lambda x: x['name'])
        
        return jsonify({'success': True, 'files': group_vars_files})
    except Exception as e:
        app.logger.error(f"Error listing group_vars files: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/group_vars/get', methods=['GET'])
@require_auth
def get_group_vars():
    """Получить содержимое group_vars файла"""
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_name = request.args.get('file', 'all.yml')
        # Используем директорию проекта
        project_group_vars_dir = get_project_group_vars_dir(project_id)
        file_path = project_group_vars_dir / file_name
        
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found'}), 404
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        vault_id = request.args.get('vault_id') or request.args.get('vaultId')
        decrypted, was_encrypted, vault_id_used, vault_id_required = _decrypt_content_if_encrypted(project_id, content, vault_id)
        if was_encrypted:
            if vault_id_required:
                vaults = _load_vaults(project_id)
                return jsonify({
                    'success': True,
                    'encrypted': True,
                    'vaultIdRequired': True,
                    'vaults': vaults,
                    'file': file_name
                })
            if decrypted is None:
                return jsonify({'success': False, 'error': 'Failed to decrypt vault-encrypted file. Check vault key.'}), 500
            content = decrypted
        resp = {'success': True, 'content': content, 'file': file_name}
        if was_encrypted:
            resp['encrypted'] = True
            resp['vaultId'] = vault_id_used
        return jsonify(resp)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/group_vars/save', methods=['POST'])
@require_auth
def save_group_vars():
    """Сохранить group_vars файл"""
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file', 'all.yml')
        content = data.get('content', '')
        inventory_file = data.get('inventory_file')  # Путь к inventory файлу (опционально)
        vault_id = data.get('vaultId') or data.get('vault_id')
        
        app.logger.info(f"User saving group_vars file: {file_name} to project {project_id}, inventory_file: {inventory_file}")
        
        if not content:
            return jsonify({'success': False, 'error': 'File content cannot be empty'}), 400
        
        # Валидируем YAML перед сохранением (до шифрования)
        is_valid, error_msg = validate_yaml_content(content)
        if not is_valid:
            app.logger.warning(f"Invalid YAML in group_vars/{file_name}: {error_msg}")
            return jsonify({'success': False, 'error': f'Invalid YAML syntax: {error_msg}'}), 400
        
        # Ansible-vault: шифрование при наличии vault_id
        if vault_id:
            encrypted_content, err = _encrypt_content_if_requested(project_id, content, vault_id)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            content = encrypted_content
        
        # Определяем путь для сохранения на основе inventory файла
        # Если inventory_file не указан, пытаемся определить его по имени группы
        if not inventory_file:
            # Извлекаем имя группы из имени файла (например, "pgsql_replicas.yml" -> "pgsql_replicas")
            group_name = file_name.replace('.yml', '').replace('.yaml', '')
            if group_name != 'all':
                inventory_file = find_inventory_file_for_group(project_id, group_name)
                if inventory_file:
                    app.logger.info(f"[save_group_vars] Found inventory_file for group {group_name}: {inventory_file}")
            if not inventory_file:
                app.logger.warning(f"[save_group_vars] Could not find inventory_file for group {group_name}, will use fallback directory")
        else:
            app.logger.info(f"[save_group_vars] Using provided inventory_file: {inventory_file}")
        
        # Определяем директорию group_vars на основе inventory файла
        group_vars_dir = get_group_vars_dir_for_inventory(project_id, inventory_file)
        group_vars_dir.mkdir(parents=True, exist_ok=True)
        file_path = group_vars_dir / file_name
        
        app.logger.info(f"[save_group_vars] Saving group_vars to: {file_path} (inventory_file: {inventory_file}, group_vars_dir: {group_vars_dir})")
        
        # Если inventory_file указан, убеждаемся, что папки group_vars и host_vars созданы рядом с inventory файлом
        if inventory_file:
            project_dir = get_project_dir(project_id)
            repo_dir = project_dir / 'repo'
            
            # Пробуем найти inventory файл
            inventory_path = None
            repo_path = repo_dir / inventory_file
            if repo_path.exists():
                inventory_path = repo_path
            else:
                # Ищем файл рекурсивно
                found_files = list(repo_dir.rglob(Path(inventory_file).name))
                if found_files:
                    inventory_path = found_files[0]
            
            if inventory_path and inventory_path.exists():
                # Создаем папки group_vars и host_vars рядом с inventory файлом
                ensure_inventory_dirs(inventory_path)
                app.logger.info(f"[save_group_vars] Ensured group_vars/host_vars directories for {inventory_path}")
        
        # Создаем бэкап если файл существует
        if file_path.exists():
            create_backup(file_path)
        
        # Сохраняем файл
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return jsonify({'success': True, 'message': f'File {file_name} saved'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/group_vars/update_var', methods=['POST'])
@require_auth
def update_group_var():
    """Обновить одну переменную в group_vars (merge)"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file', 'all.yml')
        var_name = data.get('var_name')
        var_value = data.get('var_value')
        inventory_file = data.get('inventory_file')  # Путь к inventory файлу (опционально)
        
        if not var_name:
            return jsonify({'success': False, 'error': 'Variable name not specified'}), 400
        
        # Определяем путь для сохранения на основе inventory файла
        # Если inventory_file не указан, пытаемся определить его по имени группы
        if not inventory_file:
            # Извлекаем имя группы из имени файла (например, "pgsql_replicas.yml" -> "pgsql_replicas")
            group_name = file_name.replace('.yml', '').replace('.yaml', '')
            if group_name != 'all':
                inventory_file = find_inventory_file_for_group(project_id, group_name)
                if inventory_file:
                    app.logger.info(f"[update_group_var] Found inventory_file for group {group_name}: {inventory_file}")
            if not inventory_file:
                app.logger.warning(f"[update_group_var] Could not find inventory_file for group {group_name}, will use fallback directory")
        
        # Определяем директорию group_vars на основе inventory файла
        group_vars_dir = get_group_vars_dir_for_inventory(project_id, inventory_file)
        group_vars_dir.mkdir(parents=True, exist_ok=True)
        file_path = group_vars_dir / file_name
        
        app.logger.info(f"[update_group_var] Updating group_var in: {file_path} (inventory_file: {inventory_file})")
        
        # Если inventory_file указан, убеждаемся, что папки group_vars и host_vars созданы рядом с inventory файлом
        if inventory_file:
            project_dir = get_project_dir(project_id)
            repo_dir = project_dir / 'repo'
            
            # Пробуем найти inventory файл
            inventory_path = None
            repo_path = repo_dir / inventory_file
            if repo_path.exists():
                inventory_path = repo_path
            else:
                # Ищем файл рекурсивно
                found_files = list(repo_dir.rglob(Path(inventory_file).name))
                if found_files:
                    inventory_path = found_files[0]
            
            if inventory_path and inventory_path.exists():
                # Создаем папки group_vars и host_vars рядом с inventory файлом
                ensure_inventory_dirs(inventory_path)
        
        # Читаем текущий файл
        current_data = {}
        if file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    current_data = yaml.safe_load(f) or {}
            except Exception as e:
                app.logger.warning(f"Error reading {file_path}: {e}")
                current_data = {}
        
        # Обновляем переменную
        if var_value is None:
            # Удаляем переменную
            current_data.pop(var_name, None)
        else:
            current_data[var_name] = var_value
        
        # Валидируем YAML перед сохранением
        is_valid, error_msg = validate_yaml_content(current_data)
        if not is_valid:
            app.logger.warning(f"Invalid YAML in group_vars/{file_name}: {error_msg}")
            return jsonify({'success': False, 'error': f'Invalid YAML syntax: {error_msg}'}), 400
        
        # Создаем бэкап
        if file_path.exists():
            create_backup(file_path)
        
        # Сохраняем
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(current_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        return jsonify({'success': True, 'message': f'Variable {var_name} updated'})
    except Exception as e:
        app.logger.error(f"Error updating group_var: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/host_vars/update_var', methods=['POST'])
@require_auth
def update_host_var():
    """Обновить одну переменную в host_vars (merge) в Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file')
        var_name = data.get('var_name')
        var_value = data.get('var_value')
        inventory_file = data.get('inventory_file')  # Путь к inventory файлу (опционально)
        
        if not file_name or not var_name:
            return jsonify({'success': False, 'error': 'File name or variable name not specified'}), 400
        
        # Определяем путь для сохранения на основе inventory файла
        if not inventory_file:
            # Пытаемся определить inventory_file по имени хоста
            # Для этого нужно получить список хостов из выбранных inventory файлов проекта
            host_name = file_name.replace('.yml', '').replace('.yaml', '')
            
            # Получаем выбранные inventory файлы из localStorage (если доступно через API)
            # Или используем все inventory файлы проекта
            project_dir = get_project_dir(project_id)
            repo_dir = project_dir / 'repo'
            
            # Ищем все inventory файлы в проекте
            inventory_files = []
            for pattern in ['invent.yaml', 'inventory.yml', 'hosts.yml']:
                for inv_file in repo_dir.rglob(pattern):
                    if inv_file.is_file():
                        inventory_files.append(str(inv_file))
            
            # Получаем хосты из найденных inventory файлов
            hosts_list = get_inventory_hosts(inventory_files) if inventory_files else []
            app.logger.debug(f"[update_host_var] Looking for host {host_name} in {len(hosts_list)} hosts from {len(inventory_files)} inventory files")
            
            for h in hosts_list:
                if h['name'] == host_name:
                    inventory_file = h.get('inventory_file', '')
                    if inventory_file:
                        app.logger.info(f"[update_host_var] Found inventory_file for host {host_name}: {inventory_file}")
                        break
            
            if not inventory_file:
                app.logger.warning(f"[update_host_var] Could not find inventory_file for host {host_name}")
        
        # Определяем директорию host_vars на основе inventory файла
        host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file)
        host_vars_dir.mkdir(parents=True, exist_ok=True)
        file_path = host_vars_dir / file_name
        
        app.logger.info(f"[update_host_var] Using host_vars_dir: {host_vars_dir} for inventory_file: {inventory_file}, file_path: {file_path}")
        
        # Если inventory_file указан, убеждаемся, что папки group_vars и host_vars созданы рядом с inventory файлом
        if inventory_file:
            project_dir = get_project_dir(project_id)
            repo_dir = project_dir / 'repo'
            
            # Пробуем найти inventory файл
            inventory_path = None
            repo_path = repo_dir / inventory_file
            if repo_path.exists():
                inventory_path = repo_path
            else:
                # Ищем файл рекурсивно
                found_files = list(repo_dir.rglob(Path(inventory_file).name))
                if found_files:
                    inventory_path = found_files[0]
            
            if inventory_path and inventory_path.exists():
                # Создаем папки group_vars и host_vars рядом с inventory файлом
                ensure_inventory_dirs(inventory_path)
                app.logger.info(f"[update_host_var] Ensured group_vars/host_vars directories for {inventory_path}")
        
        # Если файл не найден рядом с inventory, пробуем общую директорию (для обратной совместимости)
        if not file_path.exists():
            project_host_vars_dir = get_project_host_vars_dir(project_id)
            fallback_path = project_host_vars_dir / file_name
            if fallback_path.exists():
                file_path = fallback_path
                app.logger.debug(f"[update_host_var] Using fallback host_vars file for update: {fallback_path}")
        
        # Читаем текущий файл
        current_data = {}
        if file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    current_data = yaml.safe_load(f) or {}
            except Exception as e:
                app.logger.warning(f"Error reading {file_path}: {e}")
                current_data = {}
        
        # Обновляем переменную
        if var_value is None:
            # Удаляем переменную
            current_data.pop(var_name, None)
        else:
            current_data[var_name] = var_value
        
        # Валидируем YAML перед сохранением
        is_valid, error_msg = validate_yaml_content(current_data)
        if not is_valid:
            app.logger.warning(f"Invalid YAML in host_vars/{file_name}: {error_msg}")
            return jsonify({'success': False, 'error': f'Invalid YAML syntax: {error_msg}'}), 400
        
        # Создаем бэкап
        if file_path.exists():
            create_backup(file_path)
        
        # Сохраняем
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(current_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        return jsonify({'success': True, 'message': f'Variable {var_name} updated for {file_name}'})
    except Exception as e:
        app.logger.error(f"Error updating host_var: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/group_vars/download', methods=['GET'])
@require_auth
def download_group_vars():
    """Скачать group_vars файл из Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_name = request.args.get('file', 'all.yml')
        project_group_vars_dir = get_project_group_vars_dir(project_id)
        file_path = project_group_vars_dir / file_name
        
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        
        return send_file(
            str(file_path),
            mimetype='text/yaml',
            as_attachment=True,
            download_name=file_name
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/group_vars/delete', methods=['POST'])
@require_auth
def delete_group_vars():
    """Удалить group_vars файл из Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file')
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        project_group_vars_dir = get_project_group_vars_dir(project_id)
        file_path = project_group_vars_dir / file_name
        
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        
        # Создаем бэкап перед удалением
        create_backup(file_path)
        
        # Удаляем файл
        file_path.unlink()
        
        return jsonify({'success': True, 'message': f'File {file_name} successfully deleted from Project Storage'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/host_vars/list', methods=['GET'])
@require_auth
def list_host_vars_files():
    """Получить список всех host_vars файлов из Project Storage.
    Автоматически создаёт host_vars файлы для хостов из inventory (включая INI).
    """
    try:
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        project_dir = get_project_dir(project_id)
        repo_dir = project_dir / 'repo'
        
        project_host_vars_dir = get_project_host_vars_dir(project_id)
        project_host_vars_dir.mkdir(parents=True, exist_ok=True)
        
        ensure_all_inventory_dirs(project_id)
        
        inventories_dir = get_project_inventories_dir(project_id)
        inventory_files = []
        if inventories_dir.exists():
            for inv_file in inventories_dir.rglob('*.yaml'):
                if inv_file.is_file():
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        inventory_files.append(str(inv_file.relative_to(repo_dir)))
            for inv_file in inventories_dir.rglob('*.yml'):
                if inv_file.is_file():
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        inventory_files.append(str(inv_file.relative_to(repo_dir)))
            for inv_file in inventories_dir.rglob('*.ini'):
                if inv_file.is_file():
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        inventory_files.append(str(inv_file.relative_to(repo_dir)))
            for inv_file in inventories_dir.rglob('*'):
                if inv_file.is_file() and inv_file.name in ('hosts',) and inv_file.suffix == '':
                    rel_path = inv_file.relative_to(inventories_dir)
                    if 'host_vars' not in rel_path.parts and 'group_vars' not in rel_path.parts:
                        path_str = str(inv_file.relative_to(repo_dir))
                        if path_str not in inventory_files:
                            inventory_files.append(path_str)
        
        root_inv = repo_dir / 'inventory.yml'
        if root_inv.exists():
            inventory_files.append('inventory.yml')
        
        inventory_files_full = [str(repo_dir / p) for p in inventory_files if (repo_dir / p).exists()]
        hosts_from_inv = get_inventory_hosts(inventory_files_full) if inventory_files_full else []
        
        for h in hosts_from_inv:
            host_name = h.get('name')
            if not host_name:
                continue
            inventory_file = h.get('inventory_file', '')
            host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file)
            host_vars_dir.mkdir(parents=True, exist_ok=True)
            host_file = host_vars_dir / f"{host_name}.yml"
            if not host_file.exists():
                try:
                    with open(host_file, 'w', encoding='utf-8') as f:
                        yaml_loader.dump({}, f)
                    app.logger.info(f"Auto-created host_vars file for host: {host_name} at {host_file}")
                except Exception as e:
                    app.logger.error(f"Error creating host_vars file for {host_name}: {e}")
        
        # Собираем все host_vars файлы из всех возможных мест
        host_vars_files_dict = {}  # Используем dict для избежания дубликатов по имени файла
        
        # 1. Файлы из общей директории vars/host_vars/
        for ext in ['*.yml', '*.yaml']:
            for file_path in project_host_vars_dir.glob(ext):
                # Skip backup directory
                if 'backups' in str(file_path):
                    continue
                if file_path.is_file():
                    file_name = file_path.name
                    if file_name not in host_vars_files_dict:
                        host_vars_files_dict[file_name] = {
                            'name': file_name,
                            'path': f'host_vars/{file_name}'
                        }
        
        # 2. Файлы из директорий рядом с inventory файлами: inventories/*/host_vars/
        inventories_dir = get_project_inventories_dir(project_id)
        if inventories_dir.exists():
            for host_vars_dir in inventories_dir.rglob('host_vars'):
                if host_vars_dir.is_dir():
                    for ext in ['*.yml', '*.yaml']:
                        for file_path in host_vars_dir.glob(ext):
                            # Skip backup directory
                            if 'backups' in str(file_path):
                                continue
                            if file_path.is_file():
                                file_name = file_path.name
                                # Вычисляем относительный путь от repo
                                repo_rel_path = file_path.relative_to(repo_dir)
                                if file_name not in host_vars_files_dict:
                                    host_vars_files_dict[file_name] = {
                                        'name': file_name,
                                        'path': str(repo_rel_path)
                                    }
        
        # 3. Файлы рядом с корневым inventory.yml (если есть)
        root_host_vars_dir = repo_dir / 'host_vars'
        if root_host_vars_dir.exists() and root_host_vars_dir.is_dir():
            for ext in ['*.yml', '*.yaml']:
                for file_path in root_host_vars_dir.glob(ext):
                    if 'backups' in str(file_path):
                        continue
                    if file_path.is_file():
                        file_name = file_path.name
                        if file_name not in host_vars_files_dict:
                            host_vars_files_dict[file_name] = {
                                'name': file_name,
                                'path': f'host_vars/{file_name}'
                            }
        
        # Преобразуем dict в список и сортируем по имени
        host_vars_files = list(host_vars_files_dict.values())
        host_vars_files.sort(key=lambda x: x['name'])
        
        return jsonify({'success': True, 'files': host_vars_files})
    except Exception as e:
        app.logger.error(f"Error listing host_vars files: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/host_vars/get', methods=['GET'])
@require_auth
def get_host_vars():
    """Получить содержимое host_vars файла из Project Storage"""
    try:
        # Получаем project_id - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_name = request.args.get('file')
        inventory_file = request.args.get('inventory_file')  # Путь к inventory файлу (опционально)
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        # Определяем путь для загрузки на основе inventory файла
        if not inventory_file:
            # Пытаемся определить inventory_file по имени хоста
            host_name = file_name.replace('.yml', '').replace('.yaml', '')
            hosts_list = get_inventory_hosts()
            for h in hosts_list:
                if h['name'] == host_name:
                    inventory_file = h.get('inventory_file', '')
                    if inventory_file:
                        app.logger.info(f"Found inventory_file for host {host_name}: {inventory_file}")
                        break
        
        # Определяем директорию host_vars на основе inventory файла
        host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file)
        file_path = host_vars_dir / file_name
        
        # Если файл не найден рядом с inventory, пробуем общую директорию (для обратной совместимости)
        if not file_path.exists():
            project_host_vars_dir = get_project_host_vars_dir(project_id)
            fallback_path = project_host_vars_dir / file_name
            if fallback_path.exists():
                file_path = fallback_path
                app.logger.debug(f"Using fallback host_vars file: {fallback_path}")
        
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        vault_id = request.args.get('vault_id') or request.args.get('vaultId')
        decrypted, was_encrypted, vault_id_used, vault_id_required = _decrypt_content_if_encrypted(project_id, content, vault_id)
        if was_encrypted:
            if vault_id_required:
                vaults = _load_vaults(project_id)
                return jsonify({
                    'success': True,
                    'encrypted': True,
                    'vaultIdRequired': True,
                    'vaults': vaults,
                    'file': file_name
                })
            if decrypted is None:
                return jsonify({'success': False, 'error': 'Failed to decrypt vault-encrypted file. Check vault key.'}), 500
            content = decrypted
        resp = {'success': True, 'content': content, 'file': file_name}
        if was_encrypted:
            resp['encrypted'] = True
            resp['vaultId'] = vault_id_used
        return jsonify(resp)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/host_vars/save', methods=['POST'])
@require_auth
def save_host_vars():
    """Сохранить host_vars файл"""
    try:
        # Получаем projectId - REQUIRED, no fallback
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file')
        content = data.get('content', '')
        inventory_file = data.get('inventory_file')  # Путь к inventory файлу (опционально)
        vault_id = data.get('vaultId') or data.get('vault_id')
        
        app.logger.info(f"User saving host_vars file: {file_name} to project {project_id}, inventory_file: {inventory_file}")
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        if not content:
            return jsonify({'success': False, 'error': 'File content cannot be empty'}), 400
        
        # Валидируем YAML перед сохранением (до шифрования)
        is_valid, error_msg = validate_yaml_content(content)
        if not is_valid:
            app.logger.warning(f"Invalid YAML in host_vars/{file_name}: {error_msg}")
            return jsonify({'success': False, 'error': f'Invalid YAML syntax: {error_msg}'}), 400
        
        # Ansible-vault: шифрование при наличии vault_id
        if vault_id:
            encrypted_content, err = _encrypt_content_if_requested(project_id, content, vault_id)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            content = encrypted_content
        
        # Определяем путь для сохранения на основе inventory файла
        # Если inventory_file не указан, пытаемся определить его по имени хоста
        if not inventory_file:
            # Извлекаем имя хоста из имени файла (например, "192.168.1.133.yml" -> "192.168.1.133")
            host_name = file_name.replace('.yml', '').replace('.yaml', '')
            app.logger.debug(f"[save_host_vars] Looking for inventory_file for host: {host_name}")
            hosts_list = get_inventory_hosts()
            for h in hosts_list:
                if h['name'] == host_name:
                    inventory_file = h.get('inventory_file', '')
                    if inventory_file:
                        app.logger.info(f"[save_host_vars] Found inventory_file for host {host_name}: {inventory_file}")
                        break
            if not inventory_file:
                app.logger.warning(f"[save_host_vars] Could not find inventory_file for host {host_name}, will use fallback directory")
        else:
            app.logger.info(f"[save_host_vars] Using provided inventory_file: {inventory_file}")
        
        # Определяем директорию host_vars на основе inventory файла
        host_vars_dir = get_host_vars_dir_for_inventory(project_id, inventory_file)
        host_vars_dir.mkdir(parents=True, exist_ok=True)
        file_path = host_vars_dir / file_name
        
        app.logger.info(f"[save_host_vars] Saving host_vars to: {file_path} (inventory_file: {inventory_file}, host_vars_dir: {host_vars_dir})")
        
        # Если inventory_file указан, убеждаемся, что папки group_vars и host_vars созданы рядом с inventory файлом
        if inventory_file:
            project_dir = get_project_dir(project_id)
            repo_dir = project_dir / 'repo'
            
            # Пробуем найти inventory файл
            inventory_path = None
            repo_path = repo_dir / inventory_file
            if repo_path.exists():
                inventory_path = repo_path
            else:
                # Ищем файл рекурсивно
                found_files = list(repo_dir.rglob(Path(inventory_file).name))
                if found_files:
                    inventory_path = found_files[0]
            
            if inventory_path and inventory_path.exists():
                # Создаем папки group_vars и host_vars рядом с inventory файлом
                ensure_inventory_dirs(inventory_path)
                app.logger.info(f"[save_host_vars] Ensured group_vars/host_vars directories for {inventory_path}")
        
        # Создаем бэкап если файл существует
        if file_path.exists():
            create_backup(file_path)
        
        # Сохраняем файл
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return jsonify({'success': True, 'message': f'File {file_name} saved'})
    except Exception as e:
        app.logger.error(f"Error saving host_vars: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/host_vars/download', methods=['GET'])
@require_auth
def download_host_vars():
    """Скачать host_vars файл из Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        file_name = request.args.get('file')
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        project_host_vars_dir = get_project_host_vars_dir(project_id)
        file_path = project_host_vars_dir / file_name
        
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        
        return send_file(
            str(file_path),
            mimetype='text/yaml',
            as_attachment=True,
            download_name=file_name
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/host_vars/delete', methods=['POST'])
@require_auth
def delete_host_vars():
    """Удалить host_vars файл из Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file')
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        project_host_vars_dir = get_project_host_vars_dir(project_id)
        file_path = project_host_vars_dir / file_name
        
        if not file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} not found in Project Storage'}), 404
        
        # Создаем бэкап перед удалением
        create_backup(file_path)
        
        # Удаляем файл
        file_path.unlink()
        
        return jsonify({'success': True, 'message': f'File {file_name} successfully deleted from Project Storage'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/host_vars/create', methods=['POST'])
@require_auth
def create_host_vars():
    """Создать новый host_vars файл в Project Storage"""
    try:
        # Получаем project_id - REQUIRED
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        file_name = data.get('file')
        
        if not file_name:
            return jsonify({'success': False, 'error': 'File name not specified'}), 400
        
        # Проверяем расширение
        if not (file_name.endswith('.yml') or file_name.endswith('.yaml')):
            return jsonify({'success': False, 'error': 'File name must end with .yml or .yaml'}), 400
        
        project_host_vars_dir = get_project_host_vars_dir(project_id)
        project_host_vars_dir.mkdir(parents=True, exist_ok=True)
        file_path = project_host_vars_dir / file_name
        
        if file_path.exists():
            return jsonify({'success': False, 'error': f'File {file_name} already exists'}), 400
        
        # Создаем шаблон нового файла
        template = f"# Host-specific variables for {file_name.replace('.yml', '').replace('.yaml', '')}\n"
        
        # Сохраняем файл
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(template)
        
        return jsonify({'success': True, 'message': f'File {file_name} created'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/check_host', methods=['POST'])
@require_auth
def check_host():
    """Проверить доступность хоста через Ansible ping"""
    try:
        data = request.json or {}
        host = data.get('host')
        
        app.logger.info(f"User checking host availability: {host}")
        app.logger.debug(f"[check_host] Received request with data: host={host}, connection_secret={data.get('connection_secret')}, project_id={data.get('project_id')}")
        
        if not host:
            return jsonify({'success': False, 'error': 'Host not specified'}), 400
        
        # Получаем project_id - REQUIRED, no fallback
        project_id = data.get('project_id') or get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        app.logger.debug(f"[check_host] Project ID: {project_id}, Host: {host}")
        
        # Get ansible config - всегда из папки ansible-config внутри проекта
        selected_ansible_config = data.get('ansible_config')
        if not selected_ansible_config:
            app.logger.error(f"[check_host] ansible_config not provided in request")
            return jsonify({'success': False, 'error': 'ansible_config is required'}), 400
        project_dir = get_project_dir(project_id)
        ansible_config_path = resolve_ansible_config_path(project_id, selected_ansible_config)
        ansible_config_path.parent.mkdir(parents=True, exist_ok=True)
        if not ansible_config_path.exists() or not ansible_config_path.is_file():
            app.logger.debug(f"[check_host] Config file not found: {ansible_config_path}, creating minimal config")
            if not create_minimal_ansible_config(ansible_config_path):
                app.logger.warning(f"[check_host] Failed to create minimal config, continuing without ANSIBLE_CONFIG")
        
        # Получаем выбранные inventory файлы
        selected_inventory_files = data.get('inventory_files', ['inventory.yml'])
        project_dir = get_project_dir(project_id)
        
        # Создаем временный inventory файл с одним хостом
        temp_inventory = None
        temp_playbook = None
        try:
            temp_dir = TEMP_DIR
            try:
                temp_dir.mkdir(exist_ok=True)
            except (OSError, PermissionError) as e:
                app.logger.error(f"Failed to create temp directory: {type(e).__name__}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to create temporary directory for host check'
                }), 500
            
            temp_inventory = temp_dir / f'check_host_{uuid.uuid4().hex[:8]}.yml'
            
            # Загружаем оригинальный inventory из Project Storage и фильтруем только нужный хост
            combined_inventory = {'all': {'hosts': {}}}
            
            inventories_dir = get_project_inventories_dir(project_id)
            inventory_names_alt = {'inventory.yml': 'inventory.yaml', 'inventory.yaml': 'inventory.yml', 'hosts.yml': 'hosts.yaml', 'hosts.yaml': 'hosts.yml', 'hosts': 'hosts', 'hosts.ini': 'hosts.ini'}
            for inv_file_path in selected_inventory_files:
                # Путь может быть относительным от repo (inventories/...) или именем файла (inventory.yml)
                if inv_file_path.startswith('inventories/') or inv_file_path.startswith('inventory/'):
                    inv_path = project_dir / 'repo' / inv_file_path
                elif '/' in inv_file_path:
                    inv_path = inventories_dir / inv_file_path
                else:
                    inv_path = inventories_dir / inv_file_path
                    if not inv_path.exists() and inv_file_path in inventory_names_alt:
                        inv_path = inventories_dir / inventory_names_alt[inv_file_path]
                    if not inv_path.exists() and inventories_dir.exists():
                        for p in inventories_dir.rglob(inv_file_path):
                            if p.is_file():
                                inv_path = p
                                break
                    if not inv_path.exists():
                        inv_path = project_dir / 'repo' / inv_file_path
                
                app.logger.debug(f"[check_host] Checking inventory file: {inv_path}")
                if inv_path.exists():
                    try:
                        with open(inv_path, 'r', encoding='utf-8') as f:
                            inv_data = yaml.safe_load(f)
                            host_data = find_host_in_inventory(inv_data, host)
                            if host_data is not None:
                                app.logger.debug(f"[check_host] Host {host} found in inventory, data: {host_data}")
                                combined_inventory['all']['hosts'][host] = host_data
                                break  # Хост найден, можно прекратить поиск
                            else:
                                app.logger.warning(f"[check_host] Host {host} not found in inventory file {inv_file_path}")
                    except Exception as e:
                        app.logger.warning(f"Error reading inventory file {inv_file_path}: {type(e).__name__}: {e}")
                else:
                    app.logger.warning(f"[check_host] Inventory file does not exist: {inv_path}")
            
            # Если хост не найден, создаем минимальный inventory
            if host not in combined_inventory['all']['hosts']:
                combined_inventory['all']['hosts'][host] = {}
            
            # Формируем простую структуру inventory без вложенных vars
            # Все параметры подключения будут на верхнем уровне хоста
            host_data = combined_inventory['all']['hosts'][host]
            
            # Инициализируем список для временных ключевых файлов
            temp_key_files = []
            
            # Сначала проверяем host_vars на наличие Ansible переменных
            host_vars_dir = get_project_host_vars_dir(project_id)
            # Пробуем сначала по IP хоста, потом по имени из inventory
            host_name = host  # IP адрес или имя хоста
            if 'ansible_host' in host_data:
                host_name = host_data.get('ansible_host', host)
            
            host_file = host_vars_dir / f"{host_name}.yml"
            # Если файл не найден по host_name, пробуем по исходному host
            if not host_file.exists() and host_name != host:
                host_file = host_vars_dir / f"{host}.yml"
                if host_file.exists():
                    host_name = host
            
            host_vars_loaded = False
            secret_username = None
            
            if host_file.exists():
                try:
                    with open(host_file, 'r', encoding='utf-8') as f:
                        host_vars = yaml_loader.load(f) or {}
                    
                    app.logger.debug(f"[check_host] Host vars loaded: {list(host_vars.keys())}")
                    
                    # Проверяем наличие connection-related переменных
                    if host_vars.get('ansible_ssh_private_key_file') or host_vars.get('ansible_password'):
                        app.logger.debug(f"[check_host] Found connection variables in host_vars, using them directly")
                        host_vars_loaded = True
                        
                        # Если есть connectionSecret, создаем временный файл ключа из секрета (как в api_run_playbook)
                        connection_secret_name = host_vars.get('connectionSecret')
                        if connection_secret_name and host_vars.get('ansible_ssh_private_key_file'):
                            try:
                                secrets_dir = get_project_secrets_dir(project_id)
                                # Пробуем найти секрет в новой структуре (secrets/ssh_keys/)
                                ssh_keys_dir = secrets_dir / 'ssh_keys'
                                secret_file = ssh_keys_dir / f"{connection_secret_name}.json"
                                if not secret_file.exists():
                                    # Fallback на старую структуру для обратной совместимости
                                    secret_file = secrets_dir / f"{connection_secret_name}.json"
                                
                                if secret_file.exists():
                                    with open(secret_file, 'r', encoding='utf-8') as f:
                                        secret_data = json.load(f)
                                    
                                    if secret_data.get('type') == 'ssh_key':
                                        private_key = secret_data.get('privateKey', '')
                                        if private_key:
                                            # Создаем временный файл ключа (как в api_run_playbook)
                                            import tempfile
                                            temp_key_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem', dir=str(TEMP_DIR), encoding='utf-8')
                                            key_content = normalize_pem_key_for_file(private_key)
                                            temp_key_file.write(key_content)
                                            temp_key_file.flush()  # Убеждаемся, что данные записаны
                                            os.fsync(temp_key_file.fileno())  # Принудительно записываем на диск
                                            temp_key_file.close()
                                            os.chmod(temp_key_file.name, 0o600)
                                            
                                            # Сохраняем путь к временному файлу для последующего удаления
                                            temp_key_files.append(temp_key_file.name)
                                            
                                            # Заменяем путь к ключу на временный файл (используем абсолютный путь)
                                            abs_key_path = os.path.abspath(temp_key_file.name)
                                            host_vars['ansible_ssh_private_key_file'] = abs_key_path
                                            
                                            # Обновляем ansible_user из секрета, если он указан
                                            secret_username = secret_data.get('username', '').strip()
                                            if secret_username:
                                                host_vars['ansible_user'] = secret_username
                                                app.logger.debug(f"[check_host] Updated ansible_user from secret {connection_secret_name}: {secret_username}")
                                            
                                            app.logger.debug(f"[check_host] Created temporary SSH key file from secret {connection_secret_name} at {abs_key_path}")
                                        else:
                                            app.logger.error(f"[check_host] Private key is empty in secret {connection_secret_name}")
                                            return jsonify({
                                                'success': False,
                                                'available': False,
                                                'error': f'Private key is empty in secret {connection_secret_name}',
                                                'message': f'Secret {connection_secret_name} does not contain a private key'
                                            }), 400
                                else:
                                    app.logger.error(f"[check_host] Secret file not found: {secret_file}")
                                    return jsonify({
                                        'success': False,
                                        'available': False,
                                        'error': f'Secret {connection_secret_name} not found',
                                        'message': f'Secret {connection_secret_name} not found'
                                    }), 400
                            except Exception as e:
                                # Если не удалось создать временный ключ из секрета по имени,
                                # попробуем обработать путь напрямую (если он указывает на JSON файл)
                                # Это обработается в блоке ниже
                                app.logger.warning(f"[check_host] Error creating temporary SSH key from secret name {connection_secret_name}: {e}, will try to process path directly if it's a JSON file")
                        
                        # Создаем simple_inventory с правильным host_name из host_vars
                        ansible_host_from_vars = host_vars.get('ansible_host', host_name)
                        simple_inventory = {
                            'all': {
                                'hosts': {
                                    ansible_host_from_vars: {}
                                }
                            }
                        }
                        
                        # Копируем все ansible_* переменные из host_vars (включая обновленный путь к временному ключу)
                        for key, value in host_vars.items():
                            if key.startswith('ansible_') or key == 'ansible_host':
                                simple_inventory['all']['hosts'][ansible_host_from_vars][key] = value
                        
                        # Получаем username для playbook
                        secret_username = host_vars.get('ansible_user', 'root')
                        
                        # Если используется SSH ключ, проверяем что файл существует и логируем путь
                        if host_vars.get('ansible_ssh_private_key_file'):
                            key_file_path = Path(host_vars['ansible_ssh_private_key_file'])
                            # Преобразуем в абсолютный путь, если он относительный
                            if not key_file_path.is_absolute():
                                key_file_path = key_file_path.resolve()
                            
                            # Проверяем, указывает ли путь на JSON файл секрета
                            # Если путь уже указывает на временный PEM файл (был создан выше), пропускаем обработку JSON
                            if key_file_path.exists() and key_file_path.suffix == '.json' and not str(key_file_path).startswith(str(TEMP_DIR)):
                                # Это JSON файл секрета - нужно создать временный PEM файл
                                if connection_secret_name:
                                    try:
                                        with open(key_file_path, 'r', encoding='utf-8') as f:
                                            secret_data = json.load(f)
                                        
                                        if secret_data.get('type') == 'ssh_key':
                                            private_key = secret_data.get('privateKey', '')
                                            if private_key:
                                                import tempfile
                                                temp_key_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem', dir=str(TEMP_DIR), encoding='utf-8')
                                                key_content = normalize_pem_key_for_file(private_key)
                                                temp_key_file.write(key_content)
                                                temp_key_file.flush()
                                                os.fsync(temp_key_file.fileno())
                                                temp_key_file.close()
                                                os.chmod(temp_key_file.name, 0o600)
                                                
                                                # Сохраняем путь к временному файлу для последующего удаления
                                                if 'temp_key_files' not in locals():
                                                    temp_key_files = []
                                                temp_key_files.append(temp_key_file.name)
                                                
                                                # Заменяем путь к ключу на временный файл
                                                abs_key_path = os.path.abspath(temp_key_file.name)
                                                host_vars['ansible_ssh_private_key_file'] = abs_key_path
                                                simple_inventory['all']['hosts'][ansible_host_from_vars]['ansible_ssh_private_key_file'] = abs_key_path
                                                
                                                # Обновляем ansible_user из секрета, если он указан
                                                secret_username = secret_data.get('username', '').strip()
                                                if secret_username:
                                                    host_vars['ansible_user'] = secret_username
                                                    simple_inventory['all']['hosts'][ansible_host_from_vars]['ansible_user'] = secret_username
                                                    app.logger.debug(f"[check_host] Updated ansible_user from JSON secret: {secret_username}")
                                                
                                                app.logger.debug(f"[check_host] Created temporary SSH key file from JSON secret at {abs_key_path}")
                                            else:
                                                app.logger.error(f"[check_host] Private key is empty in secret JSON file {key_file_path}")
                                                return jsonify({
                                                    'success': False,
                                                    'available': False,
                                                    'error': f'Private key is empty in secret file {key_file_path}',
                                                    'message': f'Secret file does not contain a private key'
                                                }), 400
                                        else:
                                            app.logger.error(f"[check_host] Secret file {key_file_path} is not an SSH key type")
                                            return jsonify({
                                                'success': False,
                                                'available': False,
                                                'error': f'Secret file {key_file_path} is not an SSH key type',
                                                'message': f'Secret file type is {secret_data.get("type")}, expected ssh_key'
                                            }), 400
                                    except Exception as e:
                                        app.logger.error(f"[check_host] Error processing JSON secret file {key_file_path}: {e}")
                                        return jsonify({
                                            'success': False,
                                            'available': False,
                                            'error': f'Error processing secret file: {str(e)}',
                                            'message': f'Failed to process secret file {key_file_path}'
                                        }), 400
                                else:
                                    app.logger.error(f"[check_host] Path points to JSON secret file {key_file_path}, but connectionSecret is not set")
                                    return jsonify({
                                        'success': False,
                                        'available': False,
                                        'error': f'Path points to JSON secret file, but connectionSecret is not set',
                                        'message': f'Cannot process JSON secret file without connectionSecret'
                                    }), 400
                            else:
                                # Обычный ключевой файл (не JSON)
                                if not key_file_path.is_absolute():
                                    simple_inventory['all']['hosts'][ansible_host_from_vars]['ansible_ssh_private_key_file'] = str(key_file_path)
                                
                                if not key_file_path.exists():
                                    app.logger.error(f"[check_host] SSH key file not found: {key_file_path}")
                                    return jsonify({
                                        'success': False,
                                        'available': False,
                                        'error': f'SSH key file not found: {key_file_path}',
                                        'message': 'SSH key file specified in host_vars, but file does not exist'
                                    }), 400
                                
                                # Проверяем права доступа
                                key_stat = os.stat(key_file_path)
                                if key_stat.st_mode & 0o077 != 0:
                                    app.logger.warning(f"[check_host] SSH key file has incorrect permissions: {oct(key_stat.st_mode)}, fixing...")
                                    os.chmod(key_file_path, 0o600)
                                
                                app.logger.debug(f"[check_host] Using SSH key file: {key_file_path} (exists: {key_file_path.exists()}, size: {key_file_path.stat().st_size if key_file_path.exists() else 0} bytes)")
                        
                        # Обновляем host_name для использования в playbook
                        host_name = ansible_host_from_vars
                except Exception as e:
                    app.logger.warning(f"Error reading host vars for {host_name}: {e}")
            
            # Если host_vars не содержат connection данных, используем старую логику с connection_secret
            if not host_vars_loaded:
                # Получаем connection secret - ОБЯЗАТЕЛЕН для подключения
                connection_secret_name = data.get('connection_secret')
                # Обрабатываем None, пустую строку и 'null' как отсутствие connection_secret
                if connection_secret_name in (None, '', 'null', 'undefined'):
                    connection_secret_name = None
                app.logger.debug(f"[check_host] Connection secret from request: {connection_secret_name} (type: {type(connection_secret_name).__name__})")
                if not connection_secret_name:
                    app.logger.error(f"[check_host] ERROR: connection_secret NOT provided in request! Connection is impossible without connection secret.")
                    return jsonify({
                        'success': False,
                        'available': False,
                        'error': 'Connection secret not specified. Select a connection secret for the host in Hosts & Groups.',
                        'message': 'Connection secret is required to connect to the host'
                    }), 400
                
                # connection_secret ОБЯЗАТЕЛЕН - продолжаем только если он указан
                if connection_secret_name:
                    try:
                        # Загружаем secret (новая структура: secrets/ssh_keys/)
                        secrets_dir = get_project_secrets_dir(project_id)
                        ssh_keys_dir = secrets_dir / 'ssh_keys'
                        
                        # Пробуем найти секрет в новой структуре (secrets/ssh_keys/)
                        secret_file = ssh_keys_dir / f"{connection_secret_name}.json"
                        if not secret_file.exists():
                            # Fallback на старую структуру для обратной совместимости
                            secret_file = secrets_dir / f"{connection_secret_name}.json"
                        
                        app.logger.debug(f"[check_host] Looking for secret file: {secret_file}")
                        
                        if secret_file.exists():
                            app.logger.debug(f"[check_host] Secret file found, loading...")
                            with open(secret_file, 'r', encoding='utf-8') as f:
                                secret_data = json.load(f)
                            
                            secret_type = secret_data.get('type')
                            app.logger.debug(f"[check_host] Secret type: {secret_type} (variable type: {type(secret_type).__name__})")
                            app.logger.debug(f"[check_host] Checking secret type for processing... secret_type == 'ssh_key': {secret_type == 'ssh_key'}")
                            
                            # Добавляем credentials в inventory для хоста
                            if secret_type == 'ssh_key':
                                app.logger.debug(f"[check_host] Processing SSH key secret...")
                                # Для SSH ключа создаем временный файл ключа
                                import tempfile
                                private_key = secret_data.get('privateKey', '')
                                if not private_key:
                                    app.logger.error(f"[check_host] Private key is empty in secret {connection_secret_name}")
                                    raise ValueError(f"Private key is empty in secret {connection_secret_name}")
                                
                                temp_key_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem', dir=str(TEMP_DIR), encoding='utf-8')
                                temp_key_file.write(normalize_pem_key_for_file(private_key))
                                temp_key_file.close()
                                os.chmod(temp_key_file.name, 0o600)
                                
                                # Определяем username - ОБЯЗАТЕЛЕН в secret, дефолтных значений быть не может
                                username = secret_data.get('username', '').strip()
                                if not username:
                                    app.logger.error(f"[check_host] ERROR: username not specified in connection secret {connection_secret_name}! Username is required.")
                                    raise ValueError(f"Username is required in connection secret {connection_secret_name} but is missing or empty")
                                
                                # Сохраняем username для использования в playbook
                                secret_username = username
                                
                                app.logger.info(f"[check_host] SSH key created: {temp_key_file.name}, username: {username}")
                                app.logger.info(f"[check_host] Key size: {len(private_key)} characters")
                                
                                # Добавляем в inventory напрямую (без vars)
                                combined_inventory['all']['hosts'][host]['ansible_ssh_private_key_file'] = temp_key_file.name
                                combined_inventory['all']['hosts'][host]['ansible_user'] = username
                                app.logger.debug(f"[check_host] Added parameters to inventory: ansible_ssh_private_key_file={temp_key_file.name}, ansible_user={username}")
                                # Убираем init_ssh_connect - он не нужен для простого gather facts
                                
                                # Сохраняем путь к временному файлу для последующего удаления
                                if 'temp_key_files' not in locals():
                                    temp_key_files = []
                                temp_key_files.append(temp_key_file.name)
                            elif secret_type == 'login_password':
                                # Для пароля добавляем в inventory
                                username = secret_data.get('username', '').strip()
                                if not username:
                                    app.logger.error(f"[check_host] ERROR: username not specified in connection secret {connection_secret_name}! Username is required.")
                                    raise ValueError(f"Username is required in connection secret {connection_secret_name} but is missing or empty")
                                
                                # Сохраняем username для использования в playbook
                                secret_username = username
                                
                                password = secret_data.get('password', '')
                                
                                if not password:
                                    app.logger.error(f"[check_host] Password is empty in secret {connection_secret_name}")
                                    raise ValueError(f"Password is empty in secret {connection_secret_name}")
                                
                                app.logger.info(f"[check_host] Using password auth, username: {username}, password length: {len(password)}")
                                
                                # Добавляем в inventory напрямую (без vars) в простом формате
                                combined_inventory['all']['hosts'][host]['ansible_user'] = username
                                combined_inventory['all']['hosts'][host]['ansible_password'] = password
                                app.logger.debug(f"[check_host] Added parameters to inventory: ansible_user={username}, ansible_password=***")
                    except Exception as e:
                        app.logger.error(f"[check_host] ERROR loading connection secret {connection_secret_name}: {e}")
                        return jsonify({
                            'success': False,
                            'available': False,
                            'error': f'Error loading connection secret: {str(e)}',
                            'message': f'Failed to load connection secret {connection_secret_name}'
                        }), 400
            
            # Если host_vars не были загружены, формируем простой inventory из combined_inventory
            if not host_vars_loaded:
                # Формируем простой inventory в формате без вложенных vars
                # Все параметры подключения на верхнем уровне хоста
                app.logger.debug(f"[check_host] Creating simple inventory format from combined_inventory...")
                
                # Получаем все параметры из combined_inventory
                host_data = combined_inventory['all']['hosts'][host]
                
                # Определяем имя хоста (используем IP или имя из inventory)
                host_name = host
                if 'ansible_host' in host_data:
                    host_name = host_data.get('ansible_host', host)
                
                # Создаем simple_inventory
                simple_inventory = {
                    'all': {
                        'hosts': {
                            host_name: {}
                        }
                    }
                }
                
                # Добавляем ansible_host (IP адрес)
                simple_inventory['all']['hosts'][host_name]['ansible_host'] = host_data.get('ansible_host', host)
                
                # Копируем все ansible_* параметры напрямую на верхний уровень хоста
                # ИСКЛЮЧАЕМ ansible_ssh_common_args - он не нужен в простом inventory
                for key, value in host_data.items():
                    if key.startswith('ansible_') or key == 'ansible_host':
                        # Пропускаем ansible_ssh_common_args
                        if key != 'ansible_ssh_common_args':
                            simple_inventory['all']['hosts'][host_name][key] = value
                    elif key == 'vars':
                        # Если есть vars, копируем их параметры на верхний уровень
                        for var_key, var_value in value.items():
                            if var_key.startswith('ansible_'):
                                # Пропускаем ansible_ssh_common_args
                                if var_key != 'ansible_ssh_common_args':
                                    simple_inventory['all']['hosts'][host_name][var_key] = var_value
            else:
                # host_vars уже загружены, simple_inventory уже создан выше
                app.logger.debug(f"[check_host] Using connection variables from host_vars")
            
            # Отключаем проверку host key для Check (иначе первый SSH даёт unreachable)
            for _h in simple_inventory.get('all', {}).get('hosts', {}):
                simple_inventory['all']['hosts'][_h]['ansible_ssh_common_args'] = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
                break
            
            # Генерируем execution_id один раз: по нему создаём каталог и пути для воркера.
            # Ключи копируем в runtime/generated_playbooks/ с дедупликацией по содержимому.
            execution_id = str(uuid.uuid4())
            generated_playbooks_dir = get_project_generated_playbook_path(project_id, execution_id).parent
            generated_playbooks_dir.mkdir(parents=True, exist_ok=True)
            key_path_map = _copy_keys_to_generated_playbooks_dedup(
                temp_key_files if 'temp_key_files' in locals() else [],
                generated_playbooks_dir
            )
            for _h, hdata in simple_inventory.get('all', {}).get('hosts', {}).items():
                k = hdata.get('ansible_ssh_private_key_file')
                if k and k in key_path_map:
                    hdata['ansible_ssh_private_key_file'] = key_path_map[k]
            
            app.logger.debug(f"[check_host] Creating temporary inventory file: {temp_inventory}")
            try:
                with open(temp_inventory, 'w', encoding='utf-8') as f:
                    yaml.dump(simple_inventory, f, default_flow_style=False, allow_unicode=True)
                app.logger.debug(f"[check_host] Temporary inventory file created successfully")
                
                # Логируем структуру inventory без секретов (для отладки)
                safe_inventory = {}
                for host_name_inv, host_data_inv in simple_inventory.get('all', {}).get('hosts', {}).items():
                    safe_inventory[host_name_inv] = {}
                    for key, value in host_data_inv.items():
                        if 'key' in key.lower() or 'password' in key.lower() or 'secret' in key.lower():
                            safe_inventory[host_name_inv][key] = f"***REDACTED*** (path exists: {Path(value).exists() if isinstance(value, str) else 'N/A'})"
                        else:
                            safe_inventory[host_name_inv][key] = value
                app.logger.debug(f"[check_host] Inventory structure: {safe_inventory}")
                
                # SECURITY: Never log inventory file contents (may contain secrets).
            except (OSError, PermissionError, IOError) as e:
                app.logger.error(f"Failed to create temporary inventory file: {type(e).__name__}: {e}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to create temporary file for host check'
                }), 500
        except Exception as e:
            app.logger.error(f"Error preparing temporary files: {type(e).__name__}")
            return jsonify({
                'success': False,
                'error': 'Error preparing host check'
            }), 500
        
        # Команда для проверки доступности
        env = os.environ.copy()
        env['GIT_SSH_COMMAND'] = 'ssh -o StrictHostKeyChecking=no'
        if ansible_config_path.exists() and ansible_config_path.is_file():
            env['ANSIBLE_CONFIG'] = str(ansible_config_path)
            app.logger.debug(f"[check_host] Using ANSIBLE_CONFIG: {ansible_config_path}")
        else:
            app.logger.warning(f"Ansible config {selected_ansible_config} not found in project storage for project {project_id} at path {ansible_config_path}, using default Ansible config")
        
        # Если используется password auth, настраиваем использование sshpass
        # Проверяем параметры из simple_inventory (он уже создан выше)
        check_host_data = simple_inventory.get('all', {}).get('hosts', {}).get(host_name, {})
        if secret_username and ('ansible_password' in check_host_data or 'ansible_ssh_pass' in check_host_data):
            sshpass_path = None
            try:
                import shutil
                sshpass_path = shutil.which('sshpass')
                if sshpass_path:
                    app.logger.debug(f"[check_host] sshpass found: {sshpass_path}")
                    # Ansible автоматически использует sshpass если он доступен и ansible_ssh_pass установлен
                    password = check_host_data.get('ansible_password') or check_host_data.get('ansible_ssh_pass', '')
                    if password:
                        # Убеждаемся, что PATH содержит путь к sshpass
                        current_path = env.get('PATH', '')
                        sshpass_dir = str(Path(sshpass_path).parent)
                        if sshpass_dir not in current_path:
                            env['PATH'] = f"{sshpass_dir}:{current_path}"
                else:
                    app.logger.error(f"[check_host] sshpass not found in PATH! Password auth will NOT work without sshpass.")
                    app.logger.error(f"[check_host] Install sshpass: apt-get install sshpass (Debian/Ubuntu) or yum install sshpass (RHEL/CentOS)")
                    return jsonify({
                        'success': False,
                        'available': False,
                        'error': 'sshpass is not installed. Install sshpass to use password authentication.',
                        'message': 'Password authentication requires sshpass. Install: apt-get install sshpass'
                    }), 400
            except Exception as e:
                app.logger.error(f"[check_host] Error checking sshpass: {e}")
                return jsonify({
                    'success': False,
                    'available': False,
                    'error': f'Error checking sshpass: {str(e)}',
                    'message': 'Failed to check sshpass availability'
                }), 500
        
        # Создаем простой временный playbook для проверки доступности хоста через ansible ping
        try:
            temp_playbook = TEMP_DIR / f'check_host_playbook_{uuid.uuid4().hex[:8]}.yaml'
            
            # Простейший playbook - ansible ping для проверки доступности
            # Connection credentials указаны в inventory через host_vars или connection secret
            # Также добавляем remote_user в playbook для явного указания пользователя
            # secret_username должен быть установлен либо из host_vars, либо из connection_secret
            if not secret_username:
                # Пытаемся получить username из simple_inventory
                check_host_data = simple_inventory.get('all', {}).get('hosts', {}).get(host_name, {})
                secret_username = check_host_data.get('ansible_user', 'root')
                app.logger.debug(f"[check_host] Using username from inventory: {secret_username}")
            
            playbook_content = f"""---
- hosts: all
  remote_user: {secret_username}
  gather_facts: no
  vars:
    ansible_executable: /bin/sh
    ansible_python_interpreter: auto_silent
  tasks:
    - name: Test connectivity to host
      ansible.builtin.ping:
"""
            app.logger.debug(f"[check_host] Adding remote_user: {secret_username} to playbook from connection secret")
            
            try:
                with open(temp_playbook, 'w', encoding='utf-8') as f:
                    f.write(playbook_content)
            except (OSError, PermissionError, IOError) as e:
                app.logger.error(f"Failed to create temporary playbook file: {type(e).__name__}")
                # Удаляем temp_inventory если он был создан
                if temp_inventory and temp_inventory.exists():
                    try:
                        temp_inventory.unlink()
                    except Exception:
                        pass
                return jsonify({
                    'success': False,
                    'error': 'Failed to create temporary file for host check'
                }), 500
        except Exception as e:
            app.logger.error(f"Error creating temporary playbook: {type(e).__name__}")
            # Удаляем temp_inventory если он был создан
            if temp_inventory and temp_inventory.exists():
                try:
                    temp_inventory.unlink()
                except Exception:
                    pass
            return jsonify({
                'success': False,
                'error': 'Error preparing host check'
            }), 500
        
        # Создаем execution для выполнения через воркер
        # Вместо прямого выполнения через subprocess.run(), создаем execution
        limit_host = host_name if 'host_name' in locals() else host
        
        # Сохраняем playbook в постоянное место для воркера (execution_id уже создан выше)
        generated_playbook_path = get_project_generated_playbook_path(project_id, execution_id)
        generated_playbook_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Копируем временный playbook в постоянное место
        import shutil
        shutil.copy2(temp_playbook, generated_playbook_path)
        app.logger.debug(f"[check_host] Copied playbook to: {generated_playbook_path}")
        
        # Сохраняем inventory в постоянное место
        generated_inventory_path = get_project_generated_playbook_path(project_id, execution_id).parent / f'inventory_{execution_id}.yml'
        shutil.copy2(temp_inventory, generated_inventory_path)
        app.logger.debug(f"[check_host] Copied inventory to: {generated_inventory_path}")
        
        # Удаляем временные файлы (они скопированы в постоянное место)
        try:
            if temp_inventory.exists():
                temp_inventory.unlink()
        except Exception:
            pass
        try:
            if temp_playbook.exists():
                temp_playbook.unlink()
        except Exception:
            pass
        
        # Создаем execution record
        execution_data = {
            'status': 'QUEUED',
            'playbookName': f'host_check_{host}',
            'mode': 'HOST_CHECK',
            'runParams': {
                'temp_playbook': str(generated_playbook_path),
                'temp_inventory': str(generated_inventory_path),
                'inventory_files': selected_inventory_files,
                'ansible_config': str(ansible_config_path) if ansible_config_path.exists() else None,
                'project_dir': str(project_dir),
                'host': host,
                'limit_host': limit_host,
                'execution_type': 'HOST_CHECK',
                'temp_key_files': temp_key_files if 'temp_key_files' in locals() else []
            },
            'description': f'Check host availability: {host}'
        }
        
        execution_id_created = create_execution_record(execution_data, project_id=project_id, execution_id=execution_id)
        
        if not execution_id_created:
            app.logger.error(f"[check_host] Failed to create execution record")
            # Удаляем созданные файлы
            try:
                if generated_playbook_path.exists():
                    generated_playbook_path.unlink()
            except Exception:
                pass
            try:
                if generated_inventory_path.exists():
                    generated_inventory_path.unlink()
            except Exception:
                pass
            return jsonify({
                'success': False,
                'error': 'Failed to create execution record'
            }), 500
        
        app.logger.info(f"[check_host] Created execution {execution_id_created} for host check: {host}")
        
        # Возвращаем execution_id клиенту для опроса статуса
        return jsonify({
            'success': True,
            'executionId': execution_id_created,
            'message': 'Host check queued for execution',
            'status': 'QUEUED'
        })
            
    except Exception as e:
        # Удаляем временные файлы при глобальной ошибке
        if 'temp_inventory' in locals() and temp_inventory and temp_inventory.exists():
            try:
                temp_inventory.unlink()
            except Exception:
                pass
        if 'temp_playbook' in locals() and temp_playbook and temp_playbook.exists():
            try:
                temp_playbook.unlink()
            except Exception:
                pass
        # Логируем только тип ошибки
        app.logger.error(f"Critical error during host check: {type(e).__name__}")
        return jsonify({
            'success': False,
            'error': 'Internal error checking host'
        }), 500


def _build_one_host_check_entry(project_id, host, connection_secret_name, selected_inventory_files, project_dir, inventories_dir):
    """
    Строит одну запись инвентаря для проверки хоста (для использования в check_hosts).
    Возвращает (inventory_key, entry_dict, temp_key_files) или (None, None, []) если хост пропускаем.
    Может выбросить исключение при ошибке (например отсутствующий секрет).
    """
    combined_inventory = {'all': {'hosts': {}}}
    inventory_names_alt = {'inventory.yml': 'inventory.yaml', 'inventory.yaml': 'inventory.yml', 'hosts.yml': 'hosts.yaml', 'hosts.yaml': 'hosts.yml', 'hosts': 'hosts', 'hosts.ini': 'hosts.ini'}
    for inv_file_path in selected_inventory_files:
        if inv_file_path.startswith('inventories/') or inv_file_path.startswith('inventory/'):
            inv_path = project_dir / 'repo' / inv_file_path
        elif '/' in inv_file_path:
            inv_path = inventories_dir / inv_file_path
        else:
            inv_path = inventories_dir / inv_file_path
            if not inv_path.exists() and inv_file_path in inventory_names_alt:
                inv_path = inventories_dir / inventory_names_alt[inv_file_path]
            if not inv_path.exists() and inventories_dir.exists():
                for p in inventories_dir.rglob(inv_file_path):
                    if p.is_file():
                        inv_path = p
                        break
            if not inv_path.exists():
                inv_path = project_dir / 'repo' / inv_file_path
        if inv_path.exists():
            try:
                with open(inv_path, 'r', encoding='utf-8') as f:
                    inv_data = yaml.safe_load(f)
                    host_data = find_host_in_inventory(inv_data, host)
                    if host_data is not None:
                        combined_inventory['all']['hosts'][host] = host_data
                        break
            except Exception:
                pass
    if host not in combined_inventory['all']['hosts']:
        combined_inventory['all']['hosts'][host] = {}
    host_data = combined_inventory['all']['hosts'][host]
    temp_key_files = []
    host_vars_dir = get_project_host_vars_dir(project_id)
    host_name = host
    if 'ansible_host' in host_data:
        host_name = host_data.get('ansible_host', host)
    host_file = host_vars_dir / f"{host_name}.yml"
    if not host_file.exists() and host_name != host:
        host_file = host_vars_dir / f"{host}.yml"
        if host_file.exists():
            host_name = host
    host_vars_loaded = False
    entry = {}
    inv_key = host_name
    if host_file.exists():
        try:
            with open(host_file, 'r', encoding='utf-8') as f:
                host_vars = yaml_loader.load(f) or {}
            if host_vars.get('ansible_ssh_private_key_file') or host_vars.get('ansible_password'):
                host_vars_loaded = True
                inv_key = host_vars.get('ansible_host', host_name) or host_name
                connection_secret_name_hv = host_vars.get('connectionSecret')
                if connection_secret_name_hv and host_vars.get('ansible_ssh_private_key_file'):
                    secrets_dir = get_project_secrets_dir(project_id)
                    ssh_keys_dir = secrets_dir / 'ssh_keys'
                    secret_file = ssh_keys_dir / f"{connection_secret_name_hv}.json"
                    if not secret_file.exists():
                        secret_file = secrets_dir / f"{connection_secret_name_hv}.json"
                    if secret_file.exists():
                        with open(secret_file, 'r', encoding='utf-8') as f:
                            secret_data = json.load(f)
                        if secret_data.get('type') == 'ssh_key' and secret_data.get('privateKey'):
                            import tempfile
                            temp_key_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem', dir=str(TEMP_DIR), encoding='utf-8')
                            temp_key_file.write(normalize_pem_key_for_file(secret_data['privateKey']))
                            temp_key_file.flush()
                            os.fsync(temp_key_file.fileno())
                            temp_key_file.close()
                            os.chmod(temp_key_file.name, 0o600)
                            temp_key_files.append(temp_key_file.name)
                            host_vars = dict(host_vars)
                            host_vars['ansible_ssh_private_key_file'] = os.path.abspath(temp_key_file.name)
                            if secret_data.get('username', '').strip():
                                host_vars['ansible_user'] = secret_data['username'].strip()
                for key, value in host_vars.items():
                    if (key.startswith('ansible_') or key == 'ansible_host') and key != 'ansible_ssh_common_args':
                        entry[key] = value
        except Exception:
            pass
    if not host_vars_loaded:
        if not connection_secret_name or connection_secret_name in (None, '', 'null', 'undefined'):
            return None, None, []
        secrets_dir = get_project_secrets_dir(project_id)
        ssh_keys_dir = secrets_dir / 'ssh_keys'
        secret_file = ssh_keys_dir / f"{connection_secret_name}.json"
        if not secret_file.exists():
            secret_file = secrets_dir / f"{connection_secret_name}.json"
        if not secret_file.exists():
            raise ValueError(f"Secret {connection_secret_name} not found")
        with open(secret_file, 'r', encoding='utf-8') as f:
            secret_data = json.load(f)
        inv_key = host_data.get('ansible_host', host) or host
        entry['ansible_host'] = host_data.get('ansible_host', host) or host
        if secret_data.get('type') == 'ssh_key':
            private_key = secret_data.get('privateKey', '')
            if not private_key:
                raise ValueError(f"Private key is empty in secret {connection_secret_name}")
            username = (secret_data.get('username') or '').strip()
            if not username:
                raise ValueError(f"Username required in secret {connection_secret_name}")
            import tempfile
            temp_key_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem', dir=str(TEMP_DIR), encoding='utf-8')
            temp_key_file.write(normalize_pem_key_for_file(private_key))
            temp_key_file.close()
            os.chmod(temp_key_file.name, 0o600)
            temp_key_files.append(temp_key_file.name)
            entry['ansible_ssh_private_key_file'] = temp_key_file.name
            entry['ansible_user'] = username
        elif secret_data.get('type') == 'login_password':
            username = (secret_data.get('username') or '').strip()
            if not username:
                raise ValueError(f"Username required in secret {connection_secret_name}")
            password = secret_data.get('password', '')
            if not password:
                raise ValueError(f"Password empty in secret {connection_secret_name}")
            entry['ansible_user'] = username
            entry['ansible_password'] = password
        else:
            raise ValueError(f"Unsupported secret type: {secret_data.get('type')}")
    entry['ansible_ssh_common_args'] = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
    return inv_key, entry, temp_key_files


@app.route('/api/check_hosts', methods=['POST'])
@require_auth
def check_hosts():
    """Проверить доступность нескольких хостов одним заданием (один execution)."""
    try:
        data = request.json or {}
        hosts = data.get('hosts')
        if not hosts or not isinstance(hosts, list):
            return jsonify({'success': False, 'error': 'hosts must be a non-empty list'}), 400
        hosts = [str(h).strip() for h in hosts if str(h).strip()]
        if not hosts:
            return jsonify({'success': False, 'error': 'hosts must be a non-empty list'}), 400
        project_id = data.get('project_id') or get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        selected_ansible_config = data.get('ansible_config')
        if not selected_ansible_config:
            return jsonify({'success': False, 'error': 'ansible_config is required'}), 400
        project_dir = get_project_dir(project_id)
        ansible_config_path = resolve_ansible_config_path(project_id, selected_ansible_config)
        ansible_config_path.parent.mkdir(parents=True, exist_ok=True)
        if not ansible_config_path.exists() or not ansible_config_path.is_file():
            if not create_minimal_ansible_config(ansible_config_path):
                pass
        selected_inventory_files = data.get('inventory_files', ['inventory.yml'])
        connection_secrets = data.get('connection_secrets') or {}
        inventories_dir = get_project_inventories_dir(project_id)
        combined = {'all': {'hosts': {}}}
        all_temp_key_files = []
        hosts_resolved = []
        for host in hosts:
            secret_name = connection_secrets.get(host)
            if secret_name is None or (isinstance(secret_name, str) and secret_name.strip() in ('', 'null', 'undefined')):
                secret_name, has_conn = resolve_connection_secret_for_host(project_id, host)
                if not has_conn and not secret_name:
                    app.logger.debug(f"[check_hosts] Skipping host {host}: no connection")
                    continue
            try:
                inv_key, entry, keys = _build_one_host_check_entry(
                    project_id, host, secret_name, selected_inventory_files, project_dir, inventories_dir
                )
            except Exception as e:
                app.logger.warning(f"[check_hosts] Host {host}: {e}")
                continue
            if inv_key is None:
                continue
            combined['all']['hosts'][inv_key] = entry
            all_temp_key_files.extend(keys)
            hosts_resolved.append(inv_key)
        if not hosts_resolved:
            return jsonify({'success': False, 'error': 'No hosts with configured connection to check'}), 400
        temp_dir = TEMP_DIR
        temp_dir.mkdir(exist_ok=True)
        execution_id = str(uuid.uuid4())
        generated_playbooks_dir = get_project_generated_playbook_path(project_id, execution_id).parent
        generated_playbooks_dir.mkdir(parents=True, exist_ok=True)
        key_path_map = _copy_keys_to_generated_playbooks_dedup(all_temp_key_files, generated_playbooks_dir)
        for _h, hdata in combined.get('all', {}).get('hosts', {}).items():
            k = hdata.get('ansible_ssh_private_key_file')
            if k and k in key_path_map:
                hdata['ansible_ssh_private_key_file'] = key_path_map[k]
        temp_inventory = temp_dir / f'check_hosts_{uuid.uuid4().hex[:8]}.yml'
        with open(temp_inventory, 'w', encoding='utf-8') as f:
            yaml.dump(combined, f, default_flow_style=False, allow_unicode=True)
        playbook_content = """---
- hosts: all
  gather_facts: no
  vars:
    ansible_executable: /bin/sh
    ansible_python_interpreter: auto_silent
  tasks:
    - name: Test connectivity to host
      ansible.builtin.ping:
"""
        temp_playbook = temp_dir / f'check_hosts_playbook_{uuid.uuid4().hex[:8]}.yaml'
        with open(temp_playbook, 'w', encoding='utf-8') as f:
            f.write(playbook_content)
        generated_playbook_path = get_project_generated_playbook_path(project_id, execution_id)
        generated_inventory_path = generated_playbook_path.parent / f'inventory_{execution_id}.yml'
        import shutil
        shutil.copy2(temp_playbook, generated_playbook_path)
        shutil.copy2(temp_inventory, generated_inventory_path)
        try:
            temp_inventory.unlink()
        except Exception:
            pass
        try:
            temp_playbook.unlink()
        except Exception:
            pass
        execution_data = {
            'status': 'QUEUED',
            'playbookName': f'host_check_{len(hosts_resolved)}_hosts',
            'mode': 'HOST_CHECK',
            'runParams': {
                'temp_playbook': str(generated_playbook_path),
                'temp_inventory': str(generated_inventory_path),
                'inventory_files': selected_inventory_files,
                'ansible_config': str(ansible_config_path) if ansible_config_path.exists() else None,
                'project_dir': str(project_dir),
                'hosts': hosts_resolved,
                'execution_type': 'HOST_CHECK',
                'temp_key_files': all_temp_key_files,
            },
            'description': f'Check host availability: {", ".join(hosts_resolved[:5])}{"..." if len(hosts_resolved) > 5 else ""}'
        }
        execution_id_created = create_execution_record(execution_data, project_id=project_id, execution_id=execution_id)
        if not execution_id_created:
            try:
                generated_playbook_path.unlink(missing_ok=True)
                generated_inventory_path.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({'success': False, 'error': 'Failed to create execution record'}), 500
        app.logger.info(f"[check_hosts] Created execution {execution_id_created} for {len(hosts_resolved)} hosts")
        return jsonify({
            'success': True,
            'executionId': execution_id_created,
            'message': f'Host check queued for {len(hosts_resolved)} host(s)',
            'status': 'QUEUED'
        })
    except Exception as e:
        app.logger.error(f"[check_hosts] Error: {type(e).__name__}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hosts/<host_name>/facts', methods=['POST'])
@require_auth
def get_host_facts(host_name):
    """Получить Ansible facts для хоста через gather_facts"""
    try:
        data = request.json or {}
        project_id = data.get('project_id') or get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        app.logger.info(f"[get_host_facts] Received request for host: {host_name}, project_id: {project_id}")
        
        # Используем ту же логику подготовки inventory, что и в check_host
        # (копируем большую часть кода из check_host)
        
        # Get ansible config (для ansible.cfg — корень проекта)
        selected_ansible_config = data.get('ansible_config') or 'ansible.cfg'
        project_dir = get_project_dir(project_id)
        ansible_config_path = resolve_ansible_config_path(project_id, selected_ansible_config)
        ansible_config_path.parent.mkdir(parents=True, exist_ok=True)
        if not ansible_config_path.exists():
            create_minimal_ansible_config(ansible_config_path)
        
        # Загружаем inventory
        inventory_files = data.get('inventory_files', ['inventory.yml'])
        project_dir = get_project_dir(project_id)
        combined_inventory = {'all': {'hosts': {}}}
        
        for inv_file_path in inventory_files:
            # Путь может быть относительным от repo (например, inventories/prod/hosts.yml)
            # или просто именем файла (например, inventory.yml)
            if inv_file_path.startswith('inventories/') or inv_file_path.startswith('inventory/'):
                # Путь относительно repo
                inv_path = project_dir / 'repo' / inv_file_path
            elif '/' in inv_file_path:
                # Путь с подпапками, но без префикса inventories/
                inv_path = project_dir / 'repo' / 'inventories' / inv_file_path
            else:
                # Просто имя файла - используем get_project_inventories_dir для правильного пути
                inventories_dir = get_project_inventories_dir(project_id)
                inv_path = inventories_dir / inv_file_path
                # Если не найден, пробуем в корне repo
                if not inv_path.exists():
                    inv_path = project_dir / 'repo' / inv_file_path
            
            if inv_path.exists():
                try:
                    with open(inv_path, 'r', encoding='utf-8') as f:
                        inv_data = yaml.safe_load(f)
                        host_data = find_host_in_inventory(inv_data, host_name)
                        if host_data is not None:
                            combined_inventory['all']['hosts'][host_name] = host_data
                            break
                except Exception as e:
                    app.logger.warning(f"Error reading inventory file {inv_file_path}: {type(e).__name__}: {e}")
        
        if host_name not in combined_inventory['all']['hosts']:
            combined_inventory['all']['hosts'][host_name] = {}
        
        host_data = combined_inventory['all']['hosts'][host_name]
        host_name_actual = host_name
        if 'ansible_host' in host_data:
            host_name_actual = host_data.get('ansible_host', host_name)
        
        # Проверяем host_vars
        host_vars_dir = get_project_host_vars_dir(project_id)
        host_file = host_vars_dir / f"{host_name_actual}.yml"
        if not host_file.exists() and host_name_actual != host_name:
            host_file = host_vars_dir / f"{host_name}.yml"
            if host_file.exists():
                host_name_actual = host_name
        
        temp_key_files = []
        host_vars_loaded = False
        secret_username = None
        
        if host_file.exists():
            try:
                with open(host_file, 'r', encoding='utf-8') as f:
                    host_vars = yaml_loader.load(f) or {}
                
                if host_vars.get('ansible_ssh_private_key_file') or host_vars.get('ansible_password'):
                    host_vars_loaded = True
                    
                    # Если есть connectionSecret, создаем временный файл ключа из секрета
                    connection_secret_name = host_vars.get('connectionSecret')
                    if connection_secret_name and host_vars.get('ansible_ssh_private_key_file'):
                        try:
                            secrets_dir = get_project_secrets_dir(project_id)
                            ssh_keys_dir = secrets_dir / 'ssh_keys'
                            
                            # Пробуем найти секрет в новой структуре (secrets/ssh_keys/)
                            secret_file = ssh_keys_dir / f"{connection_secret_name}.json"
                            if not secret_file.exists():
                                # Fallback на старую структуру для обратной совместимости
                                # Пробуем найти секрет в новой структуре (secrets/ssh_keys/)
                                ssh_keys_dir = secrets_dir / 'ssh_keys'
                                secret_file = ssh_keys_dir / f"{connection_secret_name}.json"
                                if not secret_file.exists():
                                    # Fallback на старую структуру для обратной совместимости
                                    secret_file = secrets_dir / f"{connection_secret_name}.json"
                            
                            if secret_file.exists():
                                with open(secret_file, 'r', encoding='utf-8') as f:
                                    secret_data = json.load(f)
                                
                                if secret_data.get('type') == 'ssh_key':
                                    private_key = secret_data.get('privateKey', '')
                                    if private_key:
                                        # Обновляем ansible_user из секрета, если он указан (как в check_host)
                                        # Это важно, т.к. host_vars может содержать устаревший/дефолтный root,
                                        # а connection secret хранит актуального пользователя (например localuser).
                                        secret_username_from_secret = str(secret_data.get('username') or '').strip()
                                        if secret_username_from_secret:
                                            host_vars['ansible_user'] = secret_username_from_secret

                                        import tempfile
                                        temp_key_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem', dir=str(TEMP_DIR), encoding='utf-8')
                                        key_content = private_key
                                        if not key_content.endswith('\n'):
                                            key_content = key_content + '\n'
                                        temp_key_file.write(key_content)
                                        temp_key_file.flush()
                                        os.fsync(temp_key_file.fileno())
                                        temp_key_file.close()
                                        os.chmod(temp_key_file.name, 0o600)
                                        temp_key_files.append(temp_key_file.name)
                                        host_vars['ansible_ssh_private_key_file'] = os.path.abspath(temp_key_file.name)
                        except Exception as e:
                            app.logger.warning(f"Error creating temporary key from secret: {e}")
                    
                    # Создаем simple_inventory
                    ansible_host_from_vars = host_vars.get('ansible_host', host_name_actual)
                    simple_inventory = {
                        'all': {
                            'hosts': {
                                ansible_host_from_vars: {}
                            }
                        }
                    }
                    
                    for key, value in host_vars.items():
                        if key.startswith('ansible_') or key == 'ansible_host':
                            simple_inventory['all']['hosts'][ansible_host_from_vars][key] = value
                    
                    secret_username = host_vars.get('ansible_user', 'root')
                    host_name_actual = ansible_host_from_vars
            except Exception as e:
                app.logger.warning(f"Error reading host vars for {host_name}: {e}")
        
        if not host_vars_loaded:
            # Используем данные из inventory
            simple_inventory = {
                'all': {
                    'hosts': {
                        host_name_actual: {}
                    }
                }
            }
            simple_inventory['all']['hosts'][host_name_actual]['ansible_host'] = host_data.get('ansible_host', host_name)
            for key, value in host_data.items():
                if key.startswith('ansible_') or key == 'ansible_host':
                    if key != 'ansible_ssh_common_args':
                        simple_inventory['all']['hosts'][host_name_actual][key] = value
        
        # Создаем временный inventory файл
        temp_inventory = TEMP_DIR / f'facts_inventory_{uuid.uuid4().hex[:8]}.yml'
        temp_inventory.parent.mkdir(exist_ok=True)
        with open(temp_inventory, 'w', encoding='utf-8') as f:
            yaml.dump(simple_inventory, f, default_flow_style=False, allow_unicode=True)
        
        # Используем ansible ad-hoc команду с модулем setup для получения facts в JSON
        env = os.environ.copy()
        env['GIT_SSH_COMMAND'] = 'ssh -o StrictHostKeyChecking=no'
        if ansible_config_path.exists() and ansible_config_path.is_file():
            env['ANSIBLE_CONFIG'] = str(ansible_config_path)
        
        # Если используется password auth, настраиваем sshpass
        check_host_data = simple_inventory.get('all', {}).get('hosts', {}).get(host_name_actual, {})
        if secret_username and ('ansible_password' in check_host_data or 'ansible_ssh_pass' in check_host_data):
            try:
                import shutil
                sshpass_path = shutil.which('sshpass')
                if sshpass_path:
                    current_path = env.get('PATH', '')
                    sshpass_dir = str(Path(sshpass_path).parent)
                    if sshpass_dir not in current_path:
                        env['PATH'] = f"{sshpass_dir}:{current_path}"
            except Exception as e:
                app.logger.warning(f"[get_host_facts] Error checking sshpass: {e}")
        
        # Создаем простой playbook для gather_facts
        temp_playbook = TEMP_DIR / f'get_facts_playbook_{uuid.uuid4().hex[:8]}.yaml'
        
        if not secret_username:
            secret_username = check_host_data.get('ansible_user', 'root')
        
        playbook_content = f"""---
- hosts: all
  remote_user: {secret_username}
  gather_facts: no
  vars:
    ansible_executable: /bin/sh
    ansible_python_interpreter: auto_silent
  tasks:
    - name: Gather facts
      ansible.builtin.setup:
      register: facts_result
      
    - name: Convert facts to JSON string
      ansible.builtin.set_fact:
        facts_json: "{{{{ facts_result.ansible_facts | to_json }}}}"
      
    - name: Output facts as JSON
      ansible.builtin.debug:
        msg: "{{{{ facts_json }}}}"
"""
        
        try:
            with open(temp_playbook, 'w', encoding='utf-8') as f:
                f.write(playbook_content)
        except Exception as e:
            app.logger.error(f"[get_host_facts] Failed to create playbook: {e}")
            if temp_inventory.exists():
                try:
                    temp_inventory.unlink()
                except Exception:
                    pass
            return jsonify({
                'success': False,
                'error': 'Failed to create playbook for facts gathering'
            }), 500
        
        # Создаем execution для выполнения через воркер
        execution_id = str(uuid.uuid4())
        generated_playbook_path = get_project_generated_playbook_path(project_id, execution_id)
        generated_playbook_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Копируем временные файлы в постоянное место
        import shutil
        shutil.copy2(temp_playbook, generated_playbook_path)
        generated_inventory_path = generated_playbook_path.parent / f'inventory_{execution_id}.yml'
        shutil.copy2(temp_inventory, generated_inventory_path)
        
        # Удаляем временные файлы
        try:
            if temp_inventory.exists():
                temp_inventory.unlink()
        except Exception:
            pass
        try:
            if temp_playbook.exists():
                temp_playbook.unlink()
        except Exception:
            pass
        
        # Создаем execution record
        execution_data = {
            'status': 'QUEUED',
            'playbookName': f'get_facts_{host_name}',
            'mode': 'HOST_FACTS',
            'runParams': {
                'temp_playbook': str(generated_playbook_path),
                'temp_inventory': str(generated_inventory_path),
                'inventory_files': inventory_files,
                'ansible_config': str(ansible_config_path) if ansible_config_path.exists() else None,
                'project_dir': str(project_dir),
                'host': host_name,
                'limit_host': host_name_actual,
                'execution_type': 'HOST_FACTS',
                'temp_key_files': temp_key_files
            },
            'description': f'Get host facts: {host_name}'
        }
        
        execution_id_created = create_execution_record(execution_data, project_id=project_id)
        
        if not execution_id_created:
            app.logger.error(f"[get_host_facts] Failed to create execution record")
            try:
                if generated_playbook_path.exists():
                    generated_playbook_path.unlink()
            except Exception:
                pass
            try:
                if generated_inventory_path.exists():
                    generated_inventory_path.unlink()
            except Exception:
                pass
            return jsonify({
                'success': False,
                'error': 'Failed to create execution record'
            }), 500
        
        app.logger.info(f"[get_host_facts] Created execution {execution_id_created} for get facts: {host_name}")
        
        # Возвращаем execution_id клиенту для опроса статуса
        return jsonify({
            'success': True,
            'executionId': execution_id_created,
            'message': 'Get facts queued for execution',
            'status': 'QUEUED'
        })
            
    except Exception as e:
        app.logger.error(f"[get_host_facts] Critical error: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'Internal error gathering facts'
        }), 500


@app.route('/api/backups/list', methods=['GET'])
@require_auth
def list_backups():
    """Получить список всех бэкапов проекта"""
    try:
        # Получаем project_id из query параметра или из сессии
        project_id = request.args.get('project_id')
        if not project_id:
            project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        backups = []
        backup_type = request.args.get('type', 'all')  # 'group_vars', 'host_vars', 'inventory', 'playbooks', 'all'
        
        # Бэкапы group_vars из BACKUPS_DIR
        if backup_type in ['all', 'group_vars']:
            group_vars_backup_dir = BACKUPS_DIR / 'group_vars' / project_id
            if group_vars_backup_dir.exists():
                for backup_file in sorted(group_vars_backup_dir.glob('*.yml'), reverse=True):
                    backups.append({
                        'name': backup_file.name,
                        'path': f'group_vars/{project_id}/{backup_file.name}',
                        'type': 'group_vars',
                        'size': backup_file.stat().st_size,
                        'modified': backup_file.stat().st_mtime
                    })
            
            # Также проверяем старую локацию для обратной совместимости
            project_group_vars_dir = get_project_group_vars_dir(project_id)
            legacy_backup_dir = project_group_vars_dir / 'backups'
            if legacy_backup_dir.exists():
                for backup_file in sorted(legacy_backup_dir.glob('*.yml'), reverse=True):
                    # Проверяем, не добавлен ли уже этот бэкап
                    if not any(b['name'] == backup_file.name for b in backups):
                        backups.append({
                            'name': backup_file.name,
                            'path': f'group_vars/backups/{backup_file.name}',
                            'type': 'group_vars',
                            'size': backup_file.stat().st_size,
                            'modified': backup_file.stat().st_mtime
                        })
        
        # Бэкапы host_vars из BACKUPS_DIR
        if backup_type in ['all', 'host_vars']:
            host_vars_backup_dir = BACKUPS_DIR / 'host_vars' / project_id
            if host_vars_backup_dir.exists():
                for backup_file in sorted(host_vars_backup_dir.glob('*.yml'), reverse=True):
                    backups.append({
                        'name': backup_file.name,
                        'path': f'host_vars/{project_id}/{backup_file.name}',
                        'type': 'host_vars',
                        'size': backup_file.stat().st_size,
                        'modified': backup_file.stat().st_mtime
                    })
            
            # Также проверяем старую локацию для обратной совместимости
            project_host_vars_dir = get_project_host_vars_dir(project_id)
            legacy_backup_dir = project_host_vars_dir / 'backups'
            if legacy_backup_dir.exists():
                for backup_file in sorted(legacy_backup_dir.glob('*.yml'), reverse=True):
                    # Проверяем, не добавлен ли уже этот бэкап
                    if not any(b['name'] == backup_file.name for b in backups):
                        backups.append({
                            'name': backup_file.name,
                            'path': f'host_vars/backups/{backup_file.name}',
                            'type': 'host_vars',
                            'size': backup_file.stat().st_size,
                            'modified': backup_file.stat().st_mtime
                        })
        
        # Бэкапы inventory из BACKUPS_DIR
        if backup_type in ['all', 'inventory']:
            inventory_backup_dir = BACKUPS_DIR / 'inventory' / project_id
            if inventory_backup_dir.exists():
                for backup_file in sorted(inventory_backup_dir.glob('*.yml'), reverse=True):
                    backups.append({
                        'name': backup_file.name,
                        'path': f'inventory/{project_id}/{backup_file.name}',
                        'type': 'inventory',
                        'size': backup_file.stat().st_size,
                        'modified': backup_file.stat().st_mtime
                    })
        
        # Бэкапы playbooks из BACKUPS_DIR
        if backup_type in ['all', 'playbooks']:
            playbooks_backup_dir = BACKUPS_DIR / 'playbooks' / project_id
            if playbooks_backup_dir.exists():
                for backup_file in sorted(playbooks_backup_dir.glob('*.json'), reverse=True):
                    backups.append({
                        'name': backup_file.name,
                        'path': f'playbooks/{project_id}/{backup_file.name}',
                        'type': 'playbooks',
                        'size': backup_file.stat().st_size,
                        'modified': backup_file.stat().st_mtime
                    })
        
        # Сортируем по дате изменения (новые сначала)
        backups.sort(key=lambda x: x['modified'], reverse=True)
        
        return jsonify({'success': True, 'backups': backups})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backups/download', methods=['GET'])
@require_auth
def download_backup():
    """Скачать бэкап файл"""
    try:
        # Получаем project_id из query параметра или из сессии
        project_id = request.args.get('project_id')
        if not project_id:
            project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        backup_path = request.args.get('path')
        if not backup_path:
            return jsonify({'success': False, 'error': 'File path not specified'}), 400
        
        # Security: only allow downloads from project-scoped backups directories
        # Prevent path traversal and arbitrary file reads under BASE_DIR.
        rel = Path(str(backup_path))
        if rel.is_absolute() or '..' in rel.parts:
            return jsonify({'success': False, 'error': 'Invalid path'}), 400
        
        file_path = None
        
        # Проверяем новый формат пути (BACKUPS_DIR)
        if backup_path.startswith('group_vars/') and f'/{project_id}/' in backup_path:
            # Формат: group_vars/{project_id}/filename.yml
            backup_name = backup_path.split('/')[-1]
            file_path = BACKUPS_DIR / 'group_vars' / project_id / backup_name
        elif backup_path.startswith('host_vars/') and f'/{project_id}/' in backup_path:
            # Формат: host_vars/{project_id}/filename.yml
            backup_name = backup_path.split('/')[-1]
            file_path = BACKUPS_DIR / 'host_vars' / project_id / backup_name
        elif backup_path.startswith('inventory/') and f'/{project_id}/' in backup_path:
            # Формат: inventory/{project_id}/filename.yml
            backup_name = backup_path.split('/')[-1]
            file_path = BACKUPS_DIR / 'inventory' / project_id / backup_name
        elif backup_path.startswith('playbooks/') and f'/{project_id}/' in backup_path:
            # Формат: playbooks/{project_id}/filename.json
            backup_name = backup_path.split('/')[-1]
            file_path = BACKUPS_DIR / 'playbooks' / project_id / backup_name
        else:
            # Старый формат для обратной совместимости (project_dir/group_vars/backups/)
            project_dir = get_project_dir(project_id)
            file_path = (project_dir / rel).resolve()
            
            allowed_dirs = [
                (get_project_group_vars_dir(project_id) / 'backups').resolve(),
                (get_project_host_vars_dir(project_id) / 'backups').resolve(),
            ]
            if not any(str(file_path).startswith(str(d) + os.sep) or file_path == d for d in allowed_dirs):
                return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        # Проверяем, что файл находится в разрешенной директории
        if file_path:
            file_path = file_path.resolve()
            # Проверяем, что путь находится внутри BACKUPS_DIR или project_dir
            if not (str(file_path).startswith(str(BACKUPS_DIR.resolve()) + os.sep) or 
                    str(file_path).startswith(str(get_project_dir(project_id).resolve()) + os.sep)):
                return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        if not file_path or not file_path.exists():
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        # Определяем MIME тип по расширению
        mimetype = 'text/yaml'
        if file_path.suffix == '.json':
            mimetype = 'application/json'
        
        return send_file(
            str(file_path),
            mimetype=mimetype,
            as_attachment=True,
            download_name=file_path.name
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backups/restore', methods=['POST'])
@require_auth
def restore_backup():
    """Восстановить файл из бэкапа"""
    try:
        # Получаем project_id из query параметра или из body или из сессии
        project_id = request.args.get('project_id')
        if not project_id:
            data = request.json or {}
            project_id = data.get('project_id')
        if not project_id:
            project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json
        backup_path = data.get('path')
        if not backup_path:
            return jsonify({'success': False, 'error': 'Backup path not specified'}), 400
        
        # Security: only allow restores from project-scoped backups directories
        rel = Path(str(backup_path))
        if rel.is_absolute() or '..' in rel.parts:
            return jsonify({'success': False, 'error': 'Invalid path'}), 400
        
        backup_file_path = None
        target_file = None
        
        # Проверяем новый формат пути (BACKUPS_DIR)
        if backup_path.startswith('group_vars/') and f'/{project_id}/' in backup_path:
            # Формат: group_vars/{project_id}/filename.yml
            backup_name = backup_path.split('/')[-1]
            backup_file_path = BACKUPS_DIR / 'group_vars' / project_id / backup_name
            target_file = get_project_group_vars_dir(project_id) / 'all.yml'
        elif backup_path.startswith('host_vars/') and f'/{project_id}/' in backup_path:
            # Формат: host_vars/{project_id}/filename.yml
            backup_name = backup_path.split('/')[-1]
            backup_file_path = BACKUPS_DIR / 'host_vars' / project_id / backup_name
            # Извлекаем имя хоста из имени файла бэкапа
            backup_name_stem = Path(backup_name).stem
            # Убираем timestamp из имени (формат: hostname_YYYYMMDD_HHMMSS)
            host_match = re.match(r'^(.+?)_\d{8}_\d{6}$', backup_name_stem)
            if host_match:
                hostname = host_match.group(1)
            else:
                hostname = backup_name_stem
            target_file = get_project_host_vars_dir(project_id) / f'{hostname}.yml'
        elif backup_path.startswith('inventory/') and f'/{project_id}/' in backup_path:
            # Формат: inventory/{project_id}/filename.yml
            backup_name = backup_path.split('/')[-1]
            backup_file_path = BACKUPS_DIR / 'inventory' / project_id / backup_name
            # Определяем имя inventory файла (обычно inventory.yml)
            backup_name_stem = Path(backup_name).stem
            inventory_match = re.match(r'^(.+?)_\d{8}_\d{6}$', backup_name_stem)
            if inventory_match:
                inventory_name = inventory_match.group(1) + '.yml'
            else:
                inventory_name = 'inventory.yml'
            target_file = get_project_inventory_file(project_id).parent / inventory_name
        elif backup_path.startswith('playbooks/') and f'/{project_id}/' in backup_path:
            # Формат: playbooks/{project_id}/filename.json
            backup_name = backup_path.split('/')[-1]
            backup_file_path = BACKUPS_DIR / 'playbooks' / project_id / backup_name
            # Определяем имя playbook файла
            backup_name_stem = Path(backup_name).stem
            playbook_match = re.match(r'^(.+?)_\d{8}_\d{6}$', backup_name_stem)
            if playbook_match:
                playbook_id = playbook_match.group(1)
            else:
                # Пытаемся извлечь playbook_id из имени файла
                playbook_id = backup_name_stem
            target_file = playbook_storage.get_playbook_file(project_id, playbook_id)
        else:
            # Старый формат для обратной совместимости (project_dir/group_vars/backups/)
            project_dir = get_project_dir(project_id)
            backup_file_path = (project_dir / rel).resolve()
            
            allowed_dirs = [
                (get_project_group_vars_dir(project_id) / 'backups').resolve(),
                (get_project_host_vars_dir(project_id) / 'backups').resolve(),
            ]
            if not any(str(backup_file_path).startswith(str(d) + os.sep) or backup_file_path == d for d in allowed_dirs):
                return jsonify({'success': False, 'error': 'Access denied'}), 403
            
            # Определяем целевой файл на основе типа бэкапа (старый формат)
            if 'group_vars/backups' in str(backup_file_path):
                target_file = get_project_group_vars_dir(project_id) / 'all.yml'
            elif 'host_vars/backups' in str(backup_file_path):
                # Извлекаем имя хоста из имени файла бэкапа
                backup_name = backup_file_path.stem
                # Убираем timestamp из имени (формат: hostname_YYYYMMDD_HHMMSS)
                host_match = re.match(r'^(.+?)_\d{8}_\d{6}$', backup_name)
                if host_match:
                    hostname = host_match.group(1)
                else:
                    hostname = backup_name
                target_file = get_project_host_vars_dir(project_id) / f'{hostname}.yml'
            else:
                return jsonify({'success': False, 'error': 'Unknown backup type'}), 400
        
        # Проверяем, что файл находится в разрешенной директории
        if backup_file_path:
            backup_file_path = backup_file_path.resolve()
            # Проверяем, что путь находится внутри BACKUPS_DIR или project_dir
            if not (str(backup_file_path).startswith(str(BACKUPS_DIR.resolve()) + os.sep) or 
                    str(backup_file_path).startswith(str(get_project_dir(project_id).resolve()) + os.sep)):
                return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        if not backup_file_path or not backup_file_path.exists():
            return jsonify({'success': False, 'error': 'Backup not found'}), 404
        
        if not target_file:
            return jsonify({'success': False, 'error': 'Failed to determine target file for restoration'}), 400
        
        # Создаем бэкап текущего файла перед восстановлением
        if target_file.exists():
            create_backup(target_file, force=True)
        
        # Восстанавливаем файл
        import shutil
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_file_path, target_file)
        
        app.logger.info(f"Restored backup {backup_path} to {target_file} for project {project_id}")
        return jsonify({'success': True, 'message': 'Backup restored successfully'})
    except Exception as e:
        app.logger.error(f"Error restoring backup: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backup-settings', methods=['GET'])
@require_auth
def get_backup_settings():
    """Получить настройки бэкапов"""
    try:
        settings = load_backup_settings()
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        app.logger.error(f"Error loading backup settings: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backup-settings', methods=['PUT'])
@require_auth
def update_backup_settings():
    """Обновить настройки бэкапов"""
    try:
        data = request.json
        settings = load_backup_settings()
        
        # Обновляем max_depth если указан
        if 'max_depth' in data:
            max_depth = int(data['max_depth'])
            if max_depth < 1:
                return jsonify({'success': False, 'error': 'Backup depth must be greater than 0'}), 400
            settings['max_depth'] = max_depth
        
        # Обновляем настройки проектов
        if 'projects' in data:
            if not isinstance(data['projects'], dict):
                return jsonify({'success': False, 'error': 'Invalid projects settings format'}), 400
            
            if 'projects' not in settings:
                settings['projects'] = {}
            
            for project_id, project_settings in data['projects'].items():
                if not isinstance(project_settings, dict):
                    continue
                settings['projects'][project_id] = {
                    'enabled': bool(project_settings.get('enabled', False))
                }
        
        if save_backup_settings(settings):
            return jsonify({'success': True, 'settings': settings})
        else:
            return jsonify({'success': False, 'error': 'Error saving settings'}), 500
    except Exception as e:
        app.logger.error(f"Error updating backup settings: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backups/create', methods=['POST'])
@require_auth
def create_project_backup():
    """Создать полный архив проекта (tar.gz) в data/backups/archives/<project_id>/"""
    try:
        project_id = get_project_id_from_request()
        if not project_id:
            project_id = request.json.get('project_id') if request.json else None
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        reason = request.json.get('reason', 'auto') if request.json else 'auto'
        if reason != 'manual' and not get_project_backup_enabled(project_id):
            return jsonify({'success': False, 'error': 'Automatic backup is not enabled for this project'}), 400
        
        project_dir = get_project_dir(project_id)
        if not project_dir.exists():
            return jsonify({'success': False, 'error': 'Project directory not found'}), 404
        
        archives_project_dir = BACKUP_ARCHIVES_DIR / project_id
        archives_project_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_name = f'{timestamp}.tar.gz'
        archive_path = archives_project_dir / archive_name
        
        file_count = 0
        with tarfile.open(archive_path, 'w:gz') as tf:
            for item in project_dir.rglob('*'):
                if item.is_file():
                    arcname = item.relative_to(project_dir)
                    tf.add(item, arcname=str(arcname))
                    file_count += 1
        
        settings = load_backup_settings()
        max_depth = settings.get('max_depth', 100)
        if max_depth < 1:
            max_depth = 1
        cleanup_old_archives(archives_project_dir, max_depth)
        
        app.logger.info(f"Created full archive for project {project_id}: {archive_name} ({file_count} files)")
        return jsonify({
            'success': True,
            'message': 'Backup created successfully',
            'archive': archive_name,
            'path': f'archives/{project_id}/{archive_name}',
            'files_count': file_count
        })
    except Exception as e:
        app.logger.error(f"Error creating project backup: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backups/archives/list', methods=['GET'])
@require_auth
def list_project_archives():
    """Список полных архивов проекта (tar.gz) для выбора при Restore"""
    try:
        project_id = request.args.get('project_id')
        if not project_id:
            return jsonify({'success': False, 'error': 'project_id is required'}), 400
        project_dir = get_project_dir(project_id)
        if not project_dir.exists():
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        archives_dir = BACKUP_ARCHIVES_DIR / project_id
        archives = []
        if archives_dir.exists():
            for f in sorted(archives_dir.glob('*.tar.gz'), key=lambda x: x.stat().st_mtime, reverse=True):
                st = f.stat()
                archives.append({
                    'name': f.name,
                    'path': f'archives/{project_id}/{f.name}',
                    'size': st.st_size,
                    'modified': st.st_mtime,
                })
        return jsonify({'success': True, 'archives': archives})
    except Exception as e:
        app.logger.error(f"Error listing project archives: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backups/archives/restore', methods=['POST'])
@require_auth
def restore_project_archive():
    """Восстановить проект из выбранного полного архива (tar.gz)"""
    try:
        data = request.json or {}
        project_id = data.get('project_id') or request.args.get('project_id')
        if not project_id:
            return jsonify({'success': False, 'error': 'project_id is required'}), 400
        path = data.get('path') or data.get('archive')
        if not path:
            return jsonify({'success': False, 'error': 'path (archive filename) is required'}), 400
        # path: "archives/<project_id>/20260210_143022.tar.gz" или просто "20260210_143022.tar.gz"
        archive_name = path.split('/')[-1] if '/' in path else path
        if not archive_name.endswith('.tar.gz'):
            return jsonify({'success': False, 'error': 'Invalid archive path'}), 400
        archive_path = (BACKUP_ARCHIVES_DIR / project_id / archive_name).resolve()
        if not archive_path.is_file():
            return jsonify({'success': False, 'error': 'Archive not found'}), 404
        if not str(archive_path).startswith(str(BACKUP_ARCHIVES_DIR.resolve())):
            return jsonify({'success': False, 'error': 'Invalid archive path'}), 400
        project_dir = get_project_dir(project_id).resolve()
        project_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, 'r:gz') as tf:
            tf.extractall(path=project_dir)
        app.logger.info(f"Restored project {project_id} from archive {archive_name}")
        return jsonify({'success': True, 'message': 'Project restored successfully'})
    except Exception as e:
        app.logger.error(f"Error restoring project archive: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/backups/archives/download', methods=['GET'])
@require_auth
def download_project_archive():
    """Скачать полный архив проекта (tar.gz)"""
    try:
        project_id = request.args.get('project_id')
        path = request.args.get('path')
        if not project_id or not path:
            return jsonify({'success': False, 'error': 'project_id and path are required'}), 400
        archive_name = path.split('/')[-1] if '/' in path else path
        if not archive_name.endswith('.tar.gz'):
            return jsonify({'success': False, 'error': 'Invalid archive path'}), 400
        archive_path = (BACKUP_ARCHIVES_DIR / project_id / archive_name).resolve()
        if not archive_path.is_file():
            return jsonify({'success': False, 'error': 'Archive not found'}), 404
        if not str(archive_path).startswith(str(BACKUP_ARCHIVES_DIR.resolve())):
            return jsonify({'success': False, 'error': 'Invalid path'}), 400
        return send_file(archive_path, as_attachment=True, download_name=archive_name)
    except Exception as e:
        app.logger.error(f"Error downloading archive: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _parse_log_timestamp(ts_str):
    """Парсит timestamp из лога в datetime для сравнения. Формат: 2026-01-15 00:06:22 или 2026-01-15 00:06:22,123"""
    if not ts_str:
        return None
    from datetime import datetime
    ts_str = ts_str.strip().replace(',', '.')
    if not ts_str:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'):
        try:
            s = ts_str[:26] if '.%f' in fmt else (ts_str[:19] if len(ts_str) >= 19 else ts_str[:16])
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    try:
        return datetime.strptime(ts_str[:19], '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return None


@app.route('/api/server_logs', methods=['GET'])
@require_auth
def get_server_logs():
    """Получить логи серверов (worker, backend, frontend). Поддерживает date_from, date_to (YYYY-MM-DDTHH:mm или с секундами)."""
    try:
        import re
        from pathlib import Path
        from datetime import datetime, timedelta
        
        lines = request.args.get('lines', 1000, type=int)
        service = request.args.get('service', 'all')  # all, worker, backend, frontend
        level = request.args.get('level', 'all')  # all, error, warning, info, debug
        search = request.args.get('search', '').strip()
        date_from_str = request.args.get('date_from', '').strip()
        date_to_str = request.args.get('date_to', '').strip()
        
        date_from_dt = _parse_log_timestamp(date_from_str.replace('T', ' ').replace('Z', '')) if date_from_str else None
        date_to_dt = _parse_log_timestamp(date_to_str.replace('T', ' ').replace('Z', '')) if date_to_str else None
        if date_to_dt and len(date_to_str) <= 16:
            date_to_dt = date_to_dt + timedelta(seconds=59, milliseconds=999)
        if date_from_dt or date_to_dt:
            lines = min(max(lines * 10, 5000), 50000)
        
        # Определяем пути к лог-файлам (используем DATA_DIR для единообразия)
        log_files = {
            'worker': LOG_DIR / 'worker.log',
            'backend': LOG_DIR / 'backend.log',
            'frontend': LOG_DIR / 'frontend.log'
        }
        
        # Если service='all', читаем все файлы, иначе только указанный
        # Поддерживаем множественные сервисы через запятую: service=backend,worker
        if service == 'all':
            services_to_read = ['worker', 'backend', 'frontend']
        else:
            # Разделяем по запятой, если несколько сервисов
            services_to_read = [s.strip() for s in service.split(',') if s.strip()]
        
        all_log_entries = []
        
        for svc in services_to_read:
            log_file = log_files.get(svc)
            if not log_file:
                app.logger.debug(f'Log file not configured for service: {svc}')
                continue
            if not log_file.exists():
                app.logger.debug(f'Log file does not exist: {log_file}')
                continue
            app.logger.debug(f'Reading logs from: {log_file}')
            
            try:
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    file_lines = f.readlines()
                
                # Берем последние N строк
                recent_lines = file_lines[-lines:] if len(file_lines) > lines else file_lines
                
                # Парсим каждую строку лога
                for line_num, line in enumerate(recent_lines, start=len(file_lines) - len(recent_lines) + 1):
                    line = line.rstrip('\n\r')
                    if not line:
                        continue
                    
                    # Парсим формат: [TIMESTAMP] LEVEL: MESSAGE или [TIMESTAMP] LEVEL in module: MESSAGE
                    # Также поддерживаем формат: [TIMESTAMP] LEVEL MESSAGE
                    log_entry = {
                        'service': svc,
                        'raw': line,
                        'timestamp': None,
                        'level': 'INFO',
                        'message': line,
                        'line_number': line_num
                    }
                    
                    # Парсим timestamp и level
                    # Формат 1: [2026-01-15 00:06:22] ERROR: message
                    # Формат 2: [2026-01-15 00:06:22,123] WARNING in app: message
                    # Формат 3: [2026-01-15 00:06:22] INFO message
                    timestamp_match = re.match(r'\[(\d{4}-\d{2}-\d{2}[\s,]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]', line)
                    if timestamp_match:
                        log_entry['timestamp'] = timestamp_match.group(1)
                        remaining = line[timestamp_match.end():].strip()
                        
                        # Ищем уровень логирования
                        level_match = re.match(r'(ERROR|WARNING|WARN|INFO|DEBUG|CRITICAL|FATAL)\s*(?:in\s+\w+)?:?\s*(.*)', remaining, re.IGNORECASE)
                        if level_match:
                            log_entry['level'] = level_match.group(1).upper()
                            if log_entry['level'] == 'WARN':
                                log_entry['level'] = 'WARNING'
                            log_entry['message'] = level_match.group(2) if level_match.group(2) else remaining
                        else:
                            log_entry['message'] = remaining
                    
                    # Фильтрация по времени на бэкенде
                    if date_from_dt is not None or date_to_dt is not None:
                        log_dt = _parse_log_timestamp(log_entry['timestamp']) if log_entry['timestamp'] else None
                        if log_dt is None:
                            continue
                        if date_from_dt is not None and log_dt < date_from_dt:
                            continue
                        if date_to_dt is not None and log_dt > date_to_dt:
                            continue
                    
                    # Фильтрация по уровню (проверяем точное совпадение или вхождение)
                    if level != 'all':
                        level_upper = level.upper()
                        log_level_upper = log_entry['level'].upper()
                        # Проверяем точное совпадение или вхождение (например, "error" в "ERROR")
                        if level_upper not in log_level_upper and log_level_upper not in level_upper:
                            continue
                    
                    # Фильтрация по поисковому запросу
                    if search and search.lower() not in line.lower():
                        continue
                    
                    all_log_entries.append(log_entry)
            
            except Exception as e:
                app.logger.error(f'Error reading {svc} logs: {e}', exc_info=True)
                continue
        
        # Сортируем по timestamp (если есть) или по порядку
        # Новые логи должны быть внизу, поэтому reverse=False (старые сверху, новые внизу)
        all_log_entries.sort(key=lambda x: x['timestamp'] if x['timestamp'] else '', reverse=False)
        
        # Подсчитываем статистику по уровням
        stats = {
            'error': sum(1 for e in all_log_entries if 'ERROR' in e['level']),
            'warning': sum(1 for e in all_log_entries if 'WARNING' in e['level']),
            'info': sum(1 for e in all_log_entries if e['level'] == 'INFO'),
            'debug': sum(1 for e in all_log_entries if e['level'] == 'DEBUG')
        }
        
        # Security: redact sensitive information
        try:
            SENSITIVE_KEYS = [
                'password', 'passphrase', 'token', 'secret', 'privatekey', 'private_key',
                'ansible_ssh_pass', 'global_secrets_encryption_key'
            ]
            for entry in all_log_entries:
                redacted = entry['message']
                for k in SENSITIVE_KEYS:
                    redacted = re.sub(
                        rf'({k}\s*[:=]\s*)([^\s\'"]+)',
                        r'\1***REDACTED***',
                        redacted,
                        flags=re.IGNORECASE
                    )
                # Redact PEM-like blocks
                redacted = re.sub(
                    r'-----BEGIN [^-]+-----[\s\S]*?-----END [^-]+-----',
                    '-----BEGIN ***REDACTED***-----\n***REDACTED***\n-----END ***REDACTED***-----',
                    redacted
                )
                entry['message'] = redacted
                entry['raw'] = redacted  # Также обновляем raw для безопасности
        except Exception:
            pass
        
        app.logger.debug(f'Returning {len(all_log_entries)} log entries, stats: {stats}')
        
        return jsonify({
            'success': True,
            'logs': all_log_entries,
            'stats': stats,
            'total': len(all_log_entries)
        })
    except Exception as e:
        app.logger.error(f'Error reading server logs: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/frontend_logs', methods=['POST'])
@require_auth
def api_frontend_logs():
    """API: Принять логи от frontend и записать в frontend.log"""
    try:
        data = request.json or {}
        logs = data.get('logs', [])
        
        app.logger.debug(f"Received request to /api/frontend_logs with {len(logs) if isinstance(logs, list) else 0} logs")
        
        if not isinstance(logs, list):
            app.logger.warning(f"Invalid request to /api/frontend_logs: logs is not a list, got {type(logs)}")
            return jsonify({'success': False, 'error': 'logs must be an array'}), 400
        
        if not logs:
            app.logger.debug("No logs to process in /api/frontend_logs")
            return jsonify({'success': True, 'message': 'No logs to process'})
        
        # Получаем текущий уровень логирования
        log_level_str, _ = get_logging_settings()
        log_level = LOG_LEVEL_MAP.get(log_level_str.upper(), logging.INFO)
        app.logger.debug(f"Current log level: {log_level_str} (value: {log_level})")
        
        # Валидируем и записываем логи
        valid_levels = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
        logged_count = 0
        
        for log_entry in logs:
            if not isinstance(log_entry, dict):
                continue
            
            level = log_entry.get('level', 'INFO').upper()
            message = log_entry.get('message', '')
            timestamp = log_entry.get('timestamp', '')
            stack = log_entry.get('stack')
            
            # Валидация уровня
            if level not in valid_levels:
                app.logger.warning(f"Invalid log level from frontend: {level}")
                continue
            
            # Проверяем уровень логирования (фильтруем на backend)
            level_value = LOG_LEVEL_MAP.get(level, logging.INFO)
            if level_value < log_level:
                app.logger.debug(f"Filtered out log with level {level} (value: {level_value}) < current level {log_level_str} (value: {log_level})")
                continue  # Пропускаем логи ниже текущего уровня
            
            # Формируем сообщение для записи
            # Формат: [TIMESTAMP] LEVEL: message
            # Если timestamp не предоставлен, используем текущее время
            if timestamp:
                log_message = f"[{timestamp}] {level}: {message}"
            else:
                from datetime import datetime
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                log_message = f"[{timestamp}] {level}: {message}"
            
            # Добавляем stack trace если есть
            if stack:
                log_message += f"\n{stack}"
            
            # Записываем лог
            try:
                if level == 'DEBUG':
                    frontend_logger.debug(log_message)
                elif level == 'INFO':
                    frontend_logger.info(log_message)
                elif level == 'WARNING':
                    frontend_logger.warning(log_message)
                elif level == 'ERROR':
                    frontend_logger.error(log_message)
                elif level == 'CRITICAL':
                    frontend_logger.critical(log_message)
                
                logged_count += 1
                app.logger.debug(f"Wrote frontend log: {level} - {message[:50]}...")
            except Exception as log_error:
                app.logger.error(f"Error writing frontend log to file {FRONTEND_LOG_FILE}: {log_error}", exc_info=True)
        
        # Логируем информацию о полученных логах
        if logged_count > 0:
            app.logger.info(f"Received {len(logs)} logs from frontend, logged {logged_count} to {FRONTEND_LOG_FILE}")
        else:
            app.logger.debug(f"Received {len(logs)} logs from frontend, but none were logged (filtered by log level: {log_level_str})")
        
        return jsonify({
            'success': True,
            'logged': logged_count,
            'total': len(logs)
        })
        
    except Exception as e:
        app.logger.error(f'Error processing frontend logs: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== Execution History Functions ====================

def load_execution_settings():
    """Загружает настройки execution history"""
    default_settings = {
        'save_history': True,
        'retention_mode': 'count',  # 'count' or 'size'
        'retention_count': 200,
        'retention_size_mb': 200,
        'debug_mode': False,  # Debug режим Flask
        'log_level': 'INFO',  # Уровень логирования: DEBUG, INFO, WARNING, ERROR, CRITICAL
        'max_upload_size_mb': 10,  # Максимальный размер загружаемых файлов в MB
        'max_log_size_mb': 10,  # Максимальный размер лог-файлов в MB перед ротацией
        # TTL кэша статуса хостов (в секундах)
        'host_status_ttl_seconds': HOST_STATUS_TTL_DEFAULT,
    }
    try:
        if EXECUTION_SETTINGS_FILE.exists():
            with open(EXECUTION_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                return {**default_settings, **settings}
    except Exception as e:
        app.logger.error(f"Error loading execution history settings: {e}")
    return default_settings


def load_backup_settings():
    """Загружает настройки бэкапов для всех проектов"""
    default_settings = {
        'max_depth': 100,  # Максимальная глубина бэкапов для всех проектов
        'projects': {}  # Настройки для каждого проекта: {project_id: {'enabled': bool}}
    }
    try:
        if BACKUP_SETTINGS_FILE.exists():
            with open(BACKUP_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                # Объединяем с дефолтными настройками
                result = {**default_settings, **settings}
                # Убеждаемся, что projects есть
                if 'projects' not in result:
                    result['projects'] = {}
                return result
    except Exception as e:
        app.logger.error(f"Error loading backup settings: {e}")
    return default_settings


def save_backup_settings(settings):
    """Сохраняет настройки бэкапов"""
    try:
        with open(BACKUP_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        app.logger.error(f"Error saving backup settings: {e}")
        return False


def get_project_backup_enabled(project_id):
    """Проверяет, включен ли автоматический бэкап для проекта"""
    settings = load_backup_settings()
    project_settings = settings.get('projects', {}).get(project_id, {})
    return project_settings.get('enabled', False)


def should_create_backup(file_path, project_id=None):
    """Проверяет, нужно ли создавать бэкап для файла"""
    # Если project_id не передан, пытаемся извлечь из пути
    if not project_id:
        file_path_str = str(file_path)
        projects_match = re.search(r'/projects/([^/]+)/', file_path_str)
        if projects_match:
            project_id = projects_match.group(1)
    
    if not project_id:
        return False
    
    return get_project_backup_enabled(project_id)


def cleanup_old_backups(backup_dir, max_depth):
    """Удаляет старые бэкапы, оставляя только max_depth последних"""
    try:
        if not backup_dir.exists():
            return
        
        # Получаем все файлы бэкапов, отсортированные по времени изменения
        backup_files = sorted(backup_dir.glob('*.yml'), key=lambda x: x.stat().st_mtime, reverse=True)
        
        # Если файлов больше чем max_depth, удаляем старые
        if len(backup_files) > max_depth:
            files_to_delete = backup_files[max_depth:]
            for file_to_delete in files_to_delete:
                try:
                    file_to_delete.unlink()
                    app.logger.debug(f"Deleted old backup: {file_to_delete}")
                except Exception as e:
                    app.logger.error(f"Error deleting old backup {file_to_delete}: {e}")
    except Exception as e:
        app.logger.error(f"Error cleaning old backups in {backup_dir}: {e}")


def cleanup_old_archives(archive_dir, max_depth):
    """Удаляет старые архивы tar.gz, оставляя только max_depth последних"""
    try:
        if not archive_dir.exists():
            return
        archive_files = sorted(archive_dir.glob('*.tar.gz'), key=lambda x: x.stat().st_mtime, reverse=True)
        if len(archive_files) > max_depth:
            for f in archive_files[max_depth:]:
                try:
                    f.unlink()
                    app.logger.debug(f"Deleted old archive: {f}")
                except Exception as e:
                    app.logger.error(f"Error deleting old archive {f}: {e}")
    except Exception as e:
        app.logger.error(f"Error cleaning old archives in {archive_dir}: {e}")


def validate_log_level(level_name: str) -> bool:
    """
    Валидирует уровень логирования
    
    Args:
        level_name: строка с именем уровня
        
    Returns:
        True если уровень валидный, False иначе
    """
    valid_levels = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
    return level_name.upper() in valid_levels


def set_log_level(level_name):
    """
    Динамически изменяет уровень логирования без перезагрузки приложения.
    
    Args:
        level_name: строка с именем уровня ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
    """
    # Валидация уровня
    if not validate_log_level(level_name):
        app.logger.warning(f"Invalid log level: {level_name}, using INFO. Valid levels: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        level = logging.INFO
    else:
        level_map = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL
        }
        level = level_map.get(level_name.upper(), logging.INFO)
    
    # Устанавливаем уровень для всех handlers
    app.logger.setLevel(level)
    file_handler.setLevel(level)
    console_handler.setLevel(level)
    
    # Также обновляем уровень для frontend logger
    frontend_logger.setLevel(level)
    frontend_file_handler.setLevel(level)
    
    app.logger.info(f"Log level changed to: {level_name.upper()}")
    return True


def save_execution_settings(settings):
    """Сохраняет настройки execution history"""
    try:
        with open(EXECUTION_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        app.logger.error(f"Error saving execution history settings: {e}")
        return False


def create_execution_record(data, project_id=None, execution_id=None):
    """Создает новую запись execution в Project Storage
    
    REQUIRED: project_id must be provided (no fallback to legacy EXECUTIONS_DIR)
    
    Если data содержит 'status', используется он, иначе 'QUEUED' (для worker API mode).
    Поддерживает статусы: QUEUED, RUNNING, SUCCESS, FAILED, CANCELED, CANCELING
    
    Автоматически устанавливает:
    - createdAt, statusUpdatedAt
    - queuedAt для QUEUED
    - startedAt для RUNNING
    - workerId если передан
    
    Args:
        data: Данные execution
        project_id: ID проекта
        execution_id: Опциональный ID execution (если не указан, генерируется новый UUID)
    """
    if execution_id is None:
        execution_id = str(uuid.uuid4())
    now = time.time()
    
    # Получаем projectId из данных или параметра - REQUIRED
    if not project_id:
        project_id = data.get('project_id')
    if not project_id:
        app.logger.error("[create_execution_record] project_id is required")
        raise ValueError("project_id is required for create_execution_record")
    
    # Определяем статус: если передан явно, используем его, иначе 'QUEUED' (для worker API mode)
    status = data.get('status', 'QUEUED')
    status = status.upper()  # Нормализуем к верхнему регистру
    
    # Валидируем начальный статус (должен быть QUEUED или RUNNING при создании)
    if status not in ('QUEUED', 'RUNNING'):
        app.logger.warning(f"[create_execution_record] Invalid initial status '{status}', defaulting to QUEUED")
        status = 'QUEUED'
    
    execution = {
        'id': execution_id,
        'projectId': project_id,
        'createdAt': now,
        'status': status,
        'statusUpdatedAt': now,  # Всегда устанавливаем при создании
        'playbookName': data.get('playbookName', 'dynamic_playbook'),
        'mode': data.get('mode', 'PER_GROUP'),
        'inventorySnapshot': data.get('inventorySnapshot', {}),
        'selectionSnapshot': data.get('selectionSnapshot', {}),
        'stats': data.get('stats', {}),
        'warnings': data.get('warnings', [])
    }
    
    # Устанавливаем queuedAt для QUEUED статуса
    if status == 'QUEUED':
        execution['queuedAt'] = now
    
    # Устанавливаем startedAt и workerId для RUNNING статуса
    if status == 'RUNNING':
        execution['startedAt'] = now
        if 'workerId' in data:
            execution['workerId'] = data.get('workerId')
    
    # Добавляем workerId если передан (для QUEUED тоже может быть предустановлен)
    if 'workerId' in data and status == 'QUEUED':
        execution['workerId'] = data.get('workerId')
    
    # Добавляем метаданные run если они есть
    if 'runName' in data:
        execution['runName'] = data.get('runName')
    if 'tag' in data:
        execution['tag'] = data.get('tag')
    if 'description' in data:
        execution['description'] = data.get('description')
    
    # Добавляем параметры запуска для воркера (если есть)
    if 'runParams' in data:
        execution['runParams'] = data.get('runParams')
    
    # Добавляем playbookId если есть
    if 'playbookId' in data:
        execution['playbookId'] = data.get('playbookId')
    
    # ALWAYS use Project Storage - no fallback
    executions_dir = get_project_executions_dir(project_id)
    executions_dir.mkdir(parents=True, exist_ok=True)
    
    execution_file = executions_dir / f'{execution_id}.json'
    try:
        with open(execution_file, 'w', encoding='utf-8') as f:
            json.dump(execution, f, indent=2, ensure_ascii=False)
        return execution_id
    except Exception as e:
        app.logger.error(f"Error creating execution record: {e}")
        return None


def update_execution_record(execution_id, updates, project_id=None):
    """Обновляет запись execution в Project Storage с валидацией переходов статусов
    
    Wrapper для executions_store.update_execution_record() для обратной совместимости.
    Использует единую логику валидации переходов из executions_store.
    
    REQUIRED: project_id must be provided (no fallback to legacy EXECUTIONS_DIR)
    """
    # Импортируем из executions_store для единой логики
    try:
        from .executions_store import update_execution_record as _update_execution_record
    except ImportError:
        from executions_store import update_execution_record as _update_execution_record
    return _update_execution_record(execution_id, updates, project_id)


def append_execution_log(execution_id, text, project_id=None):
    """Добавляет лог в execution в Project Storage
    
    REQUIRED: project_id must be provided (no fallback to legacy EXECUTION_LOGS_DIR)
    """
    if not project_id:
        app.logger.error(f"[append_execution_log] project_id is required for execution {execution_id}")
        raise ValueError(f"project_id is required for append_execution_log (execution_id: {execution_id})")
    
    # ALWAYS use Project Storage - no fallback
    # Новая структура: history/logs/
    logs_dir = get_project_logs_dir(project_id)
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = logs_dir / f'{execution_id}.log'
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(text)
            if not text.endswith('\n'):
                f.write('\n')
        return True
    except Exception as e:
        app.logger.error(f"Error writing log for execution {execution_id}: {e}")
    return False


def get_execution_log(execution_id, project_id=None):
    """Получает лог execution из Project Storage
    
    REQUIRED: project_id must be provided (no fallback to legacy EXECUTION_LOGS_DIR)
    """
    if not project_id:
        app.logger.error(f"[get_execution_log] project_id is required for execution {execution_id}")
        raise ValueError(f"project_id is required for get_execution_log (execution_id: {execution_id})")
    
    # ALWAYS use Project Storage - no fallback
    # Новая структура: history/logs/
    logs_dir = get_project_logs_dir(project_id)
    
    log_file = logs_dir / f'{execution_id}.log'
    try:
        if log_file.exists():
            with open(log_file, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        app.logger.error(f"Error reading log for execution {execution_id}: {e}")
    return ''


def list_executions(limit=None, offset=0, search_query=None, playbook_id=None, project_id=None):
    """Список executions с сортировкой по дате (новые первые) из Project Storage
    
    REQUIRED: project_id must be provided (no fallback to legacy EXECUTIONS_DIR)
    """
    if not project_id:
        app.logger.error("[list_executions] project_id is required")
        raise ValueError("project_id is required for list_executions")
    
    executions = []
    try:
        # ALWAYS use Project Storage - no fallback
        executions_dir = get_project_executions_dir(project_id)
        
        if not executions_dir.exists():
            return []
        
        for execution_file in sorted(executions_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(execution_file, 'r', encoding='utf-8') as f:
                    execution = json.load(f)
                    
                    # Фильтрация по playbook_id
                    if playbook_id:
                        selection_snapshot = execution.get('selectionSnapshot', {})
                        execution_playbook_id = selection_snapshot.get('playbookId')
                        if execution_playbook_id != playbook_id:
                            continue
                    
                    # Фильтрация по поисковому запросу
                    if search_query:
                        query_lower = search_query.lower()
                        if (query_lower not in execution.get('id', '').lower() and
                            query_lower not in str(execution.get('stats', {}).get('hostsTargeted', '')).lower()):
                            # Проверяем хосты в inventorySnapshot
                            found = False
                            inventory = execution.get('inventorySnapshot', {})
                            for group in inventory.get('groups', []):
                                if query_lower in group.get('groupName', '').lower():
                                    found = True
                                    break
                                for host in group.get('hosts', []):
                                    if query_lower in host.get('hostId', '').lower() or query_lower in host.get('ip', '').lower():
                                        found = True
                                        break
                                if found:
                                    break
                            if not found:
                                continue
                    
                    executions.append(execution)
            except Exception as e:
                app.logger.warning(f"Error reading execution file {execution_file}: {e}")
        
        # Применяем limit и offset
        if limit:
            executions = executions[offset:offset + limit]
        elif offset:
            executions = executions[offset:]
        
        return executions
    except Exception as e:
        app.logger.error(f"Error getting list of executions: {e}")
    return []


def get_execution(execution_id, project_id=None):
    """Получает execution по ID"""
    # Если project_id не указан, ищем во всех проектах
    if project_id:
        executions_dir = get_project_executions_dir(project_id)
        execution_file = executions_dir / f'{execution_id}.json'
        if execution_file.exists():
            try:
                with open(execution_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                app.logger.error(f"Error getting execution {execution_id}: {e}")
    else:
        # Ищем во всех проектах (новая структура: history/executions/)
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            # Новая структура: history/executions/
            executions_dir = proj_dir / 'history' / 'executions'
            if not executions_dir.exists():
                # Старая структура: executions/
                executions_dir = proj_dir / 'executions'
            if not executions_dir.exists():
                continue
            execution_file = executions_dir / f'{execution_id}.json'
            if execution_file.exists():
                try:
                    with open(execution_file, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except Exception as e:
                    app.logger.error(f"Error getting execution {execution_id}: {e}")
    return None


def _cleanup_generated_playbooks_for_execution(project_id, execution_id):
    """Удаляет файлы из runtime/generated_playbooks/ для данного execution"""
    try:
        project_dir = get_project_dir(project_id)
        gp_dir = project_dir / 'runtime' / 'generated_playbooks'
        if not gp_dir.exists():
            return
        playbook_file = gp_dir / f'{execution_id}.yml'
        inventory_file = gp_dir / f'inventory_{execution_id}.yml'
        if playbook_file.exists():
            playbook_file.unlink()
            app.logger.debug(f"Deleted generated playbook: {playbook_file}")
        if inventory_file.exists():
            key_files_to_delete = set()
            try:
                with open(inventory_file, 'r', encoding='utf-8') as f:
                    inv_data = yaml_loader.load(f) or {}
                hosts_dict = (inv_data.get('all') or {}).get('hosts') or {}
                for host_data in hosts_dict.values():
                    if isinstance(host_data, dict):
                        key_path = host_data.get('ansible_ssh_private_key_file')
                        if key_path and 'generated_playbooks' in str(key_path) and str(key_path).endswith('.pem'):
                            key_name = os.path.basename(str(key_path))
                            if key_name.startswith('key_'):
                                key_files_to_delete.add(key_name)
            except Exception as e:
                app.logger.debug(f"Could not parse inventory for key paths: {e}")
            inventory_file.unlink()
            app.logger.debug(f"Deleted generated inventory: {inventory_file}")
            for key_name in key_files_to_delete:
                key_file = gp_dir / key_name
                if key_file.exists():
                    key_file.unlink()
                    app.logger.debug(f"Deleted generated key: {key_file}")
    except Exception as e:
        app.logger.warning(f"Error cleaning generated_playbooks for {execution_id}: {e}")


def _cleanup_orphaned_generated_playbooks(project_id):
    """Удаляет файлы в generated_playbooks, для которых нет execution record"""
    try:
        executions_dir = get_project_executions_dir(project_id)
        gp_dir = get_project_dir(project_id) / 'runtime' / 'generated_playbooks'
        if not gp_dir.exists():
            return
        existing_ids = {f.stem for f in executions_dir.glob('*.json')} if executions_dir.exists() else set()
        processed = set()
        for f in gp_dir.glob('*.yml'):
            ex_id = f.stem.replace('inventory_', '') if f.name.startswith('inventory_') else f.stem
            if ex_id and ex_id not in existing_ids and ex_id not in processed:
                processed.add(ex_id)
                _cleanup_generated_playbooks_for_execution(project_id, ex_id)
                app.logger.debug(f"Cleaned orphaned generated_playbooks for execution {ex_id}")
    except Exception as e:
        app.logger.warning(f"Error cleaning orphaned generated_playbooks: {e}")


def delete_execution(execution_id, project_id=None):
    """Удаляет execution, его логи и файлы в generated_playbooks"""
    try:
        # Если project_id не указан, ищем во всех проектах
        if project_id:
            executions_dir = get_project_executions_dir(project_id)
            logs_dir = get_project_logs_dir(project_id)
            execution_file = executions_dir / f'{execution_id}.json'
            log_file = logs_dir / f'{execution_id}.log'
            
            _cleanup_generated_playbooks_for_execution(project_id, execution_id)
            if execution_file.exists():
                execution_file.unlink()
            if log_file.exists():
                log_file.unlink()
            return True
        else:
            # Ищем во всех проектах (новая структура: history/executions/ и history/logs/)
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue
                # Новая структура: history/executions/ и history/logs/
                executions_dir = proj_dir / 'history' / 'executions'
                logs_dir = proj_dir / 'history' / 'logs'
                if not executions_dir.exists():
                    # Старая структура: executions/ и executions/logs/
                    executions_dir = proj_dir / 'executions'
                    logs_dir = executions_dir / 'logs'
                if not executions_dir.exists():
                    continue
                
                execution_file = executions_dir / f'{execution_id}.json'
                log_file = logs_dir / f'{execution_id}.log'
                
                if execution_file.exists():
                    proj_id = proj_dir.name
                    _cleanup_generated_playbooks_for_execution(proj_id, execution_id)
                    execution_file.unlink()
                if log_file.exists():
                    log_file.unlink()
                return True
        return False
    except Exception as e:
        app.logger.error(f"Error deleting execution {execution_id}: {e}")
        return False


def apply_retention_policy():
    """Применяет политику retention для удаления старых executions (per-project)"""
    settings = load_execution_settings()
    
    if not settings.get('save_history', True):
        return
    
    try:
        # Apply retention policy per project
        projects = load_projects()
        for project in projects:
            project_id = project.get('id')
            if not project_id:
                continue
            
            try:
                if settings.get('retention_mode') == 'count':
                    max_count = settings.get('retention_count', 200)
                    executions = list_executions(project_id=project_id)
                    
                    if len(executions) > max_count:
                        # Удаляем самые старые
                        to_delete = executions[max_count:]
                        for execution in to_delete:
                            delete_execution(execution['id'], project_id=project_id)
                        app.logger.info(f"Retention policy (project {project_id}): deleted {len(to_delete)} old executions")
                
                elif settings.get('retention_mode') == 'size':
                    max_size_mb = settings.get('retention_size_mb', 200)
                    max_size_bytes = max_size_mb * 1024 * 1024
                    
                    executions = list_executions(project_id=project_id)
                    total_size = 0
                    
                    # Вычисляем размер для каждого execution
                    execution_sizes = []
                    executions_dir = get_project_executions_dir(project_id)
                    logs_dir = get_project_logs_dir(project_id)
                    
                    for execution in executions:
                        size = 0
                        execution_file = executions_dir / f"{execution['id']}.json"
                        log_file = logs_dir / f"{execution['id']}.log"
                        
                        if execution_file.exists():
                            size += execution_file.stat().st_size
                        if log_file.exists():
                            size += log_file.stat().st_size
                        
                        execution_sizes.append((execution, size))
                        total_size += size
                    
                    # Если превышен лимит, удаляем самые старые
                    if total_size > max_size_bytes:
                        execution_sizes.sort(key=lambda x: x[0].get('createdAt', 0))
                        deleted_count = 0
                        for execution, size in execution_sizes:
                            if total_size <= max_size_bytes:
                                break
                            delete_execution(execution['id'], project_id=project_id)
                            total_size -= size
                            deleted_count += 1
                        app.logger.info(f"Retention policy (project {project_id}): deleted {deleted_count} executions by size")
            except Exception as e:
                app.logger.warning(f"Error applying retention policy for project {project_id}: {e}")
            # Очистка осиротевших файлов в generated_playbooks (execution удалён, но файлы остались)
            try:
                _cleanup_orphaned_generated_playbooks(project_id)
            except Exception as e:
                app.logger.debug(f"Cleanup orphaned generated_playbooks for {project_id}: {e}")
    except Exception as e:
        app.logger.error(f"Error applying retention policy: {e}")


def clear_all_executions(project_id=None):
    """Удаляет все executions для проекта (или всех проектов если project_id=None)
    
    Note: If project_id is None, clears executions from all projects in Project Storage.
    Legacy EXECUTIONS_DIR is no longer used.
    """
    try:
        deleted_count = 0
        if project_id:
            # Clear executions for specific project
            executions_dir = get_project_executions_dir(project_id)
            if executions_dir.exists():
                for execution_file in executions_dir.glob('*.json'):
                    execution_id = execution_file.stem
                    if delete_execution(execution_id, project_id=project_id):
                        deleted_count += 1
        else:
            # Clear executions from all projects (новая структура: history/executions/)
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue
                project_id_from_path = proj_dir.name
                # Новая структура: history/executions/
                executions_dir = proj_dir / 'history' / 'executions'
                if not executions_dir.exists():
                    # Старая структура: executions/
                    executions_dir = proj_dir / 'executions'
                if not executions_dir.exists():
                    continue
                for execution_file in executions_dir.glob('*.json'):
                    execution_id = execution_file.stem
                    if delete_execution(execution_id, project_id=project_id_from_path):
                        deleted_count += 1
        return deleted_count
    except Exception as e:
        app.logger.error(f"Error clearing executions: {e}")
    return 0


def get_execution_stats(project_id=None):
    """Получает статистику executions для проекта (или всех проектов если project_id=None)
    
    Note: Legacy EXECUTIONS_DIR is no longer used. Stats are calculated from Project Storage.
    """
    try:
        total_count = 0
        total_size = 0
        
        if project_id:
            # Stats for specific project
            executions = list_executions(project_id=project_id)
            executions_dir = get_project_executions_dir(project_id)
            logs_dir = executions_dir / 'logs'
            
            for execution in executions:
                execution_file = executions_dir / f"{execution['id']}.json"
                log_file = logs_dir / f"{execution['id']}.log"
                
                if execution_file.exists():
                    total_size += execution_file.stat().st_size
                if log_file.exists():
                    total_size += log_file.stat().st_size
            
            total_count = len(executions)
        else:
            # Stats for all projects (новая структура: history/executions/ и history/logs/)
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue
                project_id_from_path = proj_dir.name
                try:
                    executions = list_executions(project_id=project_id_from_path)
                    # Новая структура: history/logs/
                    logs_dir = proj_dir / 'history' / 'logs'
                    if not logs_dir.exists():
                        # Старая структура: executions/logs/
                        logs_dir = proj_dir / 'executions' / 'logs'
                    
                    for execution in executions:
                        execution_file = proj_dir / f"{execution['id']}.json"
                        log_file = logs_dir / f"{execution['id']}.log"
                        
                        if execution_file.exists():
                            total_size += execution_file.stat().st_size
                        if log_file.exists():
                            total_size += log_file.stat().st_size
                    
                    total_count += len(executions)
                except Exception as e:
                    app.logger.warning(f"Error getting stats for project {project_id_from_path}: {e}")
        
        return {
            'count': total_count,
            'size_mb': round(total_size / (1024 * 1024), 2)
        }
    except Exception as e:
        app.logger.error(f"Error getting execution statistics: {e}")
    return {'count': 0, 'size_mb': 0}


# ==================== Execution History API Endpoints ====================

@app.route('/api/executions', methods=['GET'])
@require_auth
def api_list_executions():
    """API: Список executions"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        limit = request.args.get('limit', type=int)
        offset = request.args.get('offset', 0, type=int)
        search = request.args.get('q', '')
        playbook_id = request.args.get('playbook_id', '').strip() or None
        
        executions = list_executions(limit=limit, offset=offset, search_query=search if search else None, playbook_id=playbook_id, project_id=project_id)
        return jsonify({'success': True, 'executions': executions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/executions', methods=['POST'])
@require_auth
def api_create_execution():
    """API: Создать execution"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        settings = load_execution_settings()
        if not settings.get('save_history', True):
            return jsonify({'success': False, 'error': 'Execution history is disabled'}), 400
        
        data = request.json or {}
        execution_id = create_execution_record(data, project_id=project_id)
        
        if execution_id:
            return jsonify({'success': True, 'executionId': execution_id})
        else:
            return jsonify({'success': False, 'error': 'Failed to create execution record'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/executions/<execution_id>', methods=['GET'])
@require_auth
def api_get_execution(execution_id):
    """API: Получить execution"""
    try:
        # Получаем projectId из запроса (опционально, но рекомендуется)
        project_id = get_project_id_from_request()
        
        # Если project_id указан, используем его для более быстрого поиска
        if project_id:
            execution = get_execution(execution_id, project_id=project_id)
        else:
            execution = get_execution(execution_id)
            
        if execution:
            # Для HOST_CHECK и HOST_FACTS извлекаем результат из логов
            run_params = execution.get('runParams', {})
            execution_type = run_params.get('execution_type')
            
            if execution_type in ('HOST_CHECK', 'HOST_FACTS'):
                result = extract_execution_result(execution, execution_type, project_id)
                execution['result'] = result
            
            return jsonify({'success': True, 'execution': execution})
        else:
            return jsonify({'success': False, 'error': 'Execution not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def extract_execution_result(execution, execution_type, project_id):
    """Извлекает результат из execution для HOST_CHECK или HOST_FACTS"""
    try:
        # Сначала проверяем, есть ли результат, сохраненный воркером
        if 'result' in execution:
            return execution['result']
        
        # Если результата нет, пытаемся извлечь из логов (fallback)
        # get_execution_log определена в этом же модуле (app.py), используем напрямую
        status = execution.get('status')
        if status not in ('SUCCESS', 'FAILED'):
            return None
        
        # Получаем логи execution
        log_content = get_execution_log(execution['id'], project_id=project_id)
        if not log_content:
            return None
        
        if execution_type == 'HOST_CHECK':
            # Парсим результат check из логов
            # Ищем строки с "failed=0" или "ok=" для успеха
            if 'failed=0' in log_content or ('ok=' in log_content and 'failed=' in log_content and 'failed=0' in log_content.split('failed=')[1].split()[0]):
                return {
                    'available': True,
                    'message': 'Host is available and ready to execute commands'
                }
            else:
                return {
                    'available': False,
                    'message': 'Host is unavailable or an error occurred during check'
                }
        elif execution_type == 'HOST_FACTS':
            # Парсим JSON facts из логов
            import re
            json_match = re.search(r'SUCCESS\s*=>\s*(\{.*\})', log_content, re.DOTALL)
            if not json_match:
                json_match = re.search(r'(\{.*\})', log_content, re.DOTALL)
            
            if json_match:
                try:
                    facts_json = json.loads(json_match.group(1))
                    ansible_facts = facts_json.get('ansible_facts', facts_json)
                    return {
                        'facts': ansible_facts
                    }
                except json.JSONDecodeError:
                    pass
            
            return {
                'error': 'Could not extract facts from output'
            }
    except Exception as e:
        app.logger.error(f"[extract_execution_result] Error: {e}")
        return None


@app.route('/api/executions/<execution_id>', methods=['PATCH'])
@require_auth
def api_update_execution(execution_id):
    """API: Обновить execution"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        
        data = request.json or {}
        if update_execution_record(execution_id, data, project_id=project_id):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to update execution'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/executions/<execution_id>/cancel', methods=['POST'])
@require_auth
def api_cancel_execution(project_id, execution_id):
    """
    API: Отмена QUEUED execution
    
    Переход: QUEUED → CANCELED
    Использует fcntl LOCK_EX для атомарности операции.
    """
    try:
        try:
            from .executions_store import (
                get_project_executions_dir,
                validate_status_transition,
            )
        except ImportError:
            from executions_store import (
                get_project_executions_dir,
                validate_status_transition,
            )
        
        executions_dir = get_project_executions_dir(project_id)
        execution_file = executions_dir / f'{execution_id}.json'
        
        if not execution_file.exists():
            return jsonify({'success': False, 'error': 'Execution not found'}), 404
        
        # Открываем файл с блокировкой для атомарной операции
        with open(execution_file, 'r+', encoding='utf-8') as f:
            # Блокируем файл для записи
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            
            try:
                # Читаем execution
                execution = json.load(f)
                current_status = execution.get('status', 'QUEUED')
                
                # Проверяем статус
                if current_status != 'QUEUED':
                    return jsonify({
                        'success': False, 
                        'error': f'Cannot cancel execution in status {current_status}'
                    }), 409
                
                # Валидируем переход
                validate_status_transition('QUEUED', 'CANCELED')
                
                # Выполняем переход QUEUED → CANCELED
                now = time.time()
                execution['status'] = 'CANCELED'
                execution['canceledAt'] = now
                execution['cancelReason'] = 'user'
                execution['statusUpdatedAt'] = now
                
                # Сохраняем
                f.seek(0)
                f.truncate()
                json.dump(execution, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
                
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        # Логируем действие
        try:
            from .executions_store import append_execution_log
        except ImportError:
            from executions_store import append_execution_log
        append_execution_log(execution_id, '[api] Canceled by user\n', project_id=project_id)
        
        app.logger.info(f"Canceled execution {execution_id} in project {project_id}")
        return jsonify({'success': True, 'status': 'CANCELED'})
        
    except ValueError as e:
        # Ошибка валидации перехода
        app.logger.warning(f"Invalid status transition for cancel: {e}")
        return jsonify({'success': False, 'error': str(e)}), 409
    except Exception as e:
        app.logger.error(f"Error canceling execution {execution_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/executions/<execution_id>/stop', methods=['POST'])
@require_auth
def api_stop_execution(project_id, execution_id):
    """
    API: Запрос остановки RUNNING execution
    
    Переход: RUNNING → CANCELING
    Идемпотентно для CANCELING статуса.
    Использует fcntl LOCK_EX для атомарности операции.
    """
    try:
        try:
            from .executions_store import (
                get_project_executions_dir,
                validate_status_transition,
            )
        except ImportError:
            from executions_store import (
                get_project_executions_dir,
                validate_status_transition,
            )
        
        executions_dir = get_project_executions_dir(project_id)
        execution_file = executions_dir / f'{execution_id}.json'
        
        if not execution_file.exists():
            return jsonify({'success': False, 'error': 'Execution not found'}), 404
        
        # Открываем файл с блокировкой для атомарной операции
        with open(execution_file, 'r+', encoding='utf-8') as f:
            # Блокируем файл для записи
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            
            try:
                # Читаем execution
                execution = json.load(f)
                current_status = execution.get('status', 'QUEUED')
                
                # Проверяем статус
                if current_status == 'CANCELING':
                    # Идемпотентно - уже в статусе CANCELING
                    return jsonify({'success': True, 'status': 'CANCELING', 'message': 'Already stopping'})
                
                if current_status != 'RUNNING':
                    return jsonify({
                        'success': False, 
                        'error': f'Cannot stop execution in status {current_status}'
                    }), 409
                
                # Валидируем переход
                validate_status_transition('RUNNING', 'CANCELING')
                
                # Выполняем переход RUNNING → CANCELING
                now = time.time()
                execution['status'] = 'CANCELING'
                execution['cancelRequestedAt'] = now
                execution['statusUpdatedAt'] = now
                
                # Сохраняем
                f.seek(0)
                f.truncate()
                json.dump(execution, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
                
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        # Логируем действие
        append_execution_log(execution_id, '[api] Stop requested by user\n', project_id=project_id)
        
        app.logger.info(f"Stop requested for execution {execution_id} in project {project_id}")
        return jsonify({'success': True, 'status': 'CANCELING'})
        
    except ValueError as e:
        # Ошибка валидации перехода
        app.logger.warning(f"Invalid status transition for stop: {e}")
        return jsonify({'success': False, 'error': str(e)}), 409
    except Exception as e:
        app.logger.error(f"Error stopping execution {execution_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/executions/<execution_id>/logs', methods=['GET'])
@require_auth
def api_get_execution_logs(execution_id):
    """API: Получить логи execution с опциональной фильтрацией
    
    Query parameters:
    - inventory: фильтр по inventory файлу (optional)
    - host: фильтр по хосту (optional)
    - playbook: фильтр по плейбуку (optional)
    - cursor: курсор для пагинации (optional, для будущего использования)
    """
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        
        # Получаем фильтры из query параметров
        inventory_filter = request.args.get('inventory', '').strip()
        host_filter = request.args.get('host', '').strip()
        playbook_filter = request.args.get('playbook', '').strip()
        cursor = request.args.get('cursor', '').strip()
        
        # Получаем raw logs
        raw_logs = get_execution_log(execution_id, project_id=project_id)
        
        if not raw_logs:
            return jsonify({
                'success': True,
                'lines': [],
                'nextCursor': None
            })
        
        # Парсим логи в структурированный формат
        log_lines = []
        lines = raw_logs.split('\n')
        current_playbook = None  # Текущий плейбук для каждой строки
        
        for line_num, line in enumerate(lines):
            if not line.strip():
                continue
            
            # Парсим строку лога (Ansible формат)
            log_line = {
                'text': line,
                'lineNumber': line_num
            }
            
            # Пытаемся извлечь playbook из строки "PLAY [playbook_name]"
            play_match = re.match(r'^PLAY\s+\[(.+?)\]', line)
            if play_match:
                current_playbook = play_match.group(1).strip()
            
            # Сохраняем текущий плейбук для строки
            if current_playbook:
                log_line['playbook'] = current_playbook
            
            # Пытаемся извлечь timestamp (если есть)
            # Ansible логи обычно имеют формат: [timestamp] host | status => ...
            timestamp_match = re.match(r'^\[?(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[\.\d]*)\]?', line)
            if timestamp_match:
                log_line['timestamp'] = timestamp_match.group(1)
            
            # Пытаемся извлечь host (Ansible формат: hostname | status => ...)
            host_match = re.match(r'^(\S+)\s*\|\s*', line)
            if host_match:
                log_line['host'] = host_match.group(1)
            
            # Определяем уровень (level)
            line_lower = line.lower()
            if 'fatal:' in line_lower or 'failed!' in line_lower or 'error' in line_lower:
                log_line['level'] = 'error'
            elif 'warning' in line_lower or 'warn' in line_lower:
                log_line['level'] = 'warning'
            elif 'changed:' in line_lower or 'ok:' in line_lower:
                log_line['level'] = 'success'
            elif 'skipping' in line_lower:
                log_line['level'] = 'info'
            else:
                log_line['level'] = 'info'
            
            # Применяем фильтры
            include_line = True
            
            # Фильтр по playbook
            if playbook_filter and playbook_filter != 'all' and include_line:
                line_playbook = log_line.get('playbook', '')
                if not line_playbook or playbook_filter.lower() not in line_playbook.lower():
                    # Также проверяем в тексте строки
                    if playbook_filter.lower() not in line.lower():
                        include_line = False
            
            # Фильтр по host
            if host_filter and host_filter != 'all' and include_line:
                line_host = log_line.get('host', '')
                if not line_host or host_filter.lower() not in line_host.lower():
                    # Также проверяем в тексте строки
                    if host_filter.lower() not in line.lower():
                        include_line = False
            
            # Фильтр по inventory (проверяем по группам хостов)
            if inventory_filter and inventory_filter != 'all' and include_line:
                # Получаем execution для проверки inventory
                execution = get_execution(execution_id, project_id=project_id)
                if execution:
                    inventory_snapshot = execution.get('inventorySnapshot', {})
                    groups = inventory_snapshot.get('groups', [])
                    
                    # Проверяем, принадлежит ли host к inventory
                    line_host = log_line.get('host', '')
                    if line_host:
                        host_in_inventory = False
                        for group in groups:
                            # Проверяем имя группы или inventory файл
                            group_name = group.get('groupName', '')
                            inventory_file = group.get('inventoryFile', '')
                            
                            # Сравниваем с фильтром (может быть имя группы или файл)
                            if (inventory_filter.lower() in group_name.lower() or 
                                inventory_filter.lower() in inventory_file.lower() or
                                group_name.lower() == inventory_filter.lower()):
                                # Проверяем, есть ли host в этой группе
                                group_hosts = group.get('hosts', [])
                                for group_host in group_hosts:
                                    host_id = group_host.get('hostId', '') or group_host.get('ip', '') or str(group_host)
                                    if line_host in host_id or host_id in line_host or line_host == host_id:
                                        host_in_inventory = True
                                        break
                                if host_in_inventory:
                                    break
                        
                        if not host_in_inventory:
                            include_line = False
                    else:
                        # Если host не определен в строке, проверяем по тексту строки
                        # (может содержать упоминание inventory)
                        line_text_lower = line.lower()
                        inventory_in_text = inventory_filter.lower() in line_text_lower
                        if not inventory_in_text:
                            include_line = False
            
            if include_line:
                log_lines.append(log_line)
        
        # Поддержка cursor для пагинации (пока не используется, но готово)
        # Если cursor указан, можно использовать его для пагинации
        next_cursor = None
        if cursor:
            # TODO: Реализовать пагинацию через cursor
            pass
        
        return jsonify({
            'success': True,
            'lines': log_lines,
            'nextCursor': next_cursor,
            'totalLines': len(log_lines)
        })
        
    except Exception as e:
        app.logger.error(f"Error getting execution logs: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/executions/<execution_id>/log', methods=['GET'])
@require_auth
def api_get_execution_log_incremental(execution_id):
    """API: Получить логи execution с incremental fetch (Jenkins-style)
    
    Query parameters:
    - offset: байтовый offset для начала чтения (default: 0)
    - limit: максимальное количество байт для чтения (default: 1MB)
    - project_id: ID проекта (обязательно)
    
    Response:
    {
        "success": true,
        "text": "...new chunk...",
        "nextOffset": 12345,
        "fileSize": 50000,
        "isComplete": false,
        "status": "RUNNING|SUCCESS|FAILED|..."
    }
    """
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        # Получаем параметры запроса
        offset = request.args.get('offset', 0, type=int)
        limit = request.args.get('limit', 1024*1024, type=int)  # Default 1MB
        
        # Импортируем функцию для чтения лога
        try:
            from .executions_store import read_log_chunk, get_execution
        except ImportError:
            from executions_store import read_log_chunk, get_execution
        
        # Читаем chunk лога
        text, next_offset, file_size, is_complete = read_log_chunk(
            execution_id, 
            offset=offset, 
            limit=limit, 
            project_id=project_id
        )
        
        # Получаем статус execution
        execution = get_execution(execution_id, project_id=project_id)
        status = execution.get('status', 'UNKNOWN') if execution else 'UNKNOWN'
        
        return jsonify({
            'success': True,
            'text': text,
            'nextOffset': next_offset,
            'fileSize': file_size,
            'isComplete': is_complete,
            'status': status
        })
        
    except Exception as e:
        app.logger.error(f"Error getting incremental log for execution {execution_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/executions/<execution_id>/log/stream', methods=['GET'])
@require_auth
def api_get_execution_log_stream(execution_id):
    """API: SSE stream для логов execution (GitLab/Jenkins style)
    
    Query parameters:
    - offset: байтовый offset для начала чтения (default: 0)
    - project_id: ID проекта (обязательно)
    
    SSE Events:
    - data: {"type": "chunk", "text": "...", "nextOffset": 12345, "fileSize": 50000, "isComplete": false, "status": "RUNNING"}
    - data: {"type": "status", "status": "SUCCESS|FAILED|CANCELED", "isComplete": true}
    - data: {"type": "error", "error": "..."}
    """
    import time
    import json
    
    def generate():
        try:
            # Получаем projectId
            project_id = request.args.get('project_id')
            if not project_id:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Project ID is required'})}\n\n"
                return
            
            # Получаем начальный offset
            offset = request.args.get('offset', 0, type=int)
            
            # Импортируем функции
            try:
                from .executions_store import read_log_chunk, get_execution
            except ImportError:
                from executions_store import read_log_chunk, get_execution
            
            last_file_size = 0
            consecutive_empty_reads = 0
            max_empty_reads = 300  # Останавливаем после 5 минут без данных (300 * 1s)
            
            while True:
                try:
                    # Проверяем, что клиент еще подключен
                    if request.is_json:  # Простая проверка
                        pass
                    
                    # Читаем chunk лога
                    text, next_offset, file_size, is_complete = read_log_chunk(
                        execution_id,
                        offset=offset,
                        limit=1024*1024,  # 1MB chunks
                        project_id=project_id
                    )
                    
                    # Получаем статус execution
                    execution = get_execution(execution_id, project_id=project_id)
                    status = execution.get('status', 'UNKNOWN') if execution else 'UNKNOWN'
                    
                    # Если есть новые данные, отправляем их
                    if text:
                        consecutive_empty_reads = 0
                        event_data = {
                            'type': 'chunk',
                            'text': text,
                            'nextOffset': next_offset,
                            'fileSize': file_size,
                            'isComplete': is_complete,
                            'status': status
                        }
                        yield f"data: {json.dumps(event_data)}\n\n"
                        offset = next_offset
                        last_file_size = file_size
                    else:
                        # Нет новых данных
                        consecutive_empty_reads += 1
                        
                        # Отправляем heartbeat каждые 10 секунд
                        if consecutive_empty_reads % 10 == 0:
                            event_data = {
                                'type': 'heartbeat',
                                'offset': offset,
                                'fileSize': file_size,
                                'isComplete': is_complete,
                                'status': status
                            }
                            yield f"data: {json.dumps(event_data)}\n\n"
                        
                        # Если execution завершен, отправляем финальный статус и закрываем
                        if is_complete:
                            event_data = {
                                'type': 'status',
                                'status': status,
                                'isComplete': True,
                                'fileSize': file_size
                            }
                            yield f"data: {json.dumps(event_data)}\n\n"
                            break
                        
                        # Если слишком долго нет данных и execution не активен, останавливаемся
                        if consecutive_empty_reads >= max_empty_reads and status not in ('RUNNING', 'QUEUED', 'CANCELING'):
                            break
                    
                    # Ждем перед следующим чтением (tail -f style)
                    time.sleep(0.5)  # 500ms для плавного обновления
                    
                except Exception as e:
                    app.logger.error(f"Error in log stream for execution {execution_id}: {e}", exc_info=True)
                    event_data = {
                        'type': 'error',
                        'error': str(e)
                    }
                    yield f"data: {json.dumps(event_data)}\n\n"
                    break
                    
        except Exception as e:
            app.logger.error(f"Error starting log stream for execution {execution_id}: {e}", exc_info=True)
            event_data = {
                'type': 'error',
                'error': str(e)
            }
            yield f"data: {json.dumps(event_data)}\n\n"
    
    response = Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # Отключаем буферизацию в nginx
            'Connection': 'keep-alive'
        }
    )
    return response


@app.route('/api/executions/<execution_id>/logs', methods=['POST'])
@require_auth
def api_append_execution_logs(execution_id):
    """API: Добавить логи в execution"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        
        data = request.json or {}
        text = data.get('text', '')
        if append_execution_log(execution_id, text, project_id=project_id):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to append logs'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/executions/clear', methods=['POST'])
@require_auth
def api_clear_executions():
    """API: Очистить все executions"""
    try:
        deleted_count = clear_all_executions()
        return jsonify({'success': True, 'deletedCount': deleted_count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/executions/stats', methods=['GET'])
@require_auth
def api_get_execution_stats():
    """API: Получить статистику executions"""
    try:
        stats = get_execution_stats()
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/execution_settings', methods=['GET'])
def api_get_execution_settings():
    """API: Получить настройки execution history (без auth — только чтение, для загрузки до выбора проекта)."""
    try:
        settings = load_execution_settings()
        stats = get_execution_stats()
        return jsonify({'success': True, 'settings': settings, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/execution_settings', methods=['POST'])
@require_auth
def api_save_execution_settings():
    """API: Сохранить настройки execution history"""
    try:
        data = request.json or {}
        settings = load_execution_settings()
        settings.update(data)
        
        # Если изменился уровень логирования, применяем его сразу
        if 'log_level' in data:
            set_log_level(data['log_level'])
        
        # Если изменился лимит размера загружаемых файлов, применяем его сразу
        if 'max_upload_size_mb' in data:
            max_size_mb = int(data['max_upload_size_mb'])
            if max_size_mb < 1:
                max_size_mb = 1  # Минимум 1MB
            elif max_size_mb > 1024:
                max_size_mb = 1024  # Максимум 1GB
            app.config['MAX_CONTENT_LENGTH'] = max_size_mb * 1024 * 1024
            app.logger.info(f"Maximum upload file size set to: {max_size_mb}MB")
        
        # Если изменился максимальный размер лог-файлов, валидируем значение
        if 'max_log_size_mb' in data:
            max_log_size_mb = int(data['max_log_size_mb'])
            if max_log_size_mb < 1:
                max_log_size_mb = 1  # Минимум 1MB
            elif max_log_size_mb > 1024:
                max_log_size_mb = 1024  # Максимум 1GB
            settings['max_log_size_mb'] = max_log_size_mb
            app.logger.info(f"Max log file size set to: {max_log_size_mb}MB (will apply after restart)")

        # Если изменился TTL статуса хостов, валидируем значение
        if 'host_status_ttl_seconds' in data:
            try:
                ttl_seconds = int(data['host_status_ttl_seconds'])
            except (TypeError, ValueError):
                ttl_seconds = HOST_STATUS_TTL_DEFAULT
            # Разумные границы: от 30 секунд до 24 часов
            if ttl_seconds < 30:
                ttl_seconds = 30
            elif ttl_seconds > 24 * 60 * 60:
                ttl_seconds = 24 * 60 * 60
            settings['host_status_ttl_seconds'] = ttl_seconds
            app.logger.info(f"Host status TTL set to: {ttl_seconds} seconds")
        
        if save_execution_settings(settings):
            # Применяем retention policy после изменения настроек
            apply_retention_policy()
            stats = get_execution_stats()
            return jsonify({'success': True, 'settings': settings, 'stats': stats})
        else:
            return jsonify({'success': False, 'error': 'Failed to save settings'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== Roles Storage API Endpoints ====================

def scan_roles_storage(project_id=None):
    """Сканирует roles-storage и возвращает дерево пакетов и ролей (использует Source Resolver)"""
    def build_tree(path, parent_id='', base_path=None):
        """Рекурсивно строит дерево директорий"""
        if base_path is None:
            base_path = path
        nodes = []
        if not path.exists() or not path.is_dir():
            return nodes
        
        try:
            for item in sorted(path.iterdir()):
                if not item.is_dir():
                    continue
                
                item_name = item.name
                item_id = f"{parent_id}/{item_name}" if parent_id else item_name
                
                # Определяем тип: pack (на первом уровне) или role/folder
                # Проверяем, есть ли tasks/main.yaml или tasks/main.yml - это роль
                has_tasks_main = (item / 'tasks' / 'main.yaml').exists() or (item / 'tasks' / 'main.yml').exists()
                
                if not parent_id:
                    # На первом уровне: если есть tasks/main.yaml, это роль, иначе pack
                    if has_tasks_main:
                        node_type = "role"
                    else:
                        node_type = "pack"
                else:
                    # Не на первом уровне: если есть tasks/main.yaml, это роль, иначе folder
                    if has_tasks_main:
                        node_type = "role"
                    else:
                        node_type = "folder"
                
                # Return project-relative path only (no absolute paths)
                # Path is relative to base_path (which is Project Storage root)
                node = {
                    'id': item_id,
                    'name': item_name,
                    'path': str(item.relative_to(base_path)),
                    'type': node_type,
                    'children': []
                }
                
                # Рекурсивно добавляем детей
                children = build_tree(item, item_id, base_path)
                node['children'] = children
                
                nodes.append(node)
        except PermissionError:
            app.logger.warning(f"Permission denied reading {path}")
        except Exception as e:
            app.logger.error(f"Error scanning {path}: {e}")
        
        return nodes
    
    # Resolve repo source for roles
    # REQUIRED: project_id must be provided
    if not project_id:
        app.logger.error("[scan_roles_storage] project_id is required")
        return []
    
    # resolve_project_source() ALWAYS returns Project Storage path
    try:
        source = resolve_project_source(project_id, 'repo')
        repo_path = source['rootPath']
        roles_storage_path = repo_path / 'roles'
    except Exception as e:
        app.logger.error(f"[scan_roles_storage] Failed to resolve Project Storage: {e}")
        return []  # Fail-fast, no fallback
    
    if not roles_storage_path.exists():
        return []
    
    return build_tree(roles_storage_path, base_path=roles_storage_path)


def load_roles_config(project_id=None):
    """Загружает конфигурацию ролей"""
    default_config = {
        'storageRoot': 'roles-storage',
        'packs': [],
        'configByPackId': {}
    }
    
    # Если project_id не указан, используем Default Project
    if not project_id:
        project_id = get_default_project_id()
    
    try:
        config_file = get_project_roles_config_file(project_id)
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # Убеждаемся, что есть все необходимые поля
                result = {
                    'storageRoot': config.get('storageRoot', 'roles-storage'),
                    'packs': config.get('packs', []),
                    'configByPackId': {}
                }
                
                # Инициализируем requiredRoles и roleColors для каждого пакета, если его нет
                for pack_id, pack_config in config.get('configByPackId', {}).items():
                    result['configByPackId'][pack_id] = {
                        'enabled': pack_config.get('enabled', True),
                        'rolesEnabled': pack_config.get('rolesEnabled', {}),
                        'requiredRoles': pack_config.get('requiredRoles', []),
                        'roleColors': pack_config.get('roleColors', {})
                    }
                
                return result
    except Exception as e:
        app.logger.error(f"Error loading roles configuration: {e}")
    
    return default_config


def save_roles_config(config, project_id=None):
    """Сохраняет конфигурацию ролей"""
    try:
        # Если project_id не указан, используем Default Project
        if not project_id:
            project_id = get_default_project_id()
        
        # Валидация и санитизация
        sanitized_config = {
            'storageRoot': str(config.get('storageRoot', 'roles-storage')),
            'packs': config.get('packs', []),
            'configByPackId': {}
        }
        
        # Санитизация configByPackId - проверяем пути
        for pack_id, pack_config in config.get('configByPackId', {}).items():
            # Предотвращаем directory traversal
            if '..' in pack_id or '/' in pack_id.lstrip('/'):
                continue
            
            sanitized_config['configByPackId'][pack_id] = {
                'enabled': bool(pack_config.get('enabled', True)),
                'rolesEnabled': {
                    role_id: bool(enabled) 
                    for role_id, enabled in pack_config.get('rolesEnabled', {}).items()
                    if '..' not in role_id
                },
                'requiredRoles': [
                    role_id 
                    for role_id in pack_config.get('requiredRoles', [])
                    if '..' not in role_id and isinstance(role_id, str)
                ],
                'roleColors': {
                    role_id: str(color)
                    for role_id, color in pack_config.get('roleColors', {}).items()
                    if '..' not in role_id and isinstance(color, str) and color.startswith('#') and len(color) == 7
                }
            }
        
        # Используем путь к файлу конфигурации проекта
        config_file = get_project_roles_config_file(project_id)
        config_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(sanitized_config, f, indent=2, ensure_ascii=False)
        
        return True
    except Exception as e:
        app.logger.error(f"Error saving roles configuration: {e}")
        return False


@app.route('/api/roles/storage', methods=['GET'])
@require_auth
def api_get_roles_storage():
    """API: Получить дерево roles-storage"""
    try:
        project_id = get_project_id_from_request()
        tree = scan_roles_storage(project_id)
        
        # Get resolved path for storageRoot - ALWAYS Project Storage
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        try:
            source = resolve_project_source(project_id, 'repo')
            repo_path = source['rootPath']  # ALWAYS Project Storage path
            storage_root = repo_path / 'roles'
        except Exception as e:
            app.logger.error(f"[api_scan_roles_storage] Failed to resolve Project Storage: {e}")
            return jsonify({'success': False, 'error': f'Failed to resolve Project Storage: {e}'}), 500
        
        # Return project-relative path only (no absolute paths, no BASE_DIR)
        # storageRoot is relative to project root: 'repo/roles'
        project_dir = get_project_dir(project_id)
        storage_root_relative = str(storage_root.relative_to(project_dir)) if storage_root.is_relative_to(project_dir) else 'repo/roles'
        
        return jsonify({
            'success': True,
            'storageRoot': storage_root_relative,
            'tree': tree
        })
    except Exception as e:
        app.logger.error(f"Error scanning roles-storage: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/roles/config', methods=['GET'])
@require_auth
def api_get_roles_config():
    """API: Получить конфигурацию ролей"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        config = load_roles_config(project_id)
        return jsonify({'success': True, 'config': config})
    except Exception as e:
        app.logger.error(f"Error loading roles configuration: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/roles/config', methods=['POST'])
@require_auth
def api_save_roles_config():
    """API: Сохранить конфигурацию ролей"""
    try:
        # Получаем projectId
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        data = request.json or {}
        config = data.get('config', {})
        
        if save_roles_config(config, project_id):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Error saving configuration'}), 500
    except Exception as e:
        app.logger.error(f"Error saving roles configuration: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== Role Details & Variables Extraction ====================

import re
from collections import defaultdict

# Системные переменные Ansible, которые нужно игнорировать или помечать отдельно
ANSIBLE_SYSTEM_VARS = {
    'ansible_', 'inventory_hostname', 'group_names', 'groups', 'hostvars',
    'play_hosts', 'playbook_dir', 'role_path', 'role_name', 'ansible_version'
}

# Литералы и ключевые слова, которые НЕ являются переменными
ANSIBLE_LITERALS = {
    'false', 'true', 'present', 'absent', 'yes', 'no', 'on', 'off',
    'Ubuntu', 'Debian', 'CentOS', 'RedHat', 'Fedora',
    'password', 'key', 'root', 'inet', 'no', 'yes'
}

def extract_variables_from_yaml_text(text, file_path=''):
    """Извлекает переменные из YAML текста (Jinja2 шаблоны + when условия)"""
    variables = defaultdict(lambda: {
        'occurrences': [],
        'files': set(),
        'examples': []
    })
    
    lines = text.split('\n')
    for line_num, line in enumerate(lines, 1):
        # Пропускаем комментарии
        if line.strip().startswith('#'):
            continue
        
        # 1. Ищем все вхождения {{ ... }}
        jinja_pattern = r'\{\{([^}]+)\}\}'
        jinja_matches = re.finditer(jinja_pattern, line)
        
        for jinja_match in jinja_matches:
            jinja_content = jinja_match.group(1).strip()
            
            # Пропускаем комментарии и пустые
            if not jinja_content or jinja_content.startswith('#'):
                continue
            
            # Обрабатываем разные паттерны
            var_names = []
            
            # 1. Простая переменная: {{ var }}, {{ var_name }}
            simple_var = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)', jinja_content)
            if simple_var:
                var_name = simple_var.group(1)
                # Извлекаем базовое имя (без вложенных ключей)
                base_var = var_name.split('.')[0]
                # Фильтруем: исключаем системные переменные и литералы
                if (base_var not in ANSIBLE_SYSTEM_VARS and 
                    base_var not in ANSIBLE_LITERALS and
                    not any(base_var.startswith(sys_var) for sys_var in ANSIBLE_SYSTEM_VARS) and
                    not any(base_var.lower() == lit.lower() for lit in ANSIBLE_LITERALS)):
                    var_names.append((base_var, 'direct'))
            
            # 2. С фильтрами: {{ var | default(...) }}, {{ var | other }}
            filter_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*\|', jinja_content)
            if filter_match:
                var_name = filter_match.group(1)
                base_var = var_name.split('.')[0]
                # Фильтруем: исключаем системные переменные и литералы
                if (base_var not in ANSIBLE_SYSTEM_VARS and 
                    base_var not in ANSIBLE_LITERALS and
                    not any(base_var.startswith(sys_var) for sys_var in ANSIBLE_SYSTEM_VARS) and
                    not any(base_var.lower() == lit.lower() for lit in ANSIBLE_LITERALS)):
                    var_names.append((base_var, 'default'))
            
            # 3. Доступ к словарю: {{ dict[key] }}, {{ dict[var] }}
            dict_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\[([^\]]+)\]', jinja_content)
            if dict_match:
                dict_name = dict_match.group(1)
                key_expr = dict_match.group(2).strip()
                # Если key - это переменная (не строка в кавычках)
                if not (key_expr.startswith('"') or key_expr.startswith("'") or key_expr.startswith("'")):
                    # Пытаемся извлечь переменную из key
                    key_var_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)', key_expr)
                    if key_var_match:
                        key_var = key_var_match.group(1)
                        base_key_var = key_var.split('.')[0]
                        if base_key_var not in ANSIBLE_SYSTEM_VARS:
                            var_names.append((base_key_var, 'dict_access'))
                # Также сохраняем сам dict если это не системная переменная
                base_dict = dict_name.split('.')[0]
                if base_dict not in ANSIBLE_SYSTEM_VARS and base_dict != 'hostvars':
                    var_names.append((base_dict, 'dict'))
            
            # 4. hostvars[inventory_hostname].my_var
            hostvars_match = re.match(r'hostvars\[([^\]]+)\]\.([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)', jinja_content)
            if hostvars_match:
                host_key = hostvars_match.group(1)
                var_name = hostvars_match.group(2)
                base_var = var_name.split('.')[0]
                if base_var not in ANSIBLE_SYSTEM_VARS:
                    var_names.append((base_var, 'hostvars'))
            
            # Сохраняем найденные переменные
            for var_name, var_type in var_names:
                variables[var_name]['occurrences'].append({
                    'line': line_num,
                    'context': line.strip()[:100],
                    'type': var_type,
                    'full_match': jinja_match.group(0)
                })
                variables[var_name]['files'].add(file_path)
                
                # Сохраняем примеры (максимум 2)
                if len(variables[var_name]['examples']) < 2:
                    variables[var_name]['examples'].append({
                        'line': line_num,
                        'context': line.strip()[:80]
                    })
        
        # 2. Ищем when условия: when: var_name, when: var_name == ..., when: not var_name, when: var_name is defined
        # Также обрабатываем: when: var_name | default(false)
        when_match = re.search(r'when:\s*(.+?)(?:\n|$)', line, re.IGNORECASE)
        if when_match:
            when_expr = when_match.group(1).strip()
            
            # Сначала ищем переменные с фильтрами: var_name | default(...)
            filter_pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*\|\s*default\s*\('
            filter_matches = re.finditer(filter_pattern, when_expr)
            for filter_match in filter_matches:
                var_name = filter_match.group(1)
                base_var = var_name.split('.')[0]
                if base_var and base_var not in ANSIBLE_SYSTEM_VARS and not any(base_var.startswith(sys_var) for sys_var in ANSIBLE_SYSTEM_VARS):
                    variables[base_var]['occurrences'].append({
                        'line': line_num,
                        'context': line.strip()[:100],
                        'type': 'when',
                        'full_match': when_expr[:80]
                    })
                    variables[base_var]['files'].add(file_path)
                    if len(variables[base_var]['examples']) < 2:
                        variables[base_var]['examples'].append({
                            'line': line_num,
                            'context': line.strip()[:80]
                        })
            
            # Затем ищем остальные переменные в when выражении
            # Исключаем литералы и системные переменные
            when_var_patterns = [
                r'\b([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*(?:==|!=|>|<|is\s+(?:not\s+)?defined|in\s+|not\s+in)',
                r'(?:^|\s|\(|!|not\s+)([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)(?:\s|$|\)|==|!=|>|<)',
            ]
            for pattern in when_var_patterns:
                when_vars = re.finditer(pattern, when_expr)
                for when_var_match in when_vars:
                    var_name = when_var_match.group(1) if when_var_match.lastindex >= 1 else when_var_match.group(0).strip()
                    # Очищаем от операторов
                    var_name = re.sub(r'^\s*(?:not|!)\s+', '', var_name).strip()
                    # Пропускаем если уже обработали как фильтр
                    if '|' in when_expr and var_name in when_expr:
                        continue
                    base_var = var_name.split('.')[0]
                    # Фильтруем: исключаем системные переменные и литералы
                    if (base_var and 
                        base_var not in ANSIBLE_SYSTEM_VARS and 
                        base_var not in ANSIBLE_LITERALS and
                        not any(base_var.startswith(sys_var) for sys_var in ANSIBLE_SYSTEM_VARS) and
                        not any(base_var.lower() == lit.lower() for lit in ANSIBLE_LITERALS)):
                        variables[base_var]['occurrences'].append({
                            'line': line_num,
                            'context': line.strip()[:100],
                            'type': 'when',
                            'full_match': when_expr[:80]
                        })
                        variables[base_var]['files'].add(file_path)
                        if len(variables[base_var]['examples']) < 2:
                            variables[base_var]['examples'].append({
                                'line': line_num,
                                'context': line.strip()[:80]
                            })
    
    # Преобразуем в список для JSON
    result = []
    for var_name, data in sorted(variables.items()):
        result.append({
            'variable': var_name,
            'source': 'tasks',  # Будет обновлено при объединении
            'occurrences': len(data['occurrences']),
            'files': list(data['files']),
            'examples': data['examples'][:2]  # Максимум 2 примера
        })
    
    return result


def get_role_details(pack_id, role_name, project_id=None):
    """Получает детали роли: файлы, переменные, defaults, vars (использует Source Resolver)"""
    try:
        # Безопасная проверка пути
        if '..' in pack_id or '..' in role_name:
            return None
        
        # Resolve roles_playbooks source
        # REQUIRED: project_id must be provided
        if not project_id:
            app.logger.error("[get_role_details] project_id is required")
            return None
        
        # resolve_project_source() ALWAYS returns Project Storage path
        try:
            source = resolve_project_source(project_id, 'repo')
            repo_path = source['rootPath']
            roles_storage_path = repo_path / 'roles'
        except Exception as e:
            app.logger.error(f"[get_role_details] Failed to resolve Project Storage path: {e}")
            return jsonify({'success': False, 'error': f'Failed to resolve Project Storage: {e}'}), 500
        
        # Если pack_id == role_name, это роль на первом уровне
        # Путь должен быть: roles/role_name (без дублирования)
        if pack_id == role_name:
            # Роль на первом уровне
            role_path = roles_storage_path / role_name
            app.logger.info(f"[get_role_details] Role at first level: role_path={role_path}")
        else:
            # Роль внутри pack
            role_path = roles_storage_path / pack_id / role_name
            app.logger.info(f"[get_role_details] Role in pack: role_path={role_path}")
        
        if not role_path.exists() or not role_path.is_dir():
            app.logger.warning(f"[get_role_details] Role not found: {role_path}")
            return None
        
        # Return project-relative path only (no absolute paths, no BASE_DIR)
        project_dir = get_project_dir(project_id)
        role_path_relative = str(role_path.relative_to(project_dir)) if role_path.is_relative_to(project_dir) else f'roles-playbooks/{pack_id}/{role_name}'
        
        result = {
            'pack': pack_id,
            'role': role_name,
            'path': role_path_relative,
            'task_files': [],  # Всегда пустой список, tasks не парсим
            'variables': [],
            'defaults': {},
            'vars': {},
            'has_defaults': False,
            'has_vars': False
        }
        
        # ⚠️ ВАЖНО: tasks НЕ парсим. Источник правды - ТОЛЬКО defaults/*.yml и defaults/*.yaml
        
        # Читаем ВСЕ файлы из defaults/ (*.yml и *.yaml)
        defaults_dir = role_path / 'defaults'
        defaults_descriptions = {}  # var_name -> description из комментариев
        defaults_files_info = []  # список файлов с их содержимым
        all_defaults = {}  # объединенные defaults из всех файлов
        
        if defaults_dir.exists() and defaults_dir.is_dir():
            # Ищем все *.yml и *.yaml файлы
            defaults_files = []
            for ext in ['*.yml', '*.yaml']:
                defaults_files.extend(list(defaults_dir.glob(ext)))
            
            if defaults_files:
                result['has_defaults'] = True
                
                # Читаем каждый файл и объединяем
                for defaults_file in sorted(defaults_files):  # сортируем для предсказуемости
                    if not defaults_file.is_file():
                        continue
                    
                    try:
                        with open(defaults_file, 'r', encoding='utf-8') as f:
                            defaults_content = f.read()
                            file_data = yaml.safe_load(defaults_content) or {}
                            
                            if file_data and not file_data.get('_error'):
                                # Объединяем данные (последний файл перезаписывает предыдущие)
                                all_defaults.update(file_data)
                                defaults_files_info.append({
                                    'name': defaults_file.name,
                                    'path': f'defaults/{defaults_file.name}',
                                    'content': defaults_content
                                })
                                
                                # Парсим inline комментарии из этого файла построчно
                                lines = defaults_content.split('\n')
                                for line in lines:
                                    line_stripped = line.strip()
                                    # Пропускаем пустые строки и комментарии
                                    if not line_stripped or line_stripped.startswith('#'):
                                        continue
                                    
                                    # Ищем паттерн: var_name: value # комментарий
                                    # Учитываем что value может быть в кавычках
                                    if '#' in line_stripped:
                                        # Проверяем что # не внутри строки в кавычках
                                        quote_count_before_hash = line_stripped[:line_stripped.index('#')].count('"') + line_stripped[:line_stripped.index('#')].count("'")
                                        if quote_count_before_hash % 2 == 0:  # четное количество = # вне кавычек
                                            # Разделяем на ключ: значение и комментарий
                                            parts = line_stripped.split('#', 1)
                                            if len(parts) == 2:
                                                key_value_part = parts[0].strip()
                                                comment_part = parts[1].strip()
                                                # Извлекаем ключ из key_value_part
                                                key_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*:', key_value_part)
                                                if key_match:
                                                    var_name = key_match.group(1)
                                                    # Сохраняем описание только если его еще нет (первый файл имеет приоритет)
                                                    if var_name not in defaults_descriptions:
                                                        defaults_descriptions[var_name] = comment_part
                    
                    except yaml.YAMLError as e:
                        app.logger.error(f"Error parsing YAML in {defaults_file}: {e}")
                        # Продолжаем чтение других файлов
                        continue
                    except Exception as e:
                        app.logger.warning(f"Error reading {defaults_file}: {e}")
                        continue
                
                # Сохраняем объединенные defaults
                result['defaults'] = all_defaults
                result['defaultsFiles'] = defaults_files_info
                
                # Сохраняем порядок переменных из всех файлов (по порядку файлов)
                variable_order = []
                for file_info in defaults_files_info:
                    lines = file_info['content'].split('\n')
                    for line in lines:
                        line_stripped = line.strip()
                        if not line_stripped or line_stripped.startswith('#'):
                            continue
                        # Извлекаем имя переменной из строки
                        key_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*:', line_stripped)
                        if key_match:
                            var_name = key_match.group(1)
                            if var_name in all_defaults and var_name not in variable_order:
                                variable_order.append(var_name)
                
                # Если порядок не удалось извлечь, используем порядок из dict (Python 3.7+)
                if not variable_order:
                    variable_order = list(all_defaults.keys())
                
                # Определяем группы переменных по префиксам
                variable_groups = {
                    'connection': {
                        'name': '🔌 Connection',
                        'icon': 'fa-plug',
                        'prefixes': ['initial_', 'init_'],
                        'variables': []
                    },
                    'ssh': {
                        'name': '🔐 SSH and Security',
                        'icon': 'fa-shield-alt',
                        'prefixes': ['disable_', 'change_', 'new_ssh'],
                        'variables': []
                    },
                    'users': {
                        'name': '👥 Users',
                        'icon': 'fa-users',
                        'prefixes': ['system_user', 'change_root', 'new_root', 'enable_system', 'pub_keys'],
                        'variables': []
                    },
                    'system': {
                        'name': '⚙️ System Settings',
                        'icon': 'fa-cog',
                        'prefixes': ['hostname', 'configure_', 'additional_', 'default_', 'timezone', 'ntp_'],
                        'variables': []
                    },
                    'other': {
                        'name': '📋 General Settings',
                        'icon': 'fa-list',
                        'prefixes': [],
                        'variables': []
                    }
                }
                
                # Создаем переменные в правильном порядке и группируем
                for var_name in variable_order:
                    if var_name in ['_error']:
                        continue
                    
                    default_value = all_defaults[var_name]
                    
                    # Определяем файл(ы), где находится переменная
                    var_files = []
                    for file_info in defaults_files_info:
                        try:
                            file_data = yaml.safe_load(file_info['content']) or {}
                            if var_name in file_data:
                                var_files.append(file_info['path'])
                        except:
                            pass
                    
                    # Определяем тип переменной
                    var_type = 'string'
                    if isinstance(default_value, bool):
                        var_type = 'boolean'
                    elif isinstance(default_value, (int, float)):
                        var_type = 'number'
                    elif isinstance(default_value, str):
                        # Проверяем, является ли это секретным полем
                        var_name_lower = var_name.lower()
                        if any(keyword in var_name_lower for keyword in ['password', 'secret', 'token', 'key']):
                            var_type = 'secret'
                        else:
                            var_type = 'string'
                    elif isinstance(default_value, list):
                        var_type = 'array'
                    elif isinstance(default_value, dict):
                        var_type = 'object'
                    
                    var_info = {
                        'name': var_name,
                        'variable': var_name,  # Для совместимости
                        'type': var_type,
                        'defaultValue': default_value,
                        'description': defaults_descriptions.get(var_name, '') or 'No description',
                        'scope': 'group_vars',
                        'source': 'defaults',
                        'sourceFile': var_files[0] if var_files else (defaults_files_info[0]['path'] if defaults_files_info else 'defaults/unknown'),
                        'files': var_files if var_files else ([defaults_files_info[0]['path']] if defaults_files_info else [])
                    }
                    
                    result['variables'].append(var_info)
                    
                    # Группируем переменную
                    assigned = False
                    for group_key, group_info in variable_groups.items():
                        if group_key == 'other':
                            continue
                        for prefix in group_info['prefixes']:
                            if var_name.startswith(prefix) or prefix in var_name:
                                group_info['variables'].append(var_info)
                                assigned = True
                                break
                        if assigned:
                            break
                    
                    if not assigned:
                        variable_groups['other']['variables'].append(var_info)
                
                # Сохраняем группы (только непустые)
                result['variableGroups'] = {
                    k: {
                        'name': v['name'],
                        'icon': v['icon'],
                        'variables': v['variables']
                    }
                    for k, v in variable_groups.items()
                    if v['variables']
                }
        
        # ⚠️ vars НЕ читаем. Только defaults/main.yaml
        # Порядок переменных сохранен из defaults/main.yaml (не сортируем)
        
        return result
    except Exception as e:
        app.logger.error(f"Error getting role details {pack_id}/{role_name}: {e}", exc_info=True)
        return None


@app.route('/api/roles/<pack_id>/<role_name>', methods=['GET'])
@require_auth
def api_get_role_details(pack_id, role_name):
    """API: Получить детали роли"""
    try:
        project_id = get_project_id_from_request()
        details = get_role_details(pack_id, role_name, project_id)
        if details:
            return jsonify({'success': True, 'role': details})
        else:
            return jsonify({'success': False, 'error': 'Role not found'}), 404
    except Exception as e:
        app.logger.error(f"Error getting role details: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/roles/files/<path:role_path>', methods=['GET'])
@require_auth
def api_get_role_files(role_path):
    """API: Получить список всех файлов роли"""
    try:
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        # Логируем для отладки
        app.logger.info(f"[api_get_role_files] role_path={role_path}, project_id={project_id}")
        
        # Разбираем путь: может быть packId/roleName или просто roleName (для роли на первом уровне)
        parts = role_path.split('/')
        pack_id = None
        role_name = None
        
        if len(parts) == 1:
            # Роль на первом уровне: путь = roleName
            role_name = parts[0]
            pack_id = role_name  # Для роли на первом уровне pack_id = role_name
            app.logger.info(f"[api_get_role_files] Role at first level: role_name={role_name}")
        elif len(parts) >= 2:
            # Роль внутри pack: путь = packId/roleName
            pack_id = parts[0]
            role_name = '/'.join(parts[1:])  # На случай, если role_name содержит /
            app.logger.info(f"[api_get_role_files] Role in pack: pack_id={pack_id}, role_name={role_name}")
        else:
            app.logger.error(f"[api_get_role_files] Invalid role_path format: {role_path}")
            return jsonify({'success': False, 'error': f'Invalid role path: {role_path}'}), 400
        
        # Безопасная проверка пути
        if '..' in (pack_id or '') or '..' in (role_name or ''):
            return jsonify({'success': False, 'error': 'Invalid path'}), 400
        
        # Resolve roles_playbooks source - ALWAYS Project Storage
        try:
            source = resolve_project_source(project_id, 'repo')
            repo_path = source['rootPath']
            roles_storage_path = repo_path / 'roles'
        except Exception as e:
            app.logger.error(f"[api_get_role_files] Failed to resolve Project Storage: {e}")
            return jsonify({'success': False, 'error': f'Failed to resolve Project Storage: {e}'}), 500
        
        # Для роли на первом уровне путь = roles/roleName
        # Для роли внутри pack путь = roles/packId/roleName
        if len(parts) == 1:
            role_path_full = roles_storage_path / role_name
        else:
            role_path_full = roles_storage_path / pack_id / role_name
        
        app.logger.info(f"[api_get_role_files] Resolved role_path_full: {role_path_full}")
        
        if not role_path_full.exists() or not role_path_full.is_dir():
            app.logger.error(f"[api_get_role_files] Role not found: {role_path_full}")
            return jsonify({'success': False, 'error': 'Role not found'}), 404
        
        # Собираем все файлы рекурсивно
        files = []
        
        def collect_files(directory, base_path=''):
            """Рекурсивно собирает все файлы из директории"""
            try:
                for item in directory.iterdir():
                    if item.name.startswith('.'):
                        continue
                    
                    relative_path = f"{base_path}/{item.name}" if base_path else item.name
                    
                    if item.is_file():
                        files.append({
                            'path': relative_path,
                            'name': item.name,
                            'type': 'file',
                            'size': item.stat().st_size
                        })
                    elif item.is_dir():
                        collect_files(item, relative_path)
            except PermissionError:
                app.logger.warning(f"Permission denied accessing {directory}")
        
        collect_files(role_path_full)
        
        return jsonify({
            'success': True,
            'files': files,
            'pack': pack_id,
            'role': role_name
        })
    except Exception as e:
        app.logger.error(f"Error getting role files: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/roles/file/<pack_id>/<path:role_name>/<path:file_path>', methods=['GET'])
@require_auth
def api_get_role_file(pack_id, role_name, file_path):
    """API: Получить содержимое файла роли"""
    try:
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        # Логируем для отладки
        app.logger.info(f"[api_get_role_file] pack_id={pack_id}, role_name={role_name}, file_path={file_path}, project_id={project_id}")
        
        # Безопасная проверка пути
        if '..' in pack_id or '..' in role_name or '..' in file_path:
            return jsonify({'success': False, 'error': 'Invalid path'}), 400
        
        # Resolve roles_playbooks source - ALWAYS Project Storage
        try:
            source = resolve_project_source(project_id, 'repo')
            repo_path = source['rootPath']
            roles_storage_path = repo_path / 'roles'
        except Exception as e:
            app.logger.error(f"[api_get_role_file] Failed to resolve Project Storage: {e}")
            return jsonify({'success': False, 'error': f'Failed to resolve Project Storage: {e}'}), 500
        
        # Если pack_id == role_name, это роль на первом уровне
        # Путь должен быть: roles/role_name/file_path (без дублирования)
        if pack_id == role_name:
            # Роль на первом уровне
            file_path_full = roles_storage_path / role_name / file_path
            role_dir = roles_storage_path / role_name
            app.logger.info(f"[api_get_role_file] Role at first level: role_dir={role_dir}, file_path_full={file_path_full}")
        else:
            # Роль внутри pack
            file_path_full = roles_storage_path / pack_id / role_name / file_path
            role_dir = roles_storage_path / pack_id / role_name
            app.logger.info(f"[api_get_role_file] Role in pack: role_dir={role_dir}, file_path_full={file_path_full}")
        try:
            file_path_full.resolve().relative_to(role_dir.resolve())
        except ValueError:
            app.logger.error(f"[api_get_role_file] File path outside role directory: {file_path_full} not in {role_dir}")
            return jsonify({'success': False, 'error': 'File path outside role directory'}), 400
        
        if not file_path_full.exists():
            app.logger.error(f"[api_get_role_file] File not found: {file_path_full}")
            return jsonify({'success': False, 'error': f'File not found: {file_path}'}), 404
        
        if not file_path_full.is_file():
            app.logger.error(f"[api_get_role_file] Path is not a file: {file_path_full}")
            return jsonify({'success': False, 'error': f'Path is not a file: {file_path}'}), 400
        
        # Читаем содержимое файла
        try:
            with open(file_path_full, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # Пробуем как бинарный файл
            with open(file_path_full, 'rb') as f:
                content = f.read().decode('utf-8', errors='replace')
        except Exception as e:
            app.logger.error(f"Error reading file {file_path_full}: {e}")
            return jsonify({'success': False, 'error': f'Error reading file: {e}'}), 500
        
        vault_id = request.args.get('vault_id') or request.args.get('vaultId')
        resp = {'success': True, 'content': content, 'path': file_path, 'pack': pack_id, 'role': role_name}
        decrypted, was_encrypted, vault_id_used, vault_id_required = _decrypt_content_if_encrypted(project_id, content, vault_id)
        if was_encrypted:
            if vault_id_required:
                vaults = _load_vaults(project_id)
                return jsonify({
                    'success': True,
                    'encrypted': True,
                    'vaultIdRequired': True,
                    'vaults': vaults,
                    'path': file_path,
                    'pack': pack_id,
                    'role': role_name
                })
            if decrypted is None:
                return jsonify({'success': False, 'error': 'Failed to decrypt vault-encrypted file. Check vault key.'}), 500
            resp['content'] = decrypted
            resp['encrypted'] = True
            resp['vaultId'] = vault_id_used
        
        return jsonify(resp)
    except Exception as e:
        app.logger.error(f"Error getting role file: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/roles/file/<pack_id>/<path:role_name>/<path:file_path>', methods=['PUT'])
@require_auth
def api_save_role_file(pack_id, role_name, file_path):
    """API: Сохранить содержимое файла роли"""
    try:
        project_id = get_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        # Логируем для отладки
        app.logger.info(f"[api_save_role_file] pack_id={pack_id}, role_name={role_name}, file_path={file_path}, project_id={project_id}")
        
        # Безопасная проверка пути
        if '..' in pack_id or '..' in role_name or '..' in file_path:
            return jsonify({'success': False, 'error': 'Invalid path'}), 400
        
        # Получаем содержимое из запроса
        data = request.get_json()
        if not data or 'content' not in data:
            return jsonify({'success': False, 'error': 'Content is required'}), 400
        
        content = data['content']
        vault_id = data.get('vaultId') or data.get('vault_id')
        
        # Ansible-vault: шифрование при наличии vault_id
        if vault_id:
            encrypted_content, err = _encrypt_content_if_requested(project_id, content, vault_id)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            content = encrypted_content
        
        # Resolve roles_playbooks source - ALWAYS Project Storage
        try:
            source = resolve_project_source(project_id, 'repo')
            repo_path = source['rootPath']
            roles_storage_path = repo_path / 'roles'
        except Exception as e:
            app.logger.error(f"[api_save_role_file] Failed to resolve Project Storage: {e}")
            return jsonify({'success': False, 'error': f'Failed to resolve Project Storage: {e}'}), 500
        
        # Если pack_id == role_name, это роль на первом уровне
        # Путь должен быть: roles/role_name/file_path (без дублирования)
        if pack_id == role_name:
            # Роль на первом уровне
            file_path_full = roles_storage_path / role_name / file_path
            role_dir = roles_storage_path / role_name
            app.logger.info(f"[api_save_role_file] Role at first level: role_dir={role_dir}, file_path_full={file_path_full}")
        else:
            # Роль внутри pack
            file_path_full = roles_storage_path / pack_id / role_name / file_path
            role_dir = roles_storage_path / pack_id / role_name
            app.logger.info(f"[api_save_role_file] Role in pack: role_dir={role_dir}, file_path_full={file_path_full}")
        
        # Проверяем, что файл находится внутри роли (безопасность)
        try:
            file_path_full.resolve().relative_to(role_dir.resolve())
        except ValueError:
            app.logger.error(f"[api_save_role_file] File path outside role directory: {file_path_full} not in {role_dir}")
            return jsonify({'success': False, 'error': 'File path outside role directory'}), 400
        
        # Создаем директорию, если её нет
        file_path_full.parent.mkdir(parents=True, exist_ok=True)
        
        # Сохраняем файл
        try:
            with open(file_path_full, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            app.logger.error(f"Error writing file {file_path_full}: {e}")
            return jsonify({'success': False, 'error': f'Error writing file: {e}'}), 500
        
        return jsonify({
            'success': True,
            'path': file_path,
            'pack': pack_id,
            'role': role_name
        })
    except Exception as e:
        app.logger.error(f"Error saving role file: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# PROJECT API ENDPOINTS
# ============================================================================

@app.route('/api/projects', methods=['GET'])
@require_auth
def api_list_projects():
    """API: Получить список всех проектов"""
    try:
        projects = load_projects()
        # Фильтруем архивные проекты (можно добавить параметр include_archived)
        include_archived = request.args.get('include_archived', 'false').lower() == 'true'
        if not include_archived:
            projects = [p for p in projects if not p.get('isArchived', False)]
        
        return jsonify({'success': True, 'projects': projects})
    except Exception as e:
        app.logger.error(f"Error listing projects: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects', methods=['POST'])
@require_auth
def api_create_project():
    """API: Создать новый проект
    
    Creates a truly empty project with:
    - All required directories (mkdir only, no content copied)
    - project.json with explicit defaults for sources (local mode)
    - Empty .sync_state.json
    - No fallback to BASE_DIR or Default Project data
    """
    try:
        data = request.json or {}
        
        # Валидация
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'Project name is required'}), 400
        
        # Проверка уникальности имени
        projects = load_projects()
        if any(p.get('name') == name and not p.get('isArchived', False) for p in projects):
            return jsonify({'success': False, 'error': 'Project with this name already exists'}), 400
        
        # Создаем проект
        project = {
            'id': str(uuid.uuid4()),
            'name': name,
            'description': data.get('description', '').strip(),
            'createdAt': time.time(),
            'updatedAt': time.time(),
            'createdBy': data.get('createdBy', 'current_user'),  # TODO: из сессии
            'isArchived': False
        }
        
        projects.append(project)
        save_projects(projects)
        
        # Создаем директорию проекта
        project_dir = get_project_dir(project['id'])
        project_dir.mkdir(exist_ok=True)
        
        # Create new structure directories
        # repo/ - Git-synced workspace (человеческий слой)
        repo_dir = project_dir / 'repo'
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / 'roles').mkdir(exist_ok=True)
        (repo_dir / 'playbooks').mkdir(exist_ok=True)
        (repo_dir / 'inventories').mkdir(exist_ok=True)
        (repo_dir / 'group_vars').mkdir(exist_ok=True)  # optional shared
        (repo_dir / 'host_vars').mkdir(exist_ok=True)    # optional shared
        (repo_dir / 'scripts').mkdir(exist_ok=True)
        
        # НЕ создаем папки prod/stage автоматически - они должны создаваться только пользователем или из Git
        
        # Единственный ansible.cfg: только в ansible-config/
        ansible_config_dir = project_dir / 'ansible-config'
        ansible_config_dir.mkdir(parents=True, exist_ok=True)
        default_ansible_cfg = ansible_config_dir / 'ansible.cfg'
        if not default_ansible_cfg.exists():
            create_minimal_ansible_config(default_ansible_cfg)
            app.logger.info(f"Created ansible-config/ansible.cfg for project {project['id']}")
        
        # ui/ - UI definitions (не Ansible)
        ui_dir = project_dir / 'ui'
        ui_dir.mkdir(exist_ok=True)
        (ui_dir / 'playbooks').mkdir(exist_ok=True)
        # Create empty folders.json
        folders_json = ui_dir / 'folders.json'
        if not folders_json.exists():
            with open(folders_json, 'w', encoding='utf-8') as f:
                json.dump({}, f, indent=2, ensure_ascii=False)
        
        # runtime/ - generated / ephemeral
        runtime_dir = project_dir / 'runtime'
        runtime_dir.mkdir(exist_ok=True)
        (runtime_dir / 'generated_playbooks').mkdir(exist_ok=True)
        (runtime_dir / 'inventory_snapshots').mkdir(exist_ok=True)
        (runtime_dir / 'artifacts').mkdir(exist_ok=True)
        
        # history/ - immutable execution history
        history_dir = project_dir / 'history'
        history_dir.mkdir(exist_ok=True)
        (history_dir / 'executions').mkdir(exist_ok=True)
        (history_dir / 'logs').mkdir(exist_ok=True)
        
        # secrets/ - credentials store (НЕ в Git)
        secrets_dir = project_dir / 'secrets'
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / 'ssh_keys').mkdir(exist_ok=True)
        (secrets_dir / 'vault').mkdir(exist_ok=True)
        (secrets_dir / 'vault_keys').mkdir(exist_ok=True)
        (secrets_dir / 'git_auth').mkdir(exist_ok=True)
        
        # Initialize project.json with new structure: single repo source
        # repo/ - единый Git-synced workspace
        initial_config = {
            'sources': {
                'repo': {
                    'mode': 'local',
                    'localPath': 'repo',
                    'syncDirection': 'pull',  # По умолчанию: Pull only (Git → Project Storage)
                    'syncStatus': {
                        'push': {'status': 'idle', 'lastSyncAt': None, 'error': None},
                        'pull': {'status': 'idle', 'lastSyncAt': None, 'error': None}
                    },
                    'syncState': {
                        'lastPullAt': None,
                        'lastPullStatus': 'idle',
                        'lastPullRevision': None,
                        'lastPullError': None,
                        'lastPushAt': None,
                        'lastPushStatus': 'idle',
                        'lastPushRevision': None,
                        'lastPushError': None
                    }
                }
            },
            'syncTimestamps': {},
            'syncStatus': {}
        }
        save_project_config(project['id'], initial_config)
        
        # Initialize .sync_state.json as empty file
        sync_state_file = project_dir / '.sync_state.json'
        if not sync_state_file.exists():
            with open(sync_state_file, 'w', encoding='utf-8') as f:
                json.dump({}, f, indent=2, ensure_ascii=False)
        
        app.logger.info(f"Project created: {project['id']} - {project['name']} (empty, deterministic)")
        return jsonify({'success': True, 'project': project})
    except Exception as e:
        app.logger.error(f"Error creating project: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>', methods=['GET'])
@require_auth
def api_get_project(project_id):
    """API: Получить проект по ID"""
    try:
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        return jsonify({'success': True, 'project': project})
    except Exception as e:
        app.logger.error(f"Error getting project: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>', methods=['PUT'])
@require_auth
def api_update_project(project_id):
    """API: Обновить проект"""
    try:
        data = request.json or {}
        projects = load_projects()
        
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Обновляем поля
        if 'name' in data:
            new_name = data['name'].strip()
            if not new_name:
                return jsonify({'success': False, 'error': 'Project name cannot be empty'}), 400
            
            # Проверка уникальности (кроме текущего проекта)
            if any(p.get('id') != project_id and p.get('name') == new_name and not p.get('isArchived', False) for p in projects):
                return jsonify({'success': False, 'error': 'Project with this name already exists'}), 400
            
            project['name'] = new_name
        
        if 'description' in data:
            project['description'] = data['description'].strip()
        
        if 'isArchived' in data:
            project['isArchived'] = bool(data['isArchived'])
        
        project['updatedAt'] = time.time()
        
        save_projects(projects)
        app.logger.info(f"Project updated: {project_id}")
        return jsonify({'success': True, 'project': project})
    except Exception as e:
        app.logger.error(f"Error updating project: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Vault Keys API (secrets/vault_keys)
# ============================================================================

@app.route('/api/projects/<project_id>/vault-keys', methods=['GET'])
@require_auth
def api_list_vault_keys(project_id):
    """API: Список vault keys проекта (метаданные без пароля)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vault_keys_dir = get_project_vault_keys_dir(project_id)
        keys = []
        for meta_file in vault_keys_dir.glob('*.json'):
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                keys.append({
                    'id': meta.get('id'),
                    'name': meta.get('name', ''),
                    'type': meta.get('type', 'vault_password'),
                    'createdAt': meta.get('createdAt'),
                    'updatedAt': meta.get('updatedAt')
                })
            except Exception as e:
                app.logger.warning(f"Error reading vault key {meta_file}: {e}")
                continue

        keys.sort(key=lambda k: (k.get('name') or '').lower())
        return jsonify({'success': True, 'keys': keys})
    except Exception as e:
        app.logger.error(f"Error listing vault keys: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vault-keys', methods=['POST'])
@require_auth
def api_create_vault_key(project_id):
    """API: Создать vault key"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        data = request.json or {}
        name = (data.get('name') or '').strip()
        password = data.get('password', '').strip()
        key_type = data.get('type', 'vault_password')

        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
        if not password:
            return jsonify({'success': False, 'error': 'Password is required'}), 400

        vault_keys_dir = get_project_vault_keys_dir(project_id)
        key_id = str(uuid.uuid4())
        meta_file = vault_keys_dir / f'{key_id}.json'
        pass_file = vault_keys_dir / f'{key_id}.pass'

        now = time.time()
        meta = {
            'id': key_id,
            'name': name,
            'type': key_type,
            'createdAt': now,
            'updatedAt': now
        }
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        with open(pass_file, 'w', encoding='utf-8') as f:
            f.write(password)
            if not password.endswith('\n'):
                f.write('\n')
        os.chmod(pass_file, 0o600)

        app.logger.info(f"Vault key created: {key_id} in project {project_id}")
        return jsonify({'success': True, 'key': meta})
    except Exception as e:
        app.logger.error(f"Error creating vault key: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vault-keys/<key_id>', methods=['GET'])
@require_auth
def api_get_vault_key(project_id, key_id):
    """API: Получить vault key (метаданные, без пароля)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vault_keys_dir = get_project_vault_keys_dir(project_id)
        meta_file = vault_keys_dir / f'{key_id}.json'
        if not meta_file.exists():
            return jsonify({'success': False, 'error': 'Key not found'}), 404

        with open(meta_file, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        return jsonify({'success': True, 'key': meta})
    except Exception as e:
        app.logger.error(f"Error getting vault key: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vault-keys/<key_id>', methods=['PUT'])
@require_auth
def api_update_vault_key(project_id, key_id):
    """API: Обновить vault key (имя, тип, пароль)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vault_keys_dir = get_project_vault_keys_dir(project_id)
        meta_file = vault_keys_dir / f'{key_id}.json'
        pass_file = vault_keys_dir / f'{key_id}.pass'
        if not meta_file.exists():
            return jsonify({'success': False, 'error': 'Key not found'}), 404

        data = request.json or {}
        with open(meta_file, 'r', encoding='utf-8') as f:
            meta = json.load(f)

        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'success': False, 'error': 'Name cannot be empty'}), 400
            meta['name'] = name
        if 'type' in data:
            meta['type'] = data.get('type', 'vault_password')
        if 'password' in data:
            password = data.get('password', '').strip()
            if password:
                with open(pass_file, 'w', encoding='utf-8') as f:
                    f.write(password)
                    if not password.endswith('\n'):
                        f.write('\n')
                os.chmod(pass_file, 0o600)

        meta['updatedAt'] = time.time()
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        app.logger.info(f"Vault key updated: {key_id} in project {project_id}")
        return jsonify({'success': True, 'key': meta})
    except Exception as e:
        app.logger.error(f"Error updating vault key: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vault-keys/<key_id>', methods=['DELETE'])
@require_auth
def api_delete_vault_key(project_id, key_id):
    """API: Удалить vault key"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vault_keys_dir = get_project_vault_keys_dir(project_id)
        meta_file = vault_keys_dir / f'{key_id}.json'
        pass_file = vault_keys_dir / f'{key_id}.pass'
        if not meta_file.exists():
            return jsonify({'success': False, 'error': 'Key not found'}), 404

        # Проверка: ключ используется в vaults?
        vaults_file = get_project_vaults_file(project_id)
        if vaults_file.exists():
            with open(vaults_file, 'r', encoding='utf-8') as f:
                vaults = json.load(f)
            if any(v.get('keyId') == key_id for v in vaults):
                return jsonify({'success': False, 'error': 'Key is used by one or more vaults. Remove vault bindings first.'}), 400

        meta_file.unlink()
        if pass_file.exists():
            pass_file.unlink()

        app.logger.info(f"Vault key deleted: {key_id} in project {project_id}")
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error deleting vault key: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Vaults API (secrets/vault/)
# ============================================================================

def _load_vault_file_keys(project_id):
    """Загружает маппинг path -> keyId для vault-файлов (сохраняется при Save с Key)."""
    f = get_project_vaults_file(project_id).parent / 'vault_file_keys.json'
    if not f.exists():
        return {}
    try:
        with open(f, 'r', encoding='utf-8') as fp:
            return json.load(fp)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_vault_file_key(project_id, path, key_id):
    """Сохраняет маппинг path -> keyId для vault-файла."""
    mapping = _load_vault_file_keys(project_id)
    if key_id:
        mapping[path] = key_id
    else:
        mapping.pop(path, None)
    f = get_project_vaults_file(project_id).parent / 'vault_file_keys.json'
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, 'w', encoding='utf-8') as fp:
        json.dump(mapping, fp, indent=2, ensure_ascii=False)


def _scan_repo_vault_files(project_id):
    """Сканирует group_vars, host_vars и vars/ в repo/, возвращает список объектов с path, key, created, updated."""
    repo_dir = get_project_dir(project_id) / 'repo'
    if not repo_dir.exists():
        return []
    vault_files = []
    try:
        for fp in repo_dir.rglob('*'):
            if not fp.is_file():
                continue
            parts = fp.relative_to(repo_dir).parts
            if 'group_vars' not in parts and 'host_vars' not in parts and 'vars' not in parts:
                continue
            try:
                with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read(4096)
                if not content.strip().startswith('$ANSIBLE_VAULT'):
                    continue
                rel = fp.relative_to(repo_dir)
                path_str = str(rel).replace('\\', '/')
                key_label = parse_vault_id_from_header(content) or None
                st = fp.stat()
                created_ts = getattr(st, 'st_birthtime', None) or st.st_ctime
                updated_ts = st.st_mtime
                vault_files.append({
                    'path': path_str,
                    'key': key_label,
                    'created': datetime.utcfromtimestamp(created_ts).isoformat() + 'Z',
                    'updated': datetime.utcfromtimestamp(updated_ts).isoformat() + 'Z',
                })
            except (IOError, OSError):
                continue
    except Exception as e:
        app.logger.warning(f"Error scanning repo for vault files: {e}")
    return sorted(vault_files, key=lambda x: x['path'])


def _load_vaults(project_id):
    """Загружает список vaults из secrets/vault/vaults.json (fallback: secrets/vaults.json)"""
    vaults_file = get_project_vaults_file(project_id)
    if not vaults_file.exists():
        old_path = get_project_secrets_dir(project_id) / 'vaults.json'
        if old_path.exists():
            vaults_file = old_path
        else:
            return []
    try:
        with open(vaults_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _enrich_vault_vault_id_from_content(vault):
    """
    Если vaultId пустой, но content зашифрован — вычитать vault ID из заголовка
    ($ANSIBLE_VAULT;1.1;AES256;777) и вернуть vault с заполненным vaultId для отображения.
    Не мутирует оригинал — возвращает dict с обогащёнными полями.
    """
    vid = (vault.get('vaultId') or '').strip()
    if vid:
        return vault
    content = vault.get('content') or ''
    if not content or not is_ansible_vault_encrypted(content):
        return vault
    parsed = parse_vault_id_from_header(content)
    if not parsed:
        return vault
    out = dict(vault)
    out['vaultId'] = parsed
    return out


def _save_vaults(project_id, vaults):
    """Сохраняет список vaults в secrets/vault/vaults.json"""
    vaults_file = get_project_vaults_file(project_id)
    vaults_file.parent.mkdir(parents=True, exist_ok=True)
    with open(vaults_file, 'w', encoding='utf-8') as f:
        json.dump(vaults, f, indent=2, ensure_ascii=False)


def get_key_password_for_vault(project_id, vault_uuid):
    """
    Получить пароль ключа по uuid vault.
    
    Args:
        project_id: ID проекта
        vault_uuid: внутренний id vault (uuid)
        
    Returns:
        Password string или None если vault/key не найден
    """
    vaults = _load_vaults(project_id)
    vault = next((v for v in vaults if v.get('id') == vault_uuid), None)
    if not vault:
        return None
    key_id = vault.get('keyId')
    if not key_id:
        return None
    return get_key_password_by_key_id(project_id, key_id)


def get_key_password_by_key_id(project_id, key_id):
    """Получить пароль ключа по key_id."""
    if not key_id:
        return None
    vault_keys_dir = get_project_vault_keys_dir(project_id)
    pass_file = vault_keys_dir / f'{key_id}.pass'
    if not pass_file.exists():
        return None
    try:
        with open(pass_file, 'r', encoding='utf-8') as f:
            return f.read().rstrip('\n')
    except Exception:
        return None


def _get_default_vault_password(project_id):
    """Legacy: пароль из secrets/vault/vault_pass (для файлов без vault_id в заголовке)"""
    vault_dir = get_project_secrets_dir(project_id) / 'vault'
    pass_file = vault_dir / 'vault_pass'
    if not pass_file.exists():
        return None
    try:
        with open(pass_file, 'r', encoding='utf-8') as f:
            return f.read().rstrip('\n')
    except Exception:
        return None


def _decrypt_content_if_encrypted(project_id, content, vault_id=None):
    """
    Расшифровать содержимое, если оно ansible-vault encrypted.
    Использует только указанный vault (ключ пользователя).
    
    Args:
        project_id: ID проекта
        content: содержимое файла
        vault_id: uuid vault — обязателен для зашифрованных файлов. Без него возвращается vaultIdRequired.
    
    Returns:
        (decrypted_content, encrypted: bool, vault_id_used, vault_id_required: bool)
        vault_id_required=True — файл зашифрован, vault_id не передан, нужен выбор пользователя
    """
    if not content or not is_ansible_vault_encrypted(content):
        return content, False, None, False
    if not vault_id:
        return None, True, None, True
    password = get_key_password_for_vault(project_id, vault_id)
    if not password:
        return None, True, None, False
    vault_id_header = parse_vault_id_from_header(content)
    use_vault_id = vault_id_header if vault_id_header else None
    ok, result = ansible_vault_decrypt(content, password, use_vault_id)
    if ok:
        return result, True, vault_id, False
    return None, True, None, False


def _encrypt_content_if_requested(project_id, content, vault_id):
    """
    Зашифровать содержимое, если указан vault_id (uuid vault).
    Использует vault name как ansible-vault label, чтобы Key отображался в таблице.
    """
    if not vault_id:
        return content, None
    password = get_key_password_for_vault(project_id, vault_id)
    if not password:
        return None, f'Vault or its key not found'
    vaults = _load_vaults(project_id)
    vault = next((v for v in vaults if v.get('id') == vault_id), None)
    vault_label = (vault.get('vaultId') or vault.get('name')) if vault else None
    ok, result = ansible_vault_encrypt(content, password, vault_id=vault_label)
    if not ok:
        return None, result or 'Encryption failed'
    return result, None


@app.route('/api/projects/<project_id>/vaults', methods=['GET'])
@require_auth
def api_list_vaults(project_id):
    """API: Список vaults проекта"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vaults = _load_vaults(project_id)
        vaults = [_enrich_vault_vault_id_from_content(v) for v in vaults]
        vault_keys_dir = get_project_vault_keys_dir(project_id)
        vault_files = _scan_repo_vault_files(project_id)

        for v in vaults:
            key_id = v.get('keyId')
            v['keyName'] = None
            if key_id:
                key_meta = vault_keys_dir / f'{key_id}.json'
                if key_meta.exists():
                    try:
                        with open(key_meta, 'r', encoding='utf-8') as f:
                            v['keyName'] = json.load(f).get('name')
                    except Exception:
                            pass

        vault_label_to_key = {}
        vault_label_to_id = {}
        for v in vaults:
            key_name = v.get('keyName')
            vid = v.get('id')
            for lbl in (v.get('vaultId'), v.get('name')):
                if lbl:
                    if key_name:
                        vault_label_to_key[lbl] = key_name
                    vault_label_to_id[lbl] = vid
        path_to_key = _load_vault_file_keys(project_id)
        for f in vault_files:
            label = f.get('key')
            path = f.get('path', '')
            key_name = vault_label_to_key.get(label) if label else None
            if not key_name and path:
                stored_key_id = path_to_key.get(path)
                if stored_key_id:
                    key_meta = vault_keys_dir / f'{stored_key_id}.json'
                    if key_meta.exists():
                        try:
                            with open(key_meta, 'r', encoding='utf-8') as kf:
                                key_name = json.load(kf).get('name')
                        except Exception:
                            pass
            f['keyName'] = key_name
            f['vaultId'] = vault_label_to_id.get(label) if label else None

        return jsonify({'success': True, 'vaults': vaults, 'vaultFiles': vault_files})
    except Exception as e:
        app.logger.error(f"Error listing vaults: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vaults', methods=['POST'])
@require_auth
def api_create_vault(project_id):
    """API: Создать vault и привязать к key"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        data = request.json or {}
        name = (data.get('name') or '').strip()
        key_id = data.get('keyId') or data.get('key_id', '').strip()
        vault_id_label = (data.get('vaultId') or data.get('vault_id') or '').strip() or None

        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
        if not key_id:
            return jsonify({'success': False, 'error': 'Key is required'}), 400

        vault_keys_dir = get_project_vault_keys_dir(project_id)
        key_meta = vault_keys_dir / f'{key_id}.json'
        if not key_meta.exists():
            return jsonify({'success': False, 'error': 'Key not found'}), 404

        vaults = _load_vaults(project_id)
        now = time.time()
        vault = {
            'id': str(uuid.uuid4()),
            'name': name,
            'keyId': key_id,
            'vaultId': vault_id_label,
            'createdAt': now,
            'updatedAt': now
        }
        vaults.append(vault)
        _save_vaults(project_id, vaults)

        app.logger.info(f"Vault created: {vault['id']} in project {project_id}")
        return jsonify({'success': True, 'vault': vault})
    except Exception as e:
        app.logger.error(f"Error creating vault: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vaults/<vault_id>', methods=['GET'])
@require_auth
def api_get_vault(project_id, vault_id):
    """API: Получить vault по ID"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vaults = _load_vaults(project_id)
        vault = next((v for v in vaults if v.get('id') == vault_id), None)
        if not vault:
            return jsonify({'success': False, 'error': 'Vault not found'}), 404

        vault = _enrich_vault_vault_id_from_content(vault)
        return jsonify({'success': True, 'vault': vault})
    except Exception as e:
        app.logger.error(f"Error getting vault: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vaults/<vault_id>', methods=['PUT'])
@require_auth
def api_update_vault(project_id, vault_id):
    """API: Обновить vault"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vaults = _load_vaults(project_id)
        vault = next((v for v in vaults if v.get('id') == vault_id), None)
        if not vault:
            return jsonify({'success': False, 'error': 'Vault not found'}), 404

        data = request.json or {}
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'success': False, 'error': 'Name cannot be empty'}), 400
            vault['name'] = name
        if 'keyId' in data or 'key_id' in data:
            new_key_id = data.get('keyId') or data.get('key_id', '').strip()
            if new_key_id:
                vault_keys_dir = get_project_vault_keys_dir(project_id)
                if not (vault_keys_dir / f'{new_key_id}.json').exists():
                    return jsonify({'success': False, 'error': 'Key not found'}), 404
                vault['keyId'] = new_key_id
        if 'vaultId' in data:
            vault['vaultId'] = (data.get('vaultId') or '').strip() or None
        if 'content' in data:
            vault['content'] = data.get('content') or ''

        vault['updatedAt'] = time.time()
        _save_vaults(project_id, vaults)

        app.logger.info(f"Vault updated: {vault_id} in project {project_id}")
        return jsonify({'success': True, 'vault': vault})
    except Exception as e:
        app.logger.error(f"Error updating vault: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vaults/<vault_id>', methods=['DELETE'])
@require_auth
def api_delete_vault(project_id, vault_id):
    """API: Удалить vault"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        vaults = _load_vaults(project_id)
        vault = next((v for v in vaults if v.get('id') == vault_id), None)
        if not vault:
            return jsonify({'success': False, 'error': 'Vault not found'}), 404

        vaults = [v for v in vaults if v.get('id') != vault_id]
        _save_vaults(project_id, vaults)

        app.logger.info(f"Vault deleted: {vault_id} in project {project_id}")
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error deleting vault: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vaults/<vault_id>/encrypt', methods=['POST'])
@require_auth
def api_vault_encrypt(project_id, vault_id):
    """API: Зашифровать содержимое vault'ом (ansible-vault на backend)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        data = request.json or {}
        content = data.get('content', '')
        encrypted, err = _encrypt_content_if_requested(project_id, content, vault_id)
        if err:
            return jsonify({'success': False, 'error': err}), 400
        return jsonify({'success': True, 'content': encrypted})
    except Exception as e:
        app.logger.error(f"Error encrypting: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vaults/<vault_id>/decrypt', methods=['POST'])
@require_auth
def api_vault_decrypt(project_id, vault_id):
    """API: Расшифровать содержимое vault'ом (ansible-vault на backend)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        data = request.json or {}
        content = data.get('content', '')
        decrypted, was_enc, _, _ = _decrypt_content_if_encrypted(project_id, content, vault_id)
        if was_enc and decrypted is None:
            return jsonify({'success': False, 'error': 'Failed to decrypt. Check vault key.'}), 400
        return jsonify({'success': True, 'content': decrypted or content})
    except Exception as e:
        app.logger.error(f"Error decrypting: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _resolve_vault_file_path(project_id, path_param):
    """Проверяет путь и возвращает полный Path к файлу в repo. Path должен быть в group_vars или host_vars."""
    if not path_param or '..' in path_param:
        return None, 'Invalid path'
    path_param = path_param.replace('\\', '/').strip('/')
    parts = path_param.split('/')
    if 'group_vars' not in parts and 'host_vars' not in parts:
        return None, 'Path must be within group_vars or host_vars'
    repo_dir = get_project_dir(project_id) / 'repo'
    full_path = (repo_dir / path_param).resolve()
    try:
        full_path.relative_to(repo_dir.resolve())
    except ValueError:
        return None, 'Path outside repo'
    return full_path, None


@app.route('/api/projects/<project_id>/vault-files/get', methods=['GET'])
@require_auth
def api_vault_file_get(project_id):
    """API: Получить содержимое vault-файла по пути (group_vars/host_vars)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        path_param = request.args.get('path', '')
        full_path, err = _resolve_vault_file_path(project_id, path_param)
        if err:
            return jsonify({'success': False, 'error': err}), 400
        if not full_path.exists() or not full_path.is_file():
            return jsonify({'success': False, 'error': 'File not found'}), 404
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        vault_id = request.args.get('vault_id') or request.args.get('vaultId')
        key_id = request.args.get('keyId') or request.args.get('key_id')
        resp = {'success': True, 'content': content, 'path': path_param}
        if key_id and content and is_ansible_vault_encrypted(content):
            password = get_key_password_by_key_id(project_id, key_id)
            if not password:
                return jsonify({'success': False, 'error': 'Key not found'}), 404
            vault_id_header = parse_vault_id_from_header(content)
            ok, decrypted = ansible_vault_decrypt(content, password, vault_id=vault_id_header)
            if not ok:
                return jsonify({'success': False, 'error': decrypted or 'Failed to decrypt'}), 500
            resp['content'] = decrypted
            resp['keyId'] = key_id
        else:
            decrypted, was_encrypted, vault_id_used, vault_id_required = _decrypt_content_if_encrypted(project_id, content, vault_id)
            if was_encrypted:
                if vault_id_required:
                    vaults = _load_vaults(project_id)
                    keys = []
                    try:
                        vault_keys_dir = get_project_vault_keys_dir(project_id)
                        for meta_file in vault_keys_dir.glob('*.json'):
                            with open(meta_file, 'r', encoding='utf-8') as f:
                                meta = json.load(f)
                            kid = meta.get('id') or meta_file.stem
                            keys.append({'id': kid, 'name': meta.get('name', '')})
                    except Exception:
                        pass
                    return jsonify({
                        'success': True, 'encrypted': True, 'vaultIdRequired': True,
                        'vaults': vaults, 'keys': keys, 'path': path_param, 'content': content
                    })
                if decrypted is None:
                    return jsonify({'success': False, 'error': 'Failed to decrypt'}), 500
                resp['content'] = decrypted
                resp['encrypted'] = True
                resp['vaultId'] = vault_id_used
        return jsonify(resp)
    except Exception as e:
        app.logger.error(f"Error getting vault file: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vault-files/encrypt', methods=['POST'])
@require_auth
def api_vault_file_encrypt(project_id):
    """API: Зашифровать контент по keyId + vaultId (ansible-vault label)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        data = request.json or {}
        content = data.get('content', '')
        key_id = data.get('keyId') or data.get('key_id')
        vault_label = (data.get('vaultId') or data.get('vault_id') or '').strip() or None
        if not key_id:
            return jsonify({'success': False, 'error': 'keyId required'}), 400
        if not vault_label:
            return jsonify({'success': False, 'error': 'vaultId (ansible-vault label) required'}), 400
        password = get_key_password_by_key_id(project_id, key_id)
        if not password:
            return jsonify({'success': False, 'error': 'Key not found'}), 404
        ok, result = ansible_vault_encrypt(content, password, vault_id=vault_label)
        if not ok:
            return jsonify({'success': False, 'error': result or 'Encryption failed'}), 400
        return jsonify({'success': True, 'content': result})
    except Exception as e:
        app.logger.error(f"Error encrypting vault file: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vault-files/decrypt', methods=['POST'])
@require_auth
def api_vault_file_decrypt(project_id):
    """API: Расшифровать контент по keyId"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        data = request.json or {}
        content = data.get('content', '')
        key_id = data.get('keyId') or data.get('key_id')
        if not key_id:
            return jsonify({'success': False, 'error': 'keyId required'}), 400
        if not content or not is_ansible_vault_encrypted(content):
            return jsonify({'success': True, 'content': content})
        password = get_key_password_by_key_id(project_id, key_id)
        if not password:
            return jsonify({'success': False, 'error': 'Key not found'}), 404
        vault_id_header = parse_vault_id_from_header(content)
        ok, result = ansible_vault_decrypt(content, password, vault_id=vault_id_header)
        if not ok:
            return jsonify({'success': False, 'error': result or 'Decryption failed'}), 400
        return jsonify({'success': True, 'content': result})
    except Exception as e:
        app.logger.error(f"Error decrypting vault file: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/vault-files/save', methods=['POST'])
@require_auth
def api_vault_file_save(project_id):
    """API: Сохранить vault-файл (с опциональным шифрованием)"""
    try:
        projects = load_projects()
        if not any(p.get('id') == project_id for p in projects):
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        data = request.json or {}
        path_param = data.get('path', '')
        content = data.get('content', '')
        vault_id = data.get('vaultId') or data.get('vault_id')
        key_id = data.get('keyId') or data.get('key_id')
        full_path, err = _resolve_vault_file_path(project_id, path_param)
        if err:
            return jsonify({'success': False, 'error': err}), 400
        if key_id and vault_id:
            password = get_key_password_by_key_id(project_id, key_id)
            if not password:
                return jsonify({'success': False, 'error': 'Key not found'}), 404
            ok, encrypted = ansible_vault_encrypt(content, password, vault_id=vault_id)
            if not ok:
                return jsonify({'success': False, 'error': encrypted or 'Encryption failed'}), 400
            content = encrypted
        elif vault_id:
            encrypted, err = _encrypt_content_if_requested(project_id, content, vault_id)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            content = encrypted
        if key_id:
            _save_vault_file_key(project_id, path_param, key_id)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True, 'path': path_param})
    except Exception as e:
        app.logger.error(f"Error saving vault file: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Project Sources API
# ============================================================================

def load_project_config(project_id):
    """Загружает конфигурацию проекта из project.json"""
    config_file = get_project_config_file(project_id)
    if not config_file.exists():
        return {}
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        app.logger.error(f"Invalid JSON in project config {project_id}: {e}")
        return {}
    except Exception as e:
        app.logger.error(f"Error loading project config {project_id}: {e}")
        return {}


def save_project_config(project_id, config):
    """Сохраняет конфигурацию проекта в project.json
    
    Raises:
        IOError: If file write fails
        TypeError: If config contains non-serializable data
    """
    config_file = get_project_config_file(project_id)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        app.logger.info(f"Project config saved: {project_id}")
    except IOError as e:
        app.logger.error(f"Failed to write project config file {config_file}: {e}", exc_info=True)
        raise IOError(f"Failed to save project config: {e}") from e
    except TypeError as e:
        app.logger.error(f"Failed to serialize project config for {project_id}: {e}. Config contains non-serializable data.", exc_info=True)
        raise TypeError(f"Failed to serialize project config: {e}") from e


@app.route('/api/projects/<project_id>/hosts_status', methods=['GET'])
@require_auth
def api_get_project_hosts_status(project_id):
    """Лёгкий API только статусов хостов для опроса фронтом (без полного get_all_data)."""
    try:
        hosts = get_project_hosts_list(project_id)
        host_statuses = {}
        for host_name in hosts:
            st = get_host_check_status(project_id, host_name)
            if st:
                host_statuses[host_name] = {
                    'status': st.get('status', 'unknown'),
                    'last_checked_at': st.get('last_checked_at'),
                    'status_expires_at': st.get('status_expires_at'),
                }
            else:
                host_statuses[host_name] = {'status': 'unknown', 'last_checked_at': None, 'status_expires_at': None}
        return jsonify({'success': True, 'hosts_status': host_statuses})
    except Exception as e:
        app.logger.warning(f"[api_get_project_hosts_status] {project_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/host_settings', methods=['GET'])
@require_auth
def api_get_project_host_settings(project_id):
    """API: Получить настройки статуса хостов для проекта (TTL, авто-проверка)."""
    try:
        config = load_project_config(project_id) or {}
        host_status_cfg = config.get('host_status', {})
        ttl = host_status_cfg.get('ttl_seconds')
        if ttl is None:
            # Обратная совместимость: читаем возможный старый ключ на верхнем уровне
            ttl = config.get('host_status_ttl_seconds', HOST_STATUS_TTL_DEFAULT)
        auto_check = bool(host_status_cfg.get('auto_check_all_hosts', False))
        return jsonify({
            'success': True,
            'settings': {
                'ttl_seconds': ttl,
                'auto_check_all_hosts': auto_check,
            }
        })
    except Exception as e:
        app.logger.error(f"Error getting host settings for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/host_settings', methods=['PUT'])
@require_auth
def api_update_project_host_settings(project_id):
    """API: Обновить настройки статуса хостов для проекта (TTL, авто-проверка)."""
    try:
        data = request.json or {}
        config = load_project_config(project_id) or {}
        host_status_cfg = config.get('host_status', {})

        if 'ttl_seconds' in data:
            try:
                ttl_seconds = int(data['ttl_seconds'])
            except (TypeError, ValueError):
                ttl_seconds = HOST_STATUS_TTL_DEFAULT
            # Ограничиваем разумными границами
            if ttl_seconds < 30:
                ttl_seconds = 30
            elif ttl_seconds > 24 * 60 * 60:
                ttl_seconds = 24 * 60 * 60
            host_status_cfg['ttl_seconds'] = ttl_seconds

        if 'auto_check_all_hosts' in data:
            host_status_cfg['auto_check_all_hosts'] = bool(data['auto_check_all_hosts'])

        config['host_status'] = host_status_cfg
        save_project_config(project_id, config)

        return jsonify({'success': True, 'settings': host_status_cfg})
    except Exception as e:
        app.logger.error(f"Error updating host settings for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error saving project config for {project_id}: {e}", exc_info=True)
        raise


def resolve_connection_secret_for_host(project_id: str, host_name: str):
    """
    Возвращает (secret_name or None, has_connection) для хоста из host_vars.
    Используется бэкендом для авто-проверки без запроса от фронта.
    """
    if not project_id or not host_name:
        return None, False
    try:
        host_vars_dir = get_project_host_vars_dir(project_id)
        host_file = host_vars_dir / f"{host_name}.yml"
        if host_file.exists():
            with open(host_file, 'r', encoding='utf-8') as f:
                host_vars = yaml_loader.load(f) or {}
            if host_vars.get('connectionSecret'):
                return host_vars['connectionSecret'], True
            if host_vars.get('ansible_ssh_private_key_file') or host_vars.get('ansible_password'):
                return host_vars.get('connectionSecret'), True
        return None, False
    except Exception as e:
        app.logger.debug(f"[resolve_connection_secret_for_host] {host_name}: {e}")
        return None, False


def get_projects_with_auto_check_hosts():
    """Список project_id, у которых включён auto_check_all_hosts."""
    result = []
    try:
        for entry in PROJECTS_DIR.iterdir():
            if not entry.is_dir():
                continue
            project_id = entry.name
            config = load_project_config(project_id) or {}
            if config.get('host_status', {}).get('auto_check_all_hosts'):
                result.append(project_id)
    except Exception as e:
        app.logger.warning(f"[get_projects_with_auto_check_hosts] {e}")
    return result


def get_project_hosts_list(project_id: str):
    """Список имён хостов проекта из inventory (как в get_all_data)."""
    try:
        project_dir = get_project_dir(project_id)
        inventories_dir = get_project_inventories_dir(project_id)
        inventory_files_to_use = []
        for name in ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']:
            p = inventories_dir / name
            if p.exists():
                inventory_files_to_use.append(str(p))
        for inv_file in inventories_dir.rglob('*'):
            if inv_file.is_file() and inv_file.name in ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']:
                try:
                    rel = inv_file.relative_to(inventories_dir)
                except ValueError:
                    continue
                if 'group_vars' not in rel.parts and 'host_vars' not in rel.parts:
                    path_str = str(inv_file)
                    if path_str not in inventory_files_to_use:
                        inventory_files_to_use.append(path_str)
        if not inventory_files_to_use and (project_dir / 'repo' / 'inventories').exists():
            inv_dir = project_dir / 'repo' / 'inventories'
            for name in ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']:
                for p in inv_dir.rglob(name):
                    if p.is_file():
                        try:
                            r = p.relative_to(inv_dir)
                            if 'group_vars' not in r.parts and 'host_vars' not in r.parts:
                                inventory_files_to_use.append(str(p))
                                break
                        except ValueError:
                            pass
        hosts = get_inventory_hosts(inventory_files_to_use) if inventory_files_to_use else []
        return [h['name'] for h in hosts]
    except Exception as e:
        app.logger.warning(f"[get_project_hosts_list] {project_id}: {e}")
        return []


_host_status_scheduler_interval = 60  # seconds
_host_status_scheduler_thread = None


def _host_status_auto_check_loop():
    """Фоновый цикл: раз в N секунд запускает авто-чек по TTL для проектов с auto_check_all_hosts."""
    while True:
        try:
            time.sleep(_host_status_scheduler_interval)
            host_status_auto_check_tick()
        except Exception as e:
            app.logger.warning(f"[host_status_auto_check_loop] {e}")


def host_status_auto_check_tick():
    """
    Один проход: для проектов с auto_check_all_hosts ставит в очередь проверку хостов с истёкшим TTL.
    Одно задание на проект через /api/check_hosts (вместо множества /api/check_host).
    """
    for project_id in get_projects_with_auto_check_hosts():
        try:
            hosts = get_project_hosts_list(project_id)
            to_check = []
            connection_secrets = {}
            for host in hosts:
                # Берём статус из кэша/файла. Нам нужны только хосты,
                # у которых либо нет статуса вовсе, либо он истёк (expired),
                # либо статус явно unknown (например, после неудачной предыдущей попытки).
                st = get_host_check_status(project_id, host)
                if st:
                    is_expired = bool(st.get('expired'))
                    status_str = (st.get('status') or 'unknown').lower()
                    if not is_expired and status_str not in ('unknown',):
                        # Есть актуальный известный статус — не трогаем этот хост
                        continue
                secret_name, has_conn = resolve_connection_secret_for_host(project_id, host)
                if not has_conn and not secret_name:
                    continue
                to_check.append(host)
                if secret_name:
                    connection_secrets[host] = secret_name
            to_check = to_check[:20]
            if not to_check:
                continue
            internal_secret = os.environ.get('INTERNAL_CALL_SECRET', 'internal')
            try:
                with app.test_client() as client:
                    rv = client.post(
                        '/api/check_hosts',
                        json={
                            'hosts': to_check,
                            'project_id': project_id,
                            'connection_secrets': connection_secrets,
                            'inventory_files': ['inventory.yml'],
                            'ansible_config': 'ansible_local_execute.cfg',
                        },
                        headers={'Content-Type': 'application/json', 'X-Internal-Call': internal_secret},
                    )
                if rv.status_code == 200:
                    app.logger.info(f"[host_status_auto_check] Queued check for {project_id}: {len(to_check)} host(s)")
                else:
                    app.logger.warning(f"[host_status_auto_check] check_hosts returned {rv.status_code} for {project_id}")
            except Exception as e:
                app.logger.debug(f"[host_status_auto_check] {project_id}: {e}")
        except Exception as e:
            app.logger.warning(f"[host_status_auto_check_tick] project {project_id}: {e}")


# Host status auto-check background thread (управление TTL и авто-проверкой на бэкенде)
def _start_host_status_scheduler():
    global _host_status_scheduler_thread
    if _host_status_scheduler_thread is not None:
        return
    _host_status_scheduler_thread = threading.Thread(target=_host_status_auto_check_loop, daemon=True)
    _host_status_scheduler_thread.start()
    app.logger.info("[host_status] Background auto-check scheduler started (interval=%ss)", _host_status_scheduler_interval)


# Initialize AutosyncScheduler (after load_project_config and save_project_config are defined)
if autosync_scheduler is None:
    from autosync_scheduler import AutosyncScheduler
    autosync_scheduler = AutosyncScheduler(
        source_sync_service=source_sync_service,
        load_project_config_func=load_project_config,
        get_project_dir_func=get_project_dir
    )
    # Set save function to avoid circular dependency
    autosync_scheduler._save_project_config = save_project_config
    # Start scheduler
    autosync_scheduler.start()
# Host status auto-check scheduler (всегда запускаем, если ещё не запущен)
_start_host_status_scheduler()


# Initialize PlaybookScheduler (after playbook_storage is defined)
playbook_scheduler = None

def _create_scheduled_run(project_id: str, playbook_id: str) -> Tuple[bool, Optional[str]]:
    """
    Helper function to create a run for scheduled playbook execution.
    Used by PlaybookScheduler.
    
    Returns:
        (success, error_message)
    """
    try:
        # Call the API function logic directly (without HTTP request)
        # This is essentially the same as api_run_playbook but without HTTP layer
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return False, 'Project not found'
        
        # Загружаем playbook
        playbook = playbook_storage.get_playbook(project_id, playbook_id)
        if not playbook:
            return False, 'Playbook not found'
        
        # Проверяем, не disabled ли playbook
        if playbook.get('disabled') or playbook.get('metadata', {}).get('disabled'):
            app.logger.info(f"[PlaybookScheduler] Skipping disabled playbook {playbook_id} in project {project_id}")
            return False, 'Playbook is disabled'
        
        # Автоматически определяем inventory_files
        inventory_files = find_inventory_files_for_playbook(project_id, playbook)
        if not inventory_files:
            inventory_files = ['inventory.yml']
        
        # Получаем default ansible_config из проекта
        project_dir = get_project_dir(project_id)
        ansible_config_dir = project_dir / 'ansible-config'
        ansible_config = None
        if ansible_config_dir.exists():
            primary = ansible_config_dir / 'ansible.cfg'
            if primary.exists():
                ansible_config = 'ansible-config/ansible.cfg'
            else:
                config_files = list(ansible_config_dir.glob('*.cfg'))
                if config_files:
                    ansible_config = f'ansible-config/{config_files[0].name}'
        
        if not ansible_config:
            app.logger.warning(f"[PlaybookScheduler] No ansible_config found for scheduled run of playbook {playbook_id}")
            return False, 'ansible_config is required'
        
        # Генерируем YAML из playbook
        try:
            yaml_content = playbook_generator.generate(playbook)
        except Exception as e:
            app.logger.error(f"[PlaybookScheduler] Error generating YAML from playbook {playbook_id}: {e}")
            return False, f'Failed to generate playbook YAML: {str(e)}'
        
        if not yaml_content or not isinstance(yaml_content, str) or not yaml_content.strip():
            return False, 'Playbook is empty'
        
        # Создаем execution record
        execution_id = str(uuid.uuid4())
        settings = load_execution_settings()
        
        if settings.get('save_history', True):
            # Получаем inventory snapshot
            inventory_snapshot = {'groups': []}
            try:
                repo_dir = project_dir / 'repo'
                inv_files_list = []
                for inv_file in inventory_files:
                    if inv_file.startswith('inventories/') or ('.' in inv_file and '/' in inv_file):
                        inv_path = repo_dir / inv_file
                    elif inv_file == 'inventory.yml':
                        inv_path = repo_dir / 'inventory.yml'
                    else:
                        inventories_dir = repo_dir / 'inventories'
                        if inventories_dir.exists():
                            found = False
                            for inv_file_search in inventories_dir.rglob(inv_file):
                                if inv_file_search.is_file():
                                    inv_path = inv_file_search
                                    found = True
                                    break
                            if not found:
                                inv_path = repo_dir / inv_file
                        else:
                            inv_path = repo_dir / inv_file
                    
                    if inv_path.exists():
                        inv_files_list.append(str(inv_path))
                
                if not inv_files_list:
                    default_inv = get_project_inventory_file(project_id)
                    if default_inv.exists():
                        inv_files_list = [str(default_inv)]
                
                if inv_files_list:
                    groups = get_inventory_groups(inv_files_list)
                    for group_name, group_data in groups.items():
                        group_hosts = group_data.get('hosts', [])
                        inventory_snapshot['groups'].append({
                            'groupId': group_name,
                            'groupName': group_name,
                            'hosts': [{'hostId': host, 'ip': host} for host in group_hosts]
                        })
            except Exception as e:
                app.logger.warning(f"[PlaybookScheduler] Error creating inventory snapshot: {e}")
            
            # Извлекаем hosts и roles из playbook
            all_hosts = set()
            all_roles = []
            for play in playbook.get('plays', []):
                hosts_value = play.get('hosts', '')
                if hosts_value and hosts_value != 'all':
                    if isinstance(hosts_value, list):
                        for host in hosts_value:
                            if host and host != 'all':
                                all_hosts.add(host)
                    elif isinstance(hosts_value, str) and hosts_value != 'all':
                        all_hosts.add(hosts_value)
                
                for role in play.get('roles', []):
                    if isinstance(role, dict):
                        role_name = role.get('role', '')
                    else:
                        role_name = str(role)
                    if role_name:
                        all_roles.append(role_name)
            
            # Стратегия из первого play (для worker: Mitogen strategy_plugins)
            plays_list = playbook.get('plays', [])
            strategy = plays_list[0].get('strategy', 'linear') if plays_list else 'linear'
            
            # Создаем runParams
            run_params = {
                'inventory_files': inventory_files,
                'ansible_config': ansible_config,
                'strategy': strategy,
            }
            
            execution_data = {
                'projectId': project_id,
                'playbook': {
                    'playbookId': playbook_id,
                    'playbookName': playbook.get('name', 'playbook')
                },
                'stats': {
                    'hostsTargeted': len(all_hosts) if all_hosts else 0,
                    'totalRoleExecutions': len(all_roles)
                },
                'warnings': [],
                'status': 'QUEUED',
                'runParams': run_params,
                'scheduled': True  # Mark as scheduled run
            }
            
            execution_id_created = create_execution_record(execution_data, project_id=project_id, execution_id=execution_id)
            if execution_id_created:
                execution_id = execution_id_created
                
                # Проверяем, что файл действительно создан и доступен
                executions_dir = get_project_executions_dir(project_id)
                execution_file = executions_dir / f'{execution_id}.json'
                if execution_file.exists():
                    # Читаем файл для проверки
                    try:
                        with open(execution_file, 'r', encoding='utf-8') as f:
                            created_execution = json.load(f)
                            status = created_execution.get('status', 'UNKNOWN')
                            queued_at = created_execution.get('queuedAt', 'NOT SET')
                            app.logger.info(f"[PlaybookScheduler] ✓ Execution file created: {execution_file}, status={status}, queuedAt={queued_at}")
                    except Exception as e:
                        app.logger.error(f"[PlaybookScheduler] Error reading created execution file: {e}")
                else:
                    app.logger.error(f"[PlaybookScheduler] ✗ Execution file NOT FOUND after creation: {execution_file}")
        
        if execution_id:
            executions_dir = get_project_executions_dir(project_id)
            app.logger.info(f"[PlaybookScheduler] ✓ Successfully created QUEUED execution {execution_id} for playbook {playbook_id} in project {project_id}. File location: {executions_dir}/{execution_id}.json. Worker should pick it up.")
        else:
            app.logger.warning(f"[PlaybookScheduler] Created execution record but got no execution_id for playbook {playbook_id} in project {project_id}")
        
        return True, None
        
    except Exception as e:
        app.logger.error(f"[PlaybookScheduler] Error creating scheduled run for playbook {playbook_id} in project {project_id}: {e}", exc_info=True)
        return False, str(e)


def _call_api_run_playbook(project_id: str, playbook_id: str) -> Tuple[bool, Optional[str]]:
    """
    Call API endpoint for running playbook (like clicking Run button).
    Calls the API endpoint function directly with Flask request context.
    Automatically determines ansible_config and inventory_files like the UI does.
    
    Returns:
        (success, error_message)
    """
    try:
        # Auto-determine ansible_config (like the UI does)
        project_dir = get_project_dir(project_id)
        ansible_config_dir = project_dir / 'ansible-config'
        ansible_config = None
        if ansible_config_dir.exists():
            if (ansible_config_dir / 'ansible.cfg').exists():
                ansible_config = 'ansible-config/ansible.cfg'
            else:
                config_files = list(ansible_config_dir.glob('*.cfg'))
                if config_files:
                    ansible_config = f'ansible-config/{config_files[0].name}'
        
        if not ansible_config:
            app.logger.error(f"[PlaybookScheduler] No ansible_config found for scheduled run of playbook {playbook_id}")
            return False, 'ansible_config is required'
        
        # Auto-determine inventory_files from playbook
        playbook = playbook_storage.get_playbook(project_id, playbook_id)
        if not playbook:
            return False, 'Playbook not found'
        
        # Check if playbook is disabled - scheduler should not run disabled playbooks
        if playbook.get('disabled') or playbook.get('metadata', {}).get('disabled'):
            app.logger.info(f"[PlaybookScheduler] ⏸️ Skipping disabled playbook {playbook_id} in project {project_id}")
            return False, 'Playbook is disabled'
        
        inventory_files = find_inventory_files_for_playbook(project_id, playbook)
        if not inventory_files:
            inventory_files = ['inventory.yml']
        
        # Create Flask request context to simulate HTTP request
        # This is the same as clicking the Run button in the UI
        with app.test_request_context(
            f'/api/projects/{project_id}/playbooks/{playbook_id}/run',
            method='POST',
            json={
                'inventory_files': inventory_files,
                'ansible_config': ansible_config
            }
        ):
            # Call the API endpoint function directly
            # This is exactly what happens when user clicks Run button
            response = api_run_playbook(project_id, playbook_id)
            
            # Parse Flask response
            # Flask route can return either:
            # 1. Tuple: (response_object, status_code)
            # 2. Response object directly
            # 3. Just response object (status_code defaults to 200)
            
            response_obj = None
            status_code = 200
            
            if isinstance(response, tuple):
                # Flask response tuple (response_object, status_code)
                response_obj, status_code = response
            elif hasattr(response, 'status_code'):
                # Direct Response object
                response_obj = response
                status_code = response.status_code
            else:
                # Unexpected format
                app.logger.error(f"[PlaybookScheduler] Unexpected response format: {type(response)}")
                return False, f'Unexpected response format: {type(response)}'
            
            # Parse JSON response
            try:
                if hasattr(response_obj, 'get_json'):
                    result = response_obj.get_json()
                elif hasattr(response_obj, 'get_data'):
                    # Try to get data and parse as JSON
                    data = response_obj.get_data(as_text=True)
                    if data:
                        import json
                        result = json.loads(data)
                    else:
                        result = {}
                else:
                    result = {}
                
                if status_code == 200:
                    if result and result.get('success'):
                        execution_id = result.get('executionId')
                        app.logger.info(f"[PlaybookScheduler] ✅ API call successful (like Run button), execution_id: {execution_id}")
                        return True, None
                    else:
                        error = result.get('error', 'Unknown error') if result else 'No response data'
                        app.logger.error(f"[PlaybookScheduler] ✗ API call failed: {error}")
                        return False, error
                else:
                    # Error status code
                    error = result.get('error', f'HTTP {status_code}') if result else f'HTTP {status_code}'
                    app.logger.error(f"[PlaybookScheduler] ✗ API call failed: {error}")
                    return False, error
            except Exception as e:
                app.logger.error(f"[PlaybookScheduler] Error parsing API response: {e}", exc_info=True)
                return False, f"Error parsing response: {str(e)}"
    except Exception as e:
        app.logger.error(f"[PlaybookScheduler] Error calling API endpoint: {e}", exc_info=True)
        return False, str(e)


# Initialize PlaybookScheduler only once using module-level lock
_playbook_scheduler_lock = threading.Lock()
if playbook_scheduler is None:
    with _playbook_scheduler_lock:
        # Double-check after acquiring lock (double-checked locking pattern)
        if playbook_scheduler is None:
            try:
                # Check if required dependencies are available
                import pytz
                from croniter import croniter
                from playbook_scheduler import PlaybookScheduler
                # Use lock file directory in data/locks for cross-process synchronization
                lock_file_dir = DATA_DIR / 'locks'
                playbook_scheduler = PlaybookScheduler(
                    playbook_storage=playbook_storage,
                    api_run_playbook_func=_call_api_run_playbook,  # Use API call instead of direct function
                    lock_file_dir=lock_file_dir  # For cross-process synchronization
                )
                # Start scheduler
                playbook_scheduler.start()
                app.logger.info("Playbook scheduler initialized and started (singleton)")
            except ImportError as e:
                app.logger.warning(f"PlaybookScheduler dependencies not available ({e}). Schedule feature will work but automatic execution is disabled. Install: pip install croniter pytz")
                playbook_scheduler = None
            except Exception as e:
                app.logger.error(f"Failed to initialize PlaybookScheduler: {e}", exc_info=True)
                playbook_scheduler = None
        else:
            app.logger.debug("Playbook scheduler already initialized by another thread")


def get_default_sources(project_id):
    """
    Возвращает источники по умолчанию на основе новой структуры проекта.
    Возвращает относительные пути относительно директории проекта.
    """
    project_dir = get_project_dir(project_id)
    
    # Новый формат: единый источник repo/
    defaults = {
        'repo': {
            'mode': 'local',
            'localPath': 'repo'
        }
    }
    
    return defaults


def normalize_sources(sources, project_id):
    """
    Нормализует sources, добавляя значения по умолчанию для отсутствующих полей.
    Поддерживает source binding с направлениями синхронизации.
    """
    defaults = get_default_sources(project_id)
    normalized = {}
    
    # Новый формат: единый источник repo/
    resource_types = ['repo']
    
    for resource_type in resource_types:
        if resource_type in sources:
            source = sources[resource_type].copy()
            # Если mode не указан, используем default
            if 'mode' not in source:
                source['mode'] = defaults[resource_type]['mode']
            # Если mode local и нет localPath, используем default
            if source['mode'] == 'local' and 'localPath' not in source:
                default_local_path = defaults[resource_type].get('localPath')
                # Если default тоже None, используем стандартное имя
                if default_local_path is None:
                    # Fallback to standard names if default is None
                    standard_paths = {
                        'repo': 'repo',
                        'roles_playbooks': 'roles-playbooks',
                        'inventory': 'inventory',
                        'ansible_config': 'ansible.cfg',
                        'group_vars_storage': 'group_vars',
                        'host_vars_storage': 'host_vars',
                        'secrets_storage': 'secrets-storage'
                    }
                    source['localPath'] = standard_paths.get(resource_type, resource_type)
                else:
                    source['localPath'] = default_local_path
            # Если mode git, проверяем наличие git конфига
            if source['mode'] == 'git':
                if 'git' not in source:
                    source['git'] = {}
                git_config = source['git']
                if 'ref' not in git_config:
                    git_config['ref'] = 'main'
                if 'subdir' not in git_config:
                    git_config['subdir'] = ''
                if 'authSecretId' not in git_config:
                    git_config['authSecretId'] = None
            
            # Source binding: направления синхронизации
            if 'syncDirection' not in source:
                source['syncDirection'] = 'pull'  # По умолчанию: Pull only (Git → Project Storage). Варианты: none, push, pull, both
            if 'syncStatus' not in source:
                source['syncStatus'] = {
                    'push': {'status': 'idle', 'lastSyncAt': None, 'error': None},
                    'pull': {'status': 'idle', 'lastSyncAt': None, 'error': None}
                }
            
            normalized[resource_type] = source
        else:
            # Используем defaults полностью
            normalized[resource_type] = defaults[resource_type].copy()
            # Если default localPath is None, используем стандартное имя
            if normalized[resource_type].get('mode') == 'local':
                default_local_path = normalized[resource_type].get('localPath')
                if default_local_path is None:
                    # Fallback to standard names if default is None
                    standard_paths = {
                        'repo': 'repo',
                        'roles_playbooks': 'roles-playbooks',
                        'inventory': 'inventory',
                        'ansible_config': 'ansible.cfg',
                        'group_vars_storage': 'group_vars',
                        'host_vars_storage': 'host_vars',
                        'secrets_storage': 'secrets-storage'
                    }
                    normalized[resource_type]['localPath'] = standard_paths.get(resource_type, resource_type)
            # Добавляем default sync binding (по умолчанию: Pull only)
            normalized[resource_type]['syncDirection'] = 'pull'
            normalized[resource_type]['syncStatus'] = {
                'push': {'status': 'idle', 'lastSyncAt': None, 'error': None},
                'pull': {'status': 'idle', 'lastSyncAt': None, 'error': None}
            }
    
    return normalized


def validate_repo_layout(repo_layout):
    """
    Валидирует repoLayout конфигурацию.
    Возвращает (is_valid, error_code, error_message)
    """
    if repo_layout is None:
        return True, None, None  # repoLayout is optional
    
    if not isinstance(repo_layout, dict):
        return False, 'INVALID_REPO_LAYOUT_FORMAT', 'repoLayout must be a dictionary'
    
    valid_keys = ['playbooks', 'roles', 'inventories', 'vars']
    for key, value in repo_layout.items():
        if key not in valid_keys:
            return False, 'INVALID_REPO_LAYOUT_KEY', f'Invalid repoLayout key: {key}. Must be one of {valid_keys}'
        
        if not isinstance(value, str):
            return False, 'INVALID_REPO_LAYOUT_VALUE', f'repoLayout.{key} must be a string'
        
        # Validate path
        is_valid, error_msg = validate_repo_layout_path(value)
        if not is_valid:
            return False, 'INVALID_REPO_LAYOUT_PATH', f'repoLayout.{key}: {error_msg}'
    
    return True, None, None


def validate_sources(sources):
    """
    Валидирует sources конфигурацию.
    Возвращает (is_valid, error_code, error_message)
    """
    if not isinstance(sources, dict):
        return False, 'INVALID_FORMAT', 'Sources must be a dictionary'
    
    valid_modes = ['local', 'git']
    valid_resource_types = ['repo']  # Только новый формат
    
    for resource_type, source_config in sources.items():
        if resource_type not in valid_resource_types:
            return False, 'INVALID_RESOURCE_TYPE', f'Invalid resource type: {resource_type}'
        
        if not isinstance(source_config, dict):
            return False, 'INVALID_SOURCE_CONFIG', f'Source config for {resource_type} must be a dictionary'
        
        mode = source_config.get('mode')
        if mode not in valid_modes:
            return False, 'UNSUPPORTED_MODE', f'Unsupported mode for {resource_type}: {mode}. Must be "local" or "git"'
        
        if mode == 'local':
            local_path = source_config.get('localPath')
            if not local_path:
                return False, 'MISSING_LOCAL_PATH', f'localPath is required for {resource_type} in local mode'
            if not isinstance(local_path, str):
                return False, 'INVALID_LOCAL_PATH', f'localPath for {resource_type} must be a string'
            # Проверка на path traversal (но разрешаем абсолютные пути)
            if '..' in local_path:
                return False, 'INVALID_LOCAL_PATH', f'localPath for {resource_type} contains path traversal (..)'
        
        elif mode == 'git':
            git_config = source_config.get('git')
            if not isinstance(git_config, dict):
                return False, 'MISSING_GIT_CONFIG', f'git configuration is required for {resource_type} in git mode'
            
            repo = git_config.get('repo')
            if not repo:
                return False, 'MISSING_GIT_REPO', f'git.repo is required for {resource_type} in git mode'
            if not isinstance(repo, str):
                return False, 'INVALID_GIT_REPO', f'git.repo for {resource_type} must be a string'
            # Простая валидация URL
            if not (repo.startswith('http://') or repo.startswith('https://') or repo.startswith('git@') or repo.startswith('git://')):
                return False, 'INVALID_GIT_REPO', f'git.repo for {resource_type} must be a valid Git URL'
            
            ref = git_config.get('ref')
            if ref and not isinstance(ref, str):
                return False, 'INVALID_GIT_REF', f'git.ref for {resource_type} must be a string'
            
            subdir = git_config.get('subdir', '')
            if subdir:
                if not isinstance(subdir, str):
                    return False, 'INVALID_GIT_SUBDIR', f'git.subdir for {resource_type} must be a string'
                # Проверка на path traversal в subdir
                if '..' in subdir or subdir.startswith('/'):
                    return False, 'INVALID_GIT_SUBDIR', f'git.subdir for {resource_type} contains invalid characters (path traversal detected)'
            
            auth_secret_id = git_config.get('authSecretId')
            if auth_secret_id is not None and not isinstance(auth_secret_id, str):
                return False, 'INVALID_AUTH_SECRET_ID', f'git.authSecretId for {resource_type} must be a string or null'
    
    return True, None, None


def resolve_project_source(project_id: str, source_key: str) -> dict:
    """
    Resolve project source configuration and return Project Storage path.
    
    CRITICAL: This function ALWAYS returns a Project Storage path.
    External paths (Local Path / Git cache) are NEVER returned.
    They are only accessed inside SourceSyncService during sync operations.
    
    Args:
        project_id: Project ID
        source_key: Source key ('repo')
    
    Returns:
        dict with keys:
            - mode: 'local' or 'git' (from source config, if available)
            - rootPath: Path to Project Storage (ALWAYS projects/<projectId>/...)
            - meta: Additional metadata (repo_url, ref, subdir for git mode, if available)
    
    Raises:
        ValueError: If source_key is invalid
    """
    # ALWAYS return Project Storage path
    root_path = get_project_storage_path(project_id, source_key)
    
    # Ensure directory exists (for directories, not files like ansible.cfg)
    if source_key != 'ansible_config':
        root_path.mkdir(parents=True, exist_ok=True)
    else:
        # For ansible.cfg, ensure parent directory exists
        root_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Try to get source configuration for metadata (if available)
    mode = 'local'  # Default
    meta = {}
    
    try:
        if PROJECT_SOURCES_ENABLED:
            config = load_project_config(project_id)
            sources = config.get('sources', {})
            normalized_sources = normalize_sources(sources, project_id)
            
            if source_key in normalized_sources:
                source_config = normalized_sources[source_key]
                mode = source_config.get('mode', 'local')
                
                if mode == 'local':
                    local_path = source_config.get('localPath')
                    if local_path:
                        meta = {
                            'localPath': local_path,
                            'projectDir': str(get_project_dir(project_id))
                        }
                elif mode == 'git':
                    git_config = source_config.get('git', {})
                    repo_url = git_config.get('repo')
                    if repo_url:
                        meta = {
                            'repoUrl': repo_url,
                            'ref': git_config.get('ref', 'main'),
                            'subdir': git_config.get('subdir', ''),
                            'authSecretId': git_config.get('authSecretId')  # Note: never log this value
                        }
    except Exception as e:
        # If we can't load config, still return Project Storage path
        app.logger.warning(f"[resolveProjectSource] Could not load source config for {source_key}: {e}, using defaults")
    
    return {
        'mode': mode,
        'rootPath': root_path.resolve(),
        'meta': meta
    }


def _resolve_legacy_source(project_id: str, source_key: str) -> dict:
    """
    DEPRECATED: This function is kept for backward compatibility but now
    always returns Project Storage paths (no legacy fallbacks).
    
    Use resolve_project_source() instead, which always returns Project Storage.
    """
    # Always return Project Storage path (no legacy fallbacks)
    return resolve_project_source(project_id, source_key)


@app.route('/api/projects/<project_id>/sources/status', methods=['GET'])
@require_auth
def api_get_project_sources_status(project_id):
    """API: Get health status of all project sources"""
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        # Load sources configuration
        config = load_project_config(project_id)
        sources = config.get('sources', {})
        normalized_sources = normalize_sources(sources, project_id)
        
        # Define source requirements
        required_sources = ['inventory', 'group_vars_storage', 'host_vars_storage', 'roles_playbooks', 'secrets_storage']
        optional_sources = ['ansible_config']
        
        status = {
            'overall': 'ok',  # ok, warning, error
            'sources': {}
        }
        
        has_errors = False
        has_warnings = False
        
        # Check each source
        for source_key in ['inventory', 'roles_playbooks', 'ansible_config', 'group_vars_storage', 'host_vars_storage', 'secrets_storage']:
            source_config = normalized_sources.get(source_key, {})
            mode = source_config.get('mode', 'local')
            
            source_status = {
                'configured': source_key in sources,  # Has custom config (not just defaults)
                'mode': mode,
                'status': 'ok',  # ok, warning, error
                'message': '',
                'resolved': False,
                'path': None
            }
            
            try:
                # Try to resolve the source
                resolved = resolve_project_source(project_id, source_key)
                source_status['resolved'] = True
                source_status['path'] = str(resolved['rootPath'])
                
                # Check if path exists and is accessible
                root_path = resolved['rootPath']
                if not root_path.exists():
                    if source_key in required_sources:
                        source_status['status'] = 'error'
                        source_status['message'] = f'Path does not exist: {root_path}'
                        has_errors = True
                    else:
                        source_status['status'] = 'warning'
                        source_status['message'] = f'Path does not exist: {root_path}'
                        has_warnings = True
                elif not os.access(root_path, os.R_OK):
                    source_status['status'] = 'error'
                    source_status['message'] = f'Path is not readable: {root_path}'
                    has_errors = True
                else:
                    # Additional checks based on source type
                    if source_key == 'inventory':
                        # Check for inventory files
                        inv_files = []
                        if root_path.is_file() and root_path.suffix in ['.yml', '.yaml']:
                            inv_files = [root_path]
                        elif root_path.is_dir():
                            inv_files = list(root_path.glob('*.yml')) + list(root_path.glob('*.yaml'))
                        
                        if not inv_files:
                            source_status['status'] = 'warning'
                            source_status['message'] = 'No inventory files found'
                            has_warnings = True
                    
                    elif source_key == 'roles_playbooks':
                        # Check for expected structure
                        if root_path.is_dir():
                            has_roles = (root_path / 'roles').exists() or any(
                                (item / 'tasks' / 'main.yml').exists() or (item / 'tasks' / 'main.yaml').exists()
                                for item in root_path.iterdir() if item.is_dir()
                            )
                            has_playbooks = (root_path / 'playbooks').exists()
                            
                            if not has_roles and not has_playbooks:
                                source_status['status'] = 'warning'
                                source_status['message'] = 'Expected structure (roles/playbooks) not found'
                                has_warnings = True
                    
                    elif source_key == 'ansible_config':
                        # ansible_config - это файл, а не директория
                        if root_path.is_file():
                            # Файл существует
                            pass
                        elif root_path.parent.is_dir():
                            # Родительская директория существует, но файл нет
                            source_status['status'] = 'warning'
                            source_status['message'] = 'ansible.cfg file not found'
                            has_warnings = True
                        else:
                            # Родительская директория не существует
                            source_status['status'] = 'warning'
                            source_status['message'] = 'ansible-config directory not found'
                            has_warnings = True
                    
                    elif source_key in ['group_vars_storage', 'host_vars_storage', 'secrets_storage']:
                        if root_path.is_dir():
                            files = list(root_path.glob('*.yml')) + list(root_path.glob('*.yaml')) + list(root_path.glob('*.json'))
                            if not files:
                                source_status['status'] = 'warning'
                                source_status['message'] = 'Directory is empty'
                                has_warnings = True
                
            except Exception as e:
                app.logger.warning(f"Error resolving source {source_key}: {e}")
                source_status['resolved'] = False
                source_status['status'] = 'error' if source_key in required_sources else 'warning'
                source_status['message'] = f'Failed to resolve: {str(e)}'
                if source_key in required_sources:
                    has_errors = True
                else:
                    has_warnings = True
            
            status['sources'][source_key] = source_status
        
        # Set overall status
        if has_errors:
            status['overall'] = 'error'
        elif has_warnings:
            status['overall'] = 'warning'
        else:
            status['overall'] = 'ok'
        
        return jsonify({
            'success': True,
            'status': status
        })
        
    except Exception as e:
        app.logger.error(f"Error getting sources status: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR'
        }), 500


@app.route('/api/projects/<project_id>/sources/revert', methods=['POST'])
@require_auth
def api_revert_source_to_defaults(project_id):
    """API: Revert a source to legacy local defaults"""
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        data = request.json or {}
        source_key = data.get('sourceKey')
        
        if not source_key:
            return jsonify({'success': False, 'error': 'sourceKey is required', 'errorCode': 'MISSING_SOURCE_KEY'}), 400
        
        # Load current config
        config = load_project_config(project_id)
        sources = config.get('sources', {})
        
        # Remove the source key (revert to defaults)
        if source_key in sources:
            del sources[source_key]
            config['sources'] = sources
            save_project_config(project_id, config)
        
        # Return normalized sources (will use defaults)
        normalized_sources = normalize_sources(sources, project_id)
        
        app.logger.info(f"Source reverted to defaults: {project_id}/{source_key}")
        return jsonify({
            'success': True,
            'sources': normalized_sources
        })
    except Exception as e:
        app.logger.error(f"Error reverting source: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/sources', methods=['GET'])
@require_auth
def api_get_project_sources(project_id):
    """API: Получить sources конфигурацию проекта"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Загружаем конфигурацию проекта
        config = load_project_config(project_id)
        sources = config.get('sources', {})
        sync_timestamps = config.get('syncTimestamps', {})
        repo_layout = config.get('repoLayout')  # Optional repoLayout
        
        # Нормализуем sources с defaults
        normalized_sources = normalize_sources(sources, project_id)
        
        # Add sync timestamps and status to response
        sync_status = config.get('syncStatus', {})
        sources_with_sync = {}
        for key, source in normalized_sources.items():
            source_copy = source.copy()
            # Legacy sync timestamp (backward compatibility)
            if key in sync_timestamps:
                source_copy['lastSyncedAt'] = sync_timestamps[key]
            # New sync status (bidirectional)
            if key in sync_status:
                source_copy['syncStatus'] = sync_status[key]
            else:
                source_copy['syncStatus'] = {
                    'push': {'status': 'idle', 'lastSyncAt': None, 'error': None},
                    'pull': {'status': 'idle', 'lastSyncAt': None, 'error': None}
                }
            # Sync direction
            if 'syncDirection' in source:
                source_copy['syncDirection'] = source['syncDirection']
            else:
                source_copy['syncDirection'] = 'none'
            
            # Get sync state from SourceSyncService
            try:
                sync_state = source_sync_service.get_sync_state(project_id, key)
                source_copy['syncState'] = sync_state
            except Exception as e:
                app.logger.warning(f"Error getting sync state for {key}: {e}")
                source_copy['syncState'] = {
                    'lastPushAt': None,
                    'lastPullAt': None,
                    'lastPushStatus': 'idle',
                    'lastPullStatus': 'idle',
                    'lastPushError': None,
                    'lastPullError': None,
                    'lastPushRevision': None,
                    'lastPullRevision': None
                }
            
            sources_with_sync[key] = source_copy
        
        return jsonify({
            'success': True,
            'sources': sources_with_sync,
            'repoLayout': repo_layout,  # Return repoLayout if exists
            'hasCustomSources': bool(sources),  # Флаг, есть ли кастомные sources или только defaults
            'syncTimestamps': sync_timestamps,  # Legacy
            'syncStatus': sync_status  # New bidirectional status
        })
    except Exception as e:
        app.logger.error(f"Error getting project sources: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/sources/analyze', methods=['POST'])
@require_auth
def api_analyze_source_impact(project_id):
    """API: Analyze impact of source configuration change"""
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        data = request.json or {}
        source_key = data.get('sourceKey')
        config = data.get('config', {})
        
        if not source_key:
            return jsonify({'success': False, 'error': 'sourceKey is required', 'errorCode': 'MISSING_SOURCE_KEY'}), 400
        
        mode = config.get('mode', 'local')
        issues = []  # List of warnings/errors
        has_errors = False
        
        # Resolve path based on mode
        if mode == 'local':
            local_path = config.get('localPath', '').strip()
            if not local_path:
                issues.append({
                    'type': 'error',
                    'message': 'Local path is required',
                    'code': 'MISSING_LOCAL_PATH'
                })
                has_errors = True
            else:
                # Hard check for path traversal
                if '..' in local_path:
                    issues.append({
                        'type': 'error',
                        'message': 'Path contains traversal (..) - not allowed',
                        'code': 'PATH_TRAVERSAL'
                    })
                    has_errors = True
                else:
                    # Resolve path
                    path_obj = Path(local_path)
                    if not path_obj.is_absolute():
                        # Relative path: resolve relative to project directory
                        project_dir = get_project_dir(project_id)
                        path_obj = (project_dir / local_path).resolve()
                        # Ensure it's within project directory
                        try:
                            path_obj.relative_to(project_dir.resolve())
                        except ValueError:
                            issues.append({
                                'type': 'error',
                                'message': 'Path resolves outside project directory',
                                'code': 'PATH_TRAVERSAL'
                            })
                            has_errors = True
                            path_obj = None
                    else:
                        # Absolute path: validate it's a valid external path (for sync only)
                        # Note: absolute paths are used for external sources during sync
                        # Runtime always uses Project Storage, not this path
                        if not path_obj.exists():
                            issues.append({
                                'type': 'warning',
                                'message': f'Absolute path does not exist: {path_obj}. It will be created during sync if needed.',
                                'code': 'PATH_NOT_FOUND'
                            })
                    
                    if path_obj:
                        # Check if path exists
                        if not path_obj.exists():
                            if source_key in ['group_vars_storage', 'host_vars_storage', 'secrets_storage']:
                                issues.append({
                                    'type': 'error',
                                    'message': f'Path does not exist: {path_obj}',
                                    'code': 'PATH_NOT_FOUND'
                                })
                                has_errors = True
                            else:
                                issues.append({
                                    'type': 'warning',
                                    'message': f'Path does not exist: {path_obj}',
                                    'code': 'PATH_NOT_FOUND'
                                })
                        else:
                            # Run source-specific checks
                            if source_key == 'roles_playbooks':
                                # Check for expected structure (roles/playbooks)
                                has_roles = False
                                has_playbooks = False
                                
                                if path_obj.is_dir():
                                    # Check for roles directory or role-like structure
                                    roles_dir = path_obj / 'roles'
                                    if roles_dir.exists() and roles_dir.is_dir():
                                        has_roles = True
                                    else:
                                        # Check if direct children are role directories (tasks/main.yml)
                                        for item in path_obj.iterdir():
                                            if item.is_dir():
                                                tasks_main = (item / 'tasks' / 'main.yml').exists() or (item / 'tasks' / 'main.yaml').exists()
                                                if tasks_main:
                                                    has_roles = True
                                                    break
                                    
                                    # Check for playbooks directory
                                    playbooks_dir = path_obj / 'playbooks'
                                    if playbooks_dir.exists() and playbooks_dir.is_dir():
                                        has_playbooks = True
                                    
                                    if not has_roles and not has_playbooks:
                                        issues.append({
                                            'type': 'warning',
                                            'message': 'Directory does not contain expected structure (roles/playbooks). This may affect executions.',
                                            'code': 'UNEXPECTED_STRUCTURE'
                                        })
                            
                            elif source_key == 'inventory':
                                # Ensure at least one inventory file detected
                                inv_files = []
                                if path_obj.is_file() and path_obj.suffix in ['.yml', '.yaml']:
                                    inv_files = [path_obj]
                                elif path_obj.is_dir():
                                    inv_files = list(path_obj.glob('*.yml')) + list(path_obj.glob('*.yaml'))
                                
                                if not inv_files:
                                    issues.append({
                                        'type': 'warning',
                                        'message': 'No inventory files (.yml/.yaml) detected. This may affect executions.',
                                        'code': 'NO_INVENTORY_FILES'
                                    })
                            
                            elif source_key == 'ansible_config':
                                # If ansible.cfg missing, warn
                                if path_obj.is_dir():
                                    cfg_file = path_obj / 'ansible.cfg'
                                    if not cfg_file.exists():
                                        issues.append({
                                            'type': 'warning',
                                            'message': 'ansible.cfg file not found in directory. Default Ansible config will be used.',
                                            'code': 'ANSIBLE_CFG_MISSING'
                                        })
                                elif path_obj.is_file() and path_obj.name != 'ansible.cfg':
                                    issues.append({
                                        'type': 'warning',
                                        'message': 'File is not named ansible.cfg. This may not be recognized as Ansible config.',
                                        'code': 'ANSIBLE_CFG_NAME_MISMATCH'
                                    })
                            
                            elif source_key in ['group_vars_storage', 'host_vars_storage', 'secrets_storage']:
                                if path_obj.is_dir():
                                    # Check if empty
                                    files = list(path_obj.glob('*.yml')) + list(path_obj.glob('*.yaml')) + list(path_obj.glob('*.json'))
                                    if not files:
                                        issues.append({
                                            'type': 'warning',
                                            'message': f'Directory is empty. No {source_key.replace("_", " ")} files found.',
                                            'code': 'EMPTY_STORAGE'
                                        })
                                elif path_obj.is_file():
                                    issues.append({
                                        'type': 'warning',
                                        'message': f'Path is a file, but {source_key.replace("_", " ")} expects a directory.',
                                        'code': 'EXPECTED_DIRECTORY'
                                    })
                            
                            # Check readability
                            if path_obj.exists() and not os.access(path_obj, os.R_OK):
                                issues.append({
                                    'type': 'error',
                                    'message': f'Path is not readable: {path_obj}',
                                    'code': 'PATH_NOT_READABLE'
                                })
                                has_errors = True
        
        elif mode == 'git':
            git_config = config.get('git', {})
            repo_url = git_config.get('repo', '').strip()
            
            if not repo_url:
                issues.append({
                    'type': 'error',
                    'message': 'Repository URL is required',
                    'code': 'MISSING_REPO_URL'
                })
                has_errors = True
            else:
                # Hard check for unsafe URLs
                if '..' in repo_url or repo_url.startswith('file://'):
                    issues.append({
                        'type': 'error',
                        'message': 'Unsafe repository URL detected',
                        'code': 'UNSAFE_REPO_URL'
                    })
                    has_errors = True
                
                # Check subdir for path traversal
                subdir = git_config.get('subdir', '').strip()
                if subdir and '..' in subdir:
                    issues.append({
                        'type': 'error',
                        'message': 'Subdirectory contains path traversal (..) - not allowed',
                        'code': 'SUBDIR_TRAVERSAL'
                    })
                    has_errors = True
        
        return jsonify({
            'success': True,
            'hasErrors': has_errors,
            'hasWarnings': len([i for i in issues if i['type'] == 'warning']) > 0,
            'issues': issues,
            'breakingChange': has_errors or len([i for i in issues if i['type'] == 'warning']) > 0
        })
        
    except Exception as e:
        app.logger.error(f"Error analyzing source impact: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR'
        }), 500


@app.route('/api/projects/<project_id>/sources', methods=['PUT'])
@require_auth
def api_update_project_sources(project_id):
    """API: Обновить sources конфигурацию проекта"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        data = request.json or {}
        sources = data.get('sources', {})
        repo_layout = data.get('repoLayout')  # Optional repoLayout
        skip_validation = data.get('skipValidation', False)  # Allow skipping if user confirmed
        
        # Normalize sources before validation (adds defaults for missing fields)
        # This ensures all sources have required fields like localPath when mode is 'local'
        sources = normalize_sources(sources, project_id)
        
        # Валидация sources
        is_valid, error_code, error_message = validate_sources(sources)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': error_message,
                'errorCode': error_code
            }), 400
        
        # Валидация repoLayout (если указан)
        if repo_layout is not None:
            is_valid, error_code, error_message = validate_repo_layout(repo_layout)
            if not is_valid:
                return jsonify({
                    'success': False,
                    'error': error_message,
                    'errorCode': error_code
                }), 400
        
        # Additional hard security checks (always run, even if skip_validation)
        for source_key, source_config in sources.items():
            mode = source_config.get('mode', 'local')
            if mode == 'local':
                local_path = source_config.get('localPath', '')
                if local_path and '..' in local_path:
                    return jsonify({
                        'success': False,
                        'error': f'Path traversal detected in {source_key}',
                        'errorCode': 'PATH_TRAVERSAL'
                    }), 400
            elif mode == 'git':
                git_config = source_config.get('git', {})
                repo_url = git_config.get('repo', '')
                if repo_url and ('..' in repo_url or repo_url.startswith('file://')):
                    return jsonify({
                        'success': False,
                        'error': f'Unsafe repository URL in {source_key}',
                        'errorCode': 'UNSAFE_REPO_URL'
                    }), 400
                subdir = git_config.get('subdir', '')
                if subdir and '..' in subdir:
                    return jsonify({
                        'success': False,
                        'error': f'Path traversal in subdir for {source_key}',
                        'errorCode': 'SUBDIR_TRAVERSAL'
                    }), 400
        
        # Загружаем текущую конфигурацию
        config = load_project_config(project_id)
        
        # Обновляем sources
        config['sources'] = sources
        
        # Обновляем repoLayout (если указан)
        # Сохраняем только если отличается от дефолтов или если явно передан
        if repo_layout is not None:
            # Проверяем, отличается ли от дефолтов
            default_layout = {
                'playbooks': 'playbooks',
                'roles': 'roles',
                'inventories': 'inventories'
            }
            
            # Если все значения дефолтные, удаляем repoLayout (не сохраняем)
            is_default = all(
                repo_layout.get(key, default_value) == default_value
                for key, default_value in default_layout.items()
            )
            
            if is_default:
                # Удаляем repoLayout если он был, так как дефолты применяются автоматически
                config.pop('repoLayout', None)
            else:
                # Сохраняем кастомный layout
                config['repoLayout'] = repo_layout
        # Если repo_layout is None, не трогаем существующий repoLayout
        
        # Сохраняем конфигурацию
        save_project_config(project_id, config)
        
        # Автоматическая синхронизация Pull для внешних путей (если Project Storage пустой)
        # Это нужно, чтобы роли/данные появились сразу после сохранения источника
        for source_key, source_config in sources.items():
            mode = source_config.get('mode', 'local')
            if mode == 'local':
                local_path = source_config.get('localPath', '')
                if local_path:
                    external_path = Path(local_path)
                    # Если это абсолютный путь (внешний источник)
                    if external_path.is_absolute() and external_path.exists():
                        # Получаем путь Project Storage
                        storage_path = get_project_storage_path(project_id, source_key)
                        # Если Project Storage пустой или не существует, выполняем автоматический Pull
                        if not storage_path.exists() or (storage_path.is_dir() and not any(storage_path.iterdir())):
                            try:
                                app.logger.info(f"Auto-syncing external source: {source_key} from {external_path} to {storage_path}")
                                success, error_code, error_msg = source_sync_service.execute_sync(
                                    project_id=project_id,
                                    source_key=source_key,
                                    source_config=source_config,
                                    direction='pull'
                                )
                                if success:
                                    app.logger.info(f"Auto-sync successful for {source_key}")
                                else:
                                    app.logger.warning(f"Auto-sync failed for {source_key}: {error_msg}")
                            except Exception as e:
                                app.logger.warning(f"Auto-sync error for {source_key}: {e}")
                                # Не блокируем сохранение, если синхронизация не удалась
        
        # Нормализуем для ответа
        normalized_sources = normalize_sources(sources, project_id)
        
        app.logger.info(f"Project sources updated: {project_id}")
        return jsonify({
            'success': True,
            'sources': normalized_sources
        })
    except Exception as e:
        app.logger.error(f"Error updating project sources: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/sources/test', methods=['POST'])
@require_auth
def api_test_project_source(project_id):
    """API: Test a source configuration (Local or Git)"""
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        data = request.json or {}
        source_key = data.get('sourceKey')
        config = data.get('config', {})
        
        if not source_key:
            return jsonify({'success': False, 'error': 'sourceKey is required', 'errorCode': 'MISSING_SOURCE_KEY'}), 400
        
        mode = config.get('mode', 'local')
        details = {}
        
        if mode == 'local':
            # Test Local source
            local_path = config.get('localPath', '').strip()
            if not local_path:
                return jsonify({
                    'success': False,
                    'error': 'Local path is required',
                    'errorCode': 'MISSING_LOCAL_PATH',
                    'details': {}
                }), 400
            
            # Resolve path
            try:
                path_obj = Path(local_path)
                if not path_obj.is_absolute():
                    # Relative path: resolve relative to project directory
                    project_dir = get_project_dir(project_id)
                    path_obj = (project_dir / local_path).resolve()
                    # Ensure it's within project directory
                    try:
                        path_obj.relative_to(project_dir.resolve())
                    except ValueError:
                        return jsonify({
                            'success': False,
                            'error': 'Path resolves outside project directory',
                            'errorCode': 'PATH_TRAVERSAL',
                            'details': {}
                        }), 400
                else:
                    # Absolute path: validate it's a valid external path (for sync only)
                    # Note: absolute paths are used for external sources during sync
                    # Runtime always uses Project Storage, not this path
                    pass  # Absolute paths are allowed for external sources
                
                # Check if path exists
                if not path_obj.exists():
                    return jsonify({
                        'success': False,
                        'error': f'Path does not exist: {path_obj}',
                        'errorCode': 'PATH_NOT_FOUND',
                        'details': {'path': str(path_obj)}
                    }), 400
                
                # Check if readable
                if not os.access(path_obj, os.R_OK):
                    return jsonify({
                        'success': False,
                        'error': f'Path is not readable: {path_obj}',
                        'errorCode': 'PATH_NOT_READABLE',
                        'details': {'path': str(path_obj)}
                    }), 400
                
                # Light content check based on source type
                content_hint = ''
                if source_key == 'inventory':
                    # Check for inventory files
                    if path_obj.is_file() and (path_obj.suffix in ['.yml', '.yaml']):
                        content_hint = 'Contains inventory file'
                    elif path_obj.is_dir():
                        inv_files = list(path_obj.glob('*.yml')) + list(path_obj.glob('*.yaml'))
                        if inv_files:
                            content_hint = f'Contains {len(inv_files)} inventory file(s)'
                        else:
                            content_hint = 'Directory exists but no inventory files found'
                elif source_key == 'roles_playbooks':
                    if path_obj.is_dir():
                        # Check for roles structure
                        role_dirs = [d for d in path_obj.iterdir() if d.is_dir()]
                        if role_dirs:
                            content_hint = f'Contains {len(role_dirs)} potential role directory(ies)'
                        else:
                            content_hint = 'Directory exists but no role directories found'
                elif source_key in ['group_vars_storage', 'host_vars_storage', 'secrets_storage']:
                    if path_obj.is_dir():
                        files = list(path_obj.glob('*.yml')) + list(path_obj.glob('*.yaml')) + list(path_obj.glob('*.json'))
                        if files:
                            content_hint = f'Contains {len(files)} variable/secret file(s)'
                        else:
                            content_hint = 'Directory exists but no variable/secret files found'
                
                details = {
                    'path': str(path_obj),
                    'exists': True,
                    'isFile': path_obj.is_file(),
                    'isDir': path_obj.is_dir(),
                    'readable': True,
                    'contentHint': content_hint
                }
                
                return jsonify({
                    'success': True,
                    'ok': True,
                    'details': details
                })
                
            except Exception as e:
                app.logger.error(f"Error testing local source: {e}", exc_info=True)
                return jsonify({
                    'success': False,
                    'error': f'Error testing path: {str(e)}',
                    'errorCode': 'TEST_ERROR',
                    'details': {}
                }), 500
        
        elif mode == 'git':
            # Test Git source
            git_config = config.get('git', {})
            repo_url = git_config.get('repo', '').strip()
            ref = git_config.get('ref', 'main').strip()
            subdir = git_config.get('subdir', '').strip()
            auth_secret_id = git_config.get('authSecretId')
            
            if not repo_url:
                return jsonify({
                    'success': False,
                    'error': 'Repository URL is required',
                    'errorCode': 'MISSING_REPO_URL',
                    'details': {}
                }), 400
            
            # Test using GitSourceManager
            try:
                # Force refresh for test
                resolved_path = git_source_manager.resolve_path(
                    project_id=project_id,
                    source_key=source_key,
                    repo_url=repo_url,
                    ref=ref,
                    subdir=subdir,
                    auth_secret_id=auth_secret_id,
                    force_refresh=True
                )
                
                # Check if resolved path exists
                if not resolved_path.exists():
                    return jsonify({
                        'success': False,
                        'error': f'Resolved path does not exist: {resolved_path}',
                        'errorCode': 'SUBDIR_NOT_FOUND',
                        'details': {'resolvedPath': str(resolved_path)}
                    }), 400
                
                details = {
                    'repo': repo_url,
                    'ref': ref,
                    'subdir': subdir,
                    'resolvedPath': str(resolved_path),
                    'exists': True,
                    'isFile': resolved_path.is_file(),
                    'isDir': resolved_path.is_dir()
                }
                
                return jsonify({
                    'success': True,
                    'ok': True,
                    'details': details
                })
                
            except GitSourceError as e:
                return jsonify({
                    'success': False,
                    'error': e.message,
                    'errorCode': e.error_code,
                    'details': {}
                }), 400
            except Exception as e:
                app.logger.error(f"Error testing git source: {e}", exc_info=True)
                return jsonify({
                    'success': False,
                    'error': f'Error testing git source: {str(e)}',
                    'errorCode': 'TEST_ERROR',
                    'details': {}
                }), 500
        
        else:
            return jsonify({
                'success': False,
                'error': f'Invalid mode: {mode}',
                'errorCode': 'INVALID_MODE',
                'details': {}
            }), 400
            
    except Exception as e:
        app.logger.error(f"Error in api_test_project_source: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR',
            'details': {}
        }), 500


def get_project_storage_path(project_id, source_key):
    """
    Возвращает путь к Project Storage для указанного source.
    Project Storage - это локальное хранилище программы.
    
    Args:
        project_id: ID проекта
        source_key: Ключ источника ('repo', 'inventory', 'roles_playbooks', 'ansible_config', 
                   'group_vars_storage', 'host_vars_storage', 'secrets_storage')
    
    Returns:
        Path к директории/файлу Project Storage
    
    Note:
        Все source_key маппятся на поддиректории/файлы внутри единого 'repo' workspace,
        кроме 'repo' который указывает на корень workspace.
    """
    project_dir = get_project_dir(project_id)
    repo_dir = project_dir / 'repo'
    
    # Маппинг source_key на директории/файлы Project Storage
    # Все они находятся внутри единого 'repo' workspace
    storage_mapping = {
        'repo': repo_dir,  # Корень workspace
        'inventory': repo_dir / 'inventories',  # Inventory files
        'roles_playbooks': repo_dir,  # Roles и playbooks находятся в repo
        'ansible_config': project_dir / 'ansible-config' / 'ansible.cfg',  # Ansible config file
        'group_vars_storage': repo_dir / 'group_vars',  # Group vars
        'host_vars_storage': repo_dir / 'host_vars',  # Host vars
        'secrets_storage': project_dir / 'secrets-storage'  # Secrets storage (вне repo для безопасности)
    }
    
    if source_key not in storage_mapping:
        raise ValueError(f"Unknown source_key: {source_key}. Supported keys: {', '.join(storage_mapping.keys())}")
    
    return storage_mapping[source_key]


def sync_source_push(project_id, source_key, external_path):
    """
    Синхронизация Push: Project Storage → External Source
    
    Args:
        project_id: ID проекта
        source_key: Ключ источника
        external_path: Path к внешнему источнику (Local Path или Git cache)
    
    Returns:
        dict с результатом синхронизации
    """
    try:
        project_storage = get_project_storage_path(project_id, source_key)
        external = Path(external_path)
        
        if not project_storage.exists():
            return {
                'success': False,
                'error': f'Project Storage does not exist: {project_storage}',
                'errorCode': 'STORAGE_NOT_FOUND'
            }
        
            # Копируем файлы из Project Storage в External
            if project_storage.is_file():
                # Для файлов (ansible.cfg)
                external.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(project_storage, external)
                app.logger.info(f"[sync_push] Copied file {project_storage} → {external}")
            else:
                # Для директорий
                if not external.exists():
                    external.mkdir(parents=True, exist_ok=True)
                
                import shutil
                # Удаляем содержимое external (кроме .git если есть)
                if external.exists() and external.is_dir():
                    for item in external.iterdir():
                        if item.name == '.git':
                            continue
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                
                # Копируем содержимое Project Storage в External
                for item in project_storage.iterdir():
                    if item.is_dir():
                        shutil.copytree(item, external / item.name, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, external / item.name)
                
                app.logger.info(f"[sync_push] Synced directory {project_storage} → {external}")
        
        return {
            'success': True,
            'message': f'Successfully pushed {source_key} to external source',
            'syncedAt': int(time.time())
        }
    except Exception as e:
        app.logger.error(f"Error in sync_push for {source_key}: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e),
            'errorCode': 'SYNC_PUSH_FAILED'
        }


def sync_source_pull(project_id, source_key, external_path):
    """
    Синхронизация Pull: External Source → Project Storage
    
    Args:
        project_id: ID проекта
        source_key: Ключ источника
        external_path: Path к внешнему источнику (Local Path или Git cache)
    
    Returns:
        dict с результатом синхронизации
    """
    try:
        project_storage = get_project_storage_path(project_id, source_key)
        external = Path(external_path)
        
        if not external.exists():
            return {
                'success': False,
                'error': f'External source does not exist: {external}',
                'errorCode': 'EXTERNAL_NOT_FOUND'
            }
        
            # Копируем файлы из External в Project Storage
            if external.is_file():
                # Для файлов (ansible.cfg)
                project_storage.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(external, project_storage)
                app.logger.info(f"[sync_pull] Copied file {external} → {project_storage}")
            else:
                # Для директорий
                project_storage.mkdir(parents=True, exist_ok=True)
                
                import shutil
                # Удаляем содержимое Project Storage
                if project_storage.exists() and project_storage.is_dir():
                    for item in project_storage.iterdir():
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                
                # Копируем содержимое External в Project Storage
                for item in external.iterdir():
                    if item.is_dir():
                        shutil.copytree(item, project_storage / item.name, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, project_storage / item.name)
            
            app.logger.info(f"[sync_pull] Synced directory {external} → {project_storage}")
        
        return {
            'success': True,
            'message': f'Successfully pulled {source_key} from external source',
            'syncedAt': int(time.time())
        }
    except Exception as e:
        app.logger.error(f"Error in sync_pull for {source_key}: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e),
            'errorCode': 'SYNC_PULL_FAILED'
        }


@app.route('/api/projects/<project_id>/sources/<source_key>/sync', methods=['POST'])
@require_auth
def api_sync_project_source_bidirectional(project_id, source_key):
    """
    API: Start async bidirectional sync operation (Push/Pull/Both)
    
    Request body:
    {
        "direction": "push|pull|both"  // Направление синхронизации
    }
    
    Returns:
    {
        "success": true,
        "jobId": "project_id_source_key_1234567890",
        "message": "Sync started"
    }
    """
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        data = request.json or {}
        direction = data.get('direction', 'push')  # push, pull, both
        
        if direction not in ['push', 'pull', 'both']:
            return jsonify({
                'success': False,
                'error': 'Invalid direction. Must be push, pull, or both',
                'errorCode': 'INVALID_DIRECTION'
            }), 400
        
        # Load project config and source config
        config = load_project_config(project_id)
        sources = config.get('sources', {})
        normalized_sources = normalize_sources(sources, project_id)
        
        if source_key not in normalized_sources:
            return jsonify({
                'success': False,
                'error': f'Source {source_key} not configured',
                'errorCode': 'SOURCE_NOT_CONFIGURED'
            }), 404
        
        source_config = normalized_sources[source_key]
        
        # Start async sync
        job_id, success, error_code, error_msg = source_sync_service.start_sync(
            project_id=project_id,
            source_key=source_key,
            source_config=source_config,
            direction=direction,
            actor='user'
        )
        
        if not success:
            return jsonify({
                'success': False,
                'error': error_msg,
                'errorCode': error_code
            }), 400
        
        # После успешной синхронизации из git проверяем все inventory файлы и создаем папки group_vars/host_vars
        # Это делается асинхронно, так как sync может быть долгим
        # Функция ensure_all_inventory_dirs будет вызвана после завершения sync через callback или отдельный endpoint
        
        return jsonify({
            'success': True,
            'jobId': job_id,
            'message': f'Sync started: {direction}'
        })
        
    except Exception as e:
        app.logger.error(f"Error starting sync: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR'
        }), 500


@app.route('/api/projects/<project_id>/sources/<source_key>/sync/state', methods=['GET'])
@require_auth
def api_get_sync_state(project_id, source_key):
    """
    API: Get sync state for a source
    
    Returns:
    {
        "success": true,
        "state": {
            "lastPushAt": 1234567890,
            "lastPullAt": 1234567890,
            "lastPushStatus": "ok|running|failed|idle",
            "lastPullStatus": "ok|running|failed|idle",
            "lastPushError": null,
            "lastPullError": null,
            "lastPushRevision": "abc123...",
            "lastPullRevision": "def456..."
        }
    }
    """
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        # Get sync state
        state = source_sync_service.get_sync_state(project_id, source_key)
        
        return jsonify({
            'success': True,
            'state': state
        })
        
    except Exception as e:
        app.logger.error(f"Error getting sync state: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR'
        }), 500


@app.route('/api/projects/<project_id>/sources/<source_key>/sync/check-conflict', methods=['GET'])
@require_auth
def api_check_sync_conflict(project_id, source_key):
    """
    API: Check for sync conflicts without performing sync
    
    Returns:
    {
        "success": true,
        "hasConflict": bool,
        "message": str or null,
        "details": {
            "storageChanged": bool,
            "externalChanged": bool,
            "lastPushAt": timestamp or null,
            "lastPullAt": timestamp or null
        } or null
    }
    """
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        # Load source config
        config = load_project_config(project_id)
        sources = config.get('sources', {})
        normalized_sources = normalize_sources(sources, project_id)
        
        if source_key not in normalized_sources:
            return jsonify({
                'success': False,
                'error': 'Source not found',
                'errorCode': 'SOURCE_NOT_FOUND'
            }), 404
        
        source_config = normalized_sources[source_key]
        
        # Check for conflicts
        conflict_result = source_sync_service.check_conflict(project_id, source_key, source_config)
        
        return jsonify({
            'success': True,
            'hasConflict': conflict_result['hasConflict'],
            'message': conflict_result['message'],
            'details': conflict_result['details']
        })
        
    except Exception as e:
        app.logger.error(f"Error checking sync conflict: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR'
        }), 500


@app.route('/api/projects/<project_id>/sources/sync', methods=['POST'])
@require_auth
def api_sync_project_source(project_id):
    """API: Sync a git source (fetch/clone and checkout) - Legacy endpoint for backward compatibility"""
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        data = request.json or {}
        source_key = data.get('sourceKey')
        
        if not source_key:
            return jsonify({'success': False, 'error': 'sourceKey is required', 'errorCode': 'MISSING_SOURCE_KEY'}), 400
        
        # Load project config
        config = load_project_config(project_id)
        sources = config.get('sources', {})
        
        # Normalize sources
        normalized_sources = normalize_sources(sources, project_id)
        
        if source_key not in normalized_sources:
            return jsonify({
                'success': False,
                'error': f'Source {source_key} not found',
                'errorCode': 'SOURCE_NOT_FOUND'
            }), 404
        
        source_config = normalized_sources[source_key]
        mode = source_config.get('mode', 'local')
        
        if mode != 'git':
            return jsonify({
                'success': False,
                'error': f'Source {source_key} is not a git source (mode: {mode})',
                'errorCode': 'NOT_GIT_SOURCE'
            }), 400
        
        git_config = source_config.get('git', {})
        repo_url = git_config.get('repo')
        ref = git_config.get('ref', 'main')
        subdir = git_config.get('subdir', '')
        auth_secret_id = git_config.get('authSecretId')
        
        if not repo_url:
            return jsonify({
                'success': False,
                'error': 'Repository URL is required',
                'errorCode': 'MISSING_REPO_URL'
            }), 400
        
        # Sync using SourceSyncService (supports repoLayout mapping)
        try:
            # Use source_sync_service for proper repoLayout support
            success, error_code, error_message = source_sync_service.execute_sync(
                project_id=project_id,
                source_key=source_key,
                source_config=source_config,
                direction='pull'
            )
            
            if not success:
                return jsonify({
                    'success': False,
                    'error': error_message or 'Sync failed',
                    'errorCode': error_code or 'SYNC_FAILED'
                }), 500
            
            # Update last synced timestamp in project config
            if 'syncTimestamps' not in config:
                config['syncTimestamps'] = {}
            config['syncTimestamps'][source_key] = int(time.time())
            save_project_config(project_id, config)
            
            # Get resolved path for response (for backward compatibility)
            try:
                resolved_path = git_source_manager.resolve_path(
                    project_id=project_id,
                    source_key=source_key,
                    repo_url=repo_url,
                    ref=ref,
                    subdir=subdir,
                    auth_secret_id=auth_secret_id,
                    force_refresh=False  # Use cached version
                )
            except Exception as e:
                app.logger.warning(f"Could not resolve path for response: {e}")
                resolved_path = None
            
            app.logger.info(f"Source synced: {project_id}/{source_key}")
            
            # После синхронизации из git проверяем все inventory файлы и создаем папки group_vars/host_vars
            if source_key == 'repo' or subdir == '' or 'inventor' in subdir.lower():
                try:
                    ensure_all_inventory_dirs(project_id)
                except Exception as e:
                    app.logger.warning(f"Failed to ensure inventory directories after sync: {e}")
            
            return jsonify({
                'success': True,
                'ok': True,
                'syncedAt': config['syncTimestamps'][source_key],
                'details': {
                    'repo': repo_url,
                    'ref': ref,
                    'subdir': subdir,
                    'resolvedPath': str(resolved_path) if resolved_path else None,
                    'exists': resolved_path.exists() if resolved_path else False,
                    'isFile': resolved_path.is_file() if resolved_path else False,
                    'isDir': resolved_path.is_dir() if resolved_path else False
                }
            })
            
        except GitSourceError as e:
            app.logger.error(f"Git sync error for {source_key}: {e.error_code} - {e.message}")
            return jsonify({
                'success': False,
                'error': e.message,
                'errorCode': e.error_code,
                'details': {}
            }), 400
        except Exception as e:
            app.logger.error(f"Error syncing source {source_key}: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'error': f'Error syncing source: {str(e)}',
                'errorCode': 'SYNC_ERROR',
                'details': {}
            }), 500
            
    except Exception as e:
        app.logger.error(f"Error in api_sync_project_source: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR',
            'details': {}
        }), 500


@app.route('/api/inventory/ensure-dirs', methods=['POST'])
@require_auth
def api_ensure_inventory_dirs():
    """API: Проверить все inventory файлы и создать папки group_vars/host_vars рядом с ними"""
    try:
        project_id = require_project_id_from_request()
        if not project_id:
            return jsonify({'success': False, 'error': 'Project ID is required'}), 400
        
        ensure_all_inventory_dirs(project_id)
        
        return jsonify({
            'success': True,
            'message': 'Inventory directories checked and created if needed'
        })
    except Exception as e:
        app.logger.error(f"Error ensuring inventory directories: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/autosync', methods=['GET'])
@require_auth
def api_get_autosync_config(project_id):
    """
    API: Get autosync configuration for a project
    
    Returns:
    {
        "success": true,
        "autosync": {
            "enabled": false,
            "direction": "pull",
            "sourceKeys": [],
            "intervalSeconds": 3600,
            "lastRunAt": null,
            "nextRunAt": null,
            "lastStatus": null,
            "lastError": null
        }
    }
    """
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        # Load project config
        config = load_project_config(project_id)
        autosync = config.get('autosync', {})
        
        # Return default structure if autosync not configured
        default_autosync = {
            'enabled': False,
            'direction': 'pull',
            'sourceKeys': [],
            'intervalSeconds': 3600,
            'lastRunAt': None,
            'nextRunAt': None,
            'lastStatus': None,
            'lastError': None
        }
        
        # Merge with defaults
        result_autosync = {**default_autosync, **autosync}
        
        return jsonify({
            'success': True,
            'autosync': result_autosync
        })
        
    except Exception as e:
        app.logger.error(f"Error getting autosync config: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR'
        }), 500


@app.route('/api/projects/<project_id>/autosync', methods=['PUT'])
@require_auth
def api_update_autosync_config(project_id):
    """
    API: Update autosync configuration for a project
    
    Request body:
    {
        "enabled": true,
        "direction": "pull|push|both",
        "sourceKeys": ["inventory", "roles_playbooks"],
        "intervalSeconds": 3600
    }
    
    Returns:
    {
        "success": true,
        "autosync": { ... }
    }
    """
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found', 'errorCode': 'PROJECT_NOT_FOUND'}), 404
        
        data = request.json or {}
        
        # Validate direction
        direction = data.get('direction', 'pull')
        if direction not in ['push', 'pull', 'both']:
            return jsonify({
                'success': False,
                'error': 'Invalid direction. Must be push, pull, or both',
                'errorCode': 'INVALID_DIRECTION'
            }), 400
        
        # Validate intervalSeconds
        interval_seconds = data.get('intervalSeconds', 3600)
        if not isinstance(interval_seconds, int) or interval_seconds < 60:
            return jsonify({
                'success': False,
                'error': 'intervalSeconds must be an integer >= 60',
                'errorCode': 'INVALID_INTERVAL'
            }), 400
        
        # Validate sourceKeys
        source_keys = data.get('sourceKeys', [])
        if not isinstance(source_keys, list):
            return jsonify({
                'success': False,
                'error': 'sourceKeys must be a list',
                'errorCode': 'INVALID_SOURCE_KEYS'
            }), 400
        
        # Load project config
        config = load_project_config(project_id)
        
        # Get existing autosync config or create new
        autosync = config.get('autosync', {})
        
        # Update autosync config
        enabled = data.get('enabled', False)
        autosync['enabled'] = enabled
        autosync['direction'] = direction
        autosync['sourceKeys'] = source_keys
        autosync['intervalSeconds'] = interval_seconds
        
        # Calculate nextRunAt if enabled
        import time
        if enabled:
            current_time = int(time.time())
            # If nextRunAt is not set or in the past, schedule for now + interval
            next_run_at = autosync.get('nextRunAt')
            if not next_run_at or next_run_at <= current_time:
                autosync['nextRunAt'] = current_time + interval_seconds
        else:
            # Clear nextRunAt if disabled
            autosync['nextRunAt'] = None
        
        # Save updated config
        config['autosync'] = autosync
        save_project_config(project_id, config)
        
        app.logger.info(f"Autosync config updated for project {project_id}: enabled={enabled}, direction={direction}, sources={source_keys}")
        
        return jsonify({
            'success': True,
            'autosync': autosync
        })
        
    except Exception as e:
        app.logger.error(f"Error updating autosync config: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'errorCode': 'INTERNAL_ERROR'
        }), 500


@app.route('/api/projects/<project_id>/sources/resolve', methods=['POST'])
@require_auth
def api_resolve_source_path(project_id):
    """API: Resolve path for a git source (INTERNAL USE ONLY - for sync operations)
    
    WARNING: This endpoint returns git cache paths which are internal implementation details.
    UI should NOT rely on these paths. Use Project Storage paths instead.
    """
    try:
        # Check project exists
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        data = request.json or {}
        source_key = data.get('sourceKey')
        repo_url = data.get('repoUrl')
        ref = data.get('ref', 'main')
        subdir = data.get('subdir', '')
        auth_secret_id = data.get('authSecretId')
        force_refresh = data.get('forceRefresh', False)
        
        if not source_key:
            return jsonify({'success': False, 'error': 'sourceKey is required'}), 400
        if not repo_url:
            return jsonify({'success': False, 'error': 'repoUrl is required'}), 400
        
        # Resolve path using GitSourceManager (returns git cache path - internal only)
        try:
            resolved_path = git_source_manager.resolve_path(
                project_id=project_id,
                source_key=source_key,
                repo_url=repo_url,
                ref=ref,
                subdir=subdir,
                auth_secret_id=auth_secret_id,
                force_refresh=force_refresh
            )
            
            # Return logical identifier instead of absolute path
            # UI should use Project Storage paths, not git cache paths
            return jsonify({
                'success': True,
                'sourceKey': source_key,
                'repoUrl': repo_url,
                'ref': ref,
                'subdir': subdir,
                'exists': resolved_path.exists(),
                'isFile': resolved_path.is_file(),
                'isDir': resolved_path.is_dir(),
                # Internal path - should not be used by UI
                '_internalPath': str(resolved_path) if resolved_path.exists() else None
            })
        except GitSourceError as e:
            return jsonify({
                'success': False,
                'error': e.message,
                'errorCode': e.error_code
            }), 400
    except Exception as e:
        app.logger.error(f"Error resolving source path: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>', methods=['DELETE'])
@require_auth
def api_delete_project(project_id):
    """API: Удалить проект навсегда (hard delete)"""
    try:
        projects = load_projects()
        
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Разрешаем удаление Default Project (пользователь может удалить его, если хочет)
        # Предупреждение будет показано на frontend
        
        # Hard delete: удаляем из списка проектов
        projects = [p for p in projects if p.get('id') != project_id]
        save_projects(projects)
        
        # Удаляем директорию проекта (опционально, можно оставить для восстановления)
        project_dir = get_project_dir(project_id)
        if project_dir.exists():
            import shutil
            try:
                shutil.rmtree(project_dir)
                app.logger.info(f"Project directory deleted: {project_dir}")
            except Exception as e:
                app.logger.warning(f"Failed to delete project directory {project_dir}: {e}")
        
        app.logger.info(f"Project deleted permanently: {project_id}")
        return jsonify({'success': True, 'message': 'Project deleted permanently'})
    except Exception as e:
        app.logger.error(f"Error deleting project: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/restore', methods=['POST'])
@require_auth
def api_restore_project(project_id):
    """API: Восстановить архивный проект"""
    try:
        projects = load_projects()
        
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Проверяем, что проект действительно архивный
        if not project.get('isArchived', False):
            return jsonify({'success': False, 'error': 'Project is not archived'}), 400
        
        # Восстанавливаем проект
        project['isArchived'] = False
        project['updatedAt'] = time.time()
        
        save_projects(projects)
        app.logger.info(f"Project restored: {project_id}")
        return jsonify({'success': True, 'project': project})
    except Exception as e:
        app.logger.error(f"Error restoring project: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/switch', methods=['POST'])
@require_auth
def api_switch_project(project_id):
    """API: Переключиться на проект (валидация существования)"""
    try:
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id and not p.get('isArchived', False)), None)
        
        if not project:
            return jsonify({'success': False, 'error': 'Project not found or archived'}), 404
        
        return jsonify({'success': True, 'project': project})
    except Exception as e:
        app.logger.error(f"Error switching project: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# PLAYBOOK API ENDPOINTS
# ============================================================================

@app.route('/api/projects/<project_id>/playbooks', methods=['GET'])
@require_auth
def api_list_playbooks(project_id):
    """API: Получить список всех playbooks проекта"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        playbooks = playbook_storage.list_playbooks(project_id)
        
        # Получаем inventory и roles для валидации
        project_inventory_file = get_project_inventory_file(project_id)
        inventory_files = [str(project_inventory_file)] if project_inventory_file.exists() else []
        inventory_groups = get_inventory_groups(inventory_files) if inventory_files else {}
        
        # Получаем список roles
        roles_tree = scan_roles_storage(project_id)
        available_roles = []
        def extract_roles(node):
            if node.get('type') == 'role':
                role_path = node.get('path', '')
                if role_path:
                    available_roles.append(role_path)
                else:
                    role_id = node.get('id', '')
                    if role_id:
                        available_roles.append(role_id)
                    else:
                        available_roles.append(node.get('name', ''))
            for child in node.get('children', []):
                extract_roles(child)
        for role_node in roles_tree:
            extract_roles(role_node)
        
        # Валидируем каждый playbook и добавляем статус валидации
        validator = PlaybookValidator(
            inventory_groups=inventory_groups,
            available_roles=available_roles
        )
        
        for playbook in playbooks:
            try:
                # Загружаем полный playbook для валидации
                full_playbook = playbook_storage.get_playbook(project_id, playbook['id'])
                if full_playbook:
                    validation_result = validator.validate(full_playbook)
                    # Статус валидации: True если playbook_can_run, иначе False
                    playbook['validation_status'] = validation_result['summary']['playbook_can_run']
                    playbook['validation_errors_count'] = validation_result['summary']['total_errors']
                else:
                    playbook['validation_status'] = False
                    playbook['validation_errors_count'] = 0
            except Exception as e:
                app.logger.warning(f"Error validating playbook {playbook['id']}: {e}")
                playbook['validation_status'] = False
                playbook['validation_errors_count'] = 0
        
        # Параметры сортировки
        sort = request.args.get('sort', 'updated_at')
        order = request.args.get('order', 'desc')
        
        if sort == 'name':
            playbooks.sort(key=lambda x: x.get('name', '').lower(), reverse=(order == 'desc'))
        elif sort == 'created_at':
            playbooks.sort(key=lambda x: x.get('created_at', ''), reverse=(order == 'desc'))
        else:  # updated_at
            playbooks.sort(key=lambda x: x.get('updated_at', ''), reverse=(order == 'desc'))
        
        return jsonify({
            'success': True,
            'playbooks': playbooks,
            'total': len(playbooks)
        })
    except Exception as e:
        app.logger.error(f"Error listing playbooks for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks', methods=['POST'])
@require_auth
def api_create_playbook(project_id):
    """API: Создать новый playbook"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        data = request.json or {}
        name = data.get('name', '').strip()
        
        if not name:
            return jsonify({'success': False, 'error': 'Playbook name is required'}), 400
        
        # Проверка конфликта имени
        if playbook_storage.check_name_conflict(project_id, name):
            return jsonify({'success': False, 'error': f'Playbook with name "{name}" already exists'}), 409
        
        description = data.get('description', '').strip()
        playbook = playbook_storage.create_playbook(project_id, name, description)
        
        if playbook:
            return jsonify({'success': True, 'playbook': playbook}), 201
        else:
            return jsonify({'success': False, 'error': 'Failed to create playbook'}), 500
            
    except Exception as e:
        app.logger.error(f"Error creating playbook for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>', methods=['GET'])
@require_auth
def api_get_playbook(project_id, playbook_id):
    """API: Получить playbook по ID"""
    try:
        playbook = playbook_storage.get_playbook(project_id, playbook_id)
        
        if not playbook:
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        return jsonify({'success': True, 'playbook': playbook})
    except Exception as e:
        app.logger.error(f"Error getting playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>', methods=['PUT'])
@require_auth
def api_update_playbook(project_id, playbook_id):
    """API: Обновить playbook"""
    try:
        # Проверяем существование playbook
        existing_playbook = playbook_storage.get_playbook(project_id, playbook_id)
        if not existing_playbook:
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        data = request.json or {}
        
        # Обновляем поля
        if 'name' in data:
            new_name = data['name'].strip()
            if not new_name:
                return jsonify({'success': False, 'error': 'Playbook name cannot be empty'}), 400
            
            # Проверка конфликта имени (исключая текущий playbook)
            if playbook_storage.check_name_conflict(project_id, new_name, exclude_playbook_id=playbook_id):
                return jsonify({'success': False, 'error': f'Playbook with name "{new_name}" already exists'}), 409
            
            existing_playbook['name'] = new_name
        
        if 'description' in data:
            existing_playbook['description'] = data['description'].strip()
        
        if 'plays' in data:
            existing_playbook['plays'] = data['plays']
        
        # Сохраняем теги и метаданные
        if 'tags' in data:
            existing_playbook['tags'] = data['tags']
        if 'metadata' in data:
            if 'metadata' not in existing_playbook:
                existing_playbook['metadata'] = {}
            # Обновляем метаданные, сохраняя существующие
            existing_playbook['metadata'].update(data['metadata'])
        if 'tag' in data:
            existing_playbook['tag'] = data['tag']
        
        # Сохраняем disabled статус
        if 'disabled' in data:
            if 'metadata' not in existing_playbook:
                existing_playbook['metadata'] = {}
            existing_playbook['metadata']['disabled'] = data['disabled']
            existing_playbook['disabled'] = data['disabled']
        
        # Сохраняем
        if playbook_storage.save_playbook(project_id, existing_playbook):
            return jsonify({'success': True, 'playbook': existing_playbook})
        else:
            return jsonify({'success': False, 'error': 'Failed to save playbook'}), 500
            
    except Exception as e:
        app.logger.error(f"Error updating playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>', methods=['DELETE'])
@require_auth
def api_delete_playbook(project_id, playbook_id):
    """API: Удалить playbook"""
    try:
        # Проверяем существование playbook
        if not playbook_storage.playbook_exists(project_id, playbook_id):
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        # TODO: Проверка использования в executions (если нужно блокировать удаление)
        
        if playbook_storage.delete_playbook(project_id, playbook_id):
            return jsonify({'success': True}), 204
        else:
            return jsonify({'success': False, 'error': 'Failed to delete playbook'}), 500
            
    except Exception as e:
        app.logger.error(f"Error deleting playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/upload', methods=['POST'])
@require_auth
def api_upload_playbook(project_id):
    """API: Загрузить YAML playbook и преобразовать в модель"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Получаем файл
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        # Проверяем расширение
        if not (file.filename.endswith('.yml') or file.filename.endswith('.yaml')):
            return jsonify({'success': False, 'error': 'Only .yml and .yaml files are allowed'}), 400
        
        # Читаем содержимое
        yaml_content = file.read().decode('utf-8')
        
        # Валидируем YAML структуру
        is_valid, error_message = playbook_parser.validate_yaml_structure(yaml_content)
        if not is_valid:
            return jsonify({'success': False, 'error': error_message}), 400
        
        # Парсим YAML
        playbook_name = request.form.get('name') or file.filename.rsplit('.', 1)[0]
        try:
            playbook = playbook_parser.parse(yaml_content, playbook_name)
        except PlaybookParseError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        
        # Проверяем конфликт имени
        if playbook_storage.check_name_conflict(project_id, playbook['name']):
            return jsonify({'success': False, 'error': f'Playbook with name "{playbook["name"]}" already exists'}), 409
        
        # Получаем inventory и roles для валидации
        project_inventory_file = get_project_inventory_file(project_id)
        inventory_files = [str(project_inventory_file)] if project_inventory_file.exists() else []
        inventory_groups = get_inventory_groups(inventory_files) if inventory_files else {}
        
        # Получаем список roles
        roles_tree = scan_roles_storage(project_id)
        available_roles = []
        def extract_roles(node):
            if node.get('type') == 'role':
                # Используем полный путь роли (pack/role), а не только имя
                role_path = node.get('path', '')
                if role_path:
                    available_roles.append(role_path)
                else:
                    # Fallback: если path нет, формируем из id (pack/role)
                    role_id = node.get('id', '')
                    if role_id:
                        available_roles.append(role_id)
                    else:
                        # Последний fallback: только имя
                        available_roles.append(node.get('name', ''))
            for child in node.get('children', []):
                extract_roles(child)
        for role_node in roles_tree:
            extract_roles(role_node)
        
        # Валидируем playbook
        validator = PlaybookValidator(
            inventory_groups=inventory_groups,
            available_roles=available_roles
        )
        validation_result = validator.validate(playbook)
        
        # Сохраняем playbook
        playbook_id = str(uuid.uuid4())
        playbook['id'] = playbook_id
        playbook['project_id'] = project_id
        
        if not playbook_storage.save_playbook(project_id, playbook):
            return jsonify({'success': False, 'error': 'Failed to save playbook'}), 500
        
        # Возвращаем результат с предупреждениями
        warnings = []
        for play_validation in validation_result['plays']:
            warnings.extend(play_validation['warnings'])
        warnings.extend(validation_result['playbook']['warnings'])
        
        return jsonify({
            'success': True,
            'playbook': playbook,
            'warnings': [w for w in warnings]
        }), 201
        
    except Exception as e:
        app.logger.error(f"Error uploading playbook for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/clone', methods=['POST'])
@require_auth
def api_clone_playbook(project_id, playbook_id):
    """API: Клонировать playbook"""
    try:
        data = request.json or {}
        new_name = data.get('name', '').strip()
        
        if not new_name:
            return jsonify({'success': False, 'error': 'New playbook name is required'}), 400
        
        # Проверка конфликта имени
        if playbook_storage.check_name_conflict(project_id, new_name):
            return jsonify({'success': False, 'error': f'Playbook with name "{new_name}" already exists'}), 409
        
        new_description = data.get('description', '').strip()
        new_playbook = playbook_storage.clone_playbook(project_id, playbook_id, new_name, new_description)
        
        if new_playbook:
            return jsonify({'success': True, 'playbook': new_playbook}), 201
        else:
            return jsonify({'success': False, 'error': 'Failed to clone playbook'}), 500
            
    except Exception as e:
        app.logger.error(f"Error cloning playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/download', methods=['GET'])
@require_auth
def api_download_playbook(project_id, playbook_id):
    """API: Скачать playbook в формате YAML"""
    try:
        playbook = playbook_storage.get_playbook(project_id, playbook_id)
        
        if not playbook:
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        # Генерируем YAML
        yaml_content = playbook_generator.generate(playbook)
        
        # Создаем response с YAML
        playbook_name = playbook.get('name', 'playbook')
        filename = f"{playbook_name}.yml"
        
        response = Response(
            yaml_content,
            mimetype='text/yaml',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
        
        return response
        
    except Exception as e:
        app.logger.error(f"Error downloading playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/validate', methods=['POST'])
@require_auth
def api_validate_playbook(project_id, playbook_id):
    """API: Валидировать playbook"""
    try:
        data = request.json or {}
        playbook = data.get('playbook')
        inventory_groups = data.get('inventory_groups', {})
        available_roles = data.get('available_roles', [])
        
        if not playbook:
            return jsonify({'success': False, 'error': 'Playbook data is required'}), 400
        
        # Создаем валидатор
        validator = PlaybookValidator(
            inventory_groups=inventory_groups,
            available_roles=available_roles
        )
        
        # Валидируем
        validation_result = validator.validate(playbook)
        
        return jsonify({
            'success': True,
            'validation': validation_result
        })
        
    except Exception as e:
        app.logger.error(f"Error validating playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/preview', methods=['POST'])
@require_auth
def api_preview_playbook(project_id, playbook_id):
    """API: Предпросмотр YAML playbook"""
    try:
        data = request.json or {}
        playbook = data.get('playbook')
        
        if not playbook:
            return jsonify({'success': False, 'error': 'Playbook data is required'}), 400
        
        # Генерируем YAML
        yaml_content = playbook_generator.generate(playbook)
        
        return jsonify({
            'success': True,
            'yaml': yaml_content
        })
        
    except Exception as e:
        app.logger.error(f"Error previewing playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/reorder', methods=['POST'])
@require_auth
def api_reorder_playbooks(project_id):
    """API: Изменить порядок playbooks"""
    try:
        data = request.json or {}
        playbook_orders = data.get('playbook_orders', [])
        
        if not playbook_orders:
            return jsonify({'success': False, 'error': 'playbook_orders is required'}), 400
        
        # Обновляем порядок для каждого playbook
        updated_count = 0
        for item in playbook_orders:
            playbook_id = item.get('playbook_id')
            order = item.get('order')
            
            if playbook_id is None or order is None:
                continue
            
            # Получаем существующий playbook
            playbook = playbook_storage.get_playbook(project_id, playbook_id)
            if not playbook:
                continue
            
            # Обновляем order в метаданных
            if 'metadata' not in playbook:
                playbook['metadata'] = {}
            playbook['metadata']['order'] = order
            
            # Сохраняем playbook (save_playbook обновит updated_at, но это нормально)
            if playbook_storage.save_playbook(project_id, playbook):
                updated_count += 1
        
        return jsonify({
            'success': True,
            'updated_count': updated_count
        })
        
    except Exception as e:
        app.logger.error(f"Error reordering playbooks for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/run', methods=['POST'])
@require_auth
def api_run_playbook(project_id, playbook_id):
    """API: Запустить сохраненный playbook - создает QUEUED run"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Загружаем playbook
        playbook = playbook_storage.get_playbook(project_id, playbook_id)
        if not playbook:
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        # Получаем опциональные параметры из запроса
        data = request.json or {}
        inventory_files = data.get('inventory_files')
        ansible_config = data.get('ansible_config')
        # Execution parameters (optional)
        check_mode = data.get('check_mode', False)
        verbosity = (data.get('verbosity') or '').strip() or None  # '', '-v', '-vv', '-vvv', '-vvvv'
        forks = data.get('forks')
        force_handlers = data.get('force_handlers', False)
        connection_timeout = data.get('connection_timeout')
        
        # Если inventory_files не указаны, определяем их на основе хостов/групп из плейбука
        if not inventory_files:
            found_inventory_files = find_inventory_files_for_playbook(project_id, playbook)
            if found_inventory_files:
                inventory_files = found_inventory_files
                app.logger.info(f"[api_run_playbook] Auto-detected inventory files from playbook: {inventory_files}")
            else:
                # Fallback на default inventory
                inventory_files = ['inventory.yml']
                app.logger.info(f"[api_run_playbook] No inventory files found for playbook hosts/groups, using default: {inventory_files}")
        else:
            app.logger.info(f"[api_run_playbook] Using inventory files from request: {inventory_files}")
        
        # Получаем project_dir для логирования путей
        project_dir = get_project_dir(project_id)
        
        # Логируем полученные параметры подробно
        app.logger.info(f"[api_run_playbook] ========== Playbook Run Request ==========")
        app.logger.info(f"[api_run_playbook] Project ID: {project_id}")
        app.logger.info(f"[api_run_playbook] Playbook ID: {playbook_id}")
        app.logger.info(f"[api_run_playbook] Playbook Name: {playbook.get('name', 'Unknown')}")
        app.logger.info(f"[api_run_playbook] Inventory Files: {inventory_files}")
        app.logger.info(f"[api_run_playbook] Ansible Config: {ansible_config}")
        app.logger.info(f"[api_run_playbook] Project Directory: {project_dir}")
        
        # Определяем полные пути к файлам для логирования
        repo_dir = project_dir / 'repo'
        inventory_paths = []
        for inv_file in inventory_files:
            # Путь может быть относительным от repo (например, "inventories/invent.yaml")
            # или просто именем файла (например, "inventory.yml")
            if inv_file.startswith('inventories/') or ('.' in inv_file and '/' in inv_file):
                inv_path = repo_dir / inv_file
            elif inv_file == 'inventory.yml':
                inv_path = repo_dir / 'inventory.yml'
            else:
                # Ищем в inventories директории
                inventories_dir = repo_dir / 'inventories'
                if inventories_dir.exists():
                    found = False
                    for inv_file_search in inventories_dir.rglob(inv_file):
                        if inv_file_search.is_file():
                            inv_path = inv_file_search
                            found = True
                            break
                    if not found:
                        inv_path = repo_dir / inv_file
                else:
                    inv_path = repo_dir / inv_file
            
            if inv_path.exists():
                inventory_paths.append(str(inv_path.resolve()))
            else:
                inventory_paths.append(f"{inv_file} (not found at {inv_path})")
        app.logger.info(f"[api_run_playbook] Inventory File Paths: {inventory_paths}")
        
        # Определяем путь к ansible.cfg
        ansible_config_path = None
        if ansible_config:
            ansible_config_path = resolve_ansible_config_path(project_id, ansible_config)
        
        if ansible_config_path and ansible_config_path.exists():
            app.logger.info(f"[api_run_playbook] Ansible Config Path: {ansible_config_path.resolve()}")
        else:
            app.logger.warning(f"[api_run_playbook] Ansible Config Path: {ansible_config_path} (not found, will use default)")
        
        app.logger.info(f"[api_run_playbook] ==========================================")
        
        # Если ansible_config не передан, возвращаем ошибку
        if not ansible_config:
            app.logger.error(f"[api_run_playbook] ansible_config not provided in request")
            return jsonify({'success': False, 'error': 'ansible_config is required'}), 400
        
        # Генерируем YAML из playbook
        try:
            yaml_content = playbook_generator.generate(playbook)
        except Exception as e:
            app.logger.error(f"Error generating YAML from playbook {playbook_id}: {e}")
            return jsonify({'success': False, 'error': f'Failed to generate playbook YAML: {str(e)}'}), 500
        
        # Проверяем, что yaml_content - строка, и она не пустая
        if not yaml_content or not isinstance(yaml_content, str) or not yaml_content.strip():
            return jsonify({'success': False, 'error': 'Playbook is empty'}), 400
        
        # Получаем repo директорию для работы с inventory файлами
        repo_dir = project_dir / 'repo'
        
        # Создаем execution record со статусом QUEUED (сначала создаем execution, чтобы получить execution_id)
        execution_id = None
        settings = load_execution_settings()
        if settings.get('save_history', True):
            # Получаем inventory snapshot
            inventory_snapshot = {'groups': []}
            try:
                # Правильно определяем пути к inventory файлам
                inv_files_list = []
                for inv_file in inventory_files:
                    # Путь может быть относительным от repo (например, "inventories/invent.yaml")
                    # или просто именем файла (например, "inventory.yml")
                    if inv_file.startswith('inventories/') or ('.' in inv_file and '/' in inv_file):
                        inv_path = repo_dir / inv_file
                    elif inv_file == 'inventory.yml':
                        inv_path = repo_dir / 'inventory.yml'
                    else:
                        # Ищем в inventories директории
                        inventories_dir = repo_dir / 'inventories'
                        if inventories_dir.exists():
                            found = False
                            for inv_file_search in inventories_dir.rglob(inv_file):
                                if inv_file_search.is_file():
                                    inv_path = inv_file_search
                                    found = True
                                    break
                            if not found:
                                inv_path = repo_dir / inv_file
                        else:
                            inv_path = repo_dir / inv_file
                    
                    if inv_path.exists():
                        inv_files_list.append(str(inv_path))
                
                if not inv_files_list:
                    default_inv = get_project_inventory_file(project_id)
                    if default_inv.exists():
                        inv_files_list = [str(default_inv)]
                
                if inv_files_list:
                    groups = get_inventory_groups(inv_files_list)
                    for group_name, group_data in groups.items():
                        group_hosts = group_data.get('hosts', [])
                        inventory_snapshot['groups'].append({
                            'groupId': group_name,
                            'groupName': group_name,
                            'hosts': [{'hostId': host, 'ip': host} for host in group_hosts]
                        })
            except Exception as e:
                app.logger.warning(f"Error creating inventory snapshot: {e}")
            
            # Извлекаем hosts из playbook plays
            all_hosts = set()
            for play in playbook.get('plays', []):
                hosts_value = play.get('hosts', '')
                if hosts_value and hosts_value != 'all':
                    if isinstance(hosts_value, list):
                        for host in hosts_value:
                            if host and host != 'all':
                                all_hosts.add(str(host).strip())
                    elif isinstance(hosts_value, str):
                        if ',' in hosts_value:
                            all_hosts.update(h.strip() for h in hosts_value.split(','))
                        else:
                            all_hosts.add(hosts_value.strip())
                    else:
                        all_hosts.add(str(hosts_value).strip())
            
            # Извлекаем roles из playbook plays
            all_roles = []
            for play in playbook.get('plays', []):
                for role in play.get('roles', []):
                    role_name = role.get('role_name') or role.get('role', '')
                    if role_name and role_name not in all_roles:
                        all_roles.append(role_name)
            
            # Генерируем execution_id заранее, чтобы создать playbook файл до создания execution record
            # Это устраняет race condition: worker может claim execution до обновления runParams
            execution_id = str(uuid.uuid4())
            
            # Сохраняем сгенерированный playbook в runtime/generated_playbooks/<execution_id>.yml
            generated_playbook_path = get_project_generated_playbook_path(project_id, execution_id)
            with open(generated_playbook_path, 'w', encoding='utf-8') as f:
                f.write(yaml_content)
            temp_playbook = generated_playbook_path
            
            # Стратегия из первого play (для worker: Mitogen strategy_plugins)
            plays_list = playbook.get('plays', [])
            strategy = plays_list[0].get('strategy', 'linear') if plays_list else 'linear'
            # vault_id из первого play с vars_files (для worker: --vault-password-file)
            vault_id = None
            for p in plays_list:
                if p.get('vars_files') and p.get('vault_id'):
                    vault_id = p.get('vault_id')
                    break

            # Подготавливаем runParams с правильным путем к playbook и параметрами выполнения
            run_params = {
                'temp_playbook': str(temp_playbook),
                'inventory_files': inventory_files,
                'ansible_config': ansible_config,
                'project_dir': str(get_project_dir(project_id)),
                'check_mode': bool(check_mode),
                'verbosity': verbosity,
                'forks': int(forks) if forks is not None and str(forks).strip() != '' else None,
                'force_handlers': bool(force_handlers),
                'connection_timeout': int(connection_timeout) if connection_timeout is not None and str(connection_timeout).strip() != '' else None,
                'strategy': strategy,
                'vault_id': vault_id,
            }
            
            # Логируем runParams, которые будут использоваться worker'ом
            app.logger.info(f"[api_run_playbook] Execution ID: {execution_id}")
            app.logger.info(f"[api_run_playbook] Run Parameters (runParams):")
            app.logger.info(f"[api_run_playbook]   - temp_playbook: {run_params['temp_playbook']}")
            app.logger.info(f"[api_run_playbook]   - inventory_files: {run_params['inventory_files']}")
            app.logger.info(f"[api_run_playbook]   - ansible_config: {run_params['ansible_config']}")
            app.logger.info(f"[api_run_playbook]   - project_dir: {run_params['project_dir']}")
            
            # Создаем execution record сразу с правильными runParams (устраняет race condition)
            execution_data = {
                'playbookName': playbook.get('name', 'playbook'),
                'playbookId': playbook_id,
                'mode': 'PER_GROUP',
                'inventorySnapshot': inventory_snapshot,
                'selectionSnapshot': {
                    'playbookId': playbook_id,
                    'playbookName': playbook.get('name', 'playbook')
                },
                'stats': {
                    'hostsTargeted': len(all_hosts) if all_hosts else 0,
                    'totalRoleExecutions': len(all_roles)
                },
                'warnings': [],
                'status': 'QUEUED',
                'runParams': run_params  # Создаем сразу с правильными runParams
            }
            
            # Создаем execution record с предопределенным execution_id и готовыми runParams
            execution_id_created = create_execution_record(execution_data, project_id=project_id, execution_id=execution_id)
            if execution_id_created:
                app.logger.debug(f"Created QUEUED execution record: {execution_id_created} for playbook {playbook_id} with runParams")
            else:
                app.logger.error(f"Failed to create execution record for playbook {playbook_id}")
                execution_id = None
                # Fallback: если execution_id не создан, используем временный файл
                temp_playbook = TEMP_DIR / f'playbook_{uuid.uuid4().hex[:8]}.yaml'
                temp_playbook.parent.mkdir(exist_ok=True)
                with open(temp_playbook, 'w', encoding='utf-8') as f:
                    f.write(yaml_content)
        
        # Возвращаем ответ сразу
        response_data = {
            'success': True,
            'message': f'Playbook "{playbook.get("name", "playbook")}" queued',
            'status': 'queued',
            'executionId': execution_id
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        app.logger.error(f"Error queuing playbook {playbook_id} for project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e), 'status': 'error'}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/schedule', methods=['GET'])
@require_auth
def api_get_playbook_schedule(project_id, playbook_id):
    """API: Получить schedule для playbook"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Проверяем существование playbook
        if not playbook_storage.playbook_exists(project_id, playbook_id):
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        # Получаем schedule
        schedule = playbook_storage.get_schedule(project_id, playbook_id)
        
        if schedule:
            return jsonify({
                'success': True,
                'schedule': schedule
            })
        else:
            # Schedule не настроен - возвращаем success: false (не 404, чтобы не было ошибок в консоли)
            return jsonify({
                'success': False,
                'schedule': None,
                'error': 'Schedule not configured'
            })
            
    except Exception as e:
        app.logger.error(f"Error getting schedule for playbook {playbook_id} in project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/schedule', methods=['PUT'])
@require_auth
def api_save_playbook_schedule(project_id, playbook_id):
    """API: Сохранить schedule для playbook"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Проверяем существование playbook
        if not playbook_storage.playbook_exists(project_id, playbook_id):
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        # Получаем данные из запроса
        data = request.json or {}
        enabled = data.get('enabled', False)
        cron = data.get('cron', '').strip()
        timezone = data.get('timezone', 'UTC')
        
        # Валидация
        if enabled:
            if not cron:
                return jsonify({'success': False, 'error': 'Cron expression is required when schedule is enabled'}), 400
            
            # Валидируем cron expression
            try:
                from playbook_scheduler import PlaybookScheduler
                is_valid, error_msg = PlaybookScheduler.validate_cron(cron)
                if not is_valid:
                    return jsonify({'success': False, 'error': error_msg or 'Invalid cron expression'}), 400
            except ImportError as e:
                # Fallback validation if scheduler module not available
                app.logger.warning(f"Cannot import PlaybookScheduler for validation: {e}. Using basic validation.")
                # Basic cron format check (5 fields: minute hour day month weekday)
                import re
                cron_pattern = r'^(\*|([0-9]|[1-5][0-9])|\*\/([0-9]|[1-5][0-9])|([0-9]|[1-5][0-9])-([0-9]|[1-5][0-9])|([0-9]|[1-5][0-9])(,([0-9]|[1-5][0-9]))+)\s+(\*|([0-9]|1[0-9]|2[0-3])|\*\/([0-9]|1[0-9]|2[0-3])|([0-9]|1[0-9]|2[0-3])-([0-9]|1[0-9]|2[0-3])|([0-9]|1[0-9]|2[0-3])(,([0-9]|1[0-9]|2[0-3]))+)\s+(\*|([1-9]|[12][0-9]|3[01])|\*\/([1-9]|[12][0-9]|3[01])|([1-9]|[12][0-9]|3[01])-([1-9]|[12][0-9]|3[01])|([1-9]|[12][0-9]|3[01])(,([1-9]|[12][0-9]|3[01]))+)\s+(\*|([1-9]|1[0-2])|\*\/([1-9]|1[0-2])|([1-9]|1[0-2])-([1-9]|1[0-2])|([1-9]|1[0-2])(,([1-9]|1[0-2]))+)\s+(\*|([0-6])|\*\/([0-6])|([0-6])-([0-6])|([0-6])(,([0-6]))+)$'
                if not re.match(cron_pattern, cron):
                    return jsonify({'success': False, 'error': 'Invalid cron expression format. Expected: minute hour day month weekday'}), 400
            
            # Валидируем timezone
            try:
                import pytz
                pytz.timezone(timezone)
            except pytz.exceptions.UnknownTimeZoneError:
                return jsonify({'success': False, 'error': f'Unknown timezone: {timezone}'}), 400
        
        # Сохраняем schedule
        schedule_data = {
            'enabled': enabled,
            'cron': cron if enabled else '',
            'timezone': timezone
        }
        
        if playbook_storage.save_schedule(project_id, playbook_id, schedule_data):
            app.logger.info(f"Schedule saved for playbook {playbook_id} in project {project_id}: enabled={enabled}, cron={cron}, timezone={timezone}")
            return jsonify({
                'success': True,
                'schedule': schedule_data
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to save schedule'}), 500
            
    except Exception as e:
        app.logger.error(f"Error saving schedule for playbook {playbook_id} in project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/schedule/next-run', methods=['GET'])
@require_auth
def api_get_next_run_time(project_id, playbook_id):
    """API: Получить следующее время выполнения для schedule"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Проверяем существование playbook
        if not playbook_storage.playbook_exists(project_id, playbook_id):
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        # Получаем schedule
        schedule = playbook_storage.get_schedule(project_id, playbook_id)
        
        if not schedule or not schedule.get('enabled', False):
            return jsonify({'success': False, 'error': 'Schedule not enabled'}), 404
        
        cron_expr = schedule.get('cron', '')
        timezone_str = schedule.get('timezone', 'UTC')
        
        if not cron_expr:
            return jsonify({'success': False, 'error': 'Cron expression not set'}), 400
        
        # Вычисляем следующее время выполнения
        try:
            import pytz
            from croniter import croniter
            from datetime import datetime
            
            # Get timezone
            try:
                tz = pytz.timezone(timezone_str)
            except pytz.exceptions.UnknownTimeZoneError:
                app.logger.warning(f"Unknown timezone {timezone_str}, using UTC")
                tz = pytz.UTC
            
            # Get current time in target timezone
            current_utc = datetime.utcnow()
            current_in_tz = current_utc.replace(tzinfo=pytz.UTC).astimezone(tz)
            current_naive = current_in_tz.replace(tzinfo=None)
            
            # Calculate next execution time
            cron = croniter(cron_expr, current_naive)
            next_time = cron.get_next(datetime)
            
            # Convert back to UTC for response
            # next_time is already in target timezone (naive), so we need to localize it
            next_time_aware = tz.localize(next_time) if next_time.tzinfo is None else next_time.replace(tzinfo=tz)
            next_time_utc = next_time_aware.astimezone(pytz.UTC)
            
            return jsonify({
                'success': True,
                'next_run_time': next_time_utc.isoformat()
            })
            
        except ImportError as e:
            app.logger.warning(f"Cannot calculate next run time: {e}")
            return jsonify({'success': False, 'error': 'Cannot calculate next run time (dependencies not available)'}), 500
        except Exception as e:
            app.logger.error(f"Error calculating next run time: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'Error calculating next run time: {str(e)}'}), 500
            
    except Exception as e:
        app.logger.error(f"Error getting next run time for playbook {playbook_id} in project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/schedule', methods=['DELETE'])
@require_auth
def api_delete_playbook_schedule(project_id, playbook_id):
    """API: Удалить schedule для playbook (отключить)"""
    try:
        # Проверяем существование проекта
        projects = load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        # Проверяем существование playbook
        if not playbook_storage.playbook_exists(project_id, playbook_id):
            return jsonify({'success': False, 'error': 'Playbook not found'}), 404
        
        # Удаляем schedule (отключаем)
        if playbook_storage.delete_schedule(project_id, playbook_id):
            app.logger.info(f"Schedule disabled for playbook {playbook_id} in project {project_id}")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to delete schedule'}), 500
            
    except Exception as e:
        app.logger.error(f"Error deleting schedule for playbook {playbook_id} in project {project_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# Старая функция для обратной совместимости - удалена, логика перенесена в воркер
# Вся логика выполнения ansible-playbook теперь в worker.py


# ============================================================================
# QUEUE API - для получения очереди runs
# ============================================================================

@app.route('/api/projects/<project_id>/playbooks/<playbook_id>/runs', methods=['GET'])
@require_auth
def api_list_playbook_runs(project_id, playbook_id):
    """API: Получить список runs для playbook"""
    try:
        limit = request.args.get('limit', 50, type=int)
        status_filter = request.args.get('status')  # Опциональный фильтр по статусу
        
        executions = list_executions(
            limit=limit,
            playbook_id=playbook_id,
            project_id=project_id
        )
        
        # Фильтруем по статусу если указан
        if status_filter:
            executions = [e for e in executions if e.get('status') == status_filter.upper()]
        
        return jsonify({
            'success': True,
            'runs': executions,
            'count': len(executions)
        })
    except Exception as e:
        app.logger.error(f"Error listing runs for playbook {playbook_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/queue', methods=['GET'])
@require_auth
def api_get_queue():
    """API: Получить очередь runs (QUEUED)"""
    try:
        project_id = request.args.get('project_id')
        playbook_id = request.args.get('playbook_id')
        limit = request.args.get('limit', 100, type=int)
        
        # Получаем все QUEUED executions
        if project_id:
            executions = list_executions(
                limit=limit,
                playbook_id=playbook_id,
                project_id=project_id
            )
            queued = [e for e in executions if e.get('status') == 'QUEUED']
        else:
            # Если project_id не указан, ищем во всех проектах (новая структура: history/executions/)
            queued = []
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue
                project_id_from_dir = proj_dir.name
                # Проверяем наличие executions (новая или старая структура)
                executions_dir = proj_dir / 'history' / 'executions'
                if not executions_dir.exists():
                    executions_dir = proj_dir / 'executions'
                if not executions_dir.exists():
                    continue
                try:
                    execs = list_executions(
                        limit=limit,
                        playbook_id=playbook_id,
                        project_id=project_id_from_dir
                    )
                    queued.extend([e for e in execs if e.get('status') == 'QUEUED'])
                except:
                    pass
        
        # Сортируем по queuedAt (старые первые)
        queued.sort(key=lambda x: x.get('queuedAt', x.get('createdAt', 0)))
        
        return jsonify({
            'success': True,
            'queued': queued[:limit],
            'count': len(queued)
        })
    except Exception as e:
        app.logger.error(f"Error getting queue: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# GLOBAL SEARCH API
# ============================================================================

def _search_in_text(text, query):
    """Проверяет, содержит ли текст запрос (case-insensitive)"""
    if not text:
        return False
    return query.lower() in str(text).lower()


def _search_global(query, entity_types=None, project_ids=None, limit=50):
    """
    Глобальный поиск по всем сущностям
    entity_types: список типов для фильтрации ['project', 'host', 'group', 'role', 'playbook', 'variable', 'execution', 'draft']
    project_ids: список ID проектов для поиска (None = все проекты)
    """
    results = {
        'projects': [],
        'hosts': [],
        'groups': [],
        'roles': [],
        'playbooks': [],
        'variables': [],
        'executions': [],
        'drafts': []
    }
    
    query_lower = query.lower() if query else ''
    if not query_lower:
        return results
    
    # Определяем список проектов для поиска
    search_project_ids = project_ids if project_ids else [p.get('id') for p in load_projects()]
    
    # Определяем, какие типы искать
    if entity_types is None:
        entity_types = ['project', 'host', 'group', 'role', 'playbook', 'variable', 'execution']
    
    # 2. Hosts, Groups, Variables (из inventory и vars)
    if any(t in entity_types for t in ['host', 'group', 'variable']):
        for project_id in search_project_ids:
                try:
                    # Получаем inventory
                    project_inventory_file = get_project_inventory_file(project_id)
                    inventory_files = [str(project_inventory_file)] if project_inventory_file.exists() else []
                    
                    if inventory_files:
                        hosts = get_inventory_hosts(inventory_files)
                        groups = get_inventory_groups(inventory_files)
                        
                        # Hosts
                        if 'host' in entity_types:
                            for host in hosts:
                                host_name = host.get('name', '')
                                if _search_in_text(host_name, query):
                                    results['hosts'].append({
                                        'id': host_name,
                                        'name': host_name,
                                        'type': 'host',
                                        'project_id': project_id,
                                        'inventory_file': host.get('inventory_file', '')
                                    })
                        
                        # Groups
                        if 'group' in entity_types:
                            for group_name, group_data in groups.items():
                                if _search_in_text(group_name, query):
                                    results['groups'].append({
                                        'id': group_name,
                                        'name': group_name,
                                        'type': 'group',
                                        'project_id': project_id,
                                        'hosts_count': len(group_data.get('hosts', [])) if isinstance(group_data, dict) else 0
                                    })
                        
                        # Variables
                        if 'variable' in entity_types:
                            project_group_vars_dir = get_project_group_vars_dir(project_id)
                            project_host_vars_dir = get_project_host_vars_dir(project_id)
                            
                            # Group vars
                            group_vars_file = project_group_vars_dir / 'all.yml'
                            if group_vars_file.exists():
                                group_vars = parse_yaml_with_comments(group_vars_file)
                                for var_name, var_value in group_vars.items():
                                    if (not var_name.startswith('_') and 
                                        (_search_in_text(var_name, query) or _search_in_text(var_value, query))):
                                        results['variables'].append({
                                            'id': f"{project_id}:group:{var_name}",
                                            'name': var_name,
                                            'value': str(var_value),
                                            'type': 'variable',
                                            'scope': 'group',
                                            'project_id': project_id
                                        })
                            
                            # Host vars
                            if project_host_vars_dir.exists():
                                for host_file in project_host_vars_dir.glob('*.yml'):
                                    host_name = host_file.stem
                                    host_vars = parse_yaml_with_comments(host_file)
                                    for var_name, var_value in host_vars.items():
                                        if (not var_name.startswith('_') and 
                                            (_search_in_text(var_name, query) or 
                                             _search_in_text(var_value, query) or
                                             _search_in_text(host_name, query))):
                                            results['variables'].append({
                                                'id': f"{project_id}:host:{host_name}:{var_name}",
                                                'name': var_name,
                                                'value': str(var_value),
                                                'type': 'variable',
                                                'scope': 'host',
                                                'host': host_name,
                                                'project_id': project_id
                                            })
                except Exception as e:
                    app.logger.warning(f"Error searching in project {project_id}: {e}")
        
        # 3. Roles & Playbooks (из roles-storage)
        if 'role' in entity_types or 'playbook' in entity_types:
            try:
                roles_config = load_roles_config()
                roles_storage_root = BASE_DIR / roles_config.get('storageRoot', 'roles-storage')
                
                if roles_storage_root.exists():
                    # Ищем роли
                    for pack_dir in roles_storage_root.iterdir():
                        if not pack_dir.is_dir() or pack_dir.name.startswith('.'):
                            continue
                        
                        roles_dir = pack_dir / 'roles'
                        if roles_dir.exists():
                            for role_dir in roles_dir.iterdir():
                                if role_dir.is_dir() and _search_in_text(role_dir.name, query):
                                    # Get project_dir for this project_id
                                    project_dir = get_project_dir(project_id)
                                    role_path_relative = str(role_dir.relative_to(project_dir)) if role_dir.is_relative_to(project_dir) else f'roles-playbooks/{pack_dir.name}/{role_dir.name}'
                                    results['roles'].append({
                                        'id': f"{pack_dir.name}:{role_dir.name}",
                                        'name': role_dir.name,
                                        'pack': pack_dir.name,
                                        'type': 'role',
                                        'path': role_path_relative
                                    })
                        
                        # Ищем playbooks
                        playbooks_dir = pack_dir / 'playbooks'
                        if playbooks_dir.exists():
                            for playbook_file in playbooks_dir.glob('*.yml'):
                                if _search_in_text(playbook_file.stem, query):
                                    # Get project_dir for this project_id
                                    project_dir = get_project_dir(project_id)
                                    playbook_path_relative = str(playbook_file.relative_to(project_dir)) if playbook_file.is_relative_to(project_dir) else f'roles-playbooks/{playbook_file.name}'
                                    results['playbooks'].append({
                                        'id': f"{pack_dir.name}:{playbook_file.stem}",
                                        'name': playbook_file.stem,
                                        'pack': pack_dir.name,
                                        'type': 'playbook',
                                        'path': playbook_path_relative
                                    })
            except Exception as e:
                app.logger.warning(f"Error searching roles and playbooks: {e}")
        
        # 4. Executions
        if 'execution' in entity_types:
            for project_id in search_project_ids:
                try:
                    executions = list_executions(limit=100, search_query=query, project_id=project_id)
                    for execution in executions[:limit]:
                        results['executions'].append({
                            'id': execution.get('id'),
                            'name': f"Execution {execution.get('id', '')[:8]}",
                            'type': 'execution',
                            'project_id': project_id,
                            'status': execution.get('status', 'unknown'),
                            'createdAt': execution.get('createdAt'),
                            'hostsTargeted': execution.get('stats', {}).get('hostsTargeted', 0)
                        })
                except Exception as e:
                    app.logger.warning(f"Error searching executions in project {project_id}: {e}")
        
        # Ограничиваем результаты
        for key in results:
            results[key] = results[key][:limit]
    
    
    return results


@app.route('/api/search', methods=['POST'])
@require_auth
def api_global_search():
    """API: Глобальный поиск"""
    try:
        data = request.get_json(silent=True) or {}
        query = data.get('query', '').strip()
        entity_types = data.get('entity_types')  # список типов для фильтрации
        project_ids = data.get('project_ids')  # список ID проектов
        limit = data.get('limit', 50)
        
        if not query:
            return jsonify({
                'success': True,
                'results': {
                    'projects': [],
                    'hosts': [],
                    'groups': [],
                    'roles': [],
                    'playbooks': [],
                    'variables': [],
                    'executions': [],
                    'drafts': []
                },
                'total': 0
            })
        
        results = _search_global(query, entity_types, project_ids, limit)
        
        # Подсчитываем общее количество
        total = sum(len(v) for v in results.values())
        
        return jsonify({
            'success': True,
            'results': results,
            'total': total,
            'query': query
        })
    
    except Exception as e:
        app.logger.error(f"Error in global search: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# ADMIN WORKER MANAGEMENT ENDPOINTS
# ============================================================================

def check_admin_auth():
    """
    Проверка прав администратора для управления workers
    TODO: Интегрировать с вашей системой аутентификации пользователей
    """
    # Placeholder - в production нужно проверить сессию/токен пользователя
    # Для dev mode возвращаем True
    return True


def _is_worker_online(worker_data: dict, heartbeat_ttl_seconds: int = 60) -> bool:
    """Проверяет, онлайн ли worker (по lastSeenAt)"""
    last_seen = worker_data.get('lastSeenAt')
    if not last_seen:
        return False
    import time
    return (time.time() - last_seen) < heartbeat_ttl_seconds


@app.route('/api/admin/workers', methods=['GET'])
@require_auth
def api_admin_list_workers():
    """API: Список всех workers (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import load_all_workers
        except ImportError:
            from worker_registry import load_all_workers
        
        workers = load_all_workers()
        
        # Убираем чувствительные данные из ответа
        workers_list = []
        for worker_id, worker_data in workers.items():
            workers_list.append({
                'id': worker_id,
                'name': worker_data.get('name'),
                'description': worker_data.get('description'),
                'enabled': worker_data.get('enabled', True),
                'capabilities': worker_data.get('capabilities', {}),
                'tags': worker_data.get('tags', []),
                'tagColors': worker_data.get('tagColors', {}),
                'createdAt': worker_data.get('createdAt'),
                'lastSeenAt': worker_data.get('lastSeenAt'),
                'currentExecutionId': worker_data.get('currentExecutionId')
            })
        
        return jsonify({
            'success': True,
            'workers': workers_list
        })
    except Exception as e:
        app.logger.error(f"Error listing workers: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/capabilities', methods=['GET'])
@require_auth
def api_capabilities():
    """API: Capabilities available for playbook runs (e.g. Mitogen for strategy plugins)."""
    try:
        try:
            from .worker_registry import load_all_workers
        except ImportError:
            from worker_registry import load_all_workers
        workers = load_all_workers()
        mitogen_available = False
        for worker_data in workers.values():
            if not worker_data.get('enabled', True):
                continue
            sinfo = worker_data.get('systemInfo') or {}
            mitogen = sinfo.get('mitogen') or {}
            version = mitogen.get('version') if isinstance(mitogen, dict) else None
            if version and str(version).lower() not in ('not installed', 'none', ''):
                mitogen_available = True
                break
        return jsonify({
            'success': True,
            'mitogen_available': mitogen_available
        })
    except Exception as e:
        app.logger.error(f"Error getting capabilities: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers', methods=['POST'])
@require_auth
def api_admin_create_worker():
    """API: Создать нового worker (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import create_worker
        except ImportError:
            from worker_registry import create_worker
        
        data = request.json or {}
        name = data.get('name', '').strip()
        capabilities = data.get('capabilities', {})
        tags = data.get('tags', [])
        
        if not name:
            return jsonify({'success': False, 'error': 'Worker name is required'}), 400
        
        worker_id, plaintext_token = create_worker(name, capabilities, tags)
        
        app.logger.info(f"Admin created worker: {worker_id} ({name})")
        
        # Токен возвращается только один раз!
        return jsonify({
            'success': True,
            'workerId': worker_id,
            'workerToken': plaintext_token,
            'message': 'IMPORTANT: Save this token! It will not be shown again.'
        })
    except Exception as e:
        app.logger.error(f"Error creating worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers/<worker_id>', methods=['GET'])
@require_auth
def api_admin_get_worker(worker_id):
    """API: Получить детальную информацию о worker"""
    try:
        try:
            from .worker_registry import load_worker
        except ImportError:
            from worker_registry import load_worker
        
        worker = load_worker(worker_id)
        if not worker:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
        
        # Формируем ответ с нужными полями
        response_data = {
            'id': worker.get('id'),
            'name': worker.get('name'),
            'description': worker.get('description'),
            'status': 'online' if _is_worker_online(worker) else 'offline',
            'lastSeenAt': worker.get('lastSeenAt'),
            'tags': worker.get('tags', []),
            'tagColors': worker.get('tagColors', {}),
            'capabilities': worker.get('capabilities', {}),
            'systemInfo': worker.get('systemInfo'),
            'systemInfoUpdatedAt': worker.get('systemInfoUpdatedAt'),
            'createdAt': worker.get('createdAt'),
            'enabled': worker.get('enabled', True),
            'currentExecutionId': worker.get('currentExecutionId')
        }
        
        return jsonify({
            'success': True,
            'worker': response_data
        })
    except Exception as e:
        app.logger.error(f"Error getting worker {worker_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers/<worker_id>', methods=['PATCH'])
@require_auth
def api_admin_update_worker(worker_id):
    """API: Обновить worker (name, description, tags) (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import load_worker, update_worker
        except ImportError:
            from worker_registry import load_worker, update_worker
        
        worker = load_worker(worker_id)
        if not worker:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
        
        data = request.json or {}
        
        # Валидация name
        if 'name' in data:
            name = data.get('name', '').strip()
            if not name:
                return jsonify({'success': False, 'error': 'Worker name cannot be empty'}), 400
        
        # Валидация tags
        if 'tags' in data:
            tags = data.get('tags', [])
            if not isinstance(tags, list):
                return jsonify({'success': False, 'error': 'Tags must be an array'}), 400
            
            # Обрабатываем tags: trim, уникальные, max length
            processed_tags = []
            seen = set()
            for tag in tags:
                if isinstance(tag, str):
                    tag_trimmed = tag.strip()
                    if tag_trimmed and len(tag_trimmed) <= 50:  # max length
                        if tag_trimmed not in seen:
                            processed_tags.append(tag_trimmed)
                            seen.add(tag_trimmed)
            tags = processed_tags
        
        # Валидация tagColors
        if 'tagColors' in data:
            tagColors = data.get('tagColors', {})
            if not isinstance(tagColors, dict):
                return jsonify({'success': False, 'error': 'tagColors must be an object'}), 400
        
        # Обновляем worker
        update_data = {}
        if 'name' in data:
            update_data['name'] = data['name'].strip()
        if 'description' in data:
            # Передаём как есть (может быть пустой строкой для удаления)
            update_data['description'] = data.get('description', '')
        if 'tags' in data:
            update_data['tags'] = tags
        if 'tagColors' in data:
            update_data['tagColors'] = tagColors
        
        if not update_data:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400
        
        if update_worker(worker_id, **update_data):
            # Загружаем обновлённого worker
            updated_worker = load_worker(worker_id)
            if not updated_worker:
                return jsonify({'success': False, 'error': 'Failed to load updated worker'}), 500
            
            app.logger.info(f"Admin updated worker: {worker_id}")
            
            # Формируем ответ
            response_data = {
                'id': updated_worker.get('id'),
                'name': updated_worker.get('name'),
                'description': updated_worker.get('description'),
                'tags': updated_worker.get('tags', []),
                'tagColors': updated_worker.get('tagColors', {}),
                'enabled': updated_worker.get('enabled', True),
                'capabilities': updated_worker.get('capabilities', {}),
                'createdAt': updated_worker.get('createdAt'),
                'lastSeenAt': updated_worker.get('lastSeenAt'),
                'currentExecutionId': updated_worker.get('currentExecutionId')
            }
            
            return jsonify({
                'success': True,
                'worker': response_data
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to update worker'}), 500
            
    except Exception as e:
        app.logger.error(f"Error updating worker {worker_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers/<worker_id>', methods=['DELETE'])
@require_auth
def api_admin_delete_worker(worker_id):
    """API: Удалить worker (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import delete_worker
        except ImportError:
            from worker_registry import delete_worker
        
        if delete_worker(worker_id):
            app.logger.info(f"Admin deleted worker: {worker_id}")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
    except Exception as e:
        app.logger.error(f"Error deleting worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers/<worker_id>/rotate-token', methods=['POST'])
@require_auth
def api_admin_rotate_worker_token(worker_id):
    """API: Обновить токен worker (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import rotate_worker_token
        except ImportError:
            from worker_registry import rotate_worker_token
        
        plaintext_token = rotate_worker_token(worker_id)
        
        app.logger.info(f"Admin rotated token for worker: {worker_id}")
        
        # Токен возвращается только один раз!
        return jsonify({
            'success': True,
            'workerToken': plaintext_token,
            'message': 'IMPORTANT: Save this token! It will not be shown again.'
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        app.logger.error(f"Error rotating worker token: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/api-tokens', methods=['GET'])
@require_auth
def api_list_api_tokens():
    """API: List API tokens for current user"""
    try:
        user_id = getattr(request, 'current_user', {}).get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        try:
            from .api_tokens_store import list_tokens
        except ImportError:
            from api_tokens_store import list_tokens
        tokens = list_tokens(user_id)
        return jsonify({'success': True, 'tokens': tokens})
    except Exception as e:
        app.logger.error(f"Error listing API tokens: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/api-tokens', methods=['POST'])
@require_auth
def api_create_api_token():
    """API: Create API token. Plaintext returned only once!"""
    try:
        user_id = getattr(request, 'current_user', {}).get('user_id')
        username = getattr(request, 'current_user', {}).get('username', '')
        if not user_id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
        scope = data.get('scope', 'global')
        if scope not in ('global', 'project'):
            scope = 'global'
        project_id = data.get('projectId') if scope == 'project' else None
        expires_days = data.get('expiresDays')
        if expires_days is not None:
            try:
                expires_days = int(expires_days)
                if expires_days < 0:
                    expires_days = None
            except (TypeError, ValueError):
                expires_days = None
        try:
            from .api_tokens_store import create_token
        except ImportError:
            from api_tokens_store import create_token
        token_id, plaintext = create_token(
            user_id=user_id,
            username=username,
            name=name,
            scope=scope,
            project_id=project_id,
            expires_days=expires_days,
        )
        app.logger.info(f"User {user_id} created API token: {token_id}")
        return jsonify({
            'success': True,
            'tokenId': token_id,
            'token': plaintext,
            'message': 'IMPORTANT: Save this token! It will not be shown again.'
        })
    except Exception as e:
        app.logger.error(f"Error creating API token: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/api-tokens/<token_id>/rotate', methods=['POST'])
@require_auth
def api_rotate_api_token(token_id):
    """API: Regenerate token (creates new, revokes old). Plaintext returned only once!"""
    try:
        user_id = getattr(request, 'current_user', {}).get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        try:
            from .api_tokens_store import rotate_token
        except ImportError:
            from api_tokens_store import rotate_token
        result = rotate_token(token_id, user_id)
        if result:
            new_id, plaintext = result
            app.logger.info(f"User {user_id} rotated API token: {token_id} -> {new_id}")
            return jsonify({
                'success': True,
                'tokenId': new_id,
                'token': plaintext,
                'message': 'IMPORTANT: Save this token! It will not be shown again.'
            })
        return jsonify({'success': False, 'error': 'Token not found or already revoked'}), 404
    except Exception as e:
        app.logger.error(f"Error rotating API token: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/api-tokens/<token_id>/revoke', methods=['POST'])
@require_auth
def api_revoke_api_token(token_id):
    """API: Revoke API token"""
    try:
        user_id = getattr(request, 'current_user', {}).get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        try:
            from .api_tokens_store import revoke_token
        except ImportError:
            from api_tokens_store import revoke_token
        if revoke_token(token_id, user_id):
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Token not found'}), 404
    except Exception as e:
        app.logger.error(f"Error revoking API token: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/api-tokens/<token_id>', methods=['DELETE'])
@require_auth
def api_delete_api_token(token_id):
    """API: Delete API token"""
    try:
        user_id = getattr(request, 'current_user', {}).get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        try:
            from .api_tokens_store import delete_token
        except ImportError:
            from api_tokens_store import delete_token
        if delete_token(token_id, user_id):
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Token not found'}), 404
    except Exception as e:
        app.logger.error(f"Error deleting API token: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/openapi.json', methods=['GET'])
def api_openapi_spec():
    """Serve OpenAPI 3.0 specification (public, no auth required)."""
    try:
        from .openapi_spec import get_openapi_spec
    except ImportError:
        from openapi_spec import get_openapi_spec
    base_url = request.host_url.rstrip('/')
    if request.headers.get('X-Forwarded-Proto') == 'https':
        base_url = base_url.replace('http://', 'https://')
    spec = get_openapi_spec(base_url)
    return jsonify(spec)


@app.route('/api/docs')
def api_docs_swagger_ui():
    """Serve Swagger UI page (public, read-only documentation)."""
    # Use relative URL so Swagger fetches from same origin (avoids CORS when behind proxy)
    spec_url = '/api/openapi.json'
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>API Documentation - Stargate</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.9.0/swagger-ui.css">
    <style>
        /* Dark theme for Swagger UI - high contrast, professional */
        body {{ background: #1a1d24 !important; margin: 0; padding: 0; }}
        .swagger-ui {{ background: #1a1d24 !important; }}
        .swagger-ui .topbar {{ display: none; }}
        .swagger-ui .info {{ background: #252830 !important; border: 1px solid #3d4451 !important; border-radius: 8px !important; margin: 0 0 24px !important; padding: 24px !important; }}
        .swagger-ui .info .title {{ color: #f0f2f5 !important; font-size: 28px !important; margin: 0 0 12px !important; font-weight: 600; }}
        .swagger-ui .info p, .swagger-ui .info .info__contact {{ color: #b8bcc8 !important; line-height: 1.6; }}
        .swagger-ui .opblock-tag {{ color: #f0f2f5 !important; border-color: #3d4451 !important; background: transparent !important; font-weight: 600; font-size: 16px; }}
        .swagger-ui .opblock-tag-section {{ border-color: #3d4451 !important; }}
        .swagger-ui .opblock-tag small, .swagger-ui .opblock-tag-description, .swagger-ui .opblock-tag .opblock-tag-description {{ color: #e2e8f0 !important; font-weight: 500 !important; }}
        .swagger-ui .opblock {{ border: 1px solid #3d4451 !important; background: #252830 !important; border-radius: 6px !important; margin-bottom: 8px !important; }}
        .swagger-ui .opblock-summary {{ border-color: #3d4451 !important; padding: 12px 16px !important; }}
        .swagger-ui .opblock-summary-path {{ color: #e8eaef !important; font-weight: 500 !important; font-size: 15px !important; }}
        .swagger-ui .opblock-summary-description {{ color: #e2e8f0 !important; font-size: 13px !important; }}
        /* Method badges - vibrant, high contrast */
        .swagger-ui .opblock .opblock-summary-method {{ font-weight: 600 !important; min-width: 60px !important; text-align: center !important; padding: 6px 12px !important; border-radius: 4px !important; }}
        .swagger-ui .opblock.opblock-get .opblock-summary-method {{ background: #0ea5e9 !important; color: #fff !important; border: none !important; }}
        .swagger-ui .opblock.opblock-post .opblock-summary-method {{ background: #22c55e !important; color: #fff !important; border: none !important; }}
        .swagger-ui .opblock.opblock-put .opblock-summary-method {{ background: #f59e0b !important; color: #1a1d24 !important; border: none !important; }}
        .swagger-ui .opblock.opblock-patch .opblock-summary-method {{ background: #a78bfa !important; color: #fff !important; border: none !important; }}
        .swagger-ui .opblock.opblock-delete .opblock-summary-method {{ background: #ef4444 !important; color: #fff !important; border: none !important; }}
        .swagger-ui .opblock-body {{ background: #1a1d24 !important; border-color: #3d4451 !important; border-top: 1px solid #3d4451 !important; }}
        .swagger-ui .opblock-description-wrapper, .swagger-ui .opblock-external-docs-wrapper, .swagger-ui .opblock-title_normal, .swagger-ui .opblock .opblock-description, .swagger-ui .opblock-body .markdown, .swagger-ui .opblock-body .markdown p, .swagger-ui .renderedMarkdown, .swagger-ui .renderedMarkdown p {{ color: #e2e8f0 !important; line-height: 1.5 !important; }}
        .swagger-ui .parameter__name {{ color: #7dd3fc !important; font-weight: 500 !important; }}
        .swagger-ui .parameter__type {{ color: #94a3b8 !important; }}
        .swagger-ui .parameter__in {{ color: #64748b !important; }}
        .swagger-ui table thead tr th {{ color: #94a3b8 !important; border-color: #3d4451 !important; font-weight: 600 !important; }}
        .swagger-ui table tbody tr td {{ color: #e2e8f0 !important; border-color: #3d4451 !important; }}
        .swagger-ui .model {{ color: #e2e8f0 !important; }}
        .swagger-ui .model-box {{ background: #252830 !important; border: 1px solid #3d4451 !important; border-radius: 6px !important; }}
        .swagger-ui .btn.authorize {{ border: 2px solid #0ea5e9 !important; color: #0ea5e9 !important; background: transparent !important; font-weight: 600 !important; }}
        .swagger-ui .btn.authorize svg {{ fill: #0ea5e9 !important; }}
        .swagger-ui .btn.authorize.unlocked svg {{ fill: #22c55e !important; }}
        .swagger-ui .btn.authorize.unlocked {{ border-color: #22c55e !important; color: #22c55e !important; }}
        .swagger-ui .scheme-container {{ background: #252830 !important; border: 1px solid #3d4451 !important; padding: 16px !important; border-radius: 6px !important; }}
        .swagger-ui .scheme-container .schemes-title {{ color: #94a3b8 !important; font-weight: 600 !important; }}
        .swagger-ui .scheme-container label {{ color: #e2e8f0 !important; }}
        .swagger-ui select {{ background: #252830 !important; color: #e2e8f0 !important; border: 1px solid #3d4451 !important; border-radius: 4px !important; }}
        .swagger-ui input[type=text], .swagger-ui textarea {{ background: #252830 !important; color: #e2e8f0 !important; border: 1px solid #3d4451 !important; border-radius: 4px !important; }}
        .swagger-ui .response-col_status {{ color: #94a3b8 !important; font-weight: 600 !important; }}
        .swagger-ui .response-col_description {{ color: #e2e8f0 !important; }}
        .swagger-ui .tab li {{ color: #94a3b8 !important; }}
        .swagger-ui .tab li.active {{ color: #0ea5e9 !important; border-color: #0ea5e9 !important; font-weight: 600 !important; }}
        .swagger-ui .opblock.opblock-deprecated {{ opacity: 0.7; }}
        .swagger-ui .opblock .opblock-summary-method {{ font-size: 12px !important; }}
        /* Parameters/Responses tab bar - dark theme (replace white/grey default) */
        .swagger-ui .opblock-section-header {{ background: #252830 !important; border-color: #3d4451 !important; color: #e2e8f0 !important; box-shadow: none !important; }}
        .swagger-ui .opblock-section-header .tab {{ border-color: #3d4451 !important; background: transparent !important; }}
        .swagger-ui .opblock-section-header .tab li {{ color: #94a3b8 !important; border-color: #3d4451 !important; background: transparent !important; }}
        .swagger-ui .opblock-section-header .tab li.active {{ color: #0ea5e9 !important; border-color: #0ea5e9 !important; }}
        .swagger-ui .opblock-section-header h4 {{ color: #e2e8f0 !important; }}
        .swagger-ui .examples__section-header, .swagger-ui .example__section-header {{ background: #252830 !important; border-color: #3d4451 !important; color: #e2e8f0 !important; }}
        .swagger-ui .btn-cancel {{ background: #252830 !important; border-color: #ef4444 !important; color: #f87171 !important; }}
        .swagger-ui .btn-cancel:hover {{ background: rgba(239, 68, 68, 0.15) !important; }}
        .swagger-ui .opblock-body .tab li {{ background: transparent !important; }}
        .swagger-ui .execute-wrapper {{ background: #252830 !important; border-color: #3d4451 !important; }}
        .swagger-ui .opblock-body .wrapper {{ background: transparent !important; }}
        /* Lock icon visibility for secured endpoints */
        .swagger-ui .authorization__btn svg {{ fill: #f59e0b !important; }}
        .swagger-ui .opblock-summary .authorization__btn {{ margin-left: 8px; }}
        /* Servers label - fix contrast (was blending with dark background) */
        .swagger-ui .servers label,
        .swagger-ui .servers .servers__title,
        .swagger-ui .servers h4,
        .swagger-ui .servers-title {{ color: #e2e8f0 !important; }}
        /* No parameters / empty state - fix contrast */
        .swagger-ui .parameters-col_description,
        .swagger-ui .opblock-body .table-container td,
        .swagger-ui .opblock-body .parameters-col_description {{ color: #e2e8f0 !important; }}
    </style>
</head>
<body style="background: #1a1d24;">
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5.9.0/swagger-ui-bundle.js"></script>
    <script>
        window.onload = function() {{
            SwaggerUIBundle({{
                url: "{spec_url}",
                dom_id: '#swagger-ui',
                deepLinking: true,
                docExpansion: "list",
                defaultModelsExpandDepth: 1,
                defaultModelExpandDepth: 2,
                presets: [
                    SwaggerUIBundle.presets.apis,
                    SwaggerUIBundle.SwaggerUIStandalonePreset
                ],
                layout: "BaseLayout",
                persistAuthorization: true,
                displayRequestDuration: true,
                tryItOutEnabled: true,
                filter: true
            }});
        }};
    </script>
</body>
</html>'''
    from flask import Response
    return Response(html, mimetype='text/html')


@app.route('/api/admin/workers/<worker_id>/enable', methods=['POST'])
@require_auth
def api_admin_enable_worker(worker_id):
    """API: Включить worker (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import enable_worker
        except ImportError:
            from worker_registry import enable_worker
        
        if enable_worker(worker_id):
            app.logger.info(f"Admin enabled worker: {worker_id}")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
    except Exception as e:
        app.logger.error(f"Error enabling worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers/<worker_id>/disable', methods=['POST'])
@require_auth
def api_admin_disable_worker(worker_id):
    """API: Отключить worker (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import disable_worker
        except ImportError:
            from worker_registry import disable_worker
        
        if disable_worker(worker_id):
            app.logger.info(f"Admin disabled worker: {worker_id}")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
    except Exception as e:
        app.logger.error(f"Error disabling worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers/<worker_id>/request-info', methods=['POST'])
@require_auth
def api_admin_request_worker_info(worker_id):
    """API: Запросить обновление systemInfo от worker (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import load_worker
        except ImportError:
            from worker_registry import load_worker, save_worker
        
        worker = load_worker(worker_id)
        if not worker:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
        
        # Устанавливаем флаг для worker, чтобы он отправил systemInfo при следующем heartbeat
        worker['systemInfoRequested'] = True
        save_worker(worker)
        
        app.logger.info(f"Admin requested system info refresh for worker: {worker_id}")
        return jsonify({'success': True, 'message': 'Worker will send system info on next heartbeat'})
    except Exception as e:
        app.logger.error(f"Error requesting worker info: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/workers/<worker_id>/runs', methods=['GET'])
@require_auth
def api_admin_worker_runs(worker_id):
    """API: Последние runs для worker (только для админов)"""
    try:
        if not check_admin_auth():
            return jsonify({'success': False, 'error': 'Permission denied'}), 403
        
        try:
            from .worker_registry import load_worker
        except ImportError:
            from worker_registry import load_worker
        try:
            from .executions_store import get_execution
        except ImportError:
            from executions_store import get_execution
        
        worker_data = load_worker(worker_id)
        if not worker_data:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
        
        # Собираем все executions для этого worker
        runs = []
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            
            executions_dir = proj_dir / 'executions'
            if not executions_dir.exists():
                continue
            
            for exec_file in executions_dir.glob('*.json'):
                try:
                    with open(exec_file, 'r', encoding='utf-8') as f:
                        execution = json.load(f)
                        if execution.get('workerId') == worker_id:
                            runs.append({
                                'executionId': execution.get('id'),
                                'projectId': execution.get('projectId'),
                                'status': execution.get('status'),
                                'startedAt': execution.get('startedAt'),
                                'finishedAt': execution.get('finishedAt'),
                                'duration': execution.get('duration'),
                                'createdAt': execution.get('createdAt')
                            })
                except:
                    pass
        
        # Сортируем по времени создания (новые первые) и берем последние 20
        runs.sort(key=lambda x: x.get('createdAt', 0), reverse=True)
        runs = runs[:20]
        
        return jsonify({
            'success': True,
            'workerId': worker_id,
            'workerName': worker_data.get('name'),
            'runs': runs,
            'total': len(runs)
        })
    except Exception as e:
        app.logger.error(f"Error getting worker runs: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<project_id>/queue-stats', methods=['GET'])
@require_auth
def api_project_queue_stats(project_id):
    """API: Статистика очереди для проекта"""
    try:
        try:
            from .worker_registry import load_all_workers
        except ImportError:
            from worker_registry import load_all_workers, is_worker_online
        
        # Проверяем существование проекта
        project_dir = PROJECTS_DIR / project_id
        if not project_dir.exists() or not project_dir.is_dir():
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        executions_dir = project_dir / 'executions'
        if not executions_dir.exists():
            return jsonify({
                'success': True,
                'projectId': project_id,
                'queuedCount': 0,
                'runningCount': 0,
                'workersOnline': 0
            })
        
        queued_count = 0
        running_count = 0
        running_worker_ids = set()
        
        # Подсчитываем executions
        for exec_file in executions_dir.glob('*.json'):
            try:
                with open(exec_file, 'r', encoding='utf-8') as f:
                    execution = json.load(f)
                    status = execution.get('status')
                    if status == 'QUEUED':
                        queued_count += 1
                    elif status == 'RUNNING':
                        running_count += 1
                        worker_id = execution.get('workerId')
                        if worker_id:
                            running_worker_ids.add(worker_id)
            except:
                pass
        
        # Считаем онлайн workers (всех, не только для этого проекта)
        workers = load_all_workers()
        workers_online = sum(1 for w_id in workers.keys() if is_worker_online(w_id, ttl_seconds=60))
        
        return jsonify({
            'success': True,
            'projectId': project_id,
            'queuedCount': queued_count,
            'runningCount': running_count,
            'workersOnline': workers_online,
            'activeWorkersForProject': len(running_worker_ids)
        })
    except Exception as e:
        app.logger.error(f"Error getting queue stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# WORKER API ENDPOINTS
# ============================================================================

def _check_execution_requirements(execution: dict, worker_capabilities: dict, worker_tags: list) -> bool:
    """
    Проверяет соответствие execution requirements возможностям worker
    
    Args:
        execution: Данные execution
        worker_capabilities: Capabilities worker
        worker_tags: Tags worker
    
    Returns:
        True если execution подходит для worker
    """
    # Получаем requirements из execution
    run_params = execution.get('runParams', {})
    requirements = run_params.get('requirements', {})
    
    # Проверяем tags
    required_tags = requirements.get('tags', [])
    if required_tags:
        worker_tags_set = set(worker_tags or [])
        required_tags_set = set(required_tags)
        if not required_tags_set.issubset(worker_tags_set):
            return False
    
    # Проверяем capabilities
    required_caps = requirements.get('capabilities', {})
    if required_caps:
        for cap_key, cap_value in required_caps.items():
            worker_cap_value = worker_capabilities.get(cap_key)
            if worker_cap_value != cap_value:
                return False
    
    return True


def server_claim_next_execution(worker_id, worker_data, project_id=None, max_concurrency=1, tags=None):
    """
    Серверная функция для атомарного claim следующего QUEUED execution
    Использует файловую блокировку для предотвращения race condition
    Учитывает фильтрацию по requirements, параллелизм и активные runs
    
    Args:
        worker_id: ID worker
        worker_data: Данные worker (capabilities, tags)
        project_id: Опциональный ID проекта для фильтрации
        max_concurrency: Максимальное количество параллельных runs для worker
        tags: Tags worker (для фильтрации)
    
    Returns:
        tuple: (execution_id, execution_data, project_id) или (None, None, None)
    """
    import fcntl
    import os
    try:
        from .worker_registry import get_worker_active_runs_count
    except ImportError:
        from worker_registry import get_worker_active_runs_count
    
    try:
        # Проверяем параллелизм worker
        active_runs = get_worker_active_runs_count(worker_id)
        if active_runs >= max_concurrency:
            app.logger.debug(f"Worker {worker_id} has {active_runs} active runs, maxConcurrency={max_concurrency}")
            return None, None, None
        
        worker_capabilities = worker_data.get('capabilities', {})
        worker_tags = tags or worker_data.get('tags', [])
        
        # Если project_id указан, ищем только в этом проекте
        if project_id:
            project_dirs = [PROJECTS_DIR / project_id]
        else:
            # Ищем во всех проектах (глобальная очередь)
            project_dirs = [d for d in PROJECTS_DIR.iterdir() if d.is_dir()]
        
        # Собираем все QUEUED executions с фильтрацией
        queued_runs = []
        total_checked = 0
        for proj_dir in project_dirs:
            proj_id = proj_dir.name
            # Используем правильный путь: history/executions/
            executions_dir = proj_dir / 'history' / 'executions'
            if not executions_dir.exists():
                continue
            
            for exec_file in executions_dir.glob('*.json'):
                try:
                    # Используем блокировку для чтения
                    with open(exec_file, 'r', encoding='utf-8') as f:
                        # Пытаемся заблокировать файл
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except IOError:
                            # Файл заблокирован другим процессом, пропускаем
                            continue
                        
                        try:
                            execution = json.load(f)
                            status = execution.get('status')
                            
                            # Проверяем что это QUEUED run
                            if status != 'QUEUED':
                                continue
                            
                            # Фильтруем по requirements
                            if not _check_execution_requirements(execution, worker_capabilities, worker_tags):
                                continue
                            
                            queued_at = execution.get('queuedAt', execution.get('createdAt', 0))
                            queued_runs.append((queued_at, exec_file, execution, proj_id))
                            total_checked += 1
                        finally:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception as e:
                    app.logger.warning(f"Error reading execution file {exec_file}: {e}")
                    continue
        
        if not queued_runs:
            app.logger.debug(f"[Server Claim] No QUEUED runs found (checked {total_checked} execution files in {len(project_dirs)} project(s))")
            return None, None, None
        
        app.logger.debug(f"[Server Claim] Found {len(queued_runs)} QUEUED run(s) (checked {total_checked} files), attempting to claim...")
        
        app.logger.debug(f"[Server Claim] Found {len(queued_runs)} QUEUED run(s) (checked {total_checked} files), attempting to claim...")
        
        # Сортируем по queuedAt (старые первые - FIFO)
        queued_runs.sort(key=lambda x: x[0])
        
        # Пытаемся claim первый подходящий run
        for queued_at, exec_file, execution, proj_id in queued_runs:
            execution_id = execution.get('id')
            if not execution_id:
                continue
            
            # Пытаемся атомарно обновить статус на RUNNING
            try:
                with open(exec_file, 'r+', encoding='utf-8') as f:
                    # Блокируем файл для записи
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    
                    try:
                        # Перечитываем файл (возможно он уже был взят другим воркером)
                        f.seek(0)
                        execution = json.load(f)
                        
                        # Проверяем что статус все еще QUEUED
                        if execution.get('status') != 'QUEUED':
                            continue
                        
                        # Двойная проверка параллелизма (на случай если другой процесс уже взял задачу)
                        active_runs = get_worker_active_runs_count(worker_id)
                        if active_runs >= max_concurrency:
                            continue
                        
                        # Обновляем статус на RUNNING
                        execution['status'] = 'RUNNING'
                        execution['startedAt'] = time.time()
                        execution['workerId'] = worker_id
                        
                        # Сохраняем snapshot worker на момент claim
                        execution['workerName'] = worker_data.get('name', 'Unknown')
                        execution['workerTags'] = worker_data.get('tags', [])
                        
                        # Записываем обратно
                        f.seek(0)
                        f.truncate()
                        json.dump(execution, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                        
                        app.logger.info(f"Server claimed run {execution_id} for project {proj_id} by worker {worker_id}")
                        return execution_id, execution, proj_id
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except IOError:
                # Файл заблокирован, пробуем следующий
                continue
            except Exception as e:
                app.logger.error(f"Error claiming run {execution_id}: {e}")
                continue
        
        return None, None, None
        
    except Exception as e:
        app.logger.error(f"Error in server_claim_next_execution: {e}", exc_info=True)
        return None, None, None


def get_worker_token_from_request():
    """Извлекает worker token из Authorization header"""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]  # Убираем "Bearer "
    return None


@app.route('/api/worker/register', methods=['POST'])
@require_optional_auth
def api_worker_register():
    """
    API: Регистрация worker (опционально, рекомендуется создавать через admin API)
    В production лучше использовать pre-shared tokens через admin API
    """
    try:
        try:
            from .worker_registry import create_worker
        except ImportError:
            from worker_registry import create_worker
        
        data = request.json or {}
        name = data.get('name', '')
        capabilities = data.get('capabilities', {})
        tags = data.get('tags', [])
        
        if not name:
            return jsonify({'success': False, 'error': 'Worker name is required'}), 400
        
        worker_id, worker_token = create_worker(name, capabilities, tags)
        
        app.logger.info(f"Worker registered via API: {worker_id} ({name})")
        
        return jsonify({
            'success': True,
            'workerId': worker_id,
            'workerToken': worker_token
        })
    except Exception as e:
        app.logger.error(f"Error registering worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# Rate limiting для /claim (простая реализация)
_claim_rate_limits = {}  # worker_id -> last_claim_time
CLAIM_RATE_LIMIT_SECONDS = 1  # Минимум 1 секунда между claims от одного worker
# Примечание: worker должен использовать poll_interval >= 2 секунды, чтобы избежать rate limiting

# Rate limiting для логирования ошибок токена (чтобы не спамить логи)
_token_error_log_times = {}  # token_prefix -> last_log_time
TOKEN_ERROR_LOG_INTERVAL = 60  # Логируем ошибку токена не чаще раза в минуту

def log_invalid_token_attempt(token: str, endpoint: str = ""):
    """Логирует попытку использования невалидного токена с ограничением частоты"""
    import time
    token_prefix = token[:8] + '...' + token[-4:] if len(token) > 12 else token[:8] + '...'
    now = time.time()
    last_log_time = _token_error_log_times.get(token_prefix, 0)
    
    if now - last_log_time >= TOKEN_ERROR_LOG_INTERVAL:
        try:
            from .worker_registry import load_all_workers
        except ImportError:
            from worker_registry import load_all_workers
        workers = load_all_workers()
        enabled_count = sum(1 for w in workers.values() if w.get('enabled', True))
        endpoint_info = f" on {endpoint}" if endpoint else ""
        app.logger.warning(
            f"Invalid worker token attempt{endpoint_info} (token: {token_prefix}, "
            f"total workers: {len(workers)}, enabled: {enabled_count}). "
            f"Worker may need to register or update token."
        )
        _token_error_log_times[token_prefix] = now

@app.route('/api/worker/claim', methods=['POST'])
@require_auth
def api_worker_claim():
    """API: Claim следующей QUEUED задачи с фильтрацией и учетом параллелизма"""
    try:
        try:
            from .worker_registry import verify_token, update_worker_heartbeat
        except ImportError:
            from worker_registry import verify_token, update_worker_heartbeat
        
        # Проверяем токен
        token = get_worker_token_from_request()
        if not token:
            return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401
        
        result = verify_token(token)
        if not result:
            log_invalid_token_attempt(token, '/api/worker/claim')
            return jsonify({'success': False, 'error': 'Invalid worker token'}), 401
        
        worker_id, worker_data = result
        
        # Rate limiting
        import time
        now = time.time()
        last_claim = _claim_rate_limits.get(worker_id, 0)
        if now - last_claim < CLAIM_RATE_LIMIT_SECONDS:
            return jsonify({'success': False, 'error': 'Rate limit exceeded'}), 429
        _claim_rate_limits[worker_id] = now
        
        # Параметры из body
        data = request.json or {}
        project_id = data.get('projectId')
        max_concurrency = data.get('maxConcurrency', 1)  # По умолчанию 1
        tags = data.get('tags')  # Опциональные tags для фильтрации
        
        # Claim execution с фильтрацией
        execution_id, execution, proj_id = server_claim_next_execution(
            worker_id=worker_id,
            worker_data=worker_data,
            project_id=project_id,
            max_concurrency=max_concurrency,
            tags=tags
        )
        
        if not execution_id:
            app.logger.debug(f"[Worker Claim] Worker {worker_id} requested task, but no QUEUED tasks available (project_id={project_id})")
            return '', 204  # No Content
        
        app.logger.info(f"[Worker Claim] ✅ Worker {worker_id} claimed execution {execution_id} from project {proj_id}")
        
        # Обновляем heartbeat
        update_worker_heartbeat(worker_id, current_execution_id=execution_id)
        
        # Логируем только workerId, не токен
        app.logger.info(f"Worker {worker_id} claimed execution {execution_id}")
        
        # Возвращаем данные execution
        return jsonify({
            'success': True,
            'executionId': execution_id,
            'projectId': proj_id,
            'playbookId': execution.get('playbookId'),
            'runParams': execution.get('runParams', {}),
            'queuedAt': execution.get('queuedAt'),
            'createdAt': execution.get('createdAt'),
            'selectionSnapshot': execution.get('selectionSnapshot', {}),
            'inventorySnapshot': execution.get('inventorySnapshot', {})
        })
    except Exception as e:
        app.logger.error(f"Error in worker claim: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/worker/executions/<execution_id>/log', methods=['POST'])
@require_auth
def api_worker_execution_log(execution_id):
    """API: Добавление лога в execution"""
    try:
        try:
            from .worker_registry import verify_token
        except ImportError:
            from worker_registry import verify_token
        try:
            from .executions_store import append_execution_log, get_execution
        except ImportError:
            from executions_store import append_execution_log, get_execution
        
        # Проверяем токен
        token = get_worker_token_from_request()
        if not token:
            return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401
        
        result = verify_token(token)
        if not result:
            log_invalid_token_attempt(token, '/api/worker/executions/<id>/log')
            return jsonify({'success': False, 'error': 'Invalid worker token'}), 401
        
        worker_id, worker_data = result
        
        data = request.json or {}
        text = data.get('text', '')
        
        if not text:
            return jsonify({'success': False, 'error': 'Log text is required'}), 400
        
        # Получаем execution для определения project_id
        execution = get_execution(execution_id)
        if not execution:
            return jsonify({'success': False, 'error': 'Execution not found'}), 404
        
        project_id = execution.get('projectId')
        if not project_id:
            return jsonify({'success': False, 'error': 'Execution has no projectId'}), 400
        
        # Проверяем что worker имеет право писать логи для этого execution
        if execution.get('workerId') != worker_id:
            return jsonify({'success': False, 'error': 'Worker does not own this execution'}), 403
        
        # Добавляем лог
        append_execution_log(execution_id, text, project_id=project_id)
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error adding execution log: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/worker/executions/<execution_id>/finish', methods=['POST'])
@require_auth
def api_worker_execution_finish(execution_id):
    """API: Завершение execution"""
    try:
        try:
            from .worker_registry import verify_token, update_worker_heartbeat
        except ImportError:
            from worker_registry import verify_token, update_worker_heartbeat
        try:
            from .executions_store import update_execution_record, get_execution
        except ImportError:
            from executions_store import update_execution_record, get_execution
        
        # Проверяем токен
        token = get_worker_token_from_request()
        if not token:
            return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401
        
        result = verify_token(token)
        if not result:
            log_invalid_token_attempt(token, '/api/worker/executions/<id>/finish')
            return jsonify({'success': False, 'error': 'Invalid worker token'}), 401
        
        worker_id, worker_data = result
        
        data = request.json or {}
        status = data.get('status')
        finished_at = data.get('finishedAt', time.time())
        duration = data.get('duration')
        return_code = data.get('returnCode')
        error = data.get('error')
        result = data.get('result')  # Результат для HOST_CHECK и HOST_FACTS
        
        if status not in ['SUCCESS', 'FAILED', 'CANCELED']:
            return jsonify({'success': False, 'error': 'Status must be SUCCESS, FAILED or CANCELED'}), 400
        
        # Получаем execution для определения project_id
        execution = get_execution(execution_id)
        if not execution:
            return jsonify({'success': False, 'error': 'Execution not found'}), 404
        
        project_id = execution.get('projectId')
        if not project_id:
            return jsonify({'success': False, 'error': 'Execution has no projectId'}), 400
        
        # Проверяем что worker имеет право завершать этот execution (если workerId установлен)
        execution_worker_id = execution.get('workerId')
        if execution_worker_id and execution_worker_id != worker_id:
            return jsonify({'success': False, 'error': 'Worker does not own this execution'}), 403
        
        # Обновляем execution
        updates = {
            'status': status,
            'finishedAt': finished_at
        }
        if duration is not None:
            updates['duration'] = duration
        if return_code is not None:
            updates['returnCode'] = return_code
        if error:
            updates['error'] = error
        if result:
            updates['result'] = result  # Сохраняем результат для HOST_CHECK и HOST_FACTS
            app.logger.debug(f"[api_worker_execution_finish] Saving result for execution {execution_id}: {result}")

        # Если это HOST_CHECK, обновляем кэш статуса хоста (один хост или несколько из result.hosts)
        try:
            run_params = execution.get('runParams', {}) or {}
            execution_type = run_params.get('execution_type')
            if execution_type == 'HOST_CHECK':
                hosts_list = run_params.get('hosts')
                if isinstance(hosts_list, list) and len(hosts_list) > 1 and isinstance(result, dict) and 'hosts' in result:
                    # Мног Host check: обновляем статус по каждому хосту из result.hosts
                    per_host = result.get('hosts') or {}
                    for host_name, host_status in per_host.items():
                        if host_name and host_status in ('online', 'offline'):
                            set_host_check_status(project_id, host_name, host_status)
                            app.logger.debug(f"[api_worker_execution_finish] Cached host status: project={project_id}, host={host_name}, status={host_status}")
                else:
                    host_name = run_params.get('host') or run_params.get('limit_host')
                    host_status = 'unknown'
                    if status == 'SUCCESS' and isinstance(result, dict):
                        host_status = 'online' if result.get('available') else 'offline'
                    elif status in ('FAILED', 'CANCELED'):
                        host_status = 'offline'
                    if host_name:
                        set_host_check_status(project_id, host_name, host_status)
                        app.logger.info(f"[api_worker_execution_finish] Cached host status: project={project_id}, host={host_name}, status={host_status}")
        except Exception as cache_e:
            app.logger.warning(f"[api_worker_execution_finish] Failed to update host status cache: {cache_e}")
        
        update_execution_record(execution_id, updates, project_id=project_id)
        app.logger.info(f"[api_worker_execution_finish] Updated execution {execution_id} with status {status}, result={'present' if result else 'none'}")
        
        # Очистка generated_playbooks (резерв: worker тоже чистит, но на случай сбоя worker)
        try:
            _cleanup_generated_playbooks_for_execution(project_id, execution_id)
        except Exception as cleanup_e:
            app.logger.debug(f"[api_worker_execution_finish] Cleanup generated_playbooks: {cleanup_e}")
        
        # Обновляем heartbeat (очищаем currentExecutionId)
        update_worker_heartbeat(worker_id, current_execution_id=None)
        
        app.logger.info(f"Worker {worker_id} finished execution {execution_id} with status {status}")
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error finishing execution: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/worker/heartbeat', methods=['POST'])
@require_auth
def api_worker_heartbeat():
    """API: Heartbeat от worker"""
    try:
        try:
            from .worker_registry import verify_token, update_worker_heartbeat
        except ImportError:
            from worker_registry import verify_token, update_worker_heartbeat
        
        # Проверяем токен
        token = get_worker_token_from_request()
        if not token:
            return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401
        
        result = verify_token(token)
        if not result:
            log_invalid_token_attempt(token, '/api/worker/heartbeat')
            return jsonify({'success': False, 'error': 'Invalid worker token'}), 401
        
        worker_id, worker_data = result
        
        data = request.json or {}
        current_execution_id = data.get('currentExecutionId')
        
        # Обновляем heartbeat
        update_worker_heartbeat(worker_id, current_execution_id=current_execution_id)
        
        # Проверяем, нужно ли запросить systemInfo
        try:
            from .worker_registry import load_worker
        except ImportError:
            from worker_registry import load_worker
        worker = load_worker(worker_id)
        request_system_info = worker and worker.get('systemInfoRequested', False)
        
        # Сбрасываем флаг если он был установлен
        if request_system_info:
            try:
                from .worker_registry import save_worker
            except ImportError:
                from worker_registry import save_worker
            worker['systemInfoRequested'] = False
            save_worker(worker)
        
        return jsonify({
            'success': True,
            'workerId': worker_id,  # Возвращаем workerId для клиента
            'requestSystemInfo': request_system_info  # Сообщаем worker, что нужно отправить systemInfo
        })
    except Exception as e:
        app.logger.error(f"Error processing heartbeat: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/worker/system-info', methods=['POST'])
@require_auth
def api_worker_system_info():
    """API: Получение системной информации от worker"""
    try:
        try:
            from .worker_registry import verify_token, load_worker, save_worker
        except ImportError:
            from worker_registry import verify_token, load_worker, save_worker
        import time
        
        # Проверяем токен
        token = get_worker_token_from_request()
        if not token:
            return '', 401
        
        result = verify_token(token)
        if not result:
            return '', 401
        
        worker_id, worker_data = result
        
        # Получаем systemInfo из запроса
        data = request.json or {}
        system_info = data.get('systemInfo')
        
        if not system_info:
            return jsonify({'success': False, 'error': 'Missing systemInfo'}), 400
        
        # Загружаем worker
        worker = load_worker(worker_id)
        if not worker:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
        
        # Обновляем systemInfo и timestamp
        worker['systemInfo'] = system_info
        worker['systemInfoUpdatedAt'] = time.time()
        
        # Сохраняем
        if save_worker(worker):
            return '', 204  # No Content
        else:
            return jsonify({'success': False, 'error': 'Failed to save worker'}), 500
            
    except Exception as e:
        app.logger.error(f"Error processing system info: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def server_recover_stuck_executions(max_age_minutes=30, grace_period_minutes=5, heartbeat_ttl_seconds=60,
                                    max_cancel_minutes=5):
    """
    Серверная функция для восстановления застрявших executions
    
    Логика recovery:
    - RUNNING: если startedAt > MAX_RUN_TIME → FAILED (timeout)
    - CANCELING: если cancelRequestedAt > MAX_CANCEL_TIME → CANCELED (timeout)
    
    Args:
        max_age_minutes: Максимальный возраст RUNNING execution перед timeout
        grace_period_minutes: Grace period для offline workers перед recovery
        heartbeat_ttl_seconds: TTL для heartbeat (считаем worker offline если нет heartbeat > TTL)
        max_cancel_minutes: Максимальный возраст CANCELING execution перед force cancel
    """
    from worker_registry import load_all_workers, is_worker_online
    try:
        from .executions_store import (
            update_execution_record,
            get_execution,
        )
    except ImportError:
        from executions_store import (
            update_execution_record,
            get_execution,
        )
    import fcntl
    
    try:
        recovered = 0
        workers = load_all_workers()
        now = time.time()
        
        # Собираем все RUNNING и CANCELING executions
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            
            project_id = proj_dir.name
            executions_dir = proj_dir / 'executions'
            if not executions_dir.exists():
                continue
            
            for exec_file in executions_dir.glob('*.json'):
                try:
                    with open(exec_file, 'r+', encoding='utf-8') as f:
                        # Блокируем файл для атомарной операции
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except IOError:
                            # Файл заблокирован, пропускаем
                            continue
                        
                        try:
                            execution = json.load(f)
                            
                            status = execution.get('status')
                            execution_id = execution.get('id')
                            
                            # Никогда не трогаем финальные статусы
                            if is_final_status(status):
                                continue
                            
                            # Обработка RUNNING → FAILED (timeout)
                            if status == 'RUNNING':
                                worker_id = execution.get('workerId')
                                started_at = execution.get('startedAt')
                                if not started_at:
                                    continue
                                
                                # Проверяем возраст execution
                                age_minutes = (now - started_at) / 60
                                
                                # Проверяем онлайн ли worker
                                worker_online = is_worker_online(worker_id, ttl_seconds=heartbeat_ttl_seconds) if worker_id else False
                                
                                # Решение о recovery:
                                # Execution старше max_age_minutes → FAILED (timeout)
                                should_recover = False
                                recovery_reason = ""
                                
                                if age_minutes > max_age_minutes:
                                    should_recover = True
                                    recovery_reason = f"timeout (age: {age_minutes:.1f} min)"
                                
                                if should_recover:
                                    # Валидируем переход
                                    try:
                                        validate_status_transition('RUNNING', 'FAILED')
                                    except ValueError as e:
                                        app.logger.warning(f"Invalid transition for recovery: {e}")
                                        continue
                                    
                                    # Переводим в FAILED
                                    execution['status'] = 'FAILED'
                                    execution['finishedAt'] = now
                                    execution['duration'] = int(now - started_at)
                                    execution['statusUpdatedAt'] = now
                                    execution['error'] = f"Execution timeout after {age_minutes:.1f} minutes"
                                    
                                    # Записываем обратно
                                    f.seek(0)
                                    f.truncate()
                                    json.dump(execution, f, indent=2, ensure_ascii=False)
                                    f.flush()
                                    
                                    # Логируем
                                    append_execution_log(execution_id, f"[recovery] Execution timeout after {age_minutes:.1f} minutes, marked as FAILED\n", project_id=project_id)
                                    
                                    app.logger.info(f"Recovered stuck RUNNING execution {execution_id} → FAILED ({recovery_reason}, worker: {worker_id})")
                                    recovered += 1
                            
                            # Обработка CANCELING → CANCELED (timeout)
                            elif status == 'CANCELING':
                                cancel_requested_at = execution.get('cancelRequestedAt')
                                if not cancel_requested_at:
                                    continue
                                
                                # Проверяем возраст CANCELING
                                cancel_age_minutes = (now - cancel_requested_at) / 60
                                
                                if cancel_age_minutes > max_cancel_minutes:
                                    # Валидируем переход
                                    try:
                                        validate_status_transition('CANCELING', 'CANCELED')
                                    except ValueError as e:
                                        app.logger.warning(f"Invalid transition for recovery: {e}")
                                        continue
                                    
                                    # Переводим в CANCELED
                                    execution['status'] = 'CANCELED'
                                    execution['canceledAt'] = now
                                    execution['cancelReason'] = 'timeout'
                                    execution['statusUpdatedAt'] = now
                                    
                                    # Вычисляем duration если есть startedAt
                                    started_at = execution.get('startedAt')
                                    if started_at:
                                        execution['duration'] = int(now - started_at)
                                        execution['finishedAt'] = now
                                    
                                    # Записываем обратно
                                    f.seek(0)
                                    f.truncate()
                                    json.dump(execution, f, indent=2, ensure_ascii=False)
                                    f.flush()
                                    
                                    # Логируем
                                    append_execution_log(execution_id, f"[recovery] Force canceled after {cancel_age_minutes:.1f} minutes timeout\n", project_id=project_id)
                                    
                                    app.logger.info(f"Recovered stuck CANCELING execution {execution_id} → CANCELED (timeout: {cancel_age_minutes:.1f} min)")
                                    recovered += 1
                        finally:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception as e:
                    app.logger.warning(f"Error checking execution {exec_file}: {e}")
        
        if recovered > 0:
            app.logger.info(f"Recovered {recovered} stuck executions")
        
        return recovered
    except Exception as e:
        app.logger.error(f"Error in server_recover_stuck_executions: {e}", exc_info=True)
        return 0


# ============================================================================
# BACKGROUND TASKS
# ============================================================================

def start_recovery_task():
    """Запускает фоновый поток для recovery застрявших executions"""
    def recovery_worker():
        import time
        recovery_interval = 60  # 1 минута (чаще для лучшего recovery)
        while True:
            try:
                time.sleep(recovery_interval)
                server_recover_stuck_executions(
                    max_age_minutes=30,
                    grace_period_minutes=5,
                    heartbeat_ttl_seconds=60,
                    max_cancel_minutes=5
                )
            except Exception as e:
                app.logger.error(f"Error in recovery task: {e}", exc_info=True)
    
    recovery_thread = threading.Thread(target=recovery_worker, daemon=True)
    recovery_thread.start()
    app.logger.info("Recovery task started")


# Запускаем recovery task при старте приложения
start_recovery_task()

# Мигрируем legacy workers (default worker не создаём — воркер регистрируется сам через POST /api/worker/register)
try:
    try:
        from .worker_registry import migrate_legacy_workers
    except ImportError:
        from worker_registry import migrate_legacy_workers
    migrate_legacy_workers()
except Exception as e:
    app.logger.error(f"Error initializing workers: {e}", exc_info=True)


if __name__ == '__main__':
    # Project Storage directories are created on demand per project
    
    # ============================================================================
    # GLOBAL SECRETS API ENDPOINTS
    # ============================================================================
    
    @app.route('/api/global/secrets', methods=['GET'])
    @require_auth
    def api_list_global_secrets():
        """API: List all global secrets (metadata only, no secret material)"""
        try:
            # Check read permission
            if not can_read_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            if global_secrets_manager is None:
                return jsonify({'success': False, 'error': 'Global secrets manager not available'}), 503
            
            secret_type = request.args.get('type')
            search = request.args.get('search')
            
            secrets = global_secrets_manager.list_secrets(
                secret_type=secret_type,
                search=search
            )
            
            return jsonify({
                'success': True,
                'secrets': secrets
            })
        except Exception as e:
            safe_error_msg = safe_log_error("Error listing global secrets", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to list global secrets'}), 500
    
    @app.route('/api/global/secrets/<secret_id>', methods=['GET'])
    @require_auth
    def api_get_global_secret(secret_id):
        """API: Get global secret by ID (metadata only, no secret material)"""
        try:
            # Check read permission
            if not can_read_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            if global_secrets_manager is None:
                return jsonify({'success': False, 'error': 'Global secrets manager not available'}), 503
            
            secret = global_secrets_manager.get_secret(secret_id, include_material=False)
            
            if not secret:
                return jsonify({'success': False, 'error': 'Secret not found'}), 404
            
            return jsonify({
                'success': True,
                'secret': secret
            })
        except GlobalSecretError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            safe_error_msg = safe_log_error("Error getting global secret", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to get global secret'}), 500
    
    @app.route('/api/global/secrets', methods=['POST'])
    @require_auth
    def api_create_global_secret():
        """API: Create a new global secret"""
        try:
            # Check write permission
            if not can_write_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            if global_secrets_manager is None:
                return jsonify({'success': False, 'error': 'Global secrets manager not available'}), 503
            
            data = request.json or {}
            
            # Safely extract and validate name
            name = data.get('name')
            if name:
                name = str(name).strip()
            else:
                name = ''
            
            # Safely extract and validate secret_type
            secret_type = data.get('type')
            if secret_type:
                secret_type = str(secret_type).strip()
            else:
                secret_type = ''
            
            # Safely extract description
            description = data.get('description')
            if description:
                description = str(description).strip() or None
            else:
                description = None
            
            if not name:
                return jsonify({'success': False, 'error': 'Secret name is required'}), 400
            
            if not secret_type:
                return jsonify({'success': False, 'error': 'Secret type is required'}), 400
            
            # Extract secret data (all fields except name, type, description, metadata)
            secret_data = {k: v for k, v in data.items() if k not in ['name', 'type', 'description', 'metadata']}
            
            # Extract metadata fields and merge them into secret_data (for create_secret to process)
            metadata = data.get('metadata', {})
            if isinstance(metadata, dict):
                # Merge metadata fields into secret_data (create_secret will move them to metadata dict)
                for key, value in metadata.items():
                    if value:  # Only add non-empty values
                        secret_data[key] = value
            
            # Create secret
            secret = global_secrets_manager.create_secret(
                name=name,
                secret_type=secret_type,
                data=secret_data,
                description=description
            )
            
            app.logger.info(f"Global secret created: {name} (id: {secret.get('id')})")
            
            return jsonify({
                'success': True,
                'secret': secret,
                'message': 'Secret created successfully'
            }), 201
        
        except GlobalSecretError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            safe_error_msg = safe_log_error("Error creating global secret", e, data if 'data' in locals() else None)
            app.logger.error(safe_error_msg)
            # Log full traceback for debugging
            import traceback
            app.logger.error(f"Traceback: {traceback.format_exc()}")
            return jsonify({
                'success': False, 
                'error': 'Failed to create global secret',
                'errorCode': 'INTERNAL_ERROR'
            }), 500
    
    @app.route('/api/global/secrets/<secret_id>', methods=['PUT'])
    @require_auth
    def api_update_global_secret(secret_id):
        """API: Update an existing global secret"""
        try:
            # Check write permission
            if not can_write_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            if global_secrets_manager is None:
                return jsonify({'success': False, 'error': 'Global secrets manager not available'}), 503
            
            data = request.json or {}
            
            name = data.get('name')
            description = data.get('description')
            metadata = data.get('metadata')
            
            # Extract secret material updates (fields that should be encrypted)
            secret_material = {}
            secret_material_fields = ['privateKey', 'passphrase', 'token', 'password']
            for field in secret_material_fields:
                if field in data:
                    secret_material[field] = data[field]
            
            # Also include other optional fields that might be updated
            optional_fields = ['username', 'publicKey', 'fingerprint']
            for field in optional_fields:
                if field in data:
                    secret_material[field] = data[field]
            
            # Update secret
            secret = global_secrets_manager.update_secret(
                secret_id=secret_id,
                name=name,
                description=description,
                metadata=metadata,
                secret_material=secret_material if secret_material else None
            )
            
            app.logger.info(f"Global secret updated: {secret_id}")
            
            return jsonify({
                'success': True,
                'secret': secret,
                'message': 'Secret updated successfully'
            })
        
        except GlobalSecretError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            safe_error_msg = safe_log_error("Error updating global secret", e, data if 'data' in locals() else None)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to update global secret'}), 500
    
    @app.route('/api/global/secrets/<secret_id>', methods=['DELETE'])
    @require_auth
    def api_delete_global_secret(secret_id):
        """API: Delete a global secret"""
        try:
            # Check write permission
            if not can_write_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            if global_secrets_manager is None:
                return jsonify({'success': False, 'error': 'Global secrets manager not available'}), 503
            
            deleted = global_secrets_manager.delete_secret(secret_id)
            
            if not deleted:
                return jsonify({'success': False, 'error': 'Secret not found'}), 404
            
            app.logger.info(f"Global secret deleted: {secret_id}")
            
            return jsonify({
                'success': True,
                'message': 'Secret deleted successfully'
            })
        
        except GlobalSecretError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            safe_error_msg = safe_log_error("Error deleting global secret", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to delete global secret'}), 500
    
    @app.route('/api/global/secrets/options', methods=['GET'])
    @require_auth
    def api_get_global_secret_options():
        """API: Get minimal secret list for dropdowns (id, name, type, short meta)"""
        try:
            # Check read permission (needed to see secrets in dropdown)
            if not can_read_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            if global_secrets_manager is None:
                return jsonify({'success': False, 'error': 'Global secrets manager not available'}), 503
            
            purpose = request.args.get('purpose')
            
            options = global_secrets_manager.get_secret_options(purpose=purpose)
            
            return jsonify({
                'success': True,
                'options': options
            })
        except Exception as e:
            safe_error_msg = safe_log_error("Error getting global secret options", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to get global secret options'}), 500
    
    @app.route('/api/global/secrets/permissions', methods=['GET'])
    @require_auth
    def api_get_global_secrets_permissions():
        """API: Get current user permissions for global secrets"""
        try:
            return jsonify({
                'success': True,
                'permissions': {
                    'read': can_read_global_secrets(),
                    'write': can_write_global_secrets()
                }
            })
        except Exception as e:
            safe_error_msg = safe_log_error("Error getting global secrets permissions", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to get permissions'}), 500
    
    @app.route('/api/global/secrets/encryption-key', methods=['GET'])
    def api_get_encryption_key():
        """API: Get encryption key presence info (never returns key material)"""
        try:
            # Check write permission (needed to view key info)
            if not can_write_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            from secret_encryption import get_encryption_key_file_path
            
            # Check if key exists in environment
            env_key = os.environ.get('GLOBAL_SECRETS_ENCRYPTION_KEY')
            key_file = get_encryption_key_file_path(DATA_DIR)
            file_exists = key_file.exists()
            key_source = 'environment' if env_key else ('file' if file_exists else 'none')
            
            return jsonify({
                'success': True,
                'key': {
                    # "exists" is derived without reading key material
                    'exists': bool(env_key) or bool(file_exists),
                    'source': key_source,
                    # No masked value, no file path (defense in depth)
                    'masked': None,
                    'filePath': None
                }
            })
        except Exception as e:
            safe_error_msg = safe_log_error("Error getting encryption key info", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to get encryption key info'}), 500
    
    @app.route('/api/global/secrets/encryption-key/download', methods=['GET'])
    def api_download_encryption_key():
        """API: Disabled (security) - encryption key is never downloadable"""
        try:
            # Always disabled: do not provide any pathway to retrieve key material.
            return jsonify({
                'success': False,
                'error': 'Endpoint disabled for security',
                'errorCode': 'DISABLED'
            }), 404
        except Exception as e:
            safe_error_msg = safe_log_error("Error downloading encryption key", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to download encryption key'}), 500
    
    @app.route('/api/global/secrets/encryption-key', methods=['POST'])
    def api_update_encryption_key():
        """API: Update encryption key (with warning about existing secrets)"""
        try:
            # Check write permission
            if not can_write_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            data = request.get_json()
            new_key = data.get('key', '').strip() if data else ''
            
            if not new_key:
                return jsonify({
                    'success': False,
                    'error': 'Encryption key is required'
                }), 400
            
            # Validate key format (should be base64url-safe string, at least 32 characters)
            if len(new_key) < 32:
                return jsonify({
                    'success': False,
                    'error': 'Encryption key must be at least 32 characters long'
                }), 400
            
            from secret_encryption import save_encryption_key, get_encryption_key_file_path, load_encryption_key
            from global_secrets_manager import GlobalSecretsManager
            
            # Check if key already exists
            existing_key = load_encryption_key(DATA_DIR)
            if existing_key:
                # Check if there are existing secrets
                if global_secrets_manager:
                    existing_secrets = global_secrets_manager.list_secrets()
                    if existing_secrets:
                        # Return warning (don't save yet)
                        return jsonify({
                            'success': False,
                            'error': 'Cannot change encryption key: existing secrets found',
                            'errorCode': 'EXISTING_SECRETS',
                            'details': {
                                'secretCount': len(existing_secrets),
                                'message': f'There are {len(existing_secrets)} existing secret(s). Changing the encryption key will make them undecryptable. Please delete all secrets first or recreate them with the new key.'
                            }
                        }), 400
            
            # Save new key
            if save_encryption_key(new_key, DATA_DIR):
                # Reset encryption instance to use new key
                import secret_encryption
                secret_encryption._encryption_instance = None
                
                return jsonify({
                    'success': True,
                    'message': 'Encryption key updated successfully'
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Failed to save encryption key'
                }), 500
                
        except Exception as e:
            safe_error_msg = safe_log_error("Error updating encryption key", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to update encryption key'}), 500
    
    @app.route('/api/global/secrets/encryption-key/create', methods=['POST'])
    def api_create_encryption_key():
        """API: Create new encryption key (if it doesn't exist)"""
        try:
            # Check write permission
            if not can_write_global_secrets():
                return jsonify({
                    'success': False,
                    'error': 'Permission denied',
                    'errorCode': 'FORBIDDEN'
                }), 403
            
            from secret_encryption import load_encryption_key, generate_and_save_encryption_key
            
            # Check if key already exists
            existing_key = load_encryption_key(DATA_DIR)
            if existing_key:
                return jsonify({
                    'success': False,
                    'error': 'Encryption key already exists',
                    'errorCode': 'KEY_EXISTS',
                    'message': 'Encryption key already exists. Use Replace to change it.'
                }), 400
            
            # Generate and save new key
            new_key = generate_and_save_encryption_key(DATA_DIR)
            
            # Reset encryption instance to use new key
            import secret_encryption
            secret_encryption._encryption_instance = None
            
            app.logger.info("New encryption key created and saved to file")
            
            return jsonify({
                'success': True,
                'message': 'Encryption key created successfully'
            })
                
        except Exception as e:
            safe_error_msg = safe_log_error("Error creating encryption key", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to create encryption key'}), 500
    
    # ============================================================================
    # PROJECT SECRETS API ENDPOINTS (unchanged)
    # ============================================================================
    
    def _sanitize_project_secret_for_api(secret_data, fallback_name=None):
        """
        Security: never return secret material after create/save.
        Returns metadata only (plus non-sensitive presence/length hints).
        """
        if not isinstance(secret_data, dict):
            secret_data = {}
        name = secret_data.get('name') or fallback_name or ''
        secret_type = secret_data.get('type', 'unknown')
        username = secret_data.get('username', '')
        description = secret_data.get('description', '')
        created_at = secret_data.get('createdAt', '')
        updated_at = secret_data.get('updatedAt', '')
        version = secret_data.get('version', 1)
        
        material = {}
        if secret_type == 'ssh_key':
            private_key = secret_data.get('privateKey') or ''
            passphrase = secret_data.get('passphrase') or ''
            material = {
                'privateKey': {'present': bool(private_key), 'length': len(private_key) if private_key else 0},
                'passphrase': {'present': bool(passphrase), 'length': len(passphrase) if passphrase else 0},
            }
        elif secret_type == 'login_password':
            password = secret_data.get('password') or ''
            material = {
                'password': {'present': bool(password), 'length': len(password) if password else 0},
            }
        
        return {
            'name': name,
            'type': secret_type,
            'username': username,
            'description': description,
            'createdAt': created_at,
            'updatedAt': updated_at,
            'version': version,
            'material': material,
        }
    
    @app.route('/api/secrets', methods=['GET'])
    @require_auth
    def api_list_secrets():
        """API: Получить список секретов (метаданные без чувствительных данных)
        
        Новая структура:
        - secrets/ssh_keys/<key_id>.json
        - secrets/vault/vault_pass
        - secrets/git_auth/<auth_id>.json
        """
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            secrets_dir = get_project_secrets_dir(project_id)
            secrets = []
            secrets_set = set()  # Для избежания дубликатов
            
            # Новая структура: secrets/ssh_keys/<key_id>.json
            ssh_keys_dir = secrets_dir / 'ssh_keys'
            ssh_keys_dir.mkdir(parents=True, exist_ok=True)
            for secret_file in ssh_keys_dir.glob('*.json'):
                try:
                    with open(secret_file, 'r', encoding='utf-8') as f:
                        secret_data = json.load(f)
                    
                    secret_name = secret_data.get('name', secret_file.stem)
                    if secret_name not in secrets_set:
                        secrets_set.add(secret_name)
                        secret_meta = {
                            'name': secret_name,
                            'type': secret_data.get('type', 'ssh_key'),
                            'username': secret_data.get('username', ''),
                            'description': secret_data.get('description', ''),
                            'createdAt': secret_data.get('createdAt', secret_data.get('updatedAt', '')),
                            'updatedAt': secret_data.get('updatedAt', ''),
                            'version': secret_data.get('version', 1)
                        }
                        secrets.append(secret_meta)
                except Exception as e:
                    app.logger.warning(f"Error reading secret file {secret_file}: {e}")
                    continue
            
            # Новая структура: secrets/git_auth/<auth_id>.json
            git_auth_dir = secrets_dir / 'git_auth'
            git_auth_dir.mkdir(parents=True, exist_ok=True)
            for secret_file in git_auth_dir.glob('*.json'):
                try:
                    with open(secret_file, 'r', encoding='utf-8') as f:
                        secret_data = json.load(f)
                    
                    secret_name = secret_data.get('name', secret_file.stem)
                    if secret_name not in secrets_set:
                        secrets_set.add(secret_name)
                        secret_meta = {
                            'name': secret_name,
                            'type': secret_data.get('type', 'git_auth'),
                            'username': secret_data.get('username', ''),
                            'description': secret_data.get('description', ''),
                            'createdAt': secret_data.get('createdAt', secret_data.get('updatedAt', '')),
                            'updatedAt': secret_data.get('updatedAt', ''),
                            'version': secret_data.get('version', 1)
                        }
                        secrets.append(secret_meta)
                except Exception as e:
                    app.logger.warning(f"Error reading secret file {secret_file}: {e}")
                    continue
            
            # Новая структура: secrets/vault/vault_pass
            vault_dir = secrets_dir / 'vault'
            vault_dir.mkdir(parents=True, exist_ok=True)
            vault_pass_file = vault_dir / 'vault_pass'
            if vault_pass_file.exists():
                if 'vault_pass' not in secrets_set:
                    secrets_set.add('vault_pass')
                    secrets.append({
                        'name': 'vault_pass',
                        'type': 'vault_password',
                        'username': '',
                        'description': 'Vault password',
                        'createdAt': '',
                        'updatedAt': '',
                        'version': 1
                    })
            
            # Сортировка по имени
            secrets.sort(key=lambda x: x['name'].lower())
            
            return jsonify({
                'success': True,
                'secrets': secrets
            })
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error listing secrets", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to list secrets'}), 500
    
    
    @app.route('/api/secrets/<secret_name>', methods=['GET'])
    @require_auth
    def api_get_secret(secret_name):
        """API: Получить секрет по имени (только метаданные, без значений)
        
        Новая структура: secrets/ssh_keys/, secrets/git_auth/, secrets/vault/
        """
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            # Ищем секрет в новой структуре: secrets/ssh_keys/, secrets/git_auth/, secrets/vault/
            secrets_dir = get_project_secrets_dir(project_id)
            secret_file = None
            
            # Проверяем ssh_keys
            ssh_keys_dir = secrets_dir / 'ssh_keys'
            ssh_key_file = ssh_keys_dir / f"{secret_name}.json"
            if ssh_key_file.exists():
                secret_file = ssh_key_file
            else:
                # Проверяем git_auth
                git_auth_dir = secrets_dir / 'git_auth'
                git_auth_file = git_auth_dir / f"{secret_name}.json"
                if git_auth_file.exists():
                    secret_file = git_auth_file
                elif secret_name == 'vault_pass':
                    # Проверяем vault
                    vault_dir = secrets_dir / 'vault'
                    vault_file = vault_dir / 'vault_pass'
                    if vault_file.exists():
                        # Для vault_pass возвращаем специальную структуру
                        with open(vault_file, 'r', encoding='utf-8') as f:
                            vault_pass = f.read().strip()
                        return jsonify({
                            'success': True,
                            'secret': {
                                'name': 'vault_pass',
                                'type': 'vault_password',
                                'username': '',
                                'description': 'Vault password',
                                'createdAt': '',
                                'updatedAt': '',
                                'version': 1,
                                'material': {
                                    'password': {'present': bool(vault_pass), 'length': len(vault_pass) if vault_pass else 0}
                                }
                            }
                        })
            
            # Если не нашли, используем get_secret_file_path для определения пути
            if not secret_file:
                secret_file = get_secret_file_path(project_id, secret_name)
            
            if not secret_file.exists():
                return jsonify({'success': False, 'error': 'Secret not found'}), 404
            
            # Для JSON файлов читаем как JSON
            if secret_file.suffix == '.json':
                with open(secret_file, 'r', encoding='utf-8') as f:
                    secret_data = json.load(f)
            else:
                # Для текстовых файлов (например vault_pass) создаем структуру
                with open(secret_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                secret_data = {
                    'name': secret_file.stem,
                    'type': 'vault_password',
                    'content': content
                }
            
            return jsonify({
                'success': True,
                'secret': _sanitize_project_secret_for_api(secret_data, fallback_name=secret_name)
            })
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error getting secret", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to get secret'}), 500
    
    
    @app.route('/api/secrets', methods=['POST'])
    @require_auth
    def api_create_secret():
        """API: Создать новый секрет"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            data = request.json or {}
            secret_name = data.get('name', '').strip()
            
            if not secret_name:
                return jsonify({'success': False, 'error': 'Secret name is required'}), 400
            
            # Валидация имени
            try:
                safe_name = slugify_secret_name(secret_name)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            
            # Валидация типа (должна быть перед использованием secret_type)
            secret_type = data.get('type', '').strip()
            if secret_type not in ['ssh_key', 'login_password']:
                return jsonify({'success': False, 'error': 'Invalid secret type. Must be "ssh_key" or "login_password"'}), 400
            
            # Определяем путь к файлу секрета в зависимости от типа
            secret_file = get_secret_file_path(project_id, safe_name, secret_type=secret_type)
            
            # Проверка на существование
            if secret_file.exists():
                return jsonify({'success': False, 'error': 'Secret with this name already exists'}), 409
            
            # Валидация полей в зависимости от типа
            if secret_type == 'ssh_key':
                private_key = data.get('privateKey', '').strip()
                if not private_key:
                    return jsonify({'success': False, 'error': 'Private key is required for SSH key secret'}), 400
                # Строгая валидация формата ключа
                is_valid, error_msg = validate_ssh_private_key(private_key)
                if not is_valid:
                    return jsonify({'success': False, 'error': f'Invalid private key format: {error_msg}'}), 400
            elif secret_type == 'login_password':
                password = data.get('password', '').strip()
                if not password:
                    return jsonify({'success': False, 'error': 'Password is required for login/password secret'}), 400
            
            # Создаем структуру секрета
            current_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            secret_data = {
                'version': 1,
                'type': secret_type,
                'name': safe_name,
                'username': data.get('username', '').strip(),
                'description': data.get('description', '').strip(),
                'createdAt': current_time,
                'updatedAt': current_time
            }
            
            # Добавляем поля в зависимости от типа
            if secret_type == 'ssh_key':
                secret_data['privateKey'] = private_key
                if data.get('passphrase'):
                    secret_data['passphrase'] = data.get('passphrase', '').strip()
            elif secret_type == 'login_password':
                secret_data['password'] = password
            
            # Сохраняем секрет
            with open(secret_file, 'w', encoding='utf-8') as f:
                json.dump(secret_data, f, indent=2, ensure_ascii=False)
            
            # Устанавливаем права доступа 600 (только владелец может читать/писать)
            os.chmod(secret_file, 0o600)
            
            app.logger.info(f"Secret created: {safe_name} in project {project_id}")
            
            return jsonify({
                'success': True,
                # Never return secret material after creation
                'secret': _sanitize_project_secret_for_api(secret_data, fallback_name=safe_name),
                'message': 'Secret created successfully'
            })
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error creating secret", e, data if 'data' in locals() else None)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to create secret'}), 500
    
    
    @app.route('/api/secrets/<secret_name>', methods=['PUT'])
    @require_auth
    def api_update_secret(secret_name):
        """API: Обновить существующий секрет
        
        Новая структура: secrets/ssh_keys/, secrets/git_auth/, secrets/vault/
        """
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            # Ищем секрет в новой структуре: secrets/ssh_keys/, secrets/git_auth/, secrets/vault/
            secrets_dir = get_project_secrets_dir(project_id)
            secret_file = None
            
            # Проверяем ssh_keys
            ssh_keys_dir = secrets_dir / 'ssh_keys'
            ssh_key_file = ssh_keys_dir / f"{secret_name}.json"
            if ssh_key_file.exists():
                secret_file = ssh_key_file
            else:
                # Проверяем git_auth
                git_auth_dir = secrets_dir / 'git_auth'
                git_auth_file = git_auth_dir / f"{secret_name}.json"
                if git_auth_file.exists():
                    secret_file = git_auth_file
                elif secret_name == 'vault_pass':
                    # Проверяем vault
                    vault_dir = secrets_dir / 'vault'
                    vault_file = vault_dir / 'vault_pass'
                    if vault_file.exists():
                        secret_file = vault_file
            
            # Если не нашли, используем get_secret_file_path для определения пути
            if not secret_file:
                secret_file = get_secret_file_path(project_id, secret_name)
            
            if not secret_file.exists():
                return jsonify({'success': False, 'error': 'Secret not found'}), 404
            
            data = request.json or {}
            secret_type = data.get('type', '').strip()
            
            # Загружаем существующий секрет
            if secret_file.suffix == '.json':
                with open(secret_file, 'r', encoding='utf-8') as f:
                    existing_secret = json.load(f)
            else:
                # Для текстовых файлов (например vault_pass) создаем структуру
                with open(secret_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                existing_secret = {
                    'name': secret_file.stem,
                    'type': 'vault_password',
                    'content': content
                }
            
            # Обновляем поля
            if 'username' in data:
                existing_secret['username'] = data.get('username', '').strip()
            
            if 'description' in data:
                existing_secret['description'] = data.get('description', '').strip()
            
            # Сохраняем createdAt при первом обновлении, если его еще нет
            if 'createdAt' not in existing_secret:
                existing_secret['createdAt'] = existing_secret.get('updatedAt', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            
            # Обновляем поля в зависимости от типа
            # Security: allow rotating secret material, but never return it.
            if existing_secret.get('type') == 'ssh_key':
                if 'privateKey' in data and str(data.get('privateKey') or '').strip():
                    private_key = str(data.get('privateKey') or '').strip()
                    # Строгая валидация формата ключа
                    is_valid, error_msg = validate_ssh_private_key(private_key)
                    if not is_valid:
                        return jsonify({'success': False, 'error': f'Invalid private key format: {error_msg}'}), 400
                    existing_secret['privateKey'] = private_key
                if 'passphrase' in data and str(data.get('passphrase') or '').strip():
                    existing_secret['passphrase'] = str(data.get('passphrase') or '').strip()
            elif existing_secret.get('type') == 'login_password':
                if 'password' in data and str(data.get('password') or '').strip():
                    existing_secret['password'] = str(data.get('password') or '').strip()
            
            # Обновляем timestamp
            existing_secret['updatedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            
            # Сохраняем обновленный секрет
            if secret_file.suffix == '.json':
                with open(secret_file, 'w', encoding='utf-8') as f:
                    json.dump(existing_secret, f, indent=2, ensure_ascii=False)
            else:
                # Для текстовых файлов (например vault_pass) сохраняем только содержимое
                if 'password' in data:
                    with open(secret_file, 'w', encoding='utf-8') as f:
                        f.write(data.get('password', '').strip())
                elif 'content' in data:
                    with open(secret_file, 'w', encoding='utf-8') as f:
                        f.write(data.get('content', '').strip())
            
            # Устанавливаем права доступа 600 (только владелец может читать/писать)
            os.chmod(secret_file, 0o600)
            
            app.logger.info(f"Secret updated: {secret_name} in project {project_id}")
            
            return jsonify({
                'success': True,
                'secret': _sanitize_project_secret_for_api(existing_secret, fallback_name=secret_name),
                'message': 'Secret updated successfully'
            })
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error updating secret", e, data if 'data' in locals() else None)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to update secret'}), 500
    
    
    @app.route('/api/secrets/<secret_name>', methods=['DELETE'])
    @require_auth
    def api_delete_secret(secret_name):
        """API: Удалить секрет
        
        Новая структура: secrets/ssh_keys/, secrets/git_auth/, secrets/vault/
        """
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            # Ищем секрет в новой структуре: secrets/ssh_keys/, secrets/git_auth/, secrets/vault/
            secrets_dir = get_project_secrets_dir(project_id)
            secret_file = None
            
            # Проверяем ssh_keys
            ssh_keys_dir = secrets_dir / 'ssh_keys'
            ssh_key_file = ssh_keys_dir / f"{secret_name}.json"
            if ssh_key_file.exists():
                secret_file = ssh_key_file
            else:
                # Проверяем git_auth
                git_auth_dir = secrets_dir / 'git_auth'
                git_auth_file = git_auth_dir / f"{secret_name}.json"
                if git_auth_file.exists():
                    secret_file = git_auth_file
                elif secret_name == 'vault_pass':
                    # Проверяем vault
                    vault_dir = secrets_dir / 'vault'
                    vault_file = vault_dir / 'vault_pass'
                    if vault_file.exists():
                        secret_file = vault_file
            
            # Если не нашли, используем get_secret_file_path для определения пути
            if not secret_file:
                secret_file = get_secret_file_path(project_id, secret_name)
            
            if not secret_file.exists():
                return jsonify({'success': False, 'error': 'Secret not found'}), 404
            
            # Удаляем файл
            secret_file.unlink()
            
            app.logger.info(f"Secret deleted: {secret_name} in project {project_id}")
            
            return jsonify({
                'success': True,
                'message': 'Secret deleted successfully'
            })
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error deleting secret", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to delete secret'}), 500
    
    # ============================================================================
    # CONNECTION SECRETS API (для Hosts & Groups)
    # ============================================================================
    
    @app.route('/api/secrets/meta', methods=['GET'])
    def api_get_secrets_meta():
        """API: Получить список секретов (только метаданные) для использования в выпадающих списках
        
        Новая структура: secrets/ssh_keys/, secrets/git_auth/, secrets/vault/
        """
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            secrets_dir = get_project_secrets_dir(project_id)
            project_dir = get_project_dir(project_id)
            secrets = []
            secrets_set = set()
            
            # Новая структура: secrets/ssh_keys/<key_id>.json
            ssh_keys_dir = secrets_dir / 'ssh_keys'
            ssh_keys_dir.mkdir(parents=True, exist_ok=True)
            for secret_file in ssh_keys_dir.glob('*.json'):
                try:
                    with open(secret_file, 'r', encoding='utf-8') as f:
                        secret_data = json.load(f)
                    
                    secret_name = secret_data.get('name', secret_file.stem)
                    if secret_name not in secrets_set:
                        secrets_set.add(secret_name)
                        secrets.append({
                            'name': secret_name,
                            'type': secret_data.get('type', 'ssh_key'),
                            'username': secret_data.get('username', '')
                        })
                except Exception as e:
                    app.logger.warning(f"Error reading secret file {secret_file}: {e}")
                    continue
            
            # Новая структура: secrets/git_auth/<auth_id>.json
            git_auth_dir = secrets_dir / 'git_auth'
            git_auth_dir.mkdir(parents=True, exist_ok=True)
            for secret_file in git_auth_dir.glob('*.json'):
                try:
                    with open(secret_file, 'r', encoding='utf-8') as f:
                        secret_data = json.load(f)
                    
                    secret_name = secret_data.get('name', secret_file.stem)
                    if secret_name not in secrets_set:
                        secrets_set.add(secret_name)
                        secrets.append({
                            'name': secret_name,
                            'type': secret_data.get('type', 'git_auth'),
                            'username': secret_data.get('username', '')
                        })
                except Exception as e:
                    app.logger.warning(f"Error reading secret file {secret_file}: {e}")
                    continue
            
            secrets.sort(key=lambda x: x['name'].lower())
            
            return jsonify({
                'success': True,
                'secrets': secrets
            })
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error getting secrets meta", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to get secrets meta'}), 500
    
    
    @app.route('/api/hosts/<host_name>/connection-secret/resolve', methods=['GET'])
    def api_resolve_host_connection_secret(host_name):
        """API: Разрешить connection secret для хоста (проверяет host_vars, затем group_vars)"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            app.logger.info(f"[api_resolve_host_connection_secret] Resolving connection secret for host {host_name} in project {project_id}")
            
            # Сначала проверяем host_vars
            host_vars_dir = get_project_host_vars_dir(project_id)
            host_file = host_vars_dir / f"{host_name}.yml"
            
            app.logger.info(f"[api_resolve_host_connection_secret] Checking host_vars file: {host_file}")
            
            if host_file.exists():
                try:
                    with open(host_file, 'r', encoding='utf-8') as f:
                        host_vars = yaml_loader.load(f) or {}
                    
                    app.logger.info(f"[api_resolve_host_connection_secret] Host vars loaded: {list(host_vars.keys())}")
                    
                    # Проверяем наличие connectionSecret (новый формат - ссылка на секрет)
                    if host_vars.get('connectionSecret'):
                        secret_name = host_vars['connectionSecret']
                        app.logger.info(f"[api_resolve_host_connection_secret] Found connection secret in host_vars: {secret_name}")
                        
                        # Загружаем секрет, чтобы получить username из него
                        ansible_user_from_secret = None
                        secrets_dir = get_project_secrets_dir(project_id)
                        ssh_keys_dir = secrets_dir / 'ssh_keys'
                        secret_file = ssh_keys_dir / f"{secret_name}.json"
                        if not secret_file.exists():
                            secret_file = secrets_dir / f"{secret_name}.json"
                        
                        if secret_file.exists():
                            try:
                                with open(secret_file, 'r', encoding='utf-8') as f:
                                    secret_data = json.load(f)
                                secret_username = secret_data.get('username', '').strip()
                                if secret_username:
                                    ansible_user_from_secret = secret_username
                                    app.logger.debug(f"[api_resolve_host_connection_secret] Found username in secret {secret_name}: {secret_username}")
                            except Exception as e:
                                app.logger.warning(f"[api_resolve_host_connection_secret] Error loading secret {secret_name}: {e}")
                        
                        # Используем username из секрета, если он есть, иначе из host_vars, иначе 'root'
                        ansible_user = ansible_user_from_secret or host_vars.get('ansible_user', 'root')
                        
                        # Проверяем, нужно ли обновить ключ из секрета
                        if host_vars.get('ansible_ssh_private_key_file'):
                            # Проверяем, находится ли ключ в secrets/ssh_keys
                            key_path = Path(host_vars['ansible_ssh_private_key_file'])
                            project_dir = get_project_dir(project_id)
                            
                            # Новая структура: secrets/ssh_keys/<key_id>.json
                            if (project_dir / 'repo').exists():
                                expected_key_path = secrets_dir / 'ssh_keys' / f"{secret_name}.json"
                            else:
                                # Старая структура: secrets/ssh_keys/<secret_name>_key
                                expected_key_path = secrets_dir / 'ssh_keys' / f"{secret_name}_key"
                            
                            # Если путь к ключу не соответствует текущему секрету, обновляем
                            if str(key_path) != str(expected_key_path):
                                app.logger.info(f"[api_resolve_host_connection_secret] Key path mismatch, will update on save")
                        
                        return jsonify({
                            'success': True,
                            'secretName': secret_name,
                            'source': 'host',
                            'hasConnection': True,
                            'ansible_user': ansible_user,
                            'ansible_port': host_vars.get('ansible_port', 22)
                        })
                    elif host_vars.get('ansible_ssh_private_key_file') or host_vars.get('ansible_password'):
                        # Connection настроен через Ansible переменные, но без connectionSecret
                        # Пытаемся определить секрет по содержимому ключа/пароля
                        app.logger.info(f"[api_resolve_host_connection_secret] Found connection variables in host_vars, trying to match secret")
                        
                        # Пытаемся найти секрет, сравнивая ключ/пароль
                        secrets_dir = get_project_secrets_dir(project_id)
                        matched_secret = None
                        
                        if host_vars.get('ansible_ssh_private_key_file'):
                            # Для SSH ключа пытаемся найти секрет по пути к ключу
                            key_path = Path(host_vars['ansible_ssh_private_key_file'])
                            project_dir = get_project_dir(project_id)
                            
                            # Проверяем, находится ли ключ в secrets/ssh_keys
                            if 'secrets' in str(key_path) and 'ssh_keys' in str(key_path):
                                # Новая структура: secrets/ssh_keys/<key_id>.json
                                key_file_name = key_path.stem  # Без расширения .json
                                secret_file = secrets_dir / 'ssh_keys' / f"{key_file_name}.json"
                                if secret_file.exists():
                                    matched_secret = key_file_name
                        
                        if not matched_secret and host_vars.get('ansible_password'):
                            # Для пароля пытаемся найти секрет по содержимому
                            password = host_vars['ansible_password']
                            # Ищем в новой структуре: secrets/ssh_keys/ и других поддиректориях
                            search_dirs = [secrets_dir]  # Старая структура для обратной совместимости
                            ssh_keys_dir = secrets_dir / 'ssh_keys'
                            if ssh_keys_dir.exists():
                                search_dirs.append(ssh_keys_dir)
                            
                            for search_dir in search_dirs:
                                for secret_file in search_dir.glob('*.json'):
                                    try:
                                        with open(secret_file, 'r', encoding='utf-8') as f:
                                            secret_data = json.load(f)
                                        if secret_data.get('type') == 'login_password' and secret_data.get('password') == password:
                                            matched_secret = secret_file.stem
                                            break
                                    except Exception:
                                        continue
                                if matched_secret:
                                    break
                        
                        if matched_secret:
                            # Загружаем секрет, чтобы получить username из него
                            ansible_user_from_secret = None
                            secrets_dir = get_project_secrets_dir(project_id)
                            ssh_keys_dir = secrets_dir / 'ssh_keys'
                            matched_secret_file = ssh_keys_dir / f"{matched_secret}.json"
                            if not matched_secret_file.exists():
                                matched_secret_file = secrets_dir / f"{matched_secret}.json"
                            
                            if matched_secret_file.exists():
                                try:
                                    with open(matched_secret_file, 'r', encoding='utf-8') as f:
                                        matched_secret_data = json.load(f)
                                    matched_secret_username = matched_secret_data.get('username', '').strip()
                                    if matched_secret_username:
                                        ansible_user_from_secret = matched_secret_username
                                except Exception:
                                    pass
                            
                            ansible_user = ansible_user_from_secret or host_vars.get('ansible_user', 'root')
                            
                            return jsonify({
                                'success': True,
                                'secretName': matched_secret,
                                'source': 'host',
                                'hasConnection': True,
                                'ansible_user': ansible_user,
                                'ansible_port': host_vars.get('ansible_port', 22)
                            })
                        else:
                            return jsonify({
                                'success': True,
                                'secretName': None,
                                'source': 'host',
                                'hasConnection': True,
                                'ansible_user': host_vars.get('ansible_user', 'root'),
                                'ansible_port': host_vars.get('ansible_port', 22)
                            })
                    else:
                        app.logger.info(f"[api_resolve_host_connection_secret] No connection variables in host_vars for {host_name}")
                except Exception as e:
                    app.logger.warning(f"Error reading host vars for {host_name}: {e}")
            else:
                app.logger.info(f"[api_resolve_host_connection_secret] Host vars file does not exist: {host_file}")
            
            # Если не найден в host_vars, проверяем группы (во всех inventory-файлах проекта)
            inventories_dir = get_project_inventories_dir(project_id)
            inventory_names = ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']
            inventory_files_to_check = []
            if inventories_dir.exists():
                for inv_file in inventories_dir.rglob('*'):
                    if inv_file.is_file() and inv_file.name in inventory_names:
                        rel = inv_file.relative_to(inventories_dir)
                        if 'host_vars' not in rel.parts and 'group_vars' not in rel.parts:
                            inventory_files_to_check.append(inv_file)
            repo_dir = get_project_dir(project_id) / 'repo'
            root_inv = repo_dir / 'inventory.yml'
            if root_inv.is_file():
                inventory_files_to_check.append(root_inv)
            if not inventory_files_to_check:
                inventory_file = get_project_inventory_file(project_id)
                if inventory_file.exists():
                    inventory_files_to_check = [inventory_file]
            for inv_path in inventory_files_to_check:
                try:
                    with open(inv_path, 'r', encoding='utf-8') as f:
                        inventory_data = yaml_loader.load(f) or {}
                    groups = inventory_data.get('all', {}).get('children', {})
                    for group_name, group_data in groups.items():
                        if not isinstance(group_data, dict):
                            continue
                        hosts = group_data.get('hosts', [])
                        if host_name not in hosts:
                            continue
                        group_vars_dir = get_project_group_vars_dir(project_id)
                        group_file = group_vars_dir / f"{group_name}.yml"
                        if not group_file.exists():
                            continue
                        try:
                            with open(group_file, 'r', encoding='utf-8') as f:
                                group_vars = yaml_loader.load(f) or {}
                        except Exception as e:
                            app.logger.warning(f"Error reading group vars for {group_name}: {e}")
                            continue
                        if not group_vars.get('connectionSecret'):
                            continue
                        secret_name = group_vars['connectionSecret']
                        ansible_user = group_vars.get('ansible_user', 'root')
                        ansible_port = group_vars.get('ansible_port', 22)
                        secrets_dir = get_project_secrets_dir(project_id)
                        secret_file = secrets_dir / 'ssh_keys' / f"{secret_name}.json"
                        if not secret_file.exists():
                            secret_file = secrets_dir / f"{secret_name}.json"
                        if secret_file.exists():
                            try:
                                with open(secret_file, 'r', encoding='utf-8') as sf:
                                    secret_data = json.load(sf)
                                if secret_data.get('username', '').strip():
                                    ansible_user = secret_data['username'].strip()
                            except Exception:
                                pass
                        return jsonify({
                            'success': True,
                            'secretName': secret_name,
                            'source': 'group',
                            'group': group_name,
                            'hasConnection': True,
                            'ansible_user': ansible_user,
                            'ansible_port': ansible_port
                        })
                except Exception as e:
                    app.logger.warning(f"Error reading inventory {inv_path}: {e}")
            
            # Не найден
            return jsonify({
                'success': True,
                'secretName': None,
                'source': 'none'
            })
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error resolving host connection secret", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to resolve connection secret'}), 500
    
    
    @app.route('/api/inventory/group-vars/<group_name>', methods=['GET'])
    @require_auth
    def api_get_group_vars(group_name):
        """API: Получить group_vars для группы"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            group_vars_dir = get_project_group_vars_dir(project_id)
            group_file = group_vars_dir / f"{group_name}.yml"
            
            if not group_file.exists():
                return jsonify({
                    'success': True,
                    'vars': {}
                })
            
            try:
                with open(group_file, 'r', encoding='utf-8') as f:
                    vars_data = yaml_loader.load(f) or {}
                
                return jsonify({
                    'success': True,
                    'vars': vars_data
                })
            except Exception as e:
                app.logger.error(f"Error reading group vars: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500
        except Exception as e:
            app.logger.error(f"Error getting group vars: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    
    @app.route('/api/hosts/<host_name>/connection-secret', methods=['PUT'])
    def api_set_host_connection_secret(host_name):
        """API: Установить connection secret для хоста (сохраняет Ansible переменные в host_vars)"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            data = request.json or {}
            secret_name = data.get('secretName')
            port = data.get('port', '22')
            ansible_user = data.get('ansibleUser', 'root')
            
            host_vars_dir = get_project_host_vars_dir(project_id)
            host_file = host_vars_dir / f"{host_name}.yml"
            
            # Загружаем существующие host_vars или создаем новые
            host_vars = {}
            if host_file.exists():
                try:
                    with open(host_file, 'r', encoding='utf-8') as f:
                        host_vars = yaml_loader.load(f) or {}
                except Exception as e:
                    app.logger.warning(f"Error reading host vars for {host_name}: {e}")
            
            # Удаляем старый ключ из secrets/ssh_keys, если он был для другого секрета
            old_connection_secret = host_vars.get('connectionSecret')
            if old_connection_secret and old_connection_secret != secret_name:
                secrets_dir = get_project_secrets_dir(project_id)
                # Новая структура: secrets/ssh_keys/<key_id>.json
                old_key_file_in_secrets = secrets_dir / 'ssh_keys' / f"{old_connection_secret}.json"
                # Не удаляем, так как ключ может использоваться другими хостами
                # Просто обновим путь в host_vars
            
            # Удаляем старые connection-related переменные перед установкой новых
            host_vars.pop('ansible_host', None)
            host_vars.pop('ansible_user', None)
            host_vars.pop('ansible_port', None)
            host_vars.pop('ansible_ssh_private_key_file', None)
            host_vars.pop('ansible_password', None)
            
            # Если secret_name указан, загружаем секрет и сохраняем Ansible переменные
            if secret_name:
                try:
                    # Используем функцию get_secret_file_path для правильного пути к секрету
                    # Сначала пробуем загрузить секрет, чтобы определить его тип
                    secrets_dir = get_project_secrets_dir(project_id)
                    ssh_keys_dir = secrets_dir / 'ssh_keys'
                    
                    # Пробуем найти секрет в новой структуре (secrets/ssh_keys/)
                    secret_file = ssh_keys_dir / f"{secret_name}.json"
                    if not secret_file.exists():
                        # Fallback на старую структуру для обратной совместимости
                        secret_file = secrets_dir / f"{secret_name}.json"
                    
                    if not secret_file.exists():
                        return jsonify({'success': False, 'error': f'Secret {secret_name} not found'}), 404
                    
                    with open(secret_file, 'r', encoding='utf-8') as f:
                        secret_data = json.load(f)
                    
                    secret_type = secret_data.get('type')
                    
                    # Берем username из секрета, если он указан, иначе используем из параметра запроса
                    secret_username = secret_data.get('username', '').strip()
                    if secret_username:
                        ansible_user = secret_username
                        app.logger.debug(f"[api_set_host_connection_secret] Using username from secret {secret_name}: {secret_username}")
                    else:
                        app.logger.debug(f"[api_set_host_connection_secret] Username not found in secret, using from request: {ansible_user}")
                    
                    # Устанавливаем базовые переменные
                    host_vars['ansible_host'] = host_name
                    host_vars['ansible_user'] = ansible_user
                    host_vars['ansible_port'] = int(port) if port else 22
                    
                    if secret_type == 'ssh_key':
                        # Для SSH ключа сохраняем ссылку на секрет и путь к ключу в secrets/ssh_keys
                        secrets_dir = get_project_secrets_dir(project_id)
                        project_dir = get_project_dir(project_id)
                        ssh_keys_dir = secrets_dir / 'ssh_keys'
                        ssh_keys_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Новая структура: secrets/ssh_keys/<key_id>.json (JSON файл с секретом)
                        # Используем путь к JSON файлу секрета
                        key_file = ssh_keys_dir / f"{secret_name}.json"
                        os.chmod(key_file, 0o600)
                        
                        # Сохраняем ссылку на секрет и путь к ключу
                        host_vars['connectionSecret'] = secret_name  # Сохраняем имя секрета для определения при загрузке
                        host_vars['ansible_ssh_private_key_file'] = str(key_file)
                        app.logger.info(f"SSH key saved/updated for host {host_name} at {key_file}, referencing secret {secret_name}")
                        
                    elif secret_type == 'login_password':
                        # Для пароля сохраняем ссылку на секрет и пароль
                        password = secret_data.get('password', '')
                        if not password:
                            return jsonify({'success': False, 'error': 'Password is empty in secret'}), 400
                        
                        # Сохраняем ссылку на секрет для определения при загрузке
                        host_vars['connectionSecret'] = secret_name
                        host_vars['ansible_password'] = password
                        app.logger.info(f"Password saved for host {host_name}, referencing secret {secret_name}")
                    else:
                        return jsonify({'success': False, 'error': f'Unsupported secret type: {secret_type}'}), 400
                        
                except Exception as e:
                    safe_error_msg = safe_log_error("Error loading secret", e)
                    app.logger.error(safe_error_msg)
                    return jsonify({'success': False, 'error': 'Failed to load secret'}), 500
            else:
                # Если secret_name не указан, удаляем connectionSecret и все connection-related переменные
                host_vars.pop('connectionSecret', None)
                # Переменные уже удалены выше
            
            # Сохраняем host_vars
            host_vars_dir.mkdir(parents=True, exist_ok=True)
            with open(host_file, 'w', encoding='utf-8') as f:
                yaml_loader.dump(host_vars, f)
            
            app.logger.info(f"Connection settings saved for host {host_name} in project {project_id}")
            
            return jsonify({
                'success': True,
                'message': 'Connection settings saved successfully'
            })
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error setting host connection secret", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to set connection secret'}), 500
    
    
    @app.route('/api/groups/<group_name>/connection-secret', methods=['PUT'])
    def api_set_group_connection_secret(group_name):
        """API: Установить connection secret для группы (сохраняет в group_vars)"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            data = request.json or {}
            secret_name = data.get('secretName')
            
            group_vars_dir = get_project_group_vars_dir(project_id)
            group_file = group_vars_dir / f"{group_name}.yml"
            
            # Загружаем существующие group_vars или создаем новые
            group_vars = {}
            if group_file.exists():
                try:
                    with open(group_file, 'r', encoding='utf-8') as f:
                        group_vars = yaml_loader.load(f) or {}
                except Exception as e:
                    app.logger.warning(f"Error reading group vars for {group_name}: {e}")
            
            # Обновляем connectionSecret
            if secret_name:
                group_vars['connectionSecret'] = secret_name
            else:
                # Удаляем connectionSecret если secret_name is None
                group_vars.pop('connectionSecret', None)
            
            # Сохраняем group_vars
            group_vars_dir.mkdir(parents=True, exist_ok=True)
            with open(group_file, 'w', encoding='utf-8') as f:
                yaml_loader.dump(group_vars, f)
            
            app.logger.info(f"Connection secret set for group {group_name} in project {project_id}: {secret_name or 'removed'}")
            
            return jsonify({
                'success': True,
                'message': 'Connection secret saved successfully'
            })
        except Exception as e:
            # Безопасное логирование без чувствительных данных
            safe_error_msg = safe_log_error("Error setting group connection secret", e)
            app.logger.error(safe_error_msg)
            return jsonify({'success': False, 'error': 'Failed to set connection secret'}), 500
    
    
    @app.route('/api/inventory/groups', methods=['GET'])
    @require_auth
    def api_get_inventory_groups():
        """API: Получить структуру групп из inventory
        
        Поддерживает выбранные inventory файлы через параметр inventory_files
        """
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            # Получаем выбранные inventory файлы из запроса
            selected_files = request.args.getlist('inventory_files')
            project_dir = get_project_dir(project_id)
            project_inventory_file = get_project_inventory_file(project_id)
            
            inventory_files_to_use = []
            
            if selected_files:
                # Если указаны файлы, используем их из директории проекта
                # Получаем inventories_dir для поддержки кастомных путей через repoLayout
                inventories_dir = get_project_inventories_dir(project_id)
                layout = get_repo_layout(project_id)
                inventories_layout_path = layout.get('inventories', 'inventories')
                
                for file_path_param in selected_files:
                    file_path = None
                    # Путь может начинаться с кастомного пути inventories (например, ansible/inventories/)
                    if file_path_param.startswith(f'{inventories_layout_path}/'):
                        # Путь вида inventories/prod/hosts.yml или ansible/inventories/prod/hosts.yml
                        # Вычисляем относительный путь от inventories_dir
                        rel_path = file_path_param[len(inventories_layout_path) + 1:]  # Убираем префикс
                        file_path = inventories_dir / rel_path
                    elif file_path_param.startswith('inventories/'):
                        # Обратная совместимость: старые пути с хардкодом inventories/
                        file_path = project_dir / 'repo' / file_path_param
                    elif file_path_param in ('inventory.yml', 'inventory.yaml', 'hosts.yml', 'hosts.yaml', 'hosts', 'hosts.ini'):
                        # Сначала в папке inventories (как в api_get_inventory_hosts), затем в корне repo
                        file_path = inventories_dir / file_path_param
                        if not file_path.exists():
                            file_path = project_dir / 'repo' / file_path_param
                    elif '/' in file_path_param and not file_path_param.startswith('inventory/'):
                        # Путь вида prod/hosts.yml - добавляем inventories_dir
                        file_path = inventories_dir / file_path_param
                    else:
                        # Если просто имя файла, ищем в inventories_dir
                        file_path = inventories_dir / file_path_param
                    
                    if file_path and file_path.exists():
                        inventory_files_to_use.append(str(file_path))
            else:
                # Файлы не указаны — собираем все inventory из папки inventories (как в api_get_inventory_hosts)
                inventories_dir = get_project_inventories_dir(project_id)
                inventory_names = ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts', 'hosts.ini']
                if inventories_dir.exists():
                    for name in inventory_names:
                        p = inventories_dir / name
                        if p.exists():
                            inventory_files_to_use.append(str(p))
                    for inv_file in inventories_dir.rglob('*'):
                        if inv_file.is_file() and inv_file.name in inventory_names:
                            rel = inv_file.relative_to(inventories_dir)
                            if 'group_vars' not in rel.parts and 'host_vars' not in rel.parts:
                                path_str = str(inv_file)
                                if path_str not in inventory_files_to_use:
                                    inventory_files_to_use.append(path_str)
                if not inventory_files_to_use and project_inventory_file.exists():
                    inventory_files_to_use = [str(project_inventory_file)]
            
            # Используем get_inventory_groups для получения групп из всех файлов
            groups = get_inventory_groups(inventory_files_to_use) if inventory_files_to_use else {}
            
            return jsonify({
                'success': True,
                'groups': groups
            })
        except Exception as e:
            app.logger.error(f"Error getting inventory groups: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/inventory/groups', methods=['POST'])
    @require_auth
    def api_add_inventory_group():
        """API: Добавить новую группу в inventory"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            data = request.json or {}
            group_name = data.get('group_name', '').strip()
            hosts = data.get('hosts', [])
            inventory_file_param = data.get('inventory_file', '')
            
            if not group_name:
                return jsonify({'success': False, 'error': 'Group name is required'}), 400
            
            # Валидация имени группы
            if not group_name.replace('_', '').replace('-', '').isalnum():
                return jsonify({'success': False, 'error': 'Group name can only contain letters, numbers, underscores and hyphens'}), 400
            
            # Определяем путь к inventory файлу
            project_dir = get_project_dir(project_id)
            inventories_dir = get_project_inventories_dir(project_id)
            layout = get_repo_layout(project_id)
            inventories_layout_path = layout.get('inventories', 'inventories')
            
            inventory_file = None
            
            if inventory_file_param:
                # Если указан inventory файл, используем его
                if inventory_file_param.startswith(f'{inventories_layout_path}/'):
                    rel_path = inventory_file_param[len(inventories_layout_path) + 1:]
                    inventory_file = inventories_dir / rel_path
                elif inventory_file_param.startswith('inventories/'):
                    inventory_file = project_dir / 'repo' / inventory_file_param
                elif inventory_file_param == 'inventory.yml':
                    inventory_file = inventories_dir / 'inventory.yml'
                elif '/' in inventory_file_param:
                    if inventory_file_param.startswith('inventories/'):
                        rel_path = inventory_file_param[len('inventories/'):]
                        inventory_file = inventories_dir / rel_path
                    else:
                        inventory_file = inventories_dir / inventory_file_param
                else:
                    inventory_file = inventories_dir / inventory_file_param
            else:
                # Если не указан, используем дефолтный
                inventory_file = get_project_inventory_file(project_id)
            
            inventory_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Загружаем существующий inventory
            inventory_data = {}
            if inventory_file.exists():
                try:
                    with open(inventory_file, 'r', encoding='utf-8') as f:
                        inventory_data = yaml_loader.load(f) or {}
                except Exception as e:
                    app.logger.error(f"Error reading inventory: {e}")
                    return jsonify({'success': False, 'error': f'Error reading inventory: {str(e)}'}), 500
            
            # Инициализируем структуру если нужно
            if 'all' not in inventory_data:
                inventory_data['all'] = {}
            if 'children' not in inventory_data['all']:
                inventory_data['all']['children'] = {}
            
            # Проверяем, не существует ли уже такая группа
            if group_name in inventory_data['all']['children']:
                return jsonify({'success': False, 'error': f'Group {group_name} already exists'}), 400
            
            # Создаем новую группу
            group_data = {}
            if hosts:
                # Если хосты переданы как список IP адресов
                group_data['hosts'] = {}
                for host in hosts:
                    if isinstance(host, str):
                        host_name = host.strip()
                        if host_name:
                            # Создаем vars_file для хоста
                            vars_file = f'host_vars/{host_name}.yml'
                            group_data['hosts'][host_name] = {
                                'vars_file': vars_file
                            }
            
            inventory_data['all']['children'][group_name] = group_data
            
            # Создаем бэкап
            if inventory_file.exists():
                create_backup(inventory_file)
            
            # Сохраняем inventory
            with open(inventory_file, 'w', encoding='utf-8') as f:
                yaml_loader.dump(inventory_data, f)
            
            app.logger.info(f"Group {group_name} added to inventory in project {project_id}")
            return jsonify({
                'success': True,
                'message': f'Group {group_name} added successfully'
            })
        except Exception as e:
            app.logger.error(f"Error adding group: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/inventory/groups/<group_name>', methods=['DELETE'])
    @require_auth
    def api_delete_inventory_group(group_name):
        """API: Удалить группу из inventory"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            inventory_file = get_project_inventory_file(project_id)
            
            if not inventory_file.exists():
                return jsonify({'success': False, 'error': 'Inventory file not found'}), 404
            
            # Загружаем inventory
            try:
                with open(inventory_file, 'r', encoding='utf-8') as f:
                    inventory_data = yaml_loader.load(f) or {}
            except Exception as e:
                app.logger.error(f"Error reading inventory: {e}")
                return jsonify({'success': False, 'error': f'Error reading inventory: {str(e)}'}), 500
            
            # Проверяем существование группы
            if 'all' not in inventory_data or 'children' not in inventory_data['all']:
                return jsonify({'success': False, 'error': 'Group not found'}), 404
            
            if group_name not in inventory_data['all']['children']:
                return jsonify({'success': False, 'error': 'Group not found'}), 404
            
            # Удаляем группу
            del inventory_data['all']['children'][group_name]
            
            # Также удаляем группу из других групп, если она там указана как child
            for other_group_name, other_group_data in inventory_data['all']['children'].items():
                if isinstance(other_group_data, dict) and 'children' in other_group_data:
                    if isinstance(other_group_data['children'], dict) and group_name in other_group_data['children']:
                        del other_group_data['children'][group_name]
                    elif isinstance(other_group_data['children'], list) and group_name in other_group_data['children']:
                        other_group_data['children'].remove(group_name)
            
            # Создаем бэкап
            create_backup(inventory_file)
            
            # Сохраняем inventory
            with open(inventory_file, 'w', encoding='utf-8') as f:
                yaml_loader.dump(inventory_data, f)
            
            app.logger.info(f"Group {group_name} deleted from inventory in project {project_id}")
            return jsonify({
                'success': True,
                'message': f'Group {group_name} deleted successfully'
            })
        except Exception as e:
            app.logger.error(f"Error deleting group: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/inventory/hosts/<host_name>/groups', methods=['PUT'])
    @require_auth
    def api_update_host_groups(host_name):
        """API: Обновить группы для хоста"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            data = request.json or {}
            target_groups = data.get('groups', [])
            
            inventory_file = get_project_inventory_file(project_id)
            
            if not inventory_file.exists():
                return jsonify({'success': False, 'error': 'Inventory file not found'}), 404
            
            # Загружаем inventory
            try:
                with open(inventory_file, 'r', encoding='utf-8') as f:
                    inventory_data = yaml_loader.load(f) or {}
            except Exception as e:
                app.logger.error(f"Error reading inventory: {e}")
                return jsonify({'success': False, 'error': f'Error reading inventory: {str(e)}'}), 500
            
            # Инициализируем структуру если нужно
            if 'all' not in inventory_data:
                inventory_data['all'] = {}
            if 'children' not in inventory_data['all']:
                inventory_data['all']['children'] = {}
            
            # Удаляем хост из всех существующих групп
            for group_name, group_data in inventory_data['all']['children'].items():
                if isinstance(group_data, dict) and 'hosts' in group_data:
                    hosts = group_data['hosts']
                    if isinstance(hosts, dict):
                        # Если хосты это словарь, удаляем хост
                        if host_name in hosts:
                            del hosts[host_name]
                            # Если словарь пустой, можно удалить секцию hosts
                            if not hosts:
                                del group_data['hosts']
                    elif isinstance(hosts, list):
                        # Если хосты это список, удаляем хост
                        if host_name in hosts:
                            hosts.remove(host_name)
            
            # Добавляем хост в выбранные группы
            for group_name in target_groups:
                if group_name not in inventory_data['all']['children']:
                    # Создаем новую группу если её нет
                    inventory_data['all']['children'][group_name] = {}
                
                group_data = inventory_data['all']['children'][group_name]
                if not isinstance(group_data, dict):
                    group_data = {}
                    inventory_data['all']['children'][group_name] = group_data
                
                # Добавляем хост в группу
                if 'hosts' not in group_data:
                    group_data['hosts'] = {}
                
                hosts = group_data['hosts']
                if isinstance(hosts, dict):
                    # Если хосты это словарь, добавляем хост с vars_file
                    if host_name not in hosts:
                        hosts[host_name] = {
                            'vars_file': f'host_vars/{host_name}.yml'
                        }
                elif isinstance(hosts, list):
                    # Если хосты это список, добавляем хост
                    if host_name not in hosts:
                        hosts.append(host_name)
                else:
                    # Если hosts не словарь и не список, создаем словарь
                    group_data['hosts'] = {
                        host_name: {
                            'vars_file': f'host_vars/{host_name}.yml'
                        }
                    }
            
            # Создаем бэкап
            create_backup(inventory_file)
            
            # Сохраняем inventory
            with open(inventory_file, 'w', encoding='utf-8') as f:
                yaml_loader.dump(inventory_data, f)
            
            app.logger.info(f"Host {host_name} groups updated in project {project_id}: {target_groups}")
            return jsonify({
                'success': True,
                'message': f'Host {host_name} groups updated successfully'
            })
        except Exception as e:
            app.logger.error(f"Error updating host groups: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/inventory/groups/<group_name>/hosts', methods=['PUT'])
    @require_auth
    def api_update_group_hosts(group_name):
        """API: Обновить хосты в группе"""
        try:
            project_id = get_project_id_from_request()
            if not project_id:
                return jsonify({'success': False, 'error': 'Project ID is required'}), 400
            
            data = request.json or {}
            hosts = data.get('hosts', [])
            
            inventory_file = get_project_inventory_file(project_id)
            
            if not inventory_file.exists():
                return jsonify({'success': False, 'error': 'Inventory file not found'}), 404
            
            # Загружаем inventory
            try:
                with open(inventory_file, 'r', encoding='utf-8') as f:
                    inventory_data = yaml_loader.load(f) or {}
            except Exception as e:
                app.logger.error(f"Error reading inventory: {e}")
                return jsonify({'success': False, 'error': f'Error reading inventory: {str(e)}'}), 500
            
            # Проверяем существование группы
            if 'all' not in inventory_data or 'children' not in inventory_data['all']:
                return jsonify({'success': False, 'error': 'Group not found'}), 404
            
            if group_name not in inventory_data['all']['children']:
                return jsonify({'success': False, 'error': 'Group not found'}), 404
            
            group_data = inventory_data['all']['children'][group_name]
            if not isinstance(group_data, dict):
                group_data = {}
                inventory_data['all']['children'][group_name] = group_data
            
            # Обновляем хосты
            if hosts:
                group_data['hosts'] = {}
                for host in hosts:
                    if isinstance(host, str):
                        host_name = host.strip()
                        if host_name:
                            # Сохраняем существующий vars_file если хост уже был в группе
                            existing_vars_file = None
                            if 'hosts' in group_data and isinstance(group_data['hosts'], dict):
                                existing_host_data = group_data['hosts'].get(host_name)
                                if isinstance(existing_host_data, dict):
                                    existing_vars_file = existing_host_data.get('vars_file')
                            
                            # Используем существующий vars_file или создаем новый
                            vars_file = existing_vars_file or f'host_vars/{host_name}.yml'
                            group_data['hosts'][host_name] = {
                                'vars_file': vars_file
                            }
            else:
                # Если список хостов пустой, удаляем секцию hosts
                if 'hosts' in group_data:
                    del group_data['hosts']
            
            # Создаем бэкап
            create_backup(inventory_file)
            
            # Сохраняем inventory
            with open(inventory_file, 'w', encoding='utf-8') as f:
                yaml_loader.dump(inventory_data, f)
            
            app.logger.info(f"Hosts updated for group {group_name} in project {project_id}")
            return jsonify({
                'success': True,
                'message': f'Hosts updated for group {group_name}'
            })
        except Exception as e:
            app.logger.error(f"Error updating group hosts: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    # ============================================================================
    # END CONNECTION SECRETS API
    # ============================================================================
    
    # ============================================================================
    # END SECRETS API
    # ============================================================================
    
    # Инициализация проектов (создаёт директории для существующих проектов)
    # НЕ создаёт проекты автоматически
    _initialize_projects()
    
    # Инициализация аутентификации: создание базовых ролей, прав и пользователя admin
    try:
        # Сначала создаем роли и права
        seed_default_roles(role_service, permission_service, DATA_DIR)
        # Затем создаем пользователя admin (если пользователей еще нет)
        seed_default_user(user_service, role_service, DATA_DIR)
        app.logger.info("Authentication system initialized successfully")
    except Exception as e:
        app.logger.error(f"Error initializing authentication system: {e}", exc_info=True)
    
    # Применяем retention policy при старте
    apply_retention_policy()
    
    print(f"Starting web service...")
    print(f"BASE_DIR: {BASE_DIR}")
    print(f"PROJECTS: {PROJECTS_DIR}")
    print(f"Note: All data (executions, drafts, vars, configs) are now project-scoped in Project Storage")
    
    # Загружаем настройки debug режима из файла или переменной окружения
    settings = load_execution_settings()
    debug_mode = os.getenv('FLASK_DEBUG', str(settings.get('debug_mode', False))).lower()
    if debug_mode == 'true' or debug_mode == '1':
        debug_mode = True
    else:
        debug_mode = False
    
    # Устанавливаем начальный уровень логирования из настроек
    initial_log_level = os.getenv('FLASK_LOG_LEVEL', settings.get('log_level', 'INFO'))
    set_log_level(initial_log_level)
    
    # Применяем ротацию логов с учетом max_log_size_mb из настроек
    # (ротация уже настроена при инициализации через get_logging_settings())
    max_log_size_mb = settings.get('max_log_size_mb', 10)
    app.logger.debug(f"Log rotation configured: maxBytes={max_log_size_mb}MB, backupCount=5")
    
    # Устанавливаем максимальный размер загружаемых файлов
    max_upload_size_mb = settings.get('max_upload_size_mb', 10)
    if max_upload_size_mb < 1:
        max_upload_size_mb = 1  # Минимум 1MB
    elif max_upload_size_mb > 1024:
        max_upload_size_mb = 1024  # Максимум 1GB
    app.config['MAX_CONTENT_LENGTH'] = max_upload_size_mb * 1024 * 1024
    
    print(f"Debug mode: {'ENABLED' if debug_mode else 'DISABLED'}")
    print(f"Log level: {initial_log_level.upper()}")
    print(f"Max upload file size: {max_upload_size_mb}MB")
    print(f"Open in browser: http://localhost:5000")
    
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)
