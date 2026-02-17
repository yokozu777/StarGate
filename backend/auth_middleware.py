#!/usr/bin/env python3
"""
Middleware для защиты API endpoints с помощью JWT аутентификации
"""
from functools import wraps
from flask import request, jsonify
from typing import Optional, Callable, List
from pathlib import Path
import logging
import os

try:
    from .auth import verify_token, get_token_from_header
except ImportError:
    from auth import verify_token, get_token_from_header

logger = logging.getLogger(__name__)


# Глобальная переменная для хранения DATA_DIR (устанавливается из app.py)
_data_dir: Optional[Path] = None
# Глобальная переменная для хранения AccessControlService (устанавливается из app.py)
_access_control_service = None

def set_data_dir(data_dir: Path):
    """Установить директорию данных (вызывается из app.py при инициализации)"""
    global _data_dir
    _data_dir = data_dir

def get_data_dir() -> Path:
    """Получить директорию данных"""
    global _data_dir
    if _data_dir is None:
        # Fallback: пытаемся определить из структуры проекта
        _data_dir = Path(__file__).parent.parent.parent / 'data'
    return _data_dir

def set_access_control_service(access_control_service):
    """Установить AccessControlService (вызывается из app.py при инициализации)"""
    global _access_control_service
    _access_control_service = access_control_service

def get_access_control_service():
    """Получить AccessControlService"""
    global _access_control_service
    if _access_control_service is None:
        # Создаем экземпляр, если он не был установлен
        try:
            from .permission_service import AccessControlService
            _access_control_service = AccessControlService(get_data_dir())
        except ImportError:
            from permission_service import AccessControlService
            _access_control_service = AccessControlService(get_data_dir())
    return _access_control_service


def require_auth(f: Callable) -> Callable:
    """
    Декоратор для защиты endpoint'а аутентификацией.
    Для путей /api/worker/* (кроме register) принимает и JWT пользователя, и worker token.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Внутренние вызовы (например, фоновый планировщик host status)
        internal_secret = os.environ.get('INTERNAL_CALL_SECRET', 'internal')
        if internal_secret and request.headers.get('X-Internal-Call') == internal_secret:
            request.current_user = {'username': 'internal', 'user_id': '__internal__', 'roles': ['admin']}
            request.token = None
            return f(*args, **kwargs)

        auth_header = request.headers.get('Authorization')
        token = get_token_from_header(auth_header)
        # EventSource/SSE не поддерживает заголовки — допускаем токен в query для GET (stream логов)
        if not token and request.method == 'GET':
            token = request.args.get('access_token') or request.args.get('token')
        if not token:
            return jsonify({
                'error': 'Authentication required',
                'message': 'Access token missing'
            }), 401

        # Для /api/worker/* и GET /api/executions/* — принимаем worker token (воркер опрашивает статус execution)
        try:
            from .worker_registry import verify_token as verify_worker_token
        except ImportError:
            from worker_registry import verify_token as verify_worker_token
        is_worker_path = request.path.startswith('/api/worker/')
        is_execution_get = request.path.startswith('/api/executions/') and request.method == 'GET'
        if is_worker_path or is_execution_get:
            result = verify_worker_token(token)
            if result:
                worker_id, _ = result
                request.current_user = {'worker_id': worker_id, 'username': f'worker:{worker_id[:8]}', 'roles': []}
                request.token = token
                return f(*args, **kwargs)
            if is_worker_path:
                return jsonify({
                    'error': 'Invalid token',
                    'message': 'Worker token is invalid or expired. Re-register the worker or set WORKER_TOKEN.'
                }), 401
            # GET /api/executions/* с невалидным worker token — пробуем JWT (мог быть запрос от UI)
            # (fallback ниже)

        data_dir = get_data_dir()
        payload = verify_token(token, data_dir, token_type='access')
        if payload:
            request.current_user = {
                'user_id': payload.get('user_id'),
                'username': payload.get('username'),
                'roles': payload.get('roles', [])
            }
            request.token = token
            return f(*args, **kwargs)

        # Try API token (long-lived tokens for programmatic access)
        try:
            from .api_tokens_store import verify_api_token
        except ImportError:
            from api_tokens_store import verify_api_token
        api_result = verify_api_token(token)
        if api_result:
            api_user_id, token_entry = api_result
            # Load roles for API token user (needed for permission checks)
            roles = []
            try:
                acs = get_access_control_service()
                if hasattr(acs, 'get_user_roles'):
                    roles = acs.get_user_roles(api_user_id) or []
            except Exception:
                pass
            request.current_user = {
                'user_id': api_user_id,
                'username': token_entry.get('username', f'api:{api_user_id[:8]}'),
                'roles': roles
            }
            request.token = token
            return f(*args, **kwargs)

        return jsonify({
            'error': 'Invalid token',
            'message': 'Access token is invalid or expired'
        }), 401
    return decorated_function


def require_optional_auth(f: Callable) -> Callable:
    """
    Декоратор для endpoint'ов, где аутентификация опциональна (например, регистрация воркера).
    Если передан Bearer-токен — проверяется как JWT и в request подставляется current_user.
    Если токена нет — request.current_user = None, выполнение продолжается.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        token = get_token_from_header(auth_header)
        if not token:
            request.current_user = None
            request.token = None
            return f(*args, **kwargs)
        data_dir = get_data_dir()
        payload = verify_token(token, data_dir, token_type='access')
        if not payload:
            request.current_user = None
            request.token = None
            return f(*args, **kwargs)
        request.current_user = {
            'user_id': payload.get('user_id'),
            'username': payload.get('username'),
            'roles': payload.get('roles', [])
        }
        request.token = token
        return f(*args, **kwargs)
    return decorated_function


