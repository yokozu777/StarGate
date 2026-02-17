#!/usr/bin/env python3
"""
Сервис для управления пользователями
"""
import bcrypt
import uuid
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

try:
    from .auth_models import User
except ImportError:
    from auth_models import User

logger = logging.getLogger(__name__)


class UserService:
    """Сервис для работы с пользователями"""
    
    def __init__(self, data_dir: Path):
        """
        Инициализация сервиса
        
        Args:
            data_dir: Директория для хранения данных
        """
        self.data_dir = data_dir
        self.auth_dir = data_dir / 'auth'
        self.auth_dir.mkdir(exist_ok=True)
        self.users_file = self.auth_dir / 'users.json'
        self._users_cache: Optional[Dict[str, User]] = None
    
    def _load_users(self) -> Dict[str, User]:
        """Загрузить всех пользователей из файла"""
        if self._users_cache is not None:
            return self._users_cache
        
        if not self.users_file.exists():
            self._users_cache = {}
            return self._users_cache
        
        try:
            with open(self.users_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._users_cache = {
                    user_id: User.from_dict(user_data)
                    for user_id, user_data in data.items()
                }
            return self._users_cache
        except Exception as e:
            logger.error(f"Error loading users: {e}")
            self._users_cache = {}
            return self._users_cache
    
    def _save_users(self, users: Dict[str, User]):
        """Сохранить всех пользователей в файл"""
        try:
            data = {
                user_id: user.to_dict_with_password()
                for user_id, user in users.items()
            }
            with open(self.users_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._users_cache = users
        except Exception as e:
            logger.error(f"Error saving users: {e}")
            raise
    
    def _hash_password(self, password: str) -> str:
        """Хешировать пароль с помощью bcrypt"""
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
        return hashed.decode('utf-8')
    
    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Проверить пароль"""
        try:
            return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
        except Exception as e:
            logger.error(f"Error verifying password: {e}")
            return False
    
    def create_user(self, username: str, password: str, email: Optional[str] = None, 
                   roles: Optional[List[str]] = None) -> User:
        """
        Создать нового пользователя
        
        Args:
            username: Имя пользователя
            password: Пароль (будет захеширован)
            email: Email пользователя
            roles: Список ID ролей
        
        Returns:
            Созданный пользователь
        
        Raises:
            ValueError: Если пользователь с таким username уже существует
        """
        users = self._load_users()
        
        # Проверяем, что пользователь с таким username не существует
        for user in users.values():
            if user.username == username:
                raise ValueError(f"Пользователь с именем '{username}' уже существует")
        
        # Создаем нового пользователя
        user_id = str(uuid.uuid4())
        password_hash = self._hash_password(password)
        now = datetime.utcnow().isoformat()
        
        user = User(
            id=user_id,
            username=username,
            email=email,
            password_hash=password_hash,
            roles=roles or [],
            is_active=True,
            created_at=now,
            updated_at=now
        )
        
        users[user_id] = user
        self._save_users(users)
        
        logger.info(f"Created user: {username} (ID: {user_id})")
        return user
    
    def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Получить пользователя по ID"""
        users = self._load_users()
        return users.get(user_id)
    
    def get_user_by_username(self, username: str) -> Optional[User]:
        """Получить пользователя по имени"""
        users = self._load_users()
        for user in users.values():
            if user.username == username:
                return user
        return None
    
    def get_all_users(self) -> List[User]:
        """Получить всех пользователей"""
        users = self._load_users()
        return list(users.values())
    
    def update_user(self, user_id: str, username: Optional[str] = None,
                   email: Optional[str] = None, roles: Optional[List[str]] = None,
                   is_active: Optional[bool] = None) -> Optional[User]:
        """
        Обновить пользователя
        
        Args:
            user_id: ID пользователя
            username: Новое имя пользователя
            email: Новый email
            roles: Новый список ролей
            is_active: Статус активности
        
        Returns:
            Обновленный пользователь или None, если не найден
        """
        users = self._load_users()
        
        if user_id not in users:
            return None
        
        user = users[user_id]
        
        # Проверяем уникальность username, если он изменяется
        if username and username != user.username:
            for u in users.values():
                if u.username == username and u.id != user_id:
                    raise ValueError(f"Пользователь с именем '{username}' уже существует")
            user.username = username
        
        if email is not None:
            user.email = email
        
        if roles is not None:
            user.roles = roles
        
        if is_active is not None:
            user.is_active = is_active
        
        user.updated_at = datetime.utcnow().isoformat()
        
        self._save_users(users)
        logger.info(f"Updated user: {user.username} (ID: {user_id})")
        return user
    
    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """
        Изменить пароль пользователя
        
        Args:
            user_id: ID пользователя
            old_password: Старый пароль
            new_password: Новый пароль
        
        Returns:
            True, если пароль успешно изменен, False в противном случае
        """
        users = self._load_users()
        
        if user_id not in users:
            return False
        
        user = users[user_id]
        
        # Проверяем старый пароль
        if not self._verify_password(old_password, user.password_hash):
            return False
        
        # Устанавливаем новый пароль
        user.password_hash = self._hash_password(new_password)
        user.updated_at = datetime.utcnow().isoformat()
        
        self._save_users(users)
        logger.info(f"Changed password for user: {user.username} (ID: {user_id})")
        return True
    
    def set_password(self, user_id: str, new_password: str) -> bool:
        """
        Установить новый пароль (без проверки старого, для администратора)
        
        Args:
            user_id: ID пользователя
            new_password: Новый пароль
        
        Returns:
            True, если пароль успешно установлен, False в противном случае
        """
        users = self._load_users()
        
        if user_id not in users:
            return False
        
        user = users[user_id]
        user.password_hash = self._hash_password(new_password)
        user.updated_at = datetime.utcnow().isoformat()
        
        self._save_users(users)
        logger.info(f"Set new password for user: {user.username} (ID: {user_id})")
        return True
    
    def delete_user(self, user_id: str) -> bool:
        """
        Удалить пользователя
        
        Args:
            user_id: ID пользователя
        
        Returns:
            True, если пользователь удален, False в противном случае
        """
        users = self._load_users()
        
        if user_id not in users:
            return False
        
        user = users[user_id]
        del users[user_id]
        self._save_users(users)
        logger.info(f"Deleted user: {user.username} (ID: {user_id})")
        return True
    
    def authenticate(self, username: str, password: str) -> Optional[User]:
        """
        Аутентифицировать пользователя
        
        Args:
            username: Имя пользователя
            password: Пароль
        
        Returns:
            Пользователь, если аутентификация успешна, None в противном случае
        """
        user = self.get_user_by_username(username)
        
        if not user:
            return None
        
        if not user.is_active:
            logger.warning(f"Attempt to login inactive user: {username}")
            return None
        
        if not self._verify_password(password, user.password_hash):
            return None
        
        # Обновляем время последнего входа
        user.last_login = datetime.utcnow().isoformat()
        users = self._load_users()
        users[user.id] = user
        self._save_users(users)
        
        return user
    
    def add_role_to_user(self, user_id: str, role_id: str) -> bool:
        """Добавить роль пользователю"""
        users = self._load_users()
        
        if user_id not in users:
            return False
        
        user = users[user_id]
        user.add_role(role_id)
        user.updated_at = datetime.utcnow().isoformat()
        
        self._save_users(users)
        return True
    
    def remove_role_from_user(self, user_id: str, role_id: str) -> bool:
        """Удалить роль у пользователя"""
        users = self._load_users()
        
        if user_id not in users:
            return False
        
        user = users[user_id]
        user.remove_role(role_id)
        user.updated_at = datetime.utcnow().isoformat()
        
        self._save_users(users)
        return True
