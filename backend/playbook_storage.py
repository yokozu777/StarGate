#!/usr/bin/env python3
"""
Хранилище playbooks для проектов.
Playbooks хранятся в JSON формате в директории каждого проекта.
Также сохраняются в YAML формате в repo/playbooks/ для синхронизации с Git.
"""
import json
import uuid
import time
import re
from pathlib import Path
from typing import List, Dict, Optional, Any
import logging

logger = logging.getLogger(__name__)

# Импортируем генератор и парсер для работы с YAML
try:
    from .playbook_generator import PlaybookGenerator
    from .playbook_parser import PlaybookParser, PlaybookParseError
except ImportError:
    from playbook_generator import PlaybookGenerator
    from playbook_parser import PlaybookParser, PlaybookParseError


class PlaybookStorage:
    """Класс для работы с хранилищем playbooks"""
    
    def __init__(self, projects_dir: Path):
        """
        Args:
            projects_dir: Базовая директория проектов (например, projects/)
        """
        self.projects_dir = projects_dir
        self.playbook_generator = PlaybookGenerator()
        self.playbook_parser = PlaybookParser()
    
    def _sanitize_filename(self, name: str) -> str:
        """Преобразовать имя playbook в безопасное имя файла"""
        # Удаляем недопустимые символы для имени файла
        name = re.sub(r'[<>:"/\\|?*]', '_', name)
        # Удаляем пробелы в начале и конце
        name = name.strip()
        # Заменяем множественные пробелы на один
        name = re.sub(r'\s+', '_', name)
        # Если имя пустое, используем дефолтное
        if not name:
            name = 'playbook'
        return name
    
    def get_repo_playbooks_dir(self, project_id: str) -> Path:
        """Возвращает директорию для playbooks в repo/ (для YAML файлов)"""
        project_dir = self.projects_dir / project_id
        repo_playbooks_dir = project_dir / 'repo' / 'playbooks'
        repo_playbooks_dir.mkdir(parents=True, exist_ok=True)
        return repo_playbooks_dir
    
    def get_repo_playbook_file(self, project_id: str, playbook_name: str) -> Path:
        """Возвращает путь к YAML файлу playbook в repo/playbooks/"""
        repo_playbooks_dir = self.get_repo_playbooks_dir(project_id)
        sanitized_name = self._sanitize_filename(playbook_name)
        return repo_playbooks_dir / f'{sanitized_name}.yml'
    
    def get_playbooks_dir(self, project_id: str) -> Path:
        """Возвращает директорию для playbooks проекта (новая структура: ui/playbooks/)"""
        project_dir = self.projects_dir / project_id
        # Новая структура: ui/playbooks/
        playbooks_dir = project_dir / 'ui' / 'playbooks'
        playbooks_dir.mkdir(parents=True, exist_ok=True)
        return playbooks_dir
    
    def get_playbook_file(self, project_id: str, playbook_id: str) -> Path:
        """Возвращает путь к файлу playbook"""
        playbooks_dir = self.get_playbooks_dir(project_id)
        return playbooks_dir / f'{playbook_id}.json'
    
    def list_playbooks(self, project_id: str) -> List[Dict[str, Any]]:
        """
        Получить список всех playbooks проекта
        Читает как из JSON файлов (ui/playbooks/), так и из YAML файлов (repo/playbooks/)
        
        Returns:
            Список метаданных playbooks (id, name, description, plays_count, created_at, updated_at)
        """
        try:
            playbooks = []
            playbook_ids_seen = set()
            
            # Сначала читаем из JSON файлов (ui/playbooks/)
            playbooks_dir = self.get_playbooks_dir(project_id)
            if playbooks_dir.exists():
                for playbook_file in playbooks_dir.glob('*.json'):
                    try:
                        with open(playbook_file, 'r', encoding='utf-8') as f:
                            playbook = json.load(f)
                        
                        # Извлекаем метаданные
                        playbook_id = playbook_file.stem
                        plays_count = len(playbook.get('plays', []))
                        
                        metadata = playbook.get('metadata', {})
                        
                        # Получаем теги - поддерживаем как массив, так и строку (для обратной совместимости)
                        tags = []
                        if playbook.get('tags') and isinstance(playbook.get('tags'), list):
                            tags = playbook.get('tags')
                        elif metadata.get('tags') and isinstance(metadata.get('tags'), list):
                            tags = metadata.get('tags')
                        elif playbook.get('tag') or metadata.get('tag'):
                            # Старый формат - строка, конвертируем в массив
                            tag_str = playbook.get('tag') or metadata.get('tag', '')
                            if tag_str:
                                tags = [t.strip() for t in str(tag_str).split(',') if t.strip()]
                        
                        # Получаем disabled статус
                        disabled = playbook.get('disabled') or metadata.get('disabled', False)
                        
                        playbooks.append({
                            'id': playbook_id,
                            'project_id': project_id,
                            'name': playbook.get('name', 'Unnamed Playbook'),
                            'description': playbook.get('description', ''),
                            'plays_count': plays_count,
                            'created_at': metadata.get('created_at'),
                            'updated_at': metadata.get('updated_at'),
                            'version': metadata.get('version', 1),
                            'tags': tags,
                            'tag': ', '.join(tags) if tags else '',  # Для обратной совместимости
                            'disabled': disabled,
                            'metadata': metadata  # Включаем полные метаданные
                        })
                        playbook_ids_seen.add(playbook_id)
                    except Exception as e:
                        logger.error(f"Error reading playbook {playbook_file}: {e}")
                        continue
            
            # Затем читаем из YAML файлов (repo/playbooks/)
            repo_playbooks_dir = self.get_repo_playbooks_dir(project_id)
            if repo_playbooks_dir.exists():
                for yaml_file in repo_playbooks_dir.glob('*.yml'):
                    try:
                        # Пропускаем файлы, которые уже есть в JSON (по имени)
                        yaml_name = yaml_file.stem
                        
                        # Читаем YAML и парсим в JSON модель
                        with open(yaml_file, 'r', encoding='utf-8') as f:
                            yaml_content = f.read()
                        
                        if not yaml_content.strip():
                            continue
                        
                        # Парсим YAML в JSON модель
                        try:
                            playbook = self.playbook_parser.parse(yaml_content, yaml_name)
                        except PlaybookParseError as e:
                            logger.warning(f"Error parsing YAML playbook {yaml_file}: {e}")
                            continue
                        
                        # Проверяем, есть ли уже такой playbook в JSON (по имени)
                        playbook_name = playbook.get('name', yaml_name)
                        existing_playbook = next(
                            (p for p in playbooks if p.get('name') == playbook_name),
                            None
                        )
                        
                        if existing_playbook:
                            # Если уже есть в JSON, пропускаем (JSON имеет приоритет)
                            continue
                        
                        # Создаем ID на основе имени файла (детерминированный)
                        playbook_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"playbook:{project_id}:{playbook_name}"))
                        
                        # Извлекаем метаданные
                        plays_count = len(playbook.get('plays', []))
                        metadata = playbook.get('metadata', {})
                        
                        # Получаем теги
                        tags = []
                        if playbook.get('tags') and isinstance(playbook.get('tags'), list):
                            tags = playbook.get('tags')
                        elif metadata.get('tags') and isinstance(metadata.get('tags'), list):
                            tags = metadata.get('tags')
                        
                        # Получаем disabled статус
                        disabled = playbook.get('disabled') or metadata.get('disabled', False)
                        
                        # Используем время модификации файла как updated_at
                        file_mtime = yaml_file.stat().st_mtime
                        updated_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(file_mtime))
                        created_at = updated_at  # Если нет метаданных, используем время файла
                        
                        playbooks.append({
                            'id': playbook_id,
                            'project_id': project_id,
                            'name': playbook_name,
                            'description': playbook.get('description', ''),
                            'plays_count': plays_count,
                            'created_at': metadata.get('created_at', created_at),
                            'updated_at': metadata.get('updated_at', updated_at),
                            'version': metadata.get('version', 1),
                            'tags': tags,
                            'tag': ', '.join(tags) if tags else '',
                            'disabled': disabled,
                            'metadata': metadata,
                            '_yaml_file': str(yaml_file)  # Помечаем, что это из YAML
                        })
                    except Exception as e:
                        logger.error(f"Error reading YAML playbook {yaml_file}: {e}")
                        continue
            
            # Сортировка: сначала по order (если есть), потом по updated_at (новые первыми)
            # Разделяем playbooks на две группы: с order и без order
            playbooks_with_order = []
            playbooks_without_order = []
            
            for playbook in playbooks:
                order = playbook.get('metadata', {}).get('order')
                if order is not None:
                    playbooks_with_order.append(playbook)
                else:
                    playbooks_without_order.append(playbook)
            
            # Сортируем playbooks с order по значению order (по возрастанию)
            playbooks_with_order.sort(key=lambda x: x.get('metadata', {}).get('order', 0))
            
            # Сортируем playbooks без order по updated_at (новые первыми)
            playbooks_without_order.sort(key=lambda x: x.get('updated_at') or '', reverse=True)
            
            # Объединяем: сначала playbooks с order, потом без order
            playbooks = playbooks_with_order + playbooks_without_order
            return playbooks
            
        except Exception as e:
            logger.error(f"Error listing playbooks for project {project_id}: {e}")
            return []
    
    def get_playbook(self, project_id: str, playbook_id: str) -> Optional[Dict[str, Any]]:
        """
        Получить playbook по ID
        Сначала ищет в JSON файлах, затем в YAML файлах
        
        Returns:
            Playbook объект или None если не найден
        """
        try:
            # Сначала ищем в JSON файлах
            playbook_file = self.get_playbook_file(project_id, playbook_id)
            
            if playbook_file.exists():
                with open(playbook_file, 'r', encoding='utf-8') as f:
                    playbook = json.load(f)
                
                # Убеждаемся что id установлен
                playbook['id'] = playbook_id
                playbook['project_id'] = project_id
                
                return playbook
            
            # Если не найден в JSON, ищем в YAML файлах по имени
            # Для этого нужно получить список всех playbooks и найти по ID
            all_playbooks = self.list_playbooks(project_id)
            playbook_meta = next((p for p in all_playbooks if p.get('id') == playbook_id), None)
            
            if playbook_meta and playbook_meta.get('_yaml_file'):
                # Загружаем из YAML файла
                yaml_file = Path(playbook_meta['_yaml_file'])
                if yaml_file.exists():
                    with open(yaml_file, 'r', encoding='utf-8') as f:
                        yaml_content = f.read()
                    
                    # Парсим YAML в JSON модель
                    playbook = self.playbook_parser.parse(yaml_content, playbook_meta.get('name'))
                    playbook['id'] = playbook_id
                    playbook['project_id'] = project_id
                    
                    return playbook
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting playbook {playbook_id} for project {project_id}: {e}")
            return None
    
    def create_playbook(self, project_id: str, name: str, description: str = '') -> Optional[Dict[str, Any]]:
        """
        Создать новый пустой playbook
        
        Returns:
            Созданный playbook объект или None при ошибке
        """
        try:
            playbook_id = str(uuid.uuid4())
            current_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            
            playbook = {
                'id': playbook_id,
                'project_id': project_id,
                'name': name,
                'description': description,
                'plays': [],
                'metadata': {
                    'created_at': current_time,
                    'updated_at': current_time,
                    'version': 1
                }
            }
            
            playbook_file = self.get_playbook_file(project_id, playbook_id)
            with open(playbook_file, 'w', encoding='utf-8') as f:
                json.dump(playbook, f, indent=2, ensure_ascii=False)
            
            # Также сохраняем в YAML формате в repo/playbooks/
            self._save_playbook_yaml(project_id, playbook)
            
            logger.info(f"Created playbook {playbook_id} for project {project_id}")
            return playbook
            
        except Exception as e:
            logger.error(f"Error creating playbook for project {project_id}: {e}")
            return None
    
    def save_playbook(self, project_id: str, playbook: Dict[str, Any]) -> bool:
        """
        Сохранить playbook
        
        Args:
            project_id: ID проекта
            playbook: Playbook объект (должен содержать 'id')
        
        Returns:
            True если успешно, False при ошибке
        """
        try:
            playbook_id = playbook.get('id')
            if not playbook_id:
                logger.error("Playbook must have 'id' field")
                return False
            
            # Обновляем метаданные
            current_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            if 'metadata' not in playbook:
                playbook['metadata'] = {}
            
            if 'created_at' not in playbook['metadata']:
                playbook['metadata']['created_at'] = current_time
            
            playbook['metadata']['updated_at'] = current_time
            playbook['metadata']['version'] = playbook['metadata'].get('version', 1) + 1
            
            # Убеждаемся что project_id установлен
            playbook['project_id'] = project_id
            
            playbook_file = self.get_playbook_file(project_id, playbook_id)
            with open(playbook_file, 'w', encoding='utf-8') as f:
                json.dump(playbook, f, indent=2, ensure_ascii=False)
            
            # Также сохраняем в YAML формате в repo/playbooks/
            self._save_playbook_yaml(project_id, playbook)
            
            logger.info(f"Saved playbook {playbook_id} for project {project_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error saving playbook {playbook.get('id')} for project {project_id}: {e}")
            return False
    
    def _save_playbook_yaml(self, project_id: str, playbook: Dict[str, Any]) -> bool:
        """
        Сохранить playbook в YAML формате в repo/playbooks/
        
        Args:
            project_id: ID проекта
            playbook: Playbook объект
        
        Returns:
            True если успешно, False при ошибке
        """
        try:
            playbook_name = playbook.get('name', 'Unnamed Playbook')
            yaml_file = self.get_repo_playbook_file(project_id, playbook_name)
            
            # Генерируем YAML из JSON модели
            yaml_content = self.playbook_generator.generate(playbook)
            
            # Сохраняем YAML файл
            yaml_file.parent.mkdir(parents=True, exist_ok=True)
            with open(yaml_file, 'w', encoding='utf-8') as f:
                f.write(yaml_content)
            
            logger.info(f"Saved playbook YAML {playbook_name} to {yaml_file}")
            return True
        except Exception as e:
            logger.error(f"Error saving playbook YAML for project {project_id}: {e}")
            return False
            
        except Exception as e:
            logger.error(f"Error saving playbook {playbook.get('id')} for project {project_id}: {e}")
            return False
    
    def delete_playbook(self, project_id: str, playbook_id: str) -> bool:
        """
        Удалить playbook
        
        Returns:
            True если успешно, False при ошибке
        """
        try:
            playbook_file = self.get_playbook_file(project_id, playbook_id)
            
            if not playbook_file.exists():
                logger.warning(f"Playbook {playbook_id} not found for project {project_id}")
                return False
            
            playbook_file.unlink()
            
            # Также удаляем YAML файл, если существует
            playbook = self.get_playbook(project_id, playbook_id)
            if playbook:
                playbook_name = playbook.get('name', '')
                if playbook_name:
                    yaml_file = self.get_repo_playbook_file(project_id, playbook_name)
                    if yaml_file.exists():
                        yaml_file.unlink()
                        logger.info(f"Deleted playbook YAML {playbook_name} from repo/playbooks/")
            
            logger.info(f"Deleted playbook {playbook_id} for project {project_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting playbook {playbook_id} for project {project_id}: {e}")
            return False
    
    def clone_playbook(self, project_id: str, source_playbook_id: str, new_name: str, new_description: str = '') -> Optional[Dict[str, Any]]:
        """
        Клонировать playbook
        
        Returns:
            Новый playbook объект или None при ошибке
        """
        try:
            source_playbook = self.get_playbook(project_id, source_playbook_id)
            if not source_playbook:
                return None
            
            # Создаем копию
            import copy
            new_playbook = copy.deepcopy(source_playbook)
            
            # Генерируем новый ID
            new_playbook_id = str(uuid.uuid4())
            new_playbook['id'] = new_playbook_id
            
            # Обновляем имя и описание
            new_playbook['name'] = new_name
            new_playbook['description'] = new_description
            
            # Генерируем новые ID для всех Plays
            for play in new_playbook.get('plays', []):
                play['id'] = str(uuid.uuid4())
            
            # Обновляем метаданные
            current_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            new_playbook['metadata'] = {
                'created_at': current_time,
                'updated_at': current_time,
                'version': 1,
                'cloned_from': source_playbook_id
            }
            
            # Сохраняем новый playbook
            if self.save_playbook(project_id, new_playbook):
                return new_playbook
            else:
                return None
                
        except Exception as e:
            logger.error(f"Error cloning playbook {source_playbook_id} for project {project_id}: {e}")
            return None
    
    def playbook_exists(self, project_id: str, playbook_id: str) -> bool:
        """Проверить существование playbook"""
        playbook_file = self.get_playbook_file(project_id, playbook_id)
        return playbook_file.exists()
    
    def check_name_conflict(self, project_id: str, name: str, exclude_playbook_id: Optional[str] = None) -> bool:
        """
        Проверить конфликт имени playbook
        
        Returns:
            True если имя уже используется, False если свободно
        """
        playbooks = self.list_playbooks(project_id)
        for playbook in playbooks:
            if playbook['name'] == name:
                if exclude_playbook_id and playbook['id'] == exclude_playbook_id:
                    continue
                return True
        return False
    
    def get_schedule(self, project_id: str, playbook_id: str) -> Optional[Dict[str, Any]]:
        """
        Получить schedule для playbook
        
        Returns:
            Schedule dict с полями: enabled, cron, timezone
            None если schedule не настроен
        """
        try:
            playbook = self.get_playbook(project_id, playbook_id)
            if not playbook:
                return None
            
            # Schedule хранится в metadata
            metadata = playbook.get('metadata', {})
            schedule = metadata.get('schedule')
            
            if not schedule or not schedule.get('enabled', False):
                return None
            
            return {
                'enabled': schedule.get('enabled', False),
                'cron': schedule.get('cron', ''),
                'timezone': schedule.get('timezone', 'UTC')
            }
        except Exception as e:
            logger.error(f"Error getting schedule for playbook {playbook_id} in project {project_id}: {e}")
            return None
    
    def save_schedule(self, project_id: str, playbook_id: str, schedule: Dict[str, Any]) -> bool:
        """
        Сохранить schedule для playbook
        
        Args:
            project_id: ID проекта
            playbook_id: ID playbook
            schedule: Dict с полями enabled, cron, timezone
        
        Returns:
            True если успешно сохранено
        """
        try:
            playbook = self.get_playbook(project_id, playbook_id)
            if not playbook:
                logger.error(f"Playbook {playbook_id} not found in project {project_id}")
                return False
            
            # Инициализируем metadata если нет
            if 'metadata' not in playbook:
                playbook['metadata'] = {}
            
            # Сохраняем schedule в metadata
            playbook['metadata']['schedule'] = {
                'enabled': schedule.get('enabled', False),
                'cron': schedule.get('cron', ''),
                'timezone': schedule.get('timezone', 'UTC'),
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            }
            
            # Сохраняем playbook
            return self.save_playbook(project_id, playbook)
        except Exception as e:
            logger.error(f"Error saving schedule for playbook {playbook_id} in project {project_id}: {e}")
            return False
    
    def delete_schedule(self, project_id: str, playbook_id: str) -> bool:
        """
        Удалить schedule для playbook (отключить)
        
        Returns:
            True если успешно удалено
        """
        try:
            playbook = self.get_playbook(project_id, playbook_id)
            if not playbook:
                logger.error(f"Playbook {playbook_id} not found in project {project_id}")
                return False
            
            # Инициализируем metadata если нет
            if 'metadata' not in playbook:
                playbook['metadata'] = {}
            
            # Удаляем schedule (или устанавливаем enabled=False)
            if 'schedule' in playbook['metadata']:
                playbook['metadata']['schedule'] = {
                    'enabled': False,
                    'cron': playbook['metadata']['schedule'].get('cron', ''),
                    'timezone': playbook['metadata']['schedule'].get('timezone', 'UTC'),
                    'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                }
            
            # Сохраняем playbook
            return self.save_playbook(project_id, playbook)
        except Exception as e:
            logger.error(f"Error deleting schedule for playbook {playbook_id} in project {project_id}: {e}")
            return False
    
    def list_all_schedules(self) -> List[Dict[str, Any]]:
        """
        Получить список всех enabled schedules для всех проектов
        Используется scheduler service для polling
        
        Returns:
            Список dict с полями: project_id, playbook_id, schedule
        """
        schedules = []
        try:
            if not self.projects_dir.exists():
                return schedules
            
            # Итерируемся по всем проектам
            for project_dir in self.projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                
                project_id = project_dir.name
                
                try:
                    # Получаем список playbooks проекта
                    playbooks = self.list_playbooks(project_id)
                    
                    for playbook in playbooks:
                        playbook_id = playbook['id']
                        
                        # Skip disabled playbooks - scheduler should not run them
                        if playbook.get('disabled') or playbook.get('metadata', {}).get('disabled'):
                            continue
                        
                        schedule = self.get_schedule(project_id, playbook_id)
                        
                        if schedule and schedule.get('enabled', False):
                            schedules.append({
                                'project_id': project_id,
                                'playbook_id': playbook_id,
                                'schedule': schedule
                            })
                except Exception as e:
                    logger.error(f"Error listing schedules for project {project_id}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error listing all schedules: {e}")
        
        return schedules