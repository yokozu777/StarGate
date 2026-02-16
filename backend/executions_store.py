#!/usr/bin/env python3
"""
Модуль для работы с executions (хранилище записей выполнения playbook runs)
Вынесен из app.py для использования worker'ом без зависимости от Flask app

State Machine для Execution:
- QUEUED → RUNNING, CANCELED
- RUNNING → SUCCESS, FAILED, CANCELING
- CANCELING → CANCELED, FAILED
- CANCELED, SUCCESS, FAILED - финальные статусы (нельзя изменить)
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any

# Импортируем константы - сначала пробуем из worker.config, потом из app
try:
    from worker.config import PROJECTS_DIR
except ImportError:
    try:
        from .worker.config import PROJECTS_DIR
    except ImportError:
        try:
            from .app import PROJECTS_DIR
        except ImportError:
            # Fallback: определяем сами (в Docker backend в /app → используем /app/data)
            import os
            _env_data = os.environ.get('DATA_DIR')
            if _env_data:
                PROJECTS_DIR = Path(_env_data) / 'projects'
            else:
                _mod_dir = Path(__file__).resolve().parent
                if str(_mod_dir) == '/app':
                    _base = _mod_dir  # Docker: код в /app
                elif _mod_dir.name == 'backend':
                    _base = _mod_dir.parent
                else:
                    _base = _mod_dir.parent.parent
                PROJECTS_DIR = _base / 'data' / 'projects'

logger = logging.getLogger(__name__)

# Execution State Machine
# Финальные статусы (нельзя изменить)
FINAL_STATUSES = {'CANCELED', 'SUCCESS', 'FAILED'}

# Допустимые переходы статусов
ALLOWED_TRANSITIONS = {
    'QUEUED': {'RUNNING', 'CANCELED'},
    'RUNNING': {'SUCCESS', 'FAILED', 'CANCELING'},
    'CANCELING': {'CANCELED', 'FAILED'},
    'CANCELED': set(),  # Финальный статус
    'SUCCESS': set(),   # Финальный статус
    'FAILED': set()     # Финальный статус
}


def validate_status_transition(from_status: str, to_status: str) -> None:
    """
    Валидирует переход статуса execution.
    
    Args:
        from_status: Текущий статус
        to_status: Новый статус
    
    Raises:
        ValueError: Если переход запрещён
    
    Examples:
        >>> validate_status_transition('QUEUED', 'RUNNING')  # OK
        >>> validate_status_transition('SUCCESS', 'RUNNING')  # ValueError
        >>> validate_status_transition('RUNNING', 'CANCELED')  # ValueError (нужно через CANCELING)
    """
    from_status = from_status.upper() if from_status else None
    to_status = to_status.upper() if to_status else None
    
    if not from_status or not to_status:
        raise ValueError(f"Status cannot be None or empty (from: {from_status}, to: {to_status})")
    
    # Если статусы одинаковые - разрешаем (idempотентность)
    if from_status == to_status:
        return
    
    # Проверяем, что переход разрешён
    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        # Формируем понятное сообщение об ошибке
        if from_status in FINAL_STATUSES:
            raise ValueError(
                f"Cannot change status from final status '{from_status}' to '{to_status}'. "
                f"Final statuses ({', '.join(FINAL_STATUSES)}) cannot be changed."
            )
        elif to_status == 'CANCELED' and from_status == 'RUNNING':
            raise ValueError(
                f"Cannot transition from '{from_status}' to '{to_status}' directly. "
                f"Must go through CANCELING first: {from_status} → CANCELING → {to_status}"
            )
        else:
            allowed_str = ', '.join(sorted(allowed)) if allowed else 'none (final status)'
            raise ValueError(
                f"Invalid status transition: '{from_status}' → '{to_status}'. "
                f"Allowed transitions from '{from_status}': {allowed_str}"
            )


def get_project_dir(project_id):
    """Возвращает путь к директории проекта"""
    return PROJECTS_DIR / project_id


def get_project_executions_dir(project_id):
    """Возвращает путь к директории executions проекта (новая структура: history/executions/)"""
    project_dir = get_project_dir(project_id)
    # Новая структура: history/executions/
    return project_dir / 'history' / 'executions'


def update_execution_record(execution_id, updates, project_id=None):
    """Обновляет запись execution в Project Storage с валидацией переходов статусов
    
    REQUIRED: project_id must be provided (no fallback to legacy EXECUTIONS_DIR)
    
    Автоматически:
    - Валидирует переходы статусов через validate_status_transition()
    - Вычисляет duration если устанавливается finishedAt и есть startedAt
    - Устанавливает statusUpdatedAt при изменении статуса
    - Устанавливает временные метки для cancel операций
    
    Args:
        execution_id: ID execution
        updates: Словарь с обновлениями (может содержать 'status')
        project_id: ID проекта (опционально, будет найден если не указан)
    
    Returns:
        bool: True если успешно обновлено
    
    Raises:
        ValueError: Если переход статуса запрещён или project_id не найден
    """
    # Получаем projectId из updates или параметра - REQUIRED
    if not project_id:
        project_id = updates.get('project_id')
    if not project_id:
        # Try to find execution in Project Storage to get projectId
        # Новая структура: history/executions/
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            # Новая структура: history/executions/
            executions_dir = proj_dir / 'history' / 'executions'
            if not executions_dir.exists():
                continue
            exec_file = executions_dir / f'{execution_id}.json'
            if exec_file.exists():
                try:
                    with open(exec_file, 'r', encoding='utf-8') as f:
                        exec_data = json.load(f)
                        project_id = exec_data.get('projectId')
                        break
                except:
                    pass
    if not project_id:
        logger.error(f"[update_execution_record] project_id is required for execution {execution_id}")
        raise ValueError(f"project_id is required for update_execution_record (execution_id: {execution_id})")
    
    # ALWAYS use Project Storage - no fallback
    executions_dir = get_project_executions_dir(project_id)
    
    execution_file = executions_dir / f'{execution_id}.json'
    try:
        if execution_file.exists():
            with open(execution_file, 'r', encoding='utf-8') as f:
                execution = json.load(f)
            
            # Валидация перехода статуса
            old_status = execution.get('status', 'QUEUED')
            new_status = updates.get('status')
            
            if new_status and new_status != old_status:
                validate_status_transition(old_status, new_status)
            
            # Автоматически устанавливаем временные метки при переходах
            now = time.time()
            
            if new_status and new_status != old_status:
                # Обновляем statusUpdatedAt при любом изменении статуса
                updates['statusUpdatedAt'] = now
                
                # Устанавливаем специфичные поля для переходов
                if new_status == 'RUNNING':
                    if 'startedAt' not in updates:
                        updates['startedAt'] = now
                    # Убеждаемся что queuedAt установлен (если был QUEUED)
                    if old_status == 'QUEUED' and 'queuedAt' not in execution:
                        updates['queuedAt'] = execution.get('createdAt', now)
                
                elif new_status == 'CANCELING':
                    if 'cancelRequestedAt' not in updates:
                        updates['cancelRequestedAt'] = now
                
                elif new_status == 'CANCELED':
                    if 'canceledAt' not in updates:
                        updates['canceledAt'] = now
                    if 'cancelReason' not in updates:
                        # Определяем причину отмены
                        if old_status == 'QUEUED':
                            updates['cancelReason'] = 'user'  # Отменено до начала выполнения
                        elif old_status == 'CANCELING':
                            updates['cancelReason'] = 'user'  # Отменено пользователем
                        else:
                            updates['cancelReason'] = 'admin'  # Fallback
                
                elif new_status in ('SUCCESS', 'FAILED'):
                    if 'finishedAt' not in updates:
                        updates['finishedAt'] = now
            
            # Автоматически вычисляем duration если устанавливается finishedAt
            if 'finishedAt' in updates and 'duration' not in updates:
                started_at = execution.get('startedAt') or updates.get('startedAt') or execution.get('createdAt')
                finished_at = updates.get('finishedAt')
                if started_at and finished_at:
                    duration = int(finished_at - started_at)
                    updates['duration'] = duration
            
            # Применяем обновления
            execution.update(updates)
            
            # Сохраняем
            with open(execution_file, 'w', encoding='utf-8') as f:
                json.dump(execution, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"Updated execution {execution_id}: {old_status} → {new_status or old_status}")
            return True
        else:
            logger.warning(f"Execution file not found: {execution_file}")
            return False
    except ValueError as e:
        # Пробрасываем ValueError (валидация переходов) наверх
        logger.error(f"Invalid status transition for execution {execution_id}: {e}")
        raise
    except Exception as e:
        logger.error(f"Error updating execution record {execution_id}: {e}", exc_info=True)
        return False


def append_execution_log(execution_id, text, project_id=None):
    """Добавляет лог в execution в Project Storage
    
    REQUIRED: project_id must be provided (no fallback to legacy EXECUTION_LOGS_DIR)
    """
    if not project_id:
        logger.error(f"[append_execution_log] project_id is required for execution {execution_id}")
        raise ValueError(f"project_id is required for append_execution_log (execution_id: {execution_id})")
    
    # ALWAYS use Project Storage - no fallback
    # Новая структура: history/logs/
    project_dir = PROJECTS_DIR / project_id
    logs_dir = project_dir / 'history' / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = logs_dir / f'{execution_id}.log'
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(text)
            if not text.endswith('\n'):
                f.write('\n')
        return True
    except Exception as e:
        logger.error(f"Error writing log for execution {execution_id}: {e}")
    return False


def read_log_chunk(execution_id, offset=0, limit=1024*1024, project_id=None):
    """Читает лог файл с указанного offset (incremental fetch)
    
    Args:
        execution_id: ID execution
        offset: Байтовый offset для начала чтения (0 = начало файла)
        limit: Максимальное количество байт для чтения (по умолчанию 1MB)
        project_id: ID проекта (обязательно)
    
    Returns:
        tuple: (text: str, next_offset: int, file_size: int, is_complete: bool)
            - text: прочитанный текст (может быть пустым если offset >= file_size)
            - next_offset: следующий offset для чтения (offset + len(text))
            - file_size: текущий размер файла в байтах
            - is_complete: True если файл больше не будет изменяться (execution завершен)
    """
    if not project_id:
        logger.error(f"[read_log_chunk] project_id is required for execution {execution_id}")
        raise ValueError(f"project_id is required for read_log_chunk (execution_id: {execution_id})")
    
    # Новая структура: history/logs/
    project_dir = PROJECTS_DIR / project_id
    logs_dir = project_dir / 'history' / 'logs'
    log_file = logs_dir / f'{execution_id}.log'
    
    try:
        if not log_file.exists():
            return ('', 0, 0, False)
        
        # Получаем размер файла
        file_size = log_file.stat().st_size
        
        # Если offset >= file_size, файл еще не содержит данных после этого offset
        if offset >= file_size:
            # Проверяем статус execution для определения is_complete
            execution = get_execution(execution_id, project_id=project_id)
            # FINAL_STATUSES определены в начале модуля
            is_complete = execution and execution.get('status') in FINAL_STATUSES if execution else False
            return ('', offset, file_size, is_complete)
        
        # Читаем chunk с offset
        with open(log_file, 'rb') as f:
            f.seek(offset)
            chunk = f.read(limit)
            # Декодируем как UTF-8, игнорируя ошибки для безопасности
            text = chunk.decode('utf-8', errors='ignore')
        
        next_offset = offset + len(chunk)
        
        # Проверяем статус execution для определения is_complete
        execution = get_execution(execution_id, project_id=project_id)
        # FINAL_STATUSES определены в начале модуля
        is_complete = execution and execution.get('status') in FINAL_STATUSES if execution else False
        
        return (text, next_offset, file_size, is_complete)
        
    except Exception as e:
        logger.error(f"Error reading log chunk for execution {execution_id}: {e}")
        return ('', offset, 0, False)


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
                logger.error(f"Error getting execution {execution_id}: {e}")
    else:
        # Ищем во всех проектах (новая структура: history/executions/)
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            # Новая структура: history/executions/
            executions_dir = proj_dir / 'history' / 'executions'
            if not executions_dir.exists():
                continue
            execution_file = executions_dir / f'{execution_id}.json'
            if execution_file.exists():
                try:
                    with open(execution_file, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"Error getting execution {execution_id}: {e}")
    return None


def is_final_status(status: str) -> bool:
    """
    Проверяет, является ли статус финальным (нельзя изменить).
    
    Args:
        status: Статус для проверки
    
    Returns:
        bool: True если статус финальный
    
    Examples:
        >>> is_final_status('SUCCESS')  # True
        >>> is_final_status('RUNNING')  # False
    """
    return status.upper() in FINAL_STATUSES if status else False


def can_transition(from_status: str, to_status: str) -> bool:
    """
    Проверяет, возможен ли переход между статусами (без выброса исключения).
    
    Args:
        from_status: Текущий статус
        to_status: Новый статус
    
    Returns:
        bool: True если переход разрешён
    
    Examples:
        >>> can_transition('QUEUED', 'RUNNING')  # True
        >>> can_transition('SUCCESS', 'RUNNING')  # False
    """
    try:
        validate_status_transition(from_status, to_status)
        return True
    except ValueError:
        return False


def get_status_transitions(from_status: str) -> set:
    """
    Возвращает множество допустимых переходов из указанного статуса.
    
    Args:
        from_status: Статус для проверки
    
    Returns:
        set: Множество допустимых статусов для перехода
    
    Examples:
        >>> get_status_transitions('RUNNING')  # {'SUCCESS', 'FAILED', 'CANCELING'}
        >>> get_status_transitions('SUCCESS')  # set() (финальный статус)
    """
    from_status = from_status.upper() if from_status else None
    return ALLOWED_TRANSITIONS.get(from_status, set()).copy() if from_status else set()
