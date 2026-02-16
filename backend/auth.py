#!/usr/bin/env python3
"""
Модуль для работы с JWT токенами аутентификации
"""
import os
import jwt
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)

# Секретный ключ для подписи JWT токенов
# В production должен быть в переменных окружения или в защищенном файле
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'default-secret-key-change-in-production')
JWT_ALGORITHM = 'HS256'
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get('JWT_ACCESS_TOKEN_EXPIRE_MINUTES', '1440'))  # 24 часа по умолчанию
JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get('JWT_REFRESH_TOKEN_EXPIRE_DAYS', '7'))  # 7 дней

# Путь к файлу с blacklist токенов
def get_token_blacklist_path(data_dir: Path) -> Path:
    """Получить путь к файлу с blacklist токенов"""
    auth_dir = data_dir / 'auth'
    auth_dir.mkdir(exist_ok=True)
    return auth_dir / 'token_blacklist.json'


def load_token_blacklist(data_dir: Path) -> set:
    """Загрузить blacklist токенов из файла"""
    blacklist_file = get_token_blacklist_path(data_dir)
    if not blacklist_file.exists():
        return set()
    
    try:
        with open(blacklist_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Очищаем истекшие токены
            current_time = datetime.utcnow().timestamp()
            active_tokens = {token for token, exp in data.items() if exp > current_time}
            
            # Сохраняем только активные токены
            if len(active_tokens) != len(data):
                save_token_blacklist(data_dir, active_tokens)
            
            return active_tokens
    except Exception as e:
        logger.error(f"Error loading token blacklist: {e}")
        return set()


def save_token_blacklist(data_dir: Path, blacklist: set):
    """Сохранить blacklist токенов в файл"""
    blacklist_file = get_token_blacklist_path(data_dir)
    try:
        # Сохраняем как словарь {token: expiration_timestamp}
        # Для упрощения используем set, но можно расширить для хранения времени истечения
        data = {token: (datetime.utcnow() + timedelta(days=JWT_REFRESH_TOKEN_EXPIRE_DAYS)).timestamp() 
                for token in blacklist}
        with open(blacklist_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving token blacklist: {e}")


def add_token_to_blacklist(data_dir: Path, token: str):
    """Добавить токен в blacklist"""
    blacklist = load_token_blacklist(data_dir)
    blacklist.add(token)
    save_token_blacklist(data_dir, blacklist)


def is_token_blacklisted(data_dir: Path, token: str) -> bool:
    """Проверить, находится ли токен в blacklist"""
    blacklist = load_token_blacklist(data_dir)
    return token in blacklist


def generate_token(user_id: str, username: str, roles: list, data_dir: Path, 
                   token_type: str = 'access', expires_delta: Optional[timedelta] = None) -> str:
    """
    Генерировать JWT токен для пользователя
    
    Args:
        user_id: ID пользователя
        username: Имя пользователя
        roles: Список ролей пользователя
        data_dir: Директория для данных
        token_type: Тип токена ('access' или 'refresh')
        expires_delta: Время жизни токена (опционально)
    
    Returns:
        JWT токен в виде строки
    """
    if expires_delta is None:
        if token_type == 'refresh':
            expires_delta = timedelta(days=JWT_REFRESH_TOKEN_EXPIRE_DAYS)
        else:
            expires_delta = timedelta(minutes=JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    
    expire = datetime.utcnow() + expires_delta
    
    payload = {
        'user_id': user_id,
        'username': username,
        'roles': roles,
        'token_type': token_type,
        'exp': expire,
        'iat': datetime.utcnow()
    }
    
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return token


def verify_token(token: str, data_dir: Path, token_type: str = 'access') -> Optional[Dict[str, Any]]:
    """
    Проверить и декодировать JWT токен
    
    Args:
        token: JWT токен
        data_dir: Директория для данных (для проверки blacklist)
        token_type: Ожидаемый тип токена ('access' или 'refresh')
    
    Returns:
        Словарь с данными токена или None, если токен невалиден
    """
    try:
        # Проверяем blacklist
        if is_token_blacklisted(data_dir, token):
            logger.warning("Attempt to use blacklisted token")
            return None
        
        # Декодируем токен
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        
        # Проверяем тип токена
        if payload.get('token_type') != token_type:
            logger.warning(f"Invalid token type. Expected {token_type}, got {payload.get('token_type')}")
            return None
        
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token: {e}")
        return None
    except Exception as e:
        logger.error(f"Error verifying token: {e}")
        return None


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Декодировать JWT токен без проверки подписи (только для чтения данных)
    Используется для получения информации о токене без валидации
    
    Args:
        token: JWT токен
    
    Returns:
        Словарь с данными токена или None, если токен невалиден
    """
    try:
        # Декодируем без проверки подписи (опция verify=False)
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload
    except Exception as e:
        logger.warning(f"Error decoding token: {e}")
        return None


def get_token_from_header(auth_header: Optional[str]) -> Optional[str]:
    """
    Извлечь токен из заголовка Authorization
    
    Args:
        auth_header: Значение заголовка Authorization (например, "Bearer <token>")
    
    Returns:
        Токен или None, если заголовок невалиден
    """
    if not auth_header:
        return None
    
    try:
        # Формат: "Bearer <token>"
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            return None
        return parts[1]
    except Exception:
        return None
