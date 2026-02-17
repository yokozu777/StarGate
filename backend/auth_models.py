#!/usr/bin/env python3
"""
Модели данных для системы аутентификации и RBAC
"""
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime
import json


@dataclass
class Permission:
    """Модель права доступа"""
    id: str
    name: str
    description: str
    resource: str  # Ресурс, к которому относится право (например, 'users', 'roles', 'projects')
    action: str    # Действие (например, 'create', 'read', 'update', 'delete')
    created_at: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Преобразовать в словарь"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Permission':
        """Создать из словаря"""
        return cls(**data)


@dataclass
class Role:
    """Модель роли"""
    id: str
    name: str
    description: str
    permissions: List[str]  # Список ID прав доступа
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Преобразовать в словарь"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Role':
        """Создать из словаря"""
        return cls(**data)
    
    def has_permission(self, permission_id: str) -> bool:
        """Проверить, есть ли у роли определенное право"""
        return permission_id in self.permissions


@dataclass
class User:
    """Модель пользователя"""
    id: str
    username: str
    email: Optional[str] = None
    password_hash: str = ""  # Хеш пароля (bcrypt)
    roles: List[str] = None  # Список ID ролей
    is_active: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_login: Optional[str] = None
    
    def __post_init__(self):
        """Инициализация после создания объекта"""
        if self.roles is None:
            self.roles = []
    
    def to_dict(self) -> dict:
        """Преобразовать в словарь (без пароля)"""
        data = asdict(self)
        # Удаляем password_hash из словаря для безопасности
        data.pop('password_hash', None)
        return data
    
    def to_dict_with_password(self) -> dict:
        """Преобразовать в словарь (с паролем, для внутреннего использования)"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'User':
        """Создать из словаря"""
        # Обрабатываем случай, когда roles может быть None
        if 'roles' not in data or data['roles'] is None:
            data['roles'] = []
        return cls(**data)
    
    def has_role(self, role_id: str) -> bool:
        """Проверить, есть ли у пользователя определенная роль"""
        return role_id in self.roles
    
    def add_role(self, role_id: str):
        """Добавить роль пользователю"""
        if role_id not in self.roles:
            self.roles.append(role_id)
    
    def remove_role(self, role_id: str):
        """Удалить роль у пользователя"""
        if role_id in self.roles:
            self.roles.remove(role_id)