def require_permission(permission_name: str):
    """
    Декоратор для проверки конкретного права доступа
    
    Использование:
        @app.route('/api/users')
        @require_auth
        @require_permission('users.read')
        def get_users():
            return jsonify({'users': []})
    
    Args:
        permission_name: Имя права доступа (например, 'users.read', 'roles.create')
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        @require_auth  # Сначала проверяем аутентификацию
        def decorated_function(*args, **kwargs):
            # Получаем ID пользователя из токена
            user_id = request.current_user.get('user_id')
            
            if not user_id:
                return jsonify({
                    'error': 'Authentication error',
                    'message': 'Failed to identify user'
                }), 401
            
            # Проверяем права доступа через AccessControlService
            access_control = get_access_control_service()
            has_permission = access_control.has_permission(user_id, permission_name)
            
            if not has_permission:
                logger.warning(f"User {request.current_user.get('username')} (ID: {user_id}) denied access to {permission_name}")
                return jsonify({
                    'error': 'Access denied',
                    'message': f'Insufficient permissions to perform operation. Required permission: {permission_name}'
                }), 403
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def require_any_role(*role_names: str):
    """
    Декоратор для проверки наличия хотя бы одной из указанных ролей
    
    Использование:
        @app.route('/api/admin')
        @require_auth
        @require_any_role('admin', 'super_admin')
        def admin_endpoint():
            return jsonify({'message': 'Admin access'})
    
    Args:
        role_names: Имена ролей (хотя бы одна должна быть у пользователя)
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        @require_auth
        def decorated_function(*args, **kwargs):
            user_roles = request.current_user.get('roles', [])
            
            # Проверяем, есть ли хотя бы одна из требуемых ролей
            if not any(role in role_names for role in user_roles):
                return jsonify({
                    'error': 'Access denied',
                    'message': f'One of the following roles is required: {", ".join(role_names)}'
                }), 403
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def require_all_roles(*role_names: str):
    """
    Декоратор для проверки наличия всех указанных ролей
    
    Использование:
        @app.route('/api/super-admin')
        @require_auth
        @require_all_roles('admin', 'super_admin')
        def super_admin_endpoint():
            return jsonify({'message': 'Super admin access'})
    
    Args:
        role_names: Имена ролей (все должны быть у пользователя)
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        @require_auth
        def decorated_function(*args, **kwargs):
            user_roles = request.current_user.get('roles', [])
            
            # Проверяем, есть ли все требуемые роли
            if not all(role in user_roles for role in role_names):
                return jsonify({
                    'error': 'Access denied',
                    'message': f'All of the following roles are required: {", ".join(role_names)}'
                }), 403
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator
