#!/usr/bin/env python3
"""
Конфигурация worker (пути, env, настройки)
"""
import os
from pathlib import Path

# Путь к корню проекта (на уровень выше web/)
BASE_DIR = Path(__file__).parent.parent.parent

# Базовая директория для всех данных (в Docker задаётся DATA_DIR=/app/data)
DATA_DIR = Path(os.environ.get('DATA_DIR', str(BASE_DIR / 'data')))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Директории для хранения данных
PROJECTS_DIR = DATA_DIR / 'projects'  # Проекты хранятся в data/projects/
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

TEMP_DIR = DATA_DIR / 'temp'
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Директория для логов
LOG_DIR = DATA_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'worker.log'


def get_project_dir(project_id):
    """Возвращает путь к директории проекта"""
    return PROJECTS_DIR / project_id


def get_project_secrets_dir(project_id):
    """Возвращает путь к директории secrets проекта (secrets/)"""
    return get_project_dir(project_id) / 'secrets'


def get_project_vault_dir(project_id):
    """Возвращает путь к директории vault проекта (secrets/vault/)"""
    return get_project_secrets_dir(project_id) / 'vault'


def get_project_vault_keys_dir(project_id):
    """Возвращает путь к директории vault_keys (secrets/vault_keys/)"""
    return get_project_secrets_dir(project_id) / 'vault_keys'


def get_project_vaults_file(project_id):
    """Возвращает путь к файлу vaults (secrets/vault/vaults.json)"""
    return get_project_vault_dir(project_id) / 'vaults.json'


def get_project_executions_dir(project_id):
    """Возвращает путь к директории executions проекта (новая структура: history/executions/)"""
    project_dir = get_project_dir(project_id)
    # Новая структура: history/executions/
    new_path = project_dir / 'history' / 'executions'
    # Обратная совместимость: если новая структура не существует, используем старую
    if new_path.exists() or (project_dir / 'history').exists():
        return new_path
    # Fallback на старую структуру для обратной совместимости
    return project_dir / 'executions'


def get_project_inventory_file(project_id):
    """Возвращает путь к inventory файлу проекта (worker).
    
    Ищем только в папке inventories: inventory.yaml, inventory.yml, hosts.yaml, hosts.yml, hosts (без расширения).
    Без fallback.
    """
    project_dir = get_project_dir(project_id)
    inventories_dir = project_dir / 'repo' / 'inventories'
    inventory_names = ['inventory.yaml', 'inventory.yml', 'hosts.yaml', 'hosts.yml', 'hosts']
    for name in inventory_names:
        p = inventories_dir / name
        if p.exists():
            return p
    if inventories_dir.exists():
        for inv_file in inventories_dir.rglob('*'):
            if inv_file.is_file() and inv_file.name in inventory_names:
                rel = inv_file.relative_to(inventories_dir)
                if 'group_vars' not in rel.parts and 'host_vars' not in rel.parts:
                    return inv_file
    return inventories_dir / 'inventory.yml'
