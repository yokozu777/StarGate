#!/usr/bin/env python3
"""
Выполнение ansible-playbook для execution runs (через HTTP API)
"""
import os
import time
import uuid
import subprocess
import logging
import threading
import signal
import queue
import json
import shutil
from pathlib import Path
from typing import Optional, Dict, Any

from .config import TEMP_DIR, get_project_inventory_file, get_project_dir
from .system_info import get_mitogen_strategy_plugins_path
from .vault_utils import get_key_password_for_vault, get_vault_name_for_vault_id

# Импортируем yaml_loader и функции для repoLayout из app.py
try:
    import sys
    from pathlib import Path as PathLib
    web_dir = PathLib(__file__).parent.parent
    if str(web_dir) not in sys.path:
        sys.path.insert(0, str(web_dir))
    from app import yaml_loader, resolve_repo_path
except ImportError:
    # Если app не доступен, создаем свой yaml_loader
    from ruamel.yaml import YAML
    yaml_loader = YAML()
    yaml_loader.preserve_quotes = True
    yaml_loader.width = 4096
    # Если app не доступен, создаем fallback функцию
    def resolve_repo_path(project_id, entity_type):
        """Fallback: используем стандартные пути"""
        project_dir = get_project_dir(project_id)
        repo_base = project_dir / 'repo'
        # Стандартные пути
        paths = {
            'playbooks': repo_base / 'playbooks',
            'roles': repo_base / 'roles',
            'inventories': repo_base / 'inventories'
        }
        return paths.get(entity_type, repo_base / entity_type)

logger = logging.getLogger(__name__)


def _parse_play_recap_per_host(output_text: str) -> Optional[dict]:
    """Парсит PLAY RECAP и возвращает { hostname: 'online'|'offline', ... }"""
    import re
    lines = output_text.split('\n')
    recap_start = -1
    for i, line in enumerate(lines):
        if 'PLAY RECAP' in line:
            recap_start = i
            break
    if recap_start < 0:
        return None
    hosts = {}
    # hostname : ok=1 changed=0 unreachable=0 failed=0 ...
    pattern = re.compile(
        r'^(\S+)\s*:\s*(?:ok[=:](\d+))?\s*(?:changed[=:](\d+))?\s*(?:unreachable[=:](\d+))?\s*(?:failed[=:](\d+))?'
    )
    for i in range(recap_start + 1, min(recap_start + 50, len(lines))):
        line = lines[i].strip()
        if not line or line.startswith('PLAY') or line.startswith('TASK'):
            break
        m = pattern.match(line)
        if m:
            hostname, ok, _changed, unreachable, failed = m.groups()
            ok = int(ok or 0)
            unreachable = int(unreachable or 0)
            failed = int(failed or 0)
            hosts[hostname] = 'online' if (unreachable == 0 and failed == 0 and ok >= 1) else 'offline'
    return hosts if hosts else None


