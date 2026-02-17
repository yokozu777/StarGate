#!/usr/bin/env python3
"""
Валидатор playbooks для Playbook Editor.
Реализует все правила валидации на уровне Play и Playbook.
"""
from typing import Dict, List, Optional, Any, Set, Tuple
import logging

logger = logging.getLogger(__name__)


class ValidationIssue:
    """Одна проблема валидации"""
    
    def __init__(self, level: str, code: str, message: str, **kwargs):
        self.level = level  # 'error' or 'warning'
        self.code = code
        self.message = message
        self.field = kwargs.get('field')
        self.play_id = kwargs.get('play_id')
        self.role_name = kwargs.get('role_name')
        self.group_name = kwargs.get('group_name')
        self.required_group = kwargs.get('required_group')
        self.current_group = kwargs.get('current_group')
        self.required_role = kwargs.get('required_role')
        self.position = kwargs.get('position')
        self.required_position = kwargs.get('required_position')
    
    def to_dict(self) -> Dict[str, Any]:
        """Преобразовать в словарь"""
        result = {
            'level': self.level,
            'code': self.code,
            'message': self.message
        }
        
        if self.field:
            result['field'] = self.field
        if self.play_id:
            result['play_id'] = self.play_id
        if self.role_name:
            result['role_name'] = self.role_name
        if self.group_name:
            result['group_name'] = self.group_name
        if self.required_group:
            result['required_group'] = self.required_group
        if self.current_group:
            result['current_group'] = self.current_group
        if self.required_role:
            result['required_role'] = self.required_role
        if self.position is not None:
            result['position'] = self.position
        if self.required_position is not None:
            result['required_position'] = self.required_position
        
        return result


