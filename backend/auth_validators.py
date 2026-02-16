#!/usr/bin/env python3
"""
Валидаторы для входных данных аутентификации
"""
import re
from typing import Tuple, Optional


def validate_username(username: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация имени пользователя
    
    Args:
        username: Имя пользователя для проверки
    
    Returns:
        Tuple[bool, Optional[str]]: (is_valid, error_message)
    """
    if not username:
        return False, "Username is required"
    
    if not isinstance(username, str):
        return False, "Username must be a string"
    
    username = username.strip()
    
    if len(username) < 3:
        return False, "Username must contain at least 3 characters"
    
    if len(username) > 50:
        return False, "Username must not exceed 50 characters"
    
    # Разрешаем буквы, цифры, подчеркивания и дефисы
    if not re.match(r'^[a-zA-Z0-9_-]+$', username):
        return False, "Username can only contain letters, numbers, underscores and hyphens"
    
    return True, None


def validate_password(password: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация пароля
    
    Args:
        password: Пароль для проверки
    
    Returns:
        Tuple[bool, Optional[str]]: (is_valid, error_message)
    """
    if not password:
        return False, "Password is required"
    
    if not isinstance(password, str):
        return False, "Password must be a string"
    
    if len(password) < 6:
        return False, "Password must contain at least 6 characters"
    
    if len(password) > 128:
        return False, "Password must not exceed 128 characters"
    
    return True, None


def validate_email(email: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Валидация email адреса
    
    Args:
        email: Email адрес для проверки (может быть None)
    
    Returns:
        Tuple[bool, Optional[str]]: (is_valid, error_message)
    """
    if email is None:
        return True, None  # Email опционален
    
    if not isinstance(email, str):
        return False, "Email must be a string"
    
    email = email.strip()
    
    if not email:
        return True, None  # Пустой email допустим
    
    # Базовая проверка формата email
    email_pattern = re.compile(
        r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    )
    
    if not email_pattern.match(email):
        return False, "Invalid email address format"
    
    if len(email) > 255:
        return False, "Email must not exceed 255 characters"
    
    return True, None


def validate_role_name(name: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация имени роли
    
    Args:
        name: Имя роли для проверки
    
    Returns:
        Tuple[bool, Optional[str]]: (is_valid, error_message)
    """
    if not name:
        return False, "Role name is required"
    
    if not isinstance(name, str):
        return False, "Role name must be a string"
    
    name = name.strip()
    
    if len(name) < 2:
        return False, "Role name must contain at least 2 characters"
    
    if len(name) > 100:
        return False, "Role name must not exceed 100 characters"
    
    # Разрешаем буквы, цифры, пробелы, подчеркивания и дефисы
    if not re.match(r'^[a-zA-Z0-9_\s-]+$', name):
        return False, "Role name can only contain letters, numbers, spaces, underscores and hyphens"
    
    return True, None


def validate_permission_name(name: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация имени права доступа
    
    Args:
        name: Имя права доступа для проверки
    
    Returns:
        Tuple[bool, Optional[str]]: (is_valid, error_message)
    """
    if not name:
        return False, "Permission name is required"
    
    if not isinstance(name, str):
        return False, "Permission name must be a string"
    
    name = name.strip()
    
    if len(name) < 3:
        return False, "Permission name must contain at least 3 characters"
    
    if len(name) > 100:
        return False, "Permission name must not exceed 100 characters"
    
    # Формат: resource.action (например, users.read, roles.create)
    if not re.match(r'^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$', name):
        return False, "Permission name must be in format 'resource.action' (e.g., 'users.read')"
    
    return True, None
