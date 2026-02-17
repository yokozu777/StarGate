#!/usr/bin/env python3
"""
Атомарное изъятие задач из очереди (claim mechanism)
"""
import os
import json
import fcntl
import logging
import uuid
import time
from pathlib import Path

from .config import PROJECTS_DIR

logger = logging.getLogger(__name__)


def claim_next_run(project_id=None):
    """
    Атомарно забирает следующий QUEUED run из очереди
    Использует файловую блокировку для предотвращения race condition
    
    Returns:
        tuple: (execution_id, execution_data, project_id) или (None, None, None)
    """
    try:
        # Если project_id указан, ищем только в этом проекте
        if project_id:
            project_dirs = [PROJECTS_DIR / project_id]
        else:
            # Ищем во всех проектах
            project_dirs = [d for d in PROJECTS_DIR.iterdir() if d.is_dir()]
        
        # Собираем все QUEUED executions
        queued_runs = []
        for proj_dir in project_dirs:
            proj_id = proj_dir.name
            # Новая структура: history/executions/
            executions_dir = proj_dir / 'history' / 'executions'
            # Обратная совместимость: если новая структура не существует, используем старую
            if not executions_dir.exists():
                executions_dir = proj_dir / 'executions'
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
                            if status == 'QUEUED':
                                queued_at = execution.get('queuedAt', execution.get('createdAt', 0))
                                queued_runs.append((queued_at, exec_file, execution, proj_id))
                        finally:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception as e:
                    logger.warning(f"Error reading execution file {exec_file}: {e}")
                    continue
        
        if not queued_runs:
            logger.debug(f"No QUEUED runs found in {len(project_dirs)} project(s)")
            return None, None, None
        
        logger.debug(f"Found {len(queued_runs)} QUEUED run(s), attempting to claim...")
        
        # Сортируем по queuedAt (старые первые)
        queued_runs.sort(key=lambda x: x[0])
        
        # Пытаемся claim первый run
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
                        
                        # Обновляем статус на RUNNING
                        execution['status'] = 'RUNNING'
                        execution['startedAt'] = time.time()
                        execution['workerId'] = str(uuid.uuid4())[:8]  # Короткий ID воркера
                        
                        # Записываем обратно
                        f.seek(0)
                        f.truncate()
                        json.dump(execution, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                        
                        logger.info(f"✅ Claimed run {execution_id} for project {proj_id} (queuedAt: {execution.get('queuedAt', 'N/A')})")
                        return execution_id, execution, proj_id
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except IOError:
                # Файл заблокирован, пробуем следующий
                continue
            except Exception as e:
                logger.error(f"Error claiming run {execution_id}: {e}")
                continue
        
        return None, None, None
        
    except Exception as e:
        logger.error(f"Error in claim_next_run: {e}", exc_info=True)
        return None, None, None
