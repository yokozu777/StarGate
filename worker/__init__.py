#!/usr/bin/env python3
"""
Worker package для обработки очереди выполнения playbook runs
Работает через HTTP API (новый режим)
"""
from .loop import worker_loop
from .execute import execute_run
from .http_client import WorkerHTTPClient

# Старые функции оставлены для обратной совместимости и тестов
from .claim import claim_next_run
from .recovery import recover_stuck_runs

__all__ = ['worker_loop', 'execute_run', 'WorkerHTTPClient', 'claim_next_run', 'recover_stuck_runs']
