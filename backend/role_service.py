#!/usr/bin/env python3
"""
Сервис для управления ролями и правами доступа
"""
import uuid
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

try:
    from .auth_models import Role, Permission
except ImportError:
    from auth_models import Role, Permission

logger = logging.getLogger(__name__)


class RoleService:
    """Сервис для работы с ролями"""
    
    def __init__(self, data_dir: Path):
        """
        Инициализация сервиса
        
        Args:
            data_dir: Директория для хранения данных
        """
        self.data_dir = data_dir
        self.auth_dir = data_dir / 'auth'
        self.auth_dir.mkdir(exist_ok=True)
        self.roles_file = self.auth_dir / 'roles.json'
        self._roles_cache: Optional[Dict[str, Role]] = None
    
    def _load_roles(self) -> Dict[str, Role]:
        """Загрузить все роли из файла"""
        if self._roles_cache is not None:
            return self._roles_cache
        
        if not self.roles_file.exists():
            self._roles_cache = {}
            return self._roles_cache
        
        try:
            with open(self.roles_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._roles_cache = {
                    role_id: Role.from_dict(role_data)
                    for role_id, role_data in data.items()
                }
            return self._roles_cache
        except Exception as e:
            logger.error(f"Error loading roles: {e}")
            self._roles_cache = {}
            return self._roles_cache
    
    def _save_roles(self, roles: Dict[str, Role]):
        """Сохранить все роли в файл"""
        try:
            data = {
                role_id: role.to_dict()
                for role_id, role in roles.items()
            }
            with open(self.roles_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._roles_cache = roles
        except Exception as e:
            logger.error(f"Error saving roles: {e}")
            raise
    
    def create_role(self, name: str, description: str, 
                   permissions: Optional[List[str]] = None) -> Role:
        """
        Создать новую роль
        
        Args:
            name: Имя роли
            description: Описание роли
            permissions: Список ID прав доступа
        
        Returns:
            Созданная роль
        
        Raises:
            ValueError: Если роль с таким именем уже существует
        """
        roles = self._load_roles()
        
        # Проверяем, что роль с таким именем не существует
        for role in roles.values():
            if role.name == name:
                raise ValueError(f"Роль с именем '{name}' уже существует")
        
        # Создаем новую роль
        role_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        
        role = Role(
            id=role_id,
            name=name,
            description=description,
            permissions=permissions or [],
            created_at=now,
            updated_at=now
        )
        
        roles[role_id] = role
        self._save_roles(roles)
        
        logger.info(f"Created role: {name} (ID: {role_id})")
        return role
    
    def get_role_by_id(self, role_id: str) -> Optional[Role]:
        """Получить роль по ID"""
        roles = self._load_roles()
        return roles.get(role_id)
    
    def get_role_by_name(self, name: str) -> Optional[Role]:
        """Получить роль по имени"""
        roles = self._load_roles()
        for role in roles.values():
            if role.name == name:
                return role
        return None
    
    def get_all_roles(self) -> List[Role]:
        """Получить все роли"""
        roles = self._load_roles()
        return list(roles.values())
    
    def update_role(self, role_id: str, name: Optional[str] = None,
                   description: Optional[str] = None,
                   permissions: Optional[List[str]] = None) -> Optional[Role]:
        """
        Обновить роль
        
        Args:
            role_id: ID роли
            name: Новое имя роли
            description: Новое описание
            permissions: Новый список прав доступа
        
        Returns:
            Обновленная роль или None, если не найдена
        """
        roles = self._load_roles()
        
        if role_id not in roles:
            return None
        
        role = roles[role_id]
        
        # Проверяем уникальность имени, если оно изменяется
        if name and name != role.name:
            for r in roles.values():
                if r.name == name and r.id != role_id:
                    raise ValueError(f"Роль с именем '{name}' уже существует")
            role.name = name
        
        if description is not None:
            role.description = description
        
        if permissions is not None:
            role.permissions = permissions
        
        role.updated_at = datetime.utcnow().isoformat()
        
        self._save_roles(roles)
        logger.info(f"Updated role: {role.name} (ID: {role_id})")
        return role
    
    def delete_role(self, role_id: str) -> bool:
        """
        Удалить роль
        
        Args:
            role_id: ID роли
        
        Returns:
            True, если роль удалена, False в противном случае
        """
        roles = self._load_roles()
        
        if role_id not in roles:
            return False
        
        role = roles[role_id]
        del roles[role_id]
        self._save_roles(roles)
        logger.info(f"Deleted role: {role.name} (ID: {role_id})")
        return True
    
    def add_permission_to_role(self, role_id: str, permission_id: str) -> bool:
        """Добавить право доступа к роли"""
        roles = self._load_roles()
        
        if role_id not in roles:
            return False
        
        role = roles[role_id]
        if permission_id not in role.permissions:
            role.permissions.append(permission_id)
            role.updated_at = datetime.utcnow().isoformat()
            self._save_roles(roles)
        return True
    
    def remove_permission_from_role(self, role_id: str, permission_id: str) -> bool:
        """Удалить право доступа у роли"""
        roles = self._load_roles()
        
        if role_id not in roles:
            return False
        
        role = roles[role_id]
        if permission_id in role.permissions:
            role.permissions.remove(permission_id)
            role.updated_at = datetime.utcnow().isoformat()
            self._save_roles(roles)
        return True


class PermissionService:
    """Сервис для работы с правами доступа"""
    
    def __init__(self, data_dir: Path):
        """
        Инициализация сервиса
        
        Args:
            data_dir: Директория для хранения данных
        """
        self.data_dir = data_dir
        self.auth_dir = data_dir / 'auth'
        self.auth_dir.mkdir(exist_ok=True)
        self.permissions_file = self.auth_dir / 'permissions.json'
        self._permissions_cache: Optional[Dict[str, Permission]] = None
    
    def _load_permissions(self) -> Dict[str, Permission]:
        """Загрузить все права доступа из файла"""
        if self._permissions_cache is not None:
            return self._permissions_cache
        
        if not self.permissions_file.exists():
            self._permissions_cache = {}
            return self._permissions_cache
        
        try:
            with open(self.permissions_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._permissions_cache = {
                    perm_id: Permission.from_dict(perm_data)
                    for perm_id, perm_data in data.items()
                }
            return self._permissions_cache
        except Exception as e:
            logger.error(f"Error loading permissions: {e}")
            self._permissions_cache = {}
            return self._permissions_cache
    
    def _save_permissions(self, permissions: Dict[str, Permission]):
        """Сохранить все права доступа в файл"""
        try:
            data = {
                perm_id: perm.to_dict()
                for perm_id, perm in permissions.items()
            }
            with open(self.permissions_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._permissions_cache = permissions
        except Exception as e:
            logger.error(f"Error saving permissions: {e}")
            raise
    
    def create_permission(self, name: str, description: str, resource: str, action: str) -> Permission:
        """
        Создать новое право доступа
        
        Args:
            name: Имя права (например, 'users.read')
            description: Описание права
            resource: Ресурс (например, 'users', 'roles')
            action: Действие (например, 'create', 'read', 'update', 'delete')
        
        Returns:
            Созданное право доступа
        
        Raises:
            ValueError: Если право с таким именем уже существует
        """
        permissions = self._load_permissions()
        
        # Проверяем, что право с таким именем не существует
        for perm in permissions.values():
            if perm.name == name:
                raise ValueError(f"Право доступа с именем '{name}' уже существует")
        
        # Создаем новое право доступа
        perm_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        
        permission = Permission(
            id=perm_id,
            name=name,
            description=description,
            resource=resource,
            action=action,
            created_at=now
        )
        
        permissions[perm_id] = permission
        self._save_permissions(permissions)
        
        logger.info(f"Created permission: {name} (ID: {perm_id})")
        return permission
    
    def get_permission_by_id(self, perm_id: str) -> Optional[Permission]:
        """Получить право доступа по ID"""
        permissions = self._load_permissions()
        return permissions.get(perm_id)
    
    def get_permission_by_name(self, name: str) -> Optional[Permission]:
        """Получить право доступа по имени"""
        permissions = self._load_permissions()
        for perm in permissions.values():
            if perm.name == name:
                return perm
        return None
    
    def get_all_permissions(self) -> List[Permission]:
        """Получить все права доступа"""
        permissions = self._load_permissions()
        return list(permissions.values())
    
    def get_permissions_by_resource(self, resource: str) -> List[Permission]:
        """Получить все права доступа для определенного ресурса"""
        permissions = self._load_permissions()
        return [perm for perm in permissions.values() if perm.resource == resource]
    
    def update_permission(self, perm_id: str, name: Optional[str] = None,
                         description: Optional[str] = None,
                         resource: Optional[str] = None,
                         action: Optional[str] = None) -> Optional[Permission]:
        """
        Обновить право доступа
        
        Args:
            perm_id: ID права доступа
            name: Новое имя
            description: Новое описание
            resource: Новый ресурс
            action: Новое действие
        
        Returns:
            Обновленное право доступа или None, если не найдено
        """
        permissions = self._load_permissions()
        
        if perm_id not in permissions:
            return None
        
        permission = permissions[perm_id]
        
        # Проверяем уникальность имени, если оно изменяется
        if name and name != permission.name:
            for p in permissions.values():
                if p.name == name and p.id != perm_id:
                    raise ValueError(f"Право доступа с именем '{name}' уже существует")
            permission.name = name
        
        if description is not None:
            permission.description = description
        
        if resource is not None:
            permission.resource = resource
        
        if action is not None:
            permission.action = action
        
        self._save_permissions(permissions)
        logger.info(f"Updated permission: {permission.name} (ID: {perm_id})")
        return permission
    
    def delete_permission(self, perm_id: str) -> bool:
        """
        Удалить право доступа
        
        Args:
            perm_id: ID права доступа
        
        Returns:
            True, если право удалено, False в противном случае
        """
        permissions = self._load_permissions()
        
        if perm_id not in permissions:
            return False
        
        permission = permissions[perm_id]
        del permissions[perm_id]
        self._save_permissions(permissions)
        logger.info(f"Deleted permission: {permission.name} (ID: {perm_id})")
        return True