def extract_result_from_output(output_text: str, execution_type: str, return_code: int, run_params: Optional[dict] = None) -> Optional[dict]:
    """Извлекает результат из вывода ansible-playbook для HOST_CHECK и HOST_FACTS"""
    try:
        if execution_type == 'HOST_CHECK':
            import re
            run_params = run_params or {}
            hosts_list = run_params.get('hosts')
            if isinstance(hosts_list, list) and len(hosts_list) > 1:
                # Мног Host check: парсим PLAY RECAP по каждому хосту
                per_host = _parse_play_recap_per_host(output_text)
                if per_host:
                    return {'hosts': per_host, 'message': f'Checked {len(per_host)} host(s)'}
                # fallback: общий результат по return_code
                return {
                    'hosts': {h: 'offline' for h in hosts_list},
                    'message': 'Could not parse per-host result from output'
                } if return_code != 0 else {
                    'hosts': {h: 'online' for h in hosts_list},
                    'message': f'Checked {len(hosts_list)} host(s)'
                }
            # Один хост: прежняя логика
            failed_match = re.search(r'failed\s*=\s*(\d+)', output_text)
            unreachable_match = re.search(r'unreachable\s*=\s*(\d+)', output_text)
            ok_match = re.search(r'ok\s*=\s*(\d+)', output_text)
            
            failed_count = int(failed_match.group(1)) if failed_match else None
            unreachable_count = int(unreachable_match.group(1)) if unreachable_match else None
            ok_count = int(ok_match.group(1)) if ok_match else None
            
            if failed_count is not None:
                if failed_count == 0 and (unreachable_count is None or unreachable_count == 0):
                    return {
                        'available': True,
                        'message': 'Host is available and ready to execute commands'
                    }
                else:
                    return {
                        'available': False,
                        'message': f'Host is unavailable (failed={failed_count}, unreachable={unreachable_count or 0})'
                    }
            elif return_code == 0:
                return {
                    'available': True,
                    'message': 'Host is available and ready to execute commands'
                }
            else:
                return {
                    'available': False,
                    'message': f'Host is unavailable (ansible return code: {return_code})'
                }
        elif execution_type == 'HOST_FACTS':
            # Парсим JSON facts из вывода
            import re
            import json as json_module
            
            # Ansible может выводить факты в разных форматах:
            # 1. "facts_result": { "ansible_facts": {...} } (из debug var)
            # 2. hostname | SUCCESS => {...} (из setup модуля)
            # 3. Просто JSON объект в выводе
            
            facts_data = None

            # Если ansible завершился с ошибкой, пробуем вытащить причину из UNREACHABLE!/FAILED! JSON
            # чтобы в UI показывалась реальная ошибка (например Permission denied), а не "Could not extract..."
            if return_code != 0:
                error_patterns = [
                    r'UNREACHABLE!\s*=>\s*(\{.*\})',
                    r'FAILED!\s*=>\s*(\{.*\})',
                ]
                for pat in error_patterns:
                    m = re.search(pat, output_text, re.DOTALL)
                    if not m:
                        continue
                    try:
                        err_obj = json_module.loads(m.group(1))
                        msg = (err_obj.get('msg') or err_obj.get('stderr') or err_obj.get('stdout') or '').strip()
                        if msg:
                            return {
                                'error': msg,
                                'return_code': return_code,
                                'unreachable': bool(err_obj.get('unreachable', False)),
                            }
                    except json_module.JSONDecodeError:
                        continue
                # Fallback: хотя бы вернуть код возврата
                return {
                    'error': f'Ansible failed (return code: {return_code})',
                    'return_code': return_code,
                }
            
            # Пробуем найти JSON в выводе debug msg
            # Формат: "msg": "{...json...}" (to_json выводит JSON строку, которая может быть очень длинной)
            # JSON экранирован внутри строки - все кавычки экранированы как \"
            
            # Ищем "msg": "..." - используем более умный поиск для длинных строк
            # Сначала находим начало "msg": "
            msg_start = output_text.find('"msg": "')
            if msg_start != -1:
                # Находим начало JSON (после "msg": ")
                json_start = msg_start + len('"msg": "')
                # Ищем конец строки - это будет закрывающая кавычка после JSON
                # Но нужно учесть, что внутри JSON могут быть экранированные кавычки \"
                # Ищем позицию, где есть " без экранирования перед ней
                json_end = json_start
                escaped = False
                while json_end < len(output_text):
                    char = output_text[json_end]
                    if char == '\\' and not escaped:
                        escaped = True
                        json_end += 1
                        continue
                    if char == '"' and not escaped:
                        # Нашли закрывающую кавычку
                        break
                    escaped = False
                    json_end += 1
                
                if json_end > json_start:
                    # Извлекаем JSON строку
                    json_str_escaped = output_text[json_start:json_end]
                    try:
                        # Распарсим экранированную строку (Python автоматически обработает экранирование)
                        # Используем eval для безопасного распарсивания строки (но лучше использовать json.loads)
                        # Сначала создаем валидную JSON строку
                        json_str = json_module.loads('"' + json_str_escaped + '"')
                        # Теперь распарсим сам JSON
                        facts_result = json_module.loads(json_str)
                        if isinstance(facts_result, dict):
                            facts_data = facts_result
                            logger.debug(f"[extract_result_from_output] Successfully extracted facts from msg, keys: {list(facts_result.keys())[:5]}")
                    except (json_module.JSONDecodeError, ValueError) as e:
                        logger.debug(f"[extract_result_from_output] Failed to parse JSON from msg: {e}")
                        pass
            
            # Fallback: пробуем многострочный формат (без кавычек)
            if not facts_data:
                msg_section = re.search(r'msg:\s*\n(.*?)(?=\n\S+:|PLAY RECAP)', output_text, re.DOTALL)
                if msg_section:
                    msg_content = msg_section.group(1).strip()
                    # Пробуем распарсить как JSON
                    try:
                        facts_result = json_module.loads(msg_content)
                        if isinstance(facts_result, dict):
                            facts_data = facts_result
                    except json_module.JSONDecodeError:
                        pass
            
            # Пробуем найти JSON в выводе debug msg (fallback)
            if not facts_data:
                msg_section = re.search(r'"msg"\s*:\s*"(.*?)"', output_text, re.DOTALL)
                if not msg_section:
                    msg_section = re.search(r'msg:\s*\n(.*?)(?=\n\S+:|PLAY RECAP)', output_text, re.DOTALL)
                
                if msg_section:
                    msg_content = msg_section.group(1)
                    json_in_msg = re.search(r'(\{.*?\})', msg_content, re.DOTALL)
                    if json_in_msg:
                        try:
                            json_str = json_in_msg.group(1).replace('\\n', '\n').replace('\\"', '"')
                            facts_result = json_module.loads(json_str)
                            if isinstance(facts_result, dict):
                                facts_data = facts_result
                        except json_module.JSONDecodeError:
                            pass
            
            # Пробуем найти факты из debug var output (старый формат)
            if not facts_data:
                debug_var_match = re.search(r'"facts_result\.ansible_facts"\s*:\s*(\{.*?\})', output_text, re.DOTALL)
                if debug_var_match:
                    try:
                        facts_result = json_module.loads(debug_var_match.group(1))
                        facts_data = facts_result.get('ansible_facts', facts_result)
                    except json_module.JSONDecodeError:
                        pass
            
            # Пробуем найти facts_result из debug output
            if not facts_data:
                facts_result_match = re.search(r'"facts_result"\s*:\s*(\{.*?\})', output_text, re.DOTALL)
                if facts_result_match:
                    try:
                        facts_result = json_module.loads(facts_result_match.group(1))
                        facts_data = facts_result.get('ansible_facts', facts_result)
                    except json_module.JSONDecodeError:
                        pass
            
            # Если не нашли, пробуем формат SUCCESS => {...}
            if not facts_data:
                success_match = re.search(r'SUCCESS\s*=>\s*(\{.*?\})', output_text, re.DOTALL)
                if success_match:
                    try:
                        facts_json = json_module.loads(success_match.group(1))
                        facts_data = facts_json.get('ansible_facts', facts_json)
                    except json_module.JSONDecodeError:
                        pass
            
            # Если не нашли, пробуем найти любой большой JSON объект (может быть факты)
            if not facts_data:
                # Ищем JSON объект, который начинается с { и содержит "ansible_facts" или похожие ключи
                json_pattern = r'(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})'
                matches = re.finditer(json_pattern, output_text, re.DOTALL)
                for match in matches:
                    try:
                        potential_json = json_module.loads(match.group(1))
                        # Проверяем, что это похоже на facts (содержит типичные ключи)
                        if isinstance(potential_json, dict):
                            if 'ansible_facts' in potential_json:
                                facts_data = potential_json['ansible_facts']
                                break
                            elif any(key.startswith('ansible_') for key in potential_json.keys()):
                                # Может быть это уже факты
                                facts_data = potential_json
                                break
                    except json_module.JSONDecodeError:
                        continue
            
            # Если все еще не нашли, пробуем найти самый большой JSON объект
            if not facts_data and return_code == 0:
                # Ищем самый большой JSON объект в выводе
                json_objects = re.finditer(r'(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})', output_text, re.DOTALL)
                largest_json = None
                largest_size = 0
                for match in json_objects:
                    try:
                        potential_json = json_module.loads(match.group(1))
                        if isinstance(potential_json, dict) and len(match.group(1)) > largest_size:
                            largest_json = potential_json
                            largest_size = len(match.group(1))
                    except json_module.JSONDecodeError:
                        continue
                
                if largest_json:
                    facts_data = largest_json.get('ansible_facts', largest_json)
            
            if facts_data and return_code == 0:
                logger.info(f"[extract_result_from_output] Successfully extracted facts, keys: {list(facts_data.keys())[:10] if isinstance(facts_data, dict) else 'N/A'}")
                return {
                    'facts': facts_data
                }
            else:
                # Логируем для отладки
                logger.warning(f"[extract_result_from_output] Could not extract facts. Output length: {len(output_text)}, return_code: {return_code}")
                if len(output_text) > 0:
                    # Логируем весь вывод для отладки (первые 1000 символов)
                    logger.debug(f"[extract_result_from_output] First 1000 chars of output: {output_text[:1000]}")
                    # И последние 500 символов
                    if len(output_text) > 1000:
                        logger.debug(f"[extract_result_from_output] Last 500 chars of output: {output_text[-500:]}")
                return {
                    'error': 'Could not extract facts from output'
                }
    except Exception as e:
        logger.error(f"[extract_result_from_output] Error extracting result: {e}")
        return None
    
    return None


