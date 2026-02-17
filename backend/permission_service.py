#!/usr/bin/env python3
"""
Сервис для проверки прав доступа пользователей
"""
from typing import List, Optional, Set
from pathlib import Path
import logging

try:
    from .user_service import UserService
    from .role_service import RoleService, PermissionService as PermissionServiceClass
except ImportError:
    from user_service import UserService
    from role_service import RoleService, PermissionService as PermissionServiceClass

logger = logging.getLogger(__name__)


class AccessControlService:
    """Сервис для проверки прав доступа"""
    
    def __init__(self, data_dir: Path):
        """
        Инициализация сервиса
        
        Args:
            data_dir: Директория для хранения данных
        """
        self.data_dir = data_dir
        self.user_service = UserService(data_dir)
        self.role_service = RoleService(data_dir)
        self.permission_service = PermissionServiceClass(data_dir)
    
    def get_user_permissions(self, user_id: str) -> Set[str]:
        """
        Получить все права доступа пользователя (через его роли)
        
        Args:
            user_id: ID пользователя
        
        Returns:
            Множество имен прав доступа
        """
        user = self.user_service.get_user_by_id(user_id)
        if not user or not user.is_active:
            return set()
        
        permissions = set()
        
        # Получаем все роли пользователя
        for role_id in user.roles:
            role = self.role_service.get_role_by_id(role_id)
            if role:
                # Получаем все права доступа роли
                for perm_id in role.permissions:
                    perm = self.permission_service.get_permission_by_id(perm_id)
                    if perm:
                        permissions.add(perm.name)
        
        return permissions
    
    def has_permission(self, user_id: str, permission_name: str) -> bool:
        """
        Проверить, есть ли у пользователя определенное право доступа
        
        Args:
            user_id: ID пользователя
            permission_name: Имя права доступа (например, 'users.read')
        
        Returns:
            True, если у пользователя есть право, False в противном случае
        """
        user_permissions = self.get_user_permissions(user_id)
        return permission_name in user_permissions
    
    def has_any_permission(self, user_id: str, permission_names: List[str]) -> bool:
        """
        Проверить, есть ли у пользователя хотя бы одно из указанных прав
        
        Args:
            user_id: ID пользователя
            permission_names: Список имен прав доступа
        
        Returns:
            True, если у пользователя есть хотя бы одно право, False в противном случае
        """
        user_permissions = self.get_user_permissions(user_id)
        return bool(user_permissions & set(permission_names))
    
    def has_all_permissions(self, user_id: str, permission_names: List[str]) -> bool:
        """
        Проверить, есть ли у пользователя все указанные права
        
        Args:
            user_id: ID пользователя
            permission_names: Список имен прав доступа
        
        Returns:
            True, если у пользователя есть все права, False в противном случае
        """
        user_permissions = self.get_user_permissions(user_id)
        return set(permission_names).issubset(user_permissions)
    
    def get_user_roles(self, user_id: str) -> List[str]:
        """
        Получить список имен ролей пользователя
        
        Args:
            user_id: ID пользователя
        
        Returns:
            Список имен ролей
        """
        user = self.user_service.get_user_by_id(user_id)
        if not user or not user.is_active:
            return []
        
        role_names = []
        for role_id in user.roles:
            role = self.role_service.get_role_by_id(role_id)
            if role:
                role_names.append(role.name)
        
        return role_names
    
    def has_role(self, user_id: str, role_name: str) -> bool:
        """
        Проверить, есть ли у пользователя определенная роль
        
        Args:
            user_id: ID пользователя
            role_name: Имя роли
        
        Returns:
            True, если у пользователя есть роль, False в противном случае
        """
        user_roles = self.get_user_roles(user_id)
        return role_name in user_roles
    
    def has_any_role(self, user_id: str, role_names: List[str]) -> bool:
        """
        Проверить, есть ли у пользователя хотя бы одна из указанных ролей
        
        Args:
            user_id: ID пользователя
            role_names: Список имен ролей
        
        Returns:
            True, если у пользователя есть хотя бы одна роль, False в противном случае
        """
        user_roles = self.get_user_roles(user_id)
        return bool(set(user_roles) & set(role_names))
    
    def check_resource_access(self, user_id: str, resource: str, action: str) -> bool:
        """
        Проверить доступ к ресурсу с определенным действием
        
        Args:
            user_id: ID пользователя
            resource: Ресурс (например, 'users', 'roles')
            action: Действие (например, 'create', 'read', 'update', 'delete')
        
        Returns:
            True, если доступ разрешен, False в противном случае
        """
        permission_name = f"{resource}.{action}"
        return self.has_permission(user_id, permission_name)
