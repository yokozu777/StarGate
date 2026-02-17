#!/usr/bin/env python3
"""
Восстановление застрявших runs (recovery system)
"""
import json
import time
import logging
from pathlib import Path

from .config import PROJECTS_DIR

logger = logging.getLogger(__name__)


def recover_stuck_runs(max_age_minutes=30, max_cancel_minutes=5):
    """
    Восстанавливает застрявшие executions
    
    Логика recovery:
    - RUNNING: если startedAt > MAX_RUN_TIME → FAILED (timeout)
    - CANCELING: если cancelRequestedAt > MAX_CANCEL_TIME → CANCELED (timeout)
    
    Args:
        max_age_minutes: Максимальный возраст RUNNING run в минутах перед timeout
        max_cancel_minutes: Максимальный возраст CANCELING run в минутах перед force cancel
    """
    try:
        # Импортируем функции для валидации и логирования
        import sys
        from pathlib import Path as PathLib
        web_dir = PathLib(__file__).parent.parent
        if str(web_dir) not in sys.path:
            sys.path.insert(0, str(web_dir))
        
        from backend.executions_store import (
            validate_status_transition,
            append_execution_log,
            is_final_status
        )
        
        recovered = 0
        now = time.time()
        
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
                        execution = json.load(f)
                        
                        status = execution.get('status')
                        execution_id = execution.get('id')
                        
                        # Никогда не трогаем финальные статусы
                        if is_final_status(status):
                            logger.debug(f"[recovery] Skipping execution {execution_id} with final status {status}")
                            continue
                        
                        # Обработка RUNNING → FAILED (timeout)
                        if status == 'RUNNING':
                            started_at = execution.get('startedAt')
                            if not started_at:
                                logger.debug(f"[recovery] Execution {execution_id} has no startedAt, skipping")
                                continue
                            
                            # Проверяем возраст
                            age_minutes = (now - started_at) / 60
                            logger.debug(f"[recovery] Checking execution {execution_id}: age={age_minutes:.1f} min, max={max_age_minutes} min")
                            if age_minutes > max_age_minutes:
                                # Валидируем переход
                                try:
                                    validate_status_transition('RUNNING', 'FAILED')
                                except ValueError as e:
                                    logger.warning(f"Invalid transition for recovery: {e}")
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
                                
                                logger.info(f"Recovered stuck RUNNING run {execution_id} → FAILED (age: {age_minutes:.1f} minutes)")
                                recovered += 1
                        
                        # Обработка CANCELING → CANCELED (timeout)
                        elif status == 'CANCELING':
                            cancel_requested_at = execution.get('cancelRequestedAt')
                            if not cancel_requested_at:
                                logger.debug(f"[recovery] Execution {execution_id} has no cancelRequestedAt, skipping")
                                continue
                            
                            # Проверяем возраст CANCELING
                            cancel_age_minutes = (now - cancel_requested_at) / 60
                            logger.debug(f"[recovery] Checking CANCELING execution {execution_id}: age={cancel_age_minutes:.1f} min, max={max_cancel_minutes} min")
                            if cancel_age_minutes > max_cancel_minutes:
                                # Валидируем переход
                                try:
                                    validate_status_transition('CANCELING', 'CANCELED')
                                except ValueError as e:
                                    logger.warning(f"Invalid transition for recovery: {e}")
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
                                
                                logger.info(f"Recovered stuck CANCELING run {execution_id} → CANCELED (timeout: {cancel_age_minutes:.1f} min)")
                                recovered += 1
                except Exception as e:
                    logger.warning(f"Error checking execution {exec_file}: {e}")
        
        if recovered > 0:
            logger.info(f"Recovered {recovered} stuck runs")
        
    except Exception as e:
        logger.error(f"Error in recover_stuck_runs: {e}", exc_info=True)