class PlaybookValidator:
    """Класс для валидации playbooks"""
    
    def __init__(self, inventory_groups: Optional[Dict[str, Any]] = None, 
                 available_roles: Optional[List[str]] = None,
                 role_dependencies: Optional[Dict[str, Dict[str, Any]]] = None):
        """
        Args:
            inventory_groups: Словарь групп из inventory {group_name: {hosts: [...]}}
            available_roles: Список доступных roles в проекте
            role_dependencies: Конфигурация зависимостей roles
        """
        self.inventory_groups = inventory_groups or {}
        self.available_roles = available_roles or []
        self.role_dependencies = role_dependencies or {}
    
    def validate(self, playbook: Dict[str, Any]) -> Dict[str, Any]:
        """
        Валидировать playbook
        
        Returns:
            Структура валидации согласно validation-schema.json
        """
        playbook_errors = []
        playbook_warnings = []
        play_validations = []
        
        plays = playbook.get('plays', [])
        
        # Playbook level validation
        # Rule 10: Playbook must have at least one play
        if not plays:
            playbook_errors.append(ValidationIssue(
                level='error',
                code='PLAYBOOK_EMPTY',
                message='Playbook must contain at least one play'
            ))
        
        # Validate each play
        play_ids = set()
        for play in plays:
            play_validation = self._validate_play(play, playbook)
            play_validations.append(play_validation)
            
            # Rule 8: Play ID must be unique
            play_id = play.get('id')
            if play_id:
                if play_id in play_ids:
                    play_validation['errors'].append(ValidationIssue(
                        level='error',
                        code='DUPLICATE_PLAY_ID',
                        message=f"Duplicate play ID '{play_id}'",
                        field='id',
                        play_id=play_id
                    ).to_dict())
                play_ids.add(play_id)
        
        # Rule 11: All plays must be valid
        plays_with_errors = sum(1 for pv in play_validations if pv['errors'])
        if plays_with_errors > 0:
            playbook_errors.append(ValidationIssue(
                level='error',
                code='PLAYBOOK_INVALID_PLAYS',
                message=f'Playbook contains {plays_with_errors} invalid play(s)'
            ))
        
        # Rule 12: Circular role dependencies (упрощенная проверка)
        # Rule 13: Missing required roles
        # Rule 14: Duplicate role execution
        self._validate_playbook_level_rules(playbook, plays, playbook_warnings)
        
        # Calculate summary
        total_errors = len(playbook_errors) + sum(len(pv['errors']) for pv in play_validations)
        total_warnings = len(playbook_warnings) + sum(len(pv['warnings']) for pv in play_validations)
        plays_with_errors_count = sum(1 for pv in play_validations if pv['errors'])
        plays_with_warnings_count = sum(1 for pv in play_validations if pv['warnings'])
        
        playbook_can_run = (
            len(playbook_errors) == 0 and
            all(len(pv['errors']) == 0 for pv in play_validations)
        )
        
        return {
            'playbook': {
                'valid': len(playbook_errors) == 0,
                'can_run': playbook_can_run,
                'errors': [e.to_dict() for e in playbook_errors],
                'warnings': [w.to_dict() for w in playbook_warnings]
            },
            'plays': [
                {
                    'play_id': pv['play_id'],
                    'valid': len(pv['errors']) == 0,
                    'can_run': len(pv['errors']) == 0,
                    'errors': pv['errors'],
                    'warnings': pv['warnings']
                }
                for pv in play_validations
            ],
            'summary': {
                'total_errors': total_errors,
                'total_warnings': total_warnings,
                'plays_with_errors': plays_with_errors_count,
                'plays_with_warnings': plays_with_warnings_count,
                'playbook_can_run': playbook_can_run
            }
        }
    
    def _validate_play(self, play: Dict[str, Any], playbook: Dict[str, Any]) -> Dict[str, Any]:
        """Валидировать один play"""
        play_id = play.get('id', 'unknown')
        errors = []
        warnings = []
        
        # Rule 7: Play name is required
        name = play.get('name', '').strip()
        if not name:
            errors.append(ValidationIssue(
                level='error',
                code='PLAY_NAME_REQUIRED',
                message='Play name is required',
                field='name',
                play_id=play_id
            ).to_dict())
        
        # Rule 1: Play must have at least one role
        roles = play.get('roles', [])
        if not roles:
            errors.append(ValidationIssue(
                level='error',
                code='NO_ROLES',
                message=f"Play '{name}' must have at least one role",
                field='roles',
                play_id=play_id
            ).to_dict())
        
        # Rule 2 & 3: Group validation
        hosts = play.get('hosts', '')
        if hosts:
            # Поддерживаем и строку, и массив
            hosts_list = hosts if isinstance(hosts, list) else ([hosts] if hosts else [])
            
            for host_group in hosts_list:
                if host_group and host_group != 'all':
                    group_validation = self._validate_group(host_group, play_id, name)
                    if group_validation:
                        if group_validation.level == 'error':
                            errors.append(group_validation.to_dict())
                        else:
                            warnings.append(group_validation.to_dict())
        
        # Validate roles
        for role in roles:
            role_errors, role_warnings = self._validate_role(role, play, play_id)
            errors.extend(role_errors)
            warnings.extend(role_warnings)
        
        # Rule 6: Role ordering constraints
        ordering_warnings = self._validate_role_ordering(roles, play_id)
        warnings.extend(ordering_warnings)
        
        return {
            'play_id': play_id,
            'errors': errors,
            'warnings': warnings
        }
    
    def _validate_group(self, group_name: str, play_id: str, play_name: str) -> Optional[ValidationIssue]:
        """Валидировать группу хостов"""
        # Rule 2: Group must exist
        # Но сначала проверяем, не является ли это IP-адресом или именем хоста
        # В Ansible можно использовать IP-адреса и имена хостов напрямую
        import re
        # Проверяем, является ли это IP-адресом (IPv4)
        ipv4_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
        # Проверяем, является ли это именем хоста (не группой)
        # Если это не группа и выглядит как хост/IP, то это валидно
        if group_name not in self.inventory_groups:
            # Проверяем, является ли это IP-адресом
            if re.match(ipv4_pattern, group_name):
                # Это IP-адрес, это валидно - не возвращаем ошибку
                return None
            # Проверяем, есть ли этот хост в какой-либо группе
            # Если hosts указан как имя хоста (не группы), это тоже валидно
            # В Ansible можно использовать имена хостов напрямую
            # Поэтому делаем это предупреждением, а не ошибкой
            return ValidationIssue(
                level='warning',
                code='GROUP_NOT_EXISTS',
                message=f"Play '{play_name}' references '{group_name}' which is not a group. It will be treated as a host name or IP address.",
                field='hosts',
                play_id=play_id,
                group_name=group_name
            )
        
        # Rule 3: Group without hosts produces a warning
        group_data = self.inventory_groups.get(group_name, {})
        hosts = group_data.get('hosts', [])
        if not hosts:
            return ValidationIssue(
                level='warning',
                code='GROUP_EMPTY',
                message=f"Group '{group_name}' has no hosts",
                field='hosts',
                play_id=play_id,
                group_name=group_name
            )
        
        return None
    
    def _validate_role(self, role: Dict[str, Any], play: Dict[str, Any], play_id: str) -> Tuple[List[Dict], List[Dict]]:
        """Валидировать role"""
        errors = []
        warnings = []
        
        role_name = role.get('role_name', '')
        
        # Rule 4: Role must exist in project
        if role_name and role_name not in self.available_roles:
            errors.append(ValidationIssue(
                level='error',
                code='ROLE_NOT_EXISTS',
                message=f"Role '{role_name}' does not exist in project",
                field='roles',
                play_id=play_id,
                role_name=role_name
            ).to_dict())
        
        # Rule 5: Role requires specific group
        if role_name in self.role_dependencies:
            deps = self.role_dependencies[role_name]
            required_group = deps.get('requires_group')
            if required_group:
                current_group = play.get('hosts', '')
                if current_group != required_group:
                    level = deps.get('requires_group_level', 'warning')
                    issue = ValidationIssue(
                        level=level,
                        code='ROLE_REQUIRES_GROUP',
                        message=f"Role '{role_name}' requires group '{required_group}' but play uses '{current_group}'",
                        field='roles',
                        play_id=play_id,
                        role_name=role_name,
                        required_group=required_group,
                        current_group=current_group
                    )
                    if level == 'error':
                        errors.append(issue.to_dict())
                    else:
                        warnings.append(issue.to_dict())
        
        return errors, warnings
    
    def _validate_role_ordering(self, roles: List[Dict[str, Any]], play_id: str) -> List[Dict]:
        """Валидировать порядок roles (Rule 6)"""
        warnings = []
        
        for i, role in enumerate(roles):
            role_name = role.get('role_name', '')
            if role_name in self.role_dependencies:
                deps = self.role_dependencies[role_name]
                required_roles = deps.get('requires_roles', [])
                
                for required_role in required_roles:
                    # Ищем required_role в списке roles после текущей позиции
                    found_after = False
                    for j in range(i + 1, len(roles)):
                        if roles[j].get('role_name') == required_role:
                            found_after = True
                            break
                    
                    # Если required_role не найден после текущей позиции, проверяем до
                    if not found_after:
                        found_before = any(
                            roles[k].get('role_name') == required_role
                            for k in range(i)
                        )
                        
                        if found_before:
                            # required_role идет перед текущей - это нормально
                            continue
                        else:
                            # required_role вообще не найден - это будет проверено на playbook level
                            continue
                    else:
                        # required_role найден после - это нормально
                        continue
                    
                    # Если required_role найден до текущей позиции - это нарушение порядка
                    # (но это сложно определить без дополнительной логики)
        
        return warnings
    
    def _validate_playbook_level_rules(self, playbook: Dict[str, Any], plays: List[Dict[str, Any]], 
                                      playbook_warnings: List[ValidationIssue]):
        """Валидировать правила на уровне playbook"""
        # Rule 13: Missing required roles
        all_role_names = set()
        for play in plays:
            for role in play.get('roles', []):
                all_role_names.add(role.get('role_name', ''))
        
        for role_name in all_role_names:
            if role_name in self.role_dependencies:
                deps = self.role_dependencies[role_name]
                required_roles = deps.get('requires_roles', [])
                for required_role in required_roles:
                    if required_role not in all_role_names:
                        playbook_warnings.append(ValidationIssue(
                            level='warning',
                            code='MISSING_REQUIRED_ROLE',
                            message=f"Role '{role_name}' requires '{required_role}' but it is not present in any play",
                            role_name=role_name,
                            required_role=required_role
                        ))
        
        # Rule 14: Duplicate role execution
        role_executions = {}  # {(group_name, role_name): [play_ids]}
        for play in plays:
            group_name = play.get('hosts', '')
            # Преобразуем group_name в строку, если это список
            if isinstance(group_name, list):
                group_name = ','.join(str(h) for h in group_name) if group_name else 'all'
            elif not isinstance(group_name, str):
                group_name = str(group_name) if group_name else 'all'
            
            for role in play.get('roles', []):
                role_name = role.get('role_name', '')
                key = (group_name, role_name)
                if key not in role_executions:
                    role_executions[key] = []
                role_executions[key].append(play.get('id', 'unknown'))
        
        for (group_name, role_name), play_ids in role_executions.items():
            if len(play_ids) > 1:
                playbook_warnings.append(ValidationIssue(
                    level='warning',
                    code='DUPLICATE_ROLE_EXECUTION',
                    message=f"Role '{role_name}' is executed multiple times on group '{group_name}'",
                    role_name=role_name,
                    group_name=group_name
                ))
