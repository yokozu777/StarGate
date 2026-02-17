#!/usr/bin/env python3
"""
Реестр workers и управление токенами с хешированием
"""
import json
import uuid
import time
import logging
import hashlib
import secrets
from pathlib import Path
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# Определяем DATA_DIR без импорта app (избегаем циклического импорта)
def _get_data_dir():
    """Возвращает DATA_DIR без импорта app"""
    try:
        from .app import DATA_DIR
        return DATA_DIR
    except ImportError:
        # Fallback если app не доступен
        BASE_DIR = Path(__file__).parent.parent.parent
        return BASE_DIR / 'data'

DATA_DIR = _get_data_dir()

# Директория для хранения workers (по одному файлу на worker)
WORKERS_DIR = DATA_DIR / 'workers'
WORKERS_DIR.mkdir(parents=True, exist_ok=True)


def _hash_token(token: str, salt: str) -> str:
    """Хеширует токен с использованием salt"""
    return hashlib.sha256((token + salt).encode('utf-8')).hexdigest()


def _generate_token() -> str:
    """Генерирует криптографически стойкий токен"""
    return secrets.token_urlsafe(32)


def _generate_salt() -> str:
    """Генерирует salt для хеширования"""
    return secrets.token_urlsafe(16)


def load_worker(worker_id: str) -> Optional[Dict]:
    """Загружает данные worker по ID"""
    worker_file = WORKERS_DIR / f'{worker_id}.json'
    if not worker_file.exists():
        return None
    
    try:
        with open(worker_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading worker {worker_id}: {e}")
        return None


def save_worker(worker_data: Dict) -> bool:
    """Сохраняет данные worker"""
    worker_id = worker_data.get('id')
    if not worker_id:
        return False
    
    worker_file = WORKERS_DIR / f'{worker_id}.json'
    try:
        with open(worker_file, 'w', encoding='utf-8') as f:
            json.dump(worker_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving worker {worker_id}: {e}")
        return False


def load_all_workers() -> Dict[str, Dict]:
    """Загружает всех workers"""
    workers = {}
    for worker_file in WORKERS_DIR.glob('*.json'):
        try:
            with open(worker_file, 'r', encoding='utf-8') as f:
                worker_data = json.load(f)
                worker_id = worker_data.get('id')
                if worker_id:
                    workers[worker_id] = worker_data
        except Exception as e:
            logger.warning(f"Error loading worker file {worker_file}: {e}")
    return workers


def create_worker(name: str, capabilities: Optional[Dict] = None, tags: Optional[list] = None) -> Tuple[str, str]:
    """
    Создает нового worker и возвращает (worker_id, plaintext_token)
    Токен возвращается только один раз при создании!
    """
    worker_id = str(uuid.uuid4())
    plaintext_token = _generate_token()
    salt = _generate_salt()
    token_hash = _hash_token(plaintext_token, salt)
    
    worker_data = {
        'id': worker_id,
        'name': name,
        'tokenHash': token_hash,
        'tokenSalt': salt,
        'capabilities': capabilities or {},
        'tags': tags or [],
        'enabled': True,
        'createdAt': time.time(),
        'lastSeenAt': None,
        'currentExecutionId': None
    }
    
    if save_worker(worker_data):
        logger.info(f"Created worker {worker_id} ({name})")
        return worker_id, plaintext_token
    else:
        raise Exception("Failed to save worker")


def verify_token(token: str) -> Optional[Tuple[str, Dict]]:
    """
    Проверяет токен и возвращает (worker_id, worker_data) если валиден
    Returns None если токен невалиден или worker disabled
    """
    workers = load_all_workers()
    
    for worker_id, worker_data in workers.items():
        # Пропускаем disabled workers
        if not worker_data.get('enabled', True):
            continue
        
        token_hash = worker_data.get('tokenHash')
        token_salt = worker_data.get('tokenSalt')
        
        if not token_hash or not token_salt:
            continue
        
        # Проверяем хеш
        computed_hash = _hash_token(token, token_salt)
        if computed_hash == token_hash:
            return worker_id, worker_data
    
    return None


def rotate_worker_token(worker_id: str) -> str:
    """
    Генерирует новый токен для worker и возвращает plaintext_token
    Токен возвращается только один раз!
    """
    worker_data = load_worker(worker_id)
    if not worker_data:
        raise ValueError(f"Worker {worker_id} not found")
    
    plaintext_token = _generate_token()
    salt = _generate_salt()
    token_hash = _hash_token(plaintext_token, salt)
    
    worker_data['tokenHash'] = token_hash
    worker_data['tokenSalt'] = salt
    worker_data['lastTokenRotatedAt'] = time.time()
    
    if save_worker(worker_data):
        logger.info(f"Rotated token for worker {worker_id}")
        return plaintext_token
    else:
        raise Exception("Failed to save worker")


def update_worker(worker_id: str, name: Optional[str] = None, description: Optional[str] = None, 
                 tags: Optional[list] = None, tagColors: Optional[dict] = None) -> bool:
    """
    Обновляет данные worker (name, description, tags, tagColors)
    
    Args:
        worker_id: ID worker
        name: Новое имя (опционально)
        description: Новое описание (опционально, может быть пустой строкой для удаления)
        tags: Новые теги (опционально)
        tagColors: Словарь цветов тегов {tag_name: color} (опционально)
    
    Returns:
        True если успешно обновлён
    """
    worker_data = load_worker(worker_id)
    if not worker_data:
        return False
    
    if name is not None:
        worker_data['name'] = name.strip()
    
    if description is not None:
        # Пустая строка означает удаление description
        if description.strip():
            worker_data['description'] = description.strip()
        else:
            worker_data.pop('description', None)
    
    if tags is not None:
        worker_data['tags'] = tags
    
    if tagColors is not None:
        if 'tagColors' not in worker_data:
            worker_data['tagColors'] = {}
        worker_data['tagColors'].update(tagColors)
    
    worker_data['updatedAt'] = time.time()
    
    return save_worker(worker_data)


def update_worker_heartbeat(worker_id: str, current_execution_id: Optional[str] = None) -> bool:
    """Обновляет lastSeenAt для worker"""
    worker_data = load_worker(worker_id)
    if not worker_data:
        return False
    
    worker_data['lastSeenAt'] = time.time()
    if current_execution_id is not None:
        worker_data['currentExecutionId'] = current_execution_id
    
    return save_worker(worker_data)


def get_worker_active_runs_count(worker_id: str) -> int:
    """Возвращает количество активных (RUNNING) runs для worker"""
    from executions_store import get_execution
    
    worker_data = load_worker(worker_id)
    if not worker_data:
        return 0
    
    # Считаем RUNNING executions с этим workerId
    count = 0
    try:
        from .app import PROJECTS_DIR
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
                        if (execution.get('status') == 'RUNNING' and 
                            execution.get('workerId') == worker_id):
                            count += 1
                except:
                    pass
    except:
        pass
    
    return count


def is_worker_online(worker_id: str, ttl_seconds: int = 60) -> bool:
    """Проверяет онлайн ли worker (heartbeat TTL)"""
    worker_data = load_worker(worker_id)
    if not worker_data or not worker_data.get('enabled', True):
        return False
    
    last_seen = worker_data.get('lastSeenAt')
    if not last_seen:
        return False
    
    return (time.time() - last_seen) <= ttl_seconds


def enable_worker(worker_id: str) -> bool:
    """Включает worker"""
    worker_data = load_worker(worker_id)
    if not worker_data:
        return False
    
    worker_data['enabled'] = True
    return save_worker(worker_data)


def disable_worker(worker_id: str) -> bool:
    """Отключает worker"""
    worker_data = load_worker(worker_id)
    if not worker_data:
        return False
    
    worker_data['enabled'] = False
    return save_worker(worker_data)


def delete_worker(worker_id: str) -> bool:
    """Удаляет worker"""
    worker_file = WORKERS_DIR / f'{worker_id}.json'
    if worker_file.exists():
        try:
            worker_file.unlink()
            logger.info(f"Deleted worker {worker_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting worker {worker_id}: {e}")
            return False
    return False


def get_stale_workers(max_age_seconds: int = 300) -> list:
    """Возвращает список workers, которые не отправляли heartbeat больше max_age_seconds"""
    workers = load_all_workers()
    now = time.time()
    stale = []
    
    for worker_id, worker_data in workers.items():
        if not worker_data.get('enabled', True):
            continue
        
        last_seen = worker_data.get('lastSeenAt')
        if last_seen and (now - last_seen > max_age_seconds):
            stale.append(worker_id)
    
    return stale


def migrate_legacy_workers():
    """Мигрирует старый формат workers.json в новый формат (по одному файлу на worker)"""
    legacy_file = DATA_DIR / 'workers.json'
    if not legacy_file.exists():
        return
    
    try:
        with open(legacy_file, 'r', encoding='utf-8') as f:
            legacy_workers = json.load(f)
        
        migrated = 0
        for worker_id, worker_data in legacy_workers.items():
            # Если worker уже существует в новом формате, пропускаем
            if load_worker(worker_id):
                continue
            
            # Мигрируем данные
            if 'token' in worker_data:
                # Старый формат с plaintext токеном - генерируем новый
                plaintext_token = worker_data.pop('token')
                salt = _generate_salt()
                token_hash = _hash_token(plaintext_token, salt)
                worker_data['tokenHash'] = token_hash
                worker_data['tokenSalt'] = salt
                logger.warning(f"Migrated worker {worker_id}: old plaintext token replaced with hash")
            
            # Устанавливаем defaults
            worker_data.setdefault('enabled', True)
            worker_data.setdefault('capabilities', {})
            worker_data.setdefault('tags', [])
            
            if save_worker(worker_data):
                migrated += 1
        
        if migrated > 0:
            logger.info(f"Migrated {migrated} workers from legacy format")
            # Переименовываем старый файл для backup
            legacy_file.rename(legacy_file.with_suffix('.json.backup'))
    
    except Exception as e:
        logger.error(f"Error migrating legacy workers: {e}")


def ensure_default_worker():
    """Создает default worker для dev mode если workers storage пуст"""
    workers = load_all_workers()
    if workers:
        return None  # Уже есть workers
    
    try:
        worker_id, plaintext_token = create_worker(
            name='local-worker',
            capabilities={},
            tags=['default', 'local']
        )
        logger.warning("=" * 80)
        logger.warning("DEV MODE: Created default worker 'local-worker'")
        logger.warning(f"Worker ID: {worker_id}")
        logger.warning(f"Worker Token: {plaintext_token}")
        logger.warning("=" * 80)
        logger.warning("IMPORTANT: Save this token! It will not be shown again.")
        logger.warning("Set it in worker/.token file or WORKER_TOKEN environment variable.")
        logger.warning("=" * 80)
        return worker_id, plaintext_token
    except Exception as e:
        logger.error(f"Error creating default worker: {e}")
        return None
