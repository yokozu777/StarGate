#!/usr/bin/env python3
"""
Генератор YAML из JSON модели Playbook.
Преобразует структурированную JSON модель в Ansible YAML формат.
"""
import yaml
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)


class PlaybookGenerator:
    """Класс для генерации YAML из JSON модели Playbook"""
    
    def __init__(self):
        pass
    
    def generate(self, playbook: Dict[str, Any]) -> str:
        """
        Генерировать YAML из Playbook модели
        
        Args:
            playbook: Playbook объект в формате JSON модели
        
        Returns:
            YAML строка
        """
        try:
            plays = playbook.get('plays', [])
            
            if not plays:
                # Пустой playbook - возвращаем пустой YAML
                return ''
            
            # Генерируем список play_dict для всех плейсов
            play_list = []
            for play in plays:
                play_dict = self._generate_play_dict(play)
                if play_dict:
                    play_list.append(play_dict)
            
            if not play_list:
                return ''
            
            # Генерируем один YAML документ из списка плейсов
            yaml_str = yaml.dump(
                play_list,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                indent=2
            )
            
            return yaml_str.strip()
            
        except Exception as e:
            logger.error(f"Error generating YAML: {e}")
            raise
    
    def _generate_play_dict(self, play: Dict[str, Any]) -> Dict[str, Any]:
        """
        Генерировать словарь для одного play (для включения в список)
        
        Args:
            play: Play объект
        
        Returns:
            Словарь для YAML (play entry)
        """
        try:
            play_dict = {}
            
            # name (всегда первый)
            name = play.get('name', 'Unnamed Play')
            play_dict['name'] = name
            
            # hosts (обязательное) - поддерживаем и строку, и массив
            hosts = play.get('hosts', [])
            if isinstance(hosts, list):
                # Если массив, объединяем через запятую или оставляем как список (Ansible поддерживает оба формата)
                if len(hosts) == 0:
                    # Пустой массив - не добавляем hosts (Ansible требует hosts, но это будет ошибка валидации)
                    play_dict['hosts'] = []
                elif len(hosts) == 1:
                    play_dict['hosts'] = hosts[0]
                elif len(hosts) > 1:
                    # Можно использовать список или строку с запятыми
                    # Используем список для лучшей читаемости
                    play_dict['hosts'] = hosts
            else:
                # Если это строка, используем как есть (но не 'all' по умолчанию)
                play_dict['hosts'] = hosts if hosts else []
            
            # remote_user (опционально)
            remote_user = play.get('remote_user')
            if remote_user:
                play_dict['remote_user'] = remote_user
            
            # become (только если True)
            become = play.get('become', False)
            if become:
                play_dict['become'] = True
            
            # strategy (только если не 'linear')
            strategy = play.get('strategy', 'linear')
            if strategy and strategy != 'linear':
                play_dict['strategy'] = strategy
            
            # pre_tasks (опционально)
            pre_tasks = play.get('pre_tasks', [])
            if pre_tasks:
                play_dict['pre_tasks'] = [self._generate_task(task) for task in pre_tasks]
            
            # roles (обязательное, но может быть пустым)
            roles = play.get('roles', [])
            if roles:
                play_dict['roles'] = [self._generate_role(role) for role in roles]
            else:
                play_dict['roles'] = []
            
            # post_tasks (опционально)
            post_tasks = play.get('post_tasks', [])
            if post_tasks:
                play_dict['post_tasks'] = [self._generate_task(task) for task in post_tasks]
            
            # vars_files (опционально)
            vars_files = play.get('vars_files', [])
            if vars_files and isinstance(vars_files, list):
                play_dict['vars_files'] = [p for p in vars_files if p and isinstance(p, str)]
            
            return play_dict
            
        except Exception as e:
            logger.error(f"Error generating play dict: {e}")
            raise
    
    def _generate_role(self, role: Dict[str, Any]) -> Dict[str, Any]:
        """
        Генерировать YAML структуру для role
        
        Args:
            role: Role объект
        
        Returns:
            Словарь для YAML (role entry)
        """
        try:
            role_dict = {}
            
            # role (обязательное)
            role_name = role.get('role_name', '')
            if not role_name:
                logger.warning("Role missing 'role_name', skipping")
                return {}
            
            role_dict['role'] = role_name
            
            # tags (всегда включаем, даже если пустой)
            tag = role.get('tag', '')
            if tag:
                # Если tag не пустой, используем массив
                role_dict['tags'] = [tag] if isinstance(tag, str) else tag
            else:
                # Пустой tag - используем пустой массив
                role_dict['tags'] = []
            
            # vars (опционально)
            vars_override = role.get('vars_override')
            if vars_override and isinstance(vars_override, dict) and vars_override:
                role_dict['vars'] = vars_override
            
            # auto игнорируется (все roles рендерятся явно)
            
            return role_dict
            
        except Exception as e:
            logger.error(f"Error generating role YAML: {e}")
            return {}
    
    def _generate_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        Генерировать YAML структуру для inline task
        
        Args:
            task: InlineTask объект
        
        Returns:
            Словарь для YAML (task entry)
        """
        try:
            # Если есть yaml_override, используем его (для advanced mode)
            yaml_override = task.get('yaml_override')
            if yaml_override and yaml_override.strip():
                # Парсим YAML override
                try:
                    parsed = yaml.safe_load(yaml_override)
                    if isinstance(parsed, dict):
                        return parsed
                    elif isinstance(parsed, list) and len(parsed) > 0:
                        return parsed[0]
                except Exception as e:
                    logger.warning(f"Failed to parse yaml_override, using regular fields: {e}")
            
            # Обычная генерация из полей
            task_dict = {}
            
            # name (обязательное)
            name = task.get('name', '')
            if name:
                task_dict['name'] = name
            
            # module (обязательное) - может быть как отдельное поле или как ключ в args
            module = task.get('module', '')
            if not module:
                logger.warning("Task missing 'module', skipping")
                return {}
            
            # args (модуль и его аргументы)
            args = task.get('args', {})
            if isinstance(args, dict):
                # Модуль может быть указан как ключ в args или отдельным полем
                # В Ansible task формат: module_name: { arg1: val1, arg2: val2 }
                task_dict[module] = args if args else {}
            else:
                task_dict[module] = args
            
            # when (опционально)
            when = task.get('when')
            if when:
                task_dict['when'] = when
            
            # tags (опционально)
            tags = task.get('tags', [])
            if tags:
                task_dict['tags'] = tags if isinstance(tags, list) else [tags]
            
            # become (опционально)
            become = task.get('become', False)
            if become:
                task_dict['become'] = True
            
            return task_dict
            
        except Exception as e:
            logger.error(f"Error generating task YAML: {e}")
            return {}