def execute_run(execution_id, execution_data, project_id, http_client, heartbeat_interval=30, 
                status_check_interval=1.0, grace_period=10):
    """
    Выполняет ansible-playbook для указанного run с поддержкой CANCELING
    
    Args:
        execution_id: ID execution
        execution_data: Данные execution (из API response)
        project_id: ID проекта
        http_client: WorkerHTTPClient для общения с сервером
        heartbeat_interval: Интервал отправки heartbeat в секундах
        status_check_interval: Интервал проверки статуса в секундах (по умолчанию 1.0)
        grace_period: Период ожидания после SIGTERM перед SIGKILL в секундах (по умолчанию 10)
    """
    start_time = time.time()
    temp_files = []  # Для очистки временных файлов
    temp_key_files = []  # Инициализируем сразу, чтобы была доступна в finally при любых исключениях
    heartbeat_stop = threading.Event()
    process = None
    pgid = None
    was_canceling = False
    
    try:
        run_params = execution_data.get('runParams', {})
        if not run_params:
            raise ValueError("runParams not found in execution")
        
        temp_playbook_path = Path(run_params.get('temp_playbook'))
        if not temp_playbook_path.exists():
            raise ValueError(f"Playbook file not found: {temp_playbook_path}")
        
        execution_type = run_params.get('execution_type', 'PLAYBOOK_RUN')
        inventory_files = run_params.get('inventory_files', ['inventory.yml'])
        temp_inventory = run_params.get('temp_inventory')  # Для HOST_CHECK и HOST_FACTS
        ansible_config = run_params.get('ansible_config')
        project_dir = Path(run_params.get('project_dir'))
        limit_host = run_params.get('limit_host')  # Для HOST_CHECK и HOST_FACTS
        
        logger.info(f"[execute_run] Received inventory_files from runParams: {inventory_files}")
        
        if not project_dir.exists():
            raise ValueError(f"Project directory not found: {project_dir}")
        
        # Для HOST_CHECK и HOST_FACTS загружаем temp_key_files из run_params (созданные в backend)
        # Для обычных playbook runs используем пустой список (уже инициализирован выше)
        if execution_type in ('HOST_CHECK', 'HOST_FACTS'):
            temp_key_files = run_params.get('temp_key_files', [])
        
        # Для HOST_CHECK и HOST_FACTS используем готовый temp_inventory
        if execution_type in ('HOST_CHECK', 'HOST_FACTS') and temp_inventory:
            temp_inventory_path = Path(temp_inventory)
            if not temp_inventory_path.exists():
                raise ValueError(f"Temporary inventory file not found: {temp_inventory_path}")
            inventory_args = ['-i', str(temp_inventory_path)]
            # НЕ добавляем в temp_files - файл находится в постоянном месте и не должен удаляться
            logger.debug(f"[execute_run] Using temporary inventory for {execution_type}: {temp_inventory_path}")
        else:
            # Подготавливаем inventory с connection secrets (для обычных playbook runs)
            # Используем пути из inventory_files напрямую, без fallback на старую структуру
            inventory_args = []
            
            # Обрабатываем connection secrets из host_vars
            # Функция для обработки connection secrets и создания временных файлов ключей
            def process_host_connection_secrets(inv_path, project_id):
                """Обрабатывает connection secrets из host_vars и создает временные файлы ключей"""
                temp_key_files_list = []
                host_vars_updates = {}  # host_name -> updated host_vars
                
                # Импортируем функции из app.py для работы с путями
                try:
                    from app import get_project_host_vars_dir, get_project_secrets_dir
                except ImportError:
                    # Fallback: используем стандартные пути
                    def get_project_host_vars_dir(project_id):
                        project_dir = get_project_dir(project_id)
                        return project_dir / 'repo' / 'inventories' / 'host_vars'
                    
                    def get_project_secrets_dir(project_id):
                        project_dir = get_project_dir(project_id)
                        return project_dir / 'secrets'
                
                try:
                    
                    # Определяем директорию host_vars (обычно рядом с inventory файлом)
                    inv_dir = inv_path.parent
                    host_vars_dirs = [
                        inv_dir / 'host_vars',  # Рядом с inventory файлом
                        get_project_host_vars_dir(project_id),  # Стандартная директория проекта
                    ]
                    
                    # Загружаем inventory для получения списка хостов
                    with open(inv_path, 'r', encoding='utf-8') as f:
                        inv_data = yaml_loader.load(f) or {}
                    
                    # Собираем все хосты из inventory
                    all_hosts = set()
                    if 'all' in inv_data:
                        if 'hosts' in inv_data['all']:
                            all_hosts.update(inv_data['all']['hosts'].keys())
                        if 'children' in inv_data['all']:
                            for group_data in inv_data['all']['children'].values():
                                if 'hosts' in group_data:
                                    all_hosts.update(group_data['hosts'].keys())
                    
                    # Обрабатываем каждый хост
                    for host_name in all_hosts:
                        host_vars_file = None
                        host_vars = {}
                        
                        # Ищем host_vars файл для хоста
                        for hv_dir in host_vars_dirs:
                            if hv_dir.exists():
                                hv_file = hv_dir / f"{host_name}.yml"
                                if hv_file.exists():
                                    host_vars_file = hv_file
                                    try:
                                        with open(hv_file, 'r', encoding='utf-8') as f:
                                            host_vars = yaml_loader.load(f) or {}
                                        break
                                    except Exception as e:
                                        logger.warning(f"[execute_run] Error loading host_vars for {host_name}: {e}")
                                        continue
                        
                        # Проверяем наличие connectionSecret
                        connection_secret_name = host_vars.get('connectionSecret')
                        key_file_path = host_vars.get('ansible_ssh_private_key_file')
                        
                        if connection_secret_name and key_file_path:
                            # Проверяем, указывает ли путь на JSON файл секрета
                            key_path = Path(key_file_path)
                            if key_path.exists() and key_path.suffix == '.json':
                                try:
                                    # Загружаем секрет из JSON
                                    with open(key_path, 'r', encoding='utf-8') as f:
                                        secret_data = json.load(f)
                                    
                                    if secret_data.get('type') == 'ssh_key':
                                        private_key = secret_data.get('privateKey', '')
                                        if private_key:
                                            # Создаем временный файл ключа
                                            temp_key_file = TEMP_DIR / f'ssh_key_{uuid.uuid4().hex[:8]}.pem'
                                            TEMP_DIR.mkdir(parents=True, exist_ok=True)
                                            
                                            with open(temp_key_file, 'w', encoding='utf-8') as f:
                                                key_content = private_key
                                                if not key_content.endswith('\n'):
                                                    key_content = key_content + '\n'
                                                f.write(key_content)
                                            
                                            os.chmod(temp_key_file, 0o600)
                                            abs_key_path = str(temp_key_file.resolve())
                                            
                                            # Сохраняем путь для последующего удаления
                                            temp_key_files_list.append(temp_key_file)
                                            
                                            # Обновляем host_vars с новым путем к ключу
                                            host_vars['ansible_ssh_private_key_file'] = abs_key_path
                                            
                                            # Обновляем ansible_user из секрета, если он указан
                                            secret_username = secret_data.get('username', '').strip()
                                            if secret_username:
                                                old_ansible_user = host_vars.get('ansible_user', 'not set')
                                                host_vars['ansible_user'] = secret_username
                                                logger.info(f"[execute_run] Updated ansible_user from secret {connection_secret_name} for host {host_name}: {old_ansible_user} -> {secret_username}")
                                            else:
                                                logger.warning(f"[execute_run] Secret {connection_secret_name} does not contain username, keeping ansible_user from host_vars: {host_vars.get('ansible_user', 'not set')}")
                                            
                                            host_vars_updates[host_name] = {
                                                'file': host_vars_file,
                                                'vars': host_vars
                                            }
                                            
                                            logger.info(f"[execute_run] Created temporary SSH key file for host {host_name} from secret {connection_secret_name}, final ansible_user: {host_vars.get('ansible_user', 'not set')}")
                                        else:
                                            logger.warning(f"[execute_run] Private key is empty in secret {connection_secret_name} for host {host_name}")
                                    elif secret_data.get('type') == 'login_password':
                                        # Для пароля ничего не делаем, он уже в host_vars
                                        logger.debug(f"[execute_run] Using password from secret {connection_secret_name} for host {host_name}")
                                except Exception as e:
                                    logger.error(f"[execute_run] Error processing connection secret for host {host_name}: {e}")
                except Exception as e:
                    logger.error(f"[execute_run] Error in process_host_connection_secrets: {e}")
                
                return temp_key_files_list, host_vars_updates
            
            # Обрабатываем connection secrets после определения inventory пути
            processed_temp_key_files = []
            host_vars_updates_map = {}
            
            if len(inventory_files) == 1:
                inv_file_path = inventory_files[0]
                
                # Путь может быть:
                # 1. Относительным от repo (например, "inventories/invent.yaml", "inventories/prod/hosts.yml")
                # 2. Просто именем файла (например, "inventory.yml", "invent.yaml")
                
                # Если путь начинается с "inventories/" или содержит "/", это путь относительно repo
                if inv_file_path.startswith('inventories/') or ('.' in inv_file_path and '/' in inv_file_path):
                    inv_path = project_dir / 'repo' / inv_file_path
                elif inv_file_path == 'hosts.yml' or inv_file_path == 'inventory.yml':
                    # Стандартные имена - проверяем новую структуру
                    new_inv_path = project_dir / 'repo' / 'inventories' / 'prod' / 'hosts.yml'
                    if new_inv_path.exists():
                        inv_path = new_inv_path
                    else:
                        # Fallback на корневой inventory.yml
                        inv_path = project_dir / 'repo' / 'inventory.yml'
                else:
                    # Другое имя файла (например, "invent.yaml", "345.yml") - ищем в inventories_dir
                    inventories_dir = project_dir / 'repo' / 'inventories'
                    logger.debug(f"[execute_run] Looking for inventory file '{inv_file_path}' in {inventories_dir}")
                    if inventories_dir.exists():
                        # Ищем рекурсивно по имени файла
                        found = False
                        for inv_file in inventories_dir.rglob(inv_file_path):
                            if inv_file.is_file():
                                inv_path = inv_file
                                found = True
                                logger.debug(f"[execute_run] Found inventory file at: {inv_path}")
                                break
                        if not found:
                            # Если не найден в inventories, пробуем в корне repo
                            inv_path = project_dir / 'repo' / inv_file_path
                            logger.debug(f"[execute_run] Not found in inventories, trying: {inv_path}")
                    else:
                        # Если директории inventories нет, пробуем в корне repo
                        inv_path = project_dir / 'repo' / inv_file_path
                        logger.debug(f"[execute_run] Inventories dir not exists, trying: {inv_path}")
                
                if inv_path.exists():
                    # Обрабатываем connection secrets из host_vars
                    processed_temp_key_files, host_vars_updates_map = process_host_connection_secrets(inv_path, project_id)
                    temp_key_files.extend(processed_temp_key_files)
                    
                    # Если были обновлены host_vars, создаем временные копии host_vars файлов
                    if host_vars_updates_map:
                        temp_host_vars_dir = TEMP_DIR / f'host_vars_{uuid.uuid4().hex[:8]}'
                        temp_host_vars_dir.mkdir(parents=True, exist_ok=True)
                        
                        for host_name, update_data in host_vars_updates_map.items():
                            temp_host_vars_file = temp_host_vars_dir / f"{host_name}.yml"
                            with open(temp_host_vars_file, 'w', encoding='utf-8') as f:
                                yaml_loader.dump(update_data['vars'], f)
                            temp_files.append(temp_host_vars_dir)
                            logger.debug(f"[execute_run] Created temporary host_vars file for {host_name}")
                        
                        # Создаем временный inventory с указанием на временную директорию host_vars
                        # Ansible автоматически загружает host_vars из директории рядом с inventory
                        # Поэтому создаем временную директорию рядом с inventory и копируем туда host_vars
                        inv_dir = inv_path.parent
                        temp_inv_dir = TEMP_DIR / f'inventory_dir_{uuid.uuid4().hex[:8]}'
                        temp_inv_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Копируем inventory файл во временную директорию
                        temp_inv_path = temp_inv_dir / inv_path.name
                        shutil.copy2(inv_path, temp_inv_path)
                        
                        # Создаем host_vars директорию рядом с временным inventory
                        temp_host_vars_dir_final = temp_inv_dir / 'host_vars'
                        temp_host_vars_dir_final.mkdir(parents=True, exist_ok=True)
                        
                        # Копируем обновленные host_vars файлы
                        for host_name, update_data in host_vars_updates_map.items():
                            temp_host_vars_file_final = temp_host_vars_dir_final / f"{host_name}.yml"
                            with open(temp_host_vars_file_final, 'w', encoding='utf-8') as f:
                                yaml_loader.dump(update_data['vars'], f)
                        
                        # Копируем остальные host_vars файлы (если есть)
                        inv_host_vars_dir = inv_dir / 'host_vars'
                        if inv_host_vars_dir.exists():
                            for hv_file in inv_host_vars_dir.glob('*.yml'):
                                if hv_file.stem not in host_vars_updates_map:
                                    shutil.copy2(hv_file, temp_host_vars_dir_final / hv_file.name)
                        
                        inventory_args = ['-i', str(temp_inv_path)]
                        temp_files.append(temp_inv_dir)
                        logger.info(f"[execute_run] Using inventory file with processed connection secrets: {temp_inv_path}")
                    else:
                        inventory_args = ['-i', str(inv_path)]
                        logger.info(f"[execute_run] Using inventory file: {inv_path}")
                else:
                    # Пробуем найти файл по другому пути или использовать default
                    default_inv = get_project_inventory_file(project_id)
                    if default_inv.exists():
                        inventory_args = ['-i', str(default_inv)]
                        logger.warning(f"[execute_run] Inventory file {inventory_files[0]} not found at {inv_path}, using default: {default_inv}")
                    else:
                        raise ValueError(f"Inventory file not found: {inventory_files[0]} (searched at {inv_path}, default: {default_inv})")
            else:
                # Multiple inventory files - combine them
                temp_inventory_combined = TEMP_DIR / f'inventory_{uuid.uuid4().hex[:8]}.yml'
                temp_inventory_combined.parent.mkdir(parents=True, exist_ok=True)
                combined_inventory = {'all': {'hosts': {}, 'children': {}}}
                
                for inv_file in inventory_files:
                    # Путь может быть:
                    # 1. Относительным от repo (например, "inventories/invent.yaml", "inventories/prod/hosts.yml")
                    # 2. Просто именем файла (например, "inventory.yml", "invent.yaml")
                    
                    # Если путь начинается с "inventories/" или содержит "/", это путь относительно repo
                    if inv_file.startswith('inventories/') or ('.' in inv_file and '/' in inv_file):
                        inv_path = project_dir / 'repo' / inv_file
                    elif inv_file == 'hosts.yml' or inv_file == 'inventory.yml':
                        # Стандартные имена - проверяем новую структуру
                        new_inv_path = project_dir / 'repo' / 'inventories' / 'prod' / 'hosts.yml'
                        if new_inv_path.exists():
                            inv_path = new_inv_path
                        else:
                            inv_path = project_dir / 'repo' / 'inventory.yml'
                    else:
                        # Другое имя файла (например, "invent.yaml", "345.yml") - ищем в inventories_dir
                        inventories_dir = project_dir / 'repo' / 'inventories'
                        if inventories_dir.exists():
                            # Ищем рекурсивно по имени файла
                            found = False
                            for inv_file_search in inventories_dir.rglob(inv_file):
                                if inv_file_search.is_file():
                                    inv_path = inv_file_search
                                    found = True
                                    break
                            if not found:
                                # Если не найден в inventories, пробуем в корне repo
                                inv_path = project_dir / 'repo' / inv_file
                        else:
                            # Если директории inventories нет, пробуем в корне repo
                            inv_path = project_dir / 'repo' / inv_file
                    
                    if inv_path.exists():
                        with open(inv_path, 'r', encoding='utf-8') as inv_f:
                            inv_data = yaml_loader.load(inv_f) or {}
                            if 'all' in inv_data:
                                if 'hosts' in inv_data['all']:
                                    combined_inventory['all']['hosts'].update(inv_data['all'].get('hosts', {}))
                                if 'children' in inv_data['all']:
                                    for group_name, group_data in inv_data['all'].get('children', {}).items():
                                        if group_name not in combined_inventory['all']['children']:
                                            combined_inventory['all']['children'][group_name] = {'hosts': {}}
                                        if 'hosts' in group_data:
                                            combined_inventory['all']['children'][group_name]['hosts'].update(group_data.get('hosts', {}))
                
                with open(temp_inventory_combined, 'w', encoding='utf-8') as f:
                    yaml_loader.dump(combined_inventory, f)
                
                # Обрабатываем connection secrets из host_vars для комбинированного inventory
                processed_temp_key_files_combined, host_vars_updates_map_combined = process_host_connection_secrets(temp_inventory_combined, project_id)
                temp_key_files.extend(processed_temp_key_files_combined)
                
                # Если были обновлены host_vars, создаем временные копии
                if host_vars_updates_map_combined:
                    temp_inv_dir_combined = temp_inventory_combined.parent
                    temp_host_vars_dir_combined = temp_inv_dir_combined / 'host_vars'
                    temp_host_vars_dir_combined.mkdir(parents=True, exist_ok=True)
                    
                    for host_name, update_data in host_vars_updates_map_combined.items():
                        temp_host_vars_file = temp_host_vars_dir_combined / f"{host_name}.yml"
                        with open(temp_host_vars_file, 'w', encoding='utf-8') as f:
                            yaml_loader.dump(update_data['vars'], f)
                        logger.debug(f"[execute_run] Created temporary host_vars file for {host_name} in combined inventory")
                
                inventory_args = ['-i', str(temp_inventory_combined)]
                temp_files.append(temp_inventory_combined)
        
        # Подготавливаем команду ansible-playbook
        cmd = ['ansible-playbook', str(temp_playbook_path)] + inventory_args
        
        # Для HOST_CHECK и HOST_FACTS добавляем --limit только для одного хоста (не для check_hosts)
        hosts_param = run_params.get('hosts') if isinstance(run_params.get('hosts'), list) else None
        if execution_type in ('HOST_CHECK', 'HOST_FACTS') and limit_host and not (hosts_param and len(hosts_param) > 1):
            cmd.extend(['--limit', limit_host])
            logger.debug(f"[execute_run] Adding --limit {limit_host} for {execution_type}")
        
        # Execution parameters from run_params
        if run_params.get('check_mode'):
            cmd.append('--check')
            logger.debug("[execute_run] Adding --check (dry-run)")
        verbosity = run_params.get('verbosity')
        if verbosity and verbosity in ('-v', '-vv', '-vvv', '-vvvv'):
            cmd.append(verbosity)
            logger.debug(f"[execute_run] Adding verbosity {verbosity}")
        forks = run_params.get('forks')
        if forks is not None and forks > 0:
            cmd.extend(['--forks', str(forks)])
            logger.debug(f"[execute_run] Adding --forks {forks}")
        if run_params.get('force_handlers'):
            cmd.append('--force-handlers')
            logger.debug("[execute_run] Adding --force-handlers")
        connection_timeout = run_params.get('connection_timeout')
        if connection_timeout is not None and connection_timeout > 0:
            cmd.extend(['--timeout', str(connection_timeout)])
            logger.debug(f"[execute_run] Adding --timeout {connection_timeout}")
        
        # Vault: если playbook использует vars_files с vault, генерируем vault_pass.txt
        # Поддержка --vault-id label@file для файлов с vault label в заголовке
        vault_id = run_params.get('vault_id')
        vault_pass_file = None
        if vault_id:
            password = get_key_password_for_vault(project_id, vault_id)
            if password:
                vault_pass_file = TEMP_DIR / f'vault_pass_{uuid.uuid4().hex}.txt'
                with open(vault_pass_file, 'w', encoding='utf-8') as f:
                    f.write(password)
                    if not password.endswith('\n'):
                        f.write('\n')
                os.chmod(vault_pass_file, 0o600)
                temp_files.append(vault_pass_file)
                vault_label = get_vault_name_for_vault_id(project_id, vault_id)
                if vault_label:
                    cmd.extend(['--vault-id', f'{vault_label}@{vault_pass_file}'])
                    logger.info(f"[execute_run] Using --vault-id {vault_label}@... for vault_id={vault_id}")
                else:
                    cmd.extend(['--vault-password-file', str(vault_pass_file)])
                    logger.info(f"[execute_run] Using vault password file for vault_id={vault_id}")
            else:
                logger.warning(f"[execute_run] Vault id {vault_id} specified but key not found; run may fail on vault decryption")
        
        # Настраиваем ansible config
        # Новая структура: repo/ansible.cfg
        repo_ansible_cfg = project_dir / 'repo' / 'ansible.cfg'
        ansible_config_dir = project_dir / 'ansible-config'  # Старая структура для обратной совместимости
        
        ansible_config_path = None
        if ansible_config:
            if ansible_config.startswith('ansible-config/'):
                ansible_config_path = project_dir / ansible_config
            elif ansible_config == 'ansible.cfg' or ansible_config.endswith('.cfg'):
                # Проверяем новую структуру: repo/ansible.cfg
                if repo_ansible_cfg.exists():
                    ansible_config_path = repo_ansible_cfg
                else:
                    ansible_config_path = ansible_config_dir / ansible_config
            else:
                ansible_config_path = ansible_config_dir / ansible_config
        
        env = os.environ.copy()
        if ansible_config_path and ansible_config_path.exists() and ansible_config_path.is_file():
            env['ANSIBLE_CONFIG'] = str(ansible_config_path)
        
        # Добавляем путь к roles используя repoLayout
        try:
            # Используем resolve_repo_path для получения пути к roles с учетом repoLayout
            project_roles_path = resolve_repo_path(project_id, 'roles')
        except Exception as e:
            logger.warning(f"Failed to resolve roles path using repoLayout, falling back to default: {e}")
            # Fallback: стандартный путь
            project_roles_path = project_dir / 'repo' / 'roles'
            # Обратная совместимость: если новая структура не существует, используем старую
            if not project_roles_path.exists() and not (project_dir / 'repo').exists():
                project_roles_path = project_dir / 'roles-playbooks'
        
        if project_roles_path.exists():
            existing_roles_path = env.get('ANSIBLE_ROLES_PATH', '')
            if existing_roles_path:
                env['ANSIBLE_ROLES_PATH'] = f"{str(project_roles_path)}:{existing_roles_path}"
            else:
                env['ANSIBLE_ROLES_PATH'] = str(project_roles_path)
        
        # Mitogen: при стратегии mitogen_* задаём ANSIBLE_STRATEGY_PLUGINS
        strategy = run_params.get('strategy') or ''
        if strategy.startswith('mitogen_'):
            strategy_plugins_path = get_mitogen_strategy_plugins_path()
            if strategy_plugins_path:
                env['ANSIBLE_STRATEGY_PLUGINS'] = strategy_plugins_path
                logger.info(f"[execute_run] Using Mitogen strategy '{strategy}', ANSIBLE_STRATEGY_PLUGINS={strategy_plugins_path}")
            else:
                logger.warning(f"[execute_run] Mitogen strategy '{strategy}' selected but strategy_plugins path not found; run may fail with 'Invalid play strategy specified'")
        
        # Логируем все параметры запуска подробно на уровне INFO
        cmd_str = ' '.join([f'"{arg}"' if ' ' in str(arg) else str(arg) for arg in cmd])
        
        logger.info(f"[execute_run] ========== Starting ansible-playbook ==========")
        logger.info(f"[execute_run] Execution ID: {execution_id}")
        logger.info(f"[execute_run] Project ID: {project_id}")
        logger.info(f"[execute_run] Execution Type: {execution_type}")
        logger.info(f"[execute_run] Project Directory: {project_dir}")
        logger.info(f"[execute_run] Playbook File: {temp_playbook_path}")
        logger.info(f"[execute_run] Inventory Files: {inventory_files}")
        logger.info(f"[execute_run] Ansible Config: {ansible_config}")
        
        # Определяем и логируем полные пути к inventory файлам
        if execution_type in ('HOST_CHECK', 'HOST_FACTS') and temp_inventory:
            logger.info(f"[execute_run] Temporary Inventory: {temp_inventory}")
        else:
            # Логируем inventory аргументы
            inv_paths = []
            for i in range(0, len(inventory_args), 2):
                if i + 1 < len(inventory_args) and inventory_args[i] == '-i':
                    inv_paths.append(inventory_args[i + 1])
            logger.info(f"[execute_run] Inventory Paths: {inv_paths}")
        
        # Логируем limit_host если есть
        if limit_host:
            logger.info(f"[execute_run] Limit Host: {limit_host}")
        
        # Для HOST_CHECK/HOST_FACTS проверяем наличие ключа из инвентаря (диагностика unreachable)
        if execution_type in ('HOST_CHECK', 'HOST_FACTS') and temp_inventory:
            try:
                inv_path = Path(temp_inventory)
                if inv_path.exists():
                    with open(inv_path, 'r', encoding='utf-8') as f:
                        inv_data = yaml_loader.load(f) or {}
                    hosts = inv_data.get('all', {}).get('hosts', {})
                    for hname, hdata in hosts.items():
                        key_path = (hdata or {}).get('ansible_ssh_private_key_file')
                        if key_path:
                            kp = Path(key_path)
                            exists = kp.is_file()
                            logger.info(f"[execute_run] Host {hname} ansible_ssh_private_key_file={key_path} exists={exists}")
                            if not exists:
                                logger.warning(f"[execute_run] SSH key file missing on worker: {key_path}")
                        else:
                            logger.info(f"[execute_run] Host {hname} has no ansible_ssh_private_key_file (password auth?)")
            except Exception as e:
                logger.warning(f"[execute_run] Could not check inventory key paths: {e}")
        
        # Определяем и логируем полный путь к ansible.cfg
        ansible_config_path_resolved = None
        if ansible_config_path and ansible_config_path.exists():
            ansible_config_path_resolved = str(ansible_config_path.resolve())
            logger.info(f"[execute_run] Ansible Config Path: {ansible_config_path_resolved}")
        else:
            logger.warning(f"[execute_run] Ansible Config Path: {ansible_config_path} (not found, using default)")
        
        # Логируем переменные окружения
        logger.info(f"[execute_run] Environment Variables:")
        if 'ANSIBLE_CONFIG' in env:
            logger.info(f"[execute_run]   ANSIBLE_CONFIG={env['ANSIBLE_CONFIG']}")
        if 'ANSIBLE_ROLES_PATH' in env:
            logger.info(f"[execute_run]   ANSIBLE_ROLES_PATH={env['ANSIBLE_ROLES_PATH']}")
        if 'ANSIBLE_STRATEGY_PLUGINS' in env:
            logger.info(f"[execute_run]   ANSIBLE_STRATEGY_PLUGINS={env['ANSIBLE_STRATEGY_PLUGINS']}")
        # Логируем другие важные переменные окружения
        ansible_env_vars = ['GIT_SSH_COMMAND', 'JOB_NAME', 'ANSIBLE_HOST_KEY_CHECKING', 
                           'ANSIBLE_SSH_ARGS', 'ANSIBLE_FORCE_COLOR', 'ANSIBLE_NOCOLOR']
        for key in ansible_env_vars:
            if key in env:
                logger.info(f"[execute_run]   {key}={env[key]}")
        
        # Логируем полную команду
        logger.info(f"[execute_run] Full Command: {cmd_str}")
        logger.info(f"[execute_run] Working Directory (cwd): {project_dir}")
        logger.info(f"[execute_run] ===========================================")
        
        # Также логируем на уровне DEBUG для обратной совместимости
        logger.debug(f"[execute_run] ========== Starting ansible-playbook ========== | execution_id={execution_id} | project_id={project_id}")
        logger.debug(f"[execute_run] Full command: {cmd_str} | execution_id={execution_id} | project_id={project_id}")
        logger.debug(f"[execute_run] Project directory: {project_dir} | execution_id={execution_id} | project_id={project_id}")
        logger.debug(f"[execute_run] ===========================================")
        
        # Запускаем heartbeat в отдельном потоке
        def heartbeat_worker():
            while not heartbeat_stop.is_set():
                http_client.heartbeat(current_execution_id=execution_id)
                heartbeat_stop.wait(heartbeat_interval)
        
        heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
        heartbeat_thread.start()
        
        # Запускаем ansible-playbook с process group для управления
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            cwd=str(project_dir),
            env=env,
            preexec_fn=os.setsid  # Создаём новую process group
        )
        
        # Сохраняем process group ID
        pgid = os.getpgid(process.pid)
        logger.debug(f"[execute_run] Started process with PID {process.pid}, PGID {pgid}")
        
        # Читаем вывод построчно и отправляем в API
        # Одновременно проверяем статус execution
        last_status_check = 0
        status_check_interval_sec = status_check_interval
        
        # Используем неблокирующее чтение для возможности проверки статуса
        import queue
        
        # Создаём очередь для строк вывода
        output_queue = queue.Queue()
        # Сохраняем весь вывод для HOST_CHECK и HOST_FACTS
        full_output = [] if execution_type in ('HOST_CHECK', 'HOST_FACTS') else None
        
        def read_output():
            """Читает вывод процесса в отдельном потоке"""
            try:
                for line in process.stdout:
                    output_queue.put(line)
                    # Сохраняем вывод для HOST_CHECK и HOST_FACTS
                    if full_output is not None:
                        full_output.append(line)
                output_queue.put(None)  # Маркер конца
            except Exception as e:
                logger.error(f"[execute_run] Error reading output: {e}")
                output_queue.put(None)
        
        output_thread = threading.Thread(target=read_output, daemon=True)
        output_thread.start()
        
        # Основной цикл чтения вывода и проверки статуса
        while True:
            current_time = time.time()
            
            # Проверяем статус каждые status_check_interval секунд
            if current_time - last_status_check >= status_check_interval_sec:
                try:
                    execution_status = http_client.get_execution_status(execution_id, project_id)
                    
                    # Защита от гонок: если execution уже финальный, прекращаем выполнение
                    if execution_status in ('SUCCESS', 'FAILED', 'CANCELED'):
                        logger.warning(f"[execute_run] Execution {execution_id} already in final status {execution_status}, stopping")
                        if process and process.poll() is None:
                            # Процесс ещё работает, но execution уже финальный - убиваем
                            try:
                                if pgid:
                                    os.killpg(pgid, signal.SIGKILL)
                                else:
                                    process.kill()
                            except Exception as e:
                                logger.error(f"[execute_run] Error killing process group: {e}")
                        break
                    
                    # Если статус стал CANCELING
                    if execution_status == 'CANCELING' and not was_canceling:
                        was_canceling = True
                        logger.info(f"[execute_run] Execution {execution_id} status changed to CANCELING, sending SIGTERM")
                        http_client.send_log(execution_id, "[worker] Canceling execution, sending SIGTERM\n")
                        
                        # Отправляем SIGTERM процессу и всей группе
                        try:
                            if pgid:
                                os.killpg(pgid, signal.SIGTERM)
                                logger.debug(f"[execute_run] Sent SIGTERM to process group {pgid}")
                            else:
                                process.terminate()
                                logger.debug(f"[execute_run] Sent SIGTERM to process {process.pid}")
                        except Exception as e:
                            logger.error(f"[execute_run] Error sending SIGTERM: {e}")
                        
                        # Ждём grace_period
                        grace_start = time.time()
                        while time.time() - grace_start < grace_period:
                            if process.poll() is not None:
                                # Процесс завершился
                                break
                            time.sleep(0.5)
                        
                        # Если процесс ещё жив, отправляем SIGKILL
                        if process.poll() is None:
                            logger.warning(f"[execute_run] Process still alive after {grace_period}s, sending SIGKILL")
                            http_client.send_log(execution_id, f"[worker] Force killing execution after {grace_period}s grace period\n")
                            try:
                                if pgid:
                                    os.killpg(pgid, signal.SIGKILL)
                                else:
                                    process.kill()
                            except Exception as e:
                                logger.error(f"[execute_run] Error sending SIGKILL: {e}")
                    
                    last_status_check = current_time
                except Exception as e:
                    logger.warning(f"[execute_run] Error checking execution status: {e}")
                    # Продолжаем выполнение даже при ошибке проверки статуса
            
            # Читаем вывод из очереди (неблокирующе)
            try:
                line = output_queue.get(timeout=0.1)
                if line is None:
                    # Конец вывода
                    break
                http_client.send_log(execution_id, line)
            except queue.Empty:
                # Очередь пуста, продолжаем цикл
                continue
        
        # Ждём завершения процесса
        process.wait()
        heartbeat_stop.set()  # Останавливаем heartbeat
        
        # Вычисляем время выполнения
        finished_at = time.time()
        duration = int(finished_at - start_time)
        
        # Проверяем финальный статус перед завершением (защита от гонок)
        try:
            final_status = http_client.get_execution_status(execution_id, project_id)
            if final_status in ('SUCCESS', 'FAILED', 'CANCELED'):
                logger.debug(f"[execute_run] Execution {execution_id} already in final status {final_status}, skipping finish")
                return
        except Exception as e:
            logger.warning(f"[execute_run] Error checking final status: {e}")
        
        # Извлекаем результат для HOST_CHECK и HOST_FACTS
        result_data = None
        if execution_type in ('HOST_CHECK', 'HOST_FACTS') and full_output is not None:
            output_text = ''.join(full_output)
            result_data = extract_result_from_output(output_text, execution_type, process.returncode, run_params=run_params)
            if result_data:
                logger.info(f"[execute_run] Extracted result for {execution_type}: {result_data}")
                # При unreachable логируем хвост вывода Ansible, чтобы увидеть реальную ошибку SSH
                if execution_type == 'HOST_CHECK' and result_data.get('available') is False and 'unreachable' in (result_data.get('message') or ''):
                    lines = output_text.split('\n')
                    # Ищем строки с UNREACHABLE/fatal/Failed to connect
                    err_lines = [ln for ln in lines if 'UNREACHABLE' in ln or 'fatal:' in ln or 'Failed to connect' in ln or 'Permission denied' in ln or 'Connection refused' in ln]
                    if err_lines:
                        logger.warning(f"[execute_run] HOST_CHECK unreachable - SSH/Ansible errors: {' | '.join(err_lines[:5])}")
                    if not err_lines and lines:
                        logger.warning(f"[execute_run] HOST_CHECK unreachable - last 8 lines: {' '.join(ln.strip() for ln in lines[-8:] if ln.strip())}")
            else:
                logger.warning(f"[execute_run] Failed to extract result for {execution_type} from output (len={len(output_text)}, return_code={process.returncode})")
                if output_text:
                    last_lines = output_text.split('\n')[-10:]
                    logger.debug(f"[execute_run] Last 10 lines of output: {''.join(last_lines)}")
        
        # Завершаем execution через API
        if was_canceling:
            # Если был CANCELING, переходим в CANCELED
            logger.info(f"[execute_run] Execution {execution_id} was canceled, transitioning to CANCELED")
            http_client.finish_execution(
                execution_id=execution_id,
                status='CANCELED',
                finished_at=finished_at,
                duration=duration,
                return_code=process.returncode,
                result=result_data
            )
        elif process.returncode == 0:
            logger.info(f"[execute_run] Execution {execution_id} completed successfully")
            if result_data:
                logger.info(f"[execute_run] Sending result to finish_execution: {result_data}")
            http_client.finish_execution(
                execution_id=execution_id,
                status='SUCCESS',
                finished_at=finished_at,
                duration=duration,
                return_code=process.returncode,
                result=result_data
            )
        else:
            # Если процесс упал во время CANCELING, переходим в FAILED
            if was_canceling:
                logger.warning(f"[execute_run] Execution {execution_id} failed during cancel, transitioning to FAILED")
            else:
                logger.warning(f"[execute_run] Execution {execution_id} failed with return code {process.returncode}")
            if result_data:
                logger.info(f"[execute_run] Sending result to finish_execution (FAILED): {result_data}")
            http_client.finish_execution(
                execution_id=execution_id,
                status='FAILED',
                finished_at=finished_at,
                duration=duration,
                return_code=process.returncode,
                result=result_data
            )
        
    except Exception as e:
        logger.error(f"[execute_run] Error executing run {execution_id}: {e}", exc_info=True)
        finished_at = time.time()
        duration = int(finished_at - start_time) if start_time else None
        
        error_msg = str(e)
        http_client.send_log(execution_id, f"ERROR: {error_msg}\n")
        
        http_client.finish_execution(
            execution_id=execution_id,
            status='FAILED',
            finished_at=finished_at,
            duration=duration,
            error=error_msg
        )
    
    finally:
        heartbeat_stop.set()  # Останавливаем heartbeat
        
        # Убиваем процесс если он ещё работает
        if process and process.poll() is None:
            logger.warning(f"[execute_run] Process still running in finally, killing")
            try:
                if pgid:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    process.kill()
            except Exception as e:
                logger.error(f"[execute_run] Error killing process in finally: {e}")
        
        # Закрываем stdout если открыт
        if process and process.stdout:
            try:
                process.stdout.close()
            except Exception as e:
                logger.warning(f"[execute_run] Error closing stdout: {e}")
        
        # Очищаем временные файлы
        # Удаляем временные файлы ключей
        for key_file in temp_key_files:
            try:
                if isinstance(key_file, (str, Path)):
                    key_path = Path(key_file)
                    if key_path.exists():
                        key_path.unlink()
                        logger.debug(f"[execute_run] Removed temporary key file: {key_path}")
            except Exception as e:
                logger.warning(f"[execute_run] Error removing temporary key file {key_file}: {e}")
        
        # Удаляем временные файлы и директории
        for temp_file in temp_files:
            try:
                if isinstance(temp_file, Path) and temp_file.exists():
                    if temp_file.is_dir():
                        shutil.rmtree(temp_file)
                        logger.debug(f"[execute_run] Removed temporary directory: {temp_file}")
                    else:
                        temp_file.unlink()
                        logger.debug(f"[execute_run] Removed temporary file: {temp_file}")
            except Exception as e:
                logger.warning(f"Error cleaning up temp file {temp_file}: {e}")
        
        # Очистка generated_playbooks после выполнения (playbook, inventory, key_*.pem)
        try:
            run_params = execution_data.get('runParams', {})
            if run_params:
                temp_playbook_path = Path(run_params.get('temp_playbook', ''))
                temp_inventory_path = run_params.get('temp_inventory')
                gp_dir = temp_playbook_path.parent if temp_playbook_path else None
                if temp_inventory_path and gp_dir:
                    inv_path = Path(temp_inventory_path)
                    if inv_path.exists() and str(inv_path.parent) == str(Path(gp_dir)):
                        key_files_to_delete = set()
                        try:
                            with open(inv_path, 'r', encoding='utf-8') as inv_f:
                                inv_data = yaml_loader.load(inv_f) or {}
                            hosts_dict = (inv_data.get('all') or {}).get('hosts') or {}
                            for host_data in hosts_dict.values():
                                if isinstance(host_data, dict):
                                    kp = host_data.get('ansible_ssh_private_key_file')
                                    if kp and 'generated_playbooks' in str(kp) and str(kp).endswith('.pem'):
                                        key_name = os.path.basename(str(kp))
                                        if key_name.startswith('key_'):
                                            key_files_to_delete.add(key_name)
                        except Exception as inv_e:
                            logger.debug(f"[execute_run] Could not parse inventory for keys: {inv_e}")
                        inv_path.unlink()
                        logger.debug(f"[execute_run] Removed generated inventory: {inv_path}")
                        for key_name in key_files_to_delete:
                            kf = gp_dir / key_name
                            if kf.exists():
                                kf.unlink()
                                logger.debug(f"[execute_run] Removed generated key: {kf}")
                if temp_playbook_path.exists():
                    temp_playbook_path.unlink()
                    logger.debug(f"[execute_run] Removed generated playbook: {temp_playbook_path}")
        except Exception as e:
            logger.warning(f"[execute_run] Error cleaning up generated_playbooks: {e}")
