#!/usr/bin/env python3
"""
Функции для инициализации базовых данных аутентификации (seed)
"""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def seed_default_user(user_service, role_service, data_dir: Path) -> bool:
    """
    Создать пользователя admin/admin при первом запуске, если пользователей еще нет
    
    Args:
        user_service: Экземпляр UserService
        role_service: Экземпляр RoleService
        data_dir: Директория для данных
    
    Returns:
        True, если пользователь был создан, False если уже существовал
    """
    try:
        # Проверяем, есть ли уже пользователи
        users = user_service.get_all_users()
        if users:
            logger.info(f"Users already exist ({len(users)} users). Skipping default user creation.")
            return False
        
        # Создаем пользователя admin
        admin_user = user_service.create_user(
            username='admin',
            password='admin123',  # Минимум 6 символов для валидации
            email=None,
            roles=[]  # Роли будут назначены после создания ролей
        )
        
        logger.info(f"Default admin user created: {admin_user.username} (ID: {admin_user.id})")
        
        # Пытаемся найти роль 'admin' и назначить её пользователю
        admin_role = role_service.get_role_by_name('admin')
        if admin_role:
            user_service.add_role_to_user(admin_user.id, admin_role.id)
            logger.info(f"Admin role assigned to default user")
        
        return True
    except ValueError as e:
        # Пользователь уже существует
        logger.info(f"Default admin user already exists: {e}")
        return False
    except Exception as e:
        logger.error(f"Error creating default user: {e}", exc_info=True)
        return False


def seed_default_roles(role_service, permission_service, data_dir: Path) -> bool:
    """
    Создать базовые роли и права доступа при первом запуске
    
    Args:
        role_service: Экземпляр RoleService
        permission_service: Экземпляр PermissionService
        data_dir: Директория для данных
    
    Returns:
        True, если роли/права были созданы, False если уже существовали
    """
    created_any = False
    
    try:
        # Список базовых прав доступа
        default_permissions = [
            # Users permissions
            {'name': 'users.read', 'description': 'View users', 'resource': 'users', 'action': 'read'},
            {'name': 'users.create', 'description': 'Create users', 'resource': 'users', 'action': 'create'},
            {'name': 'users.update', 'description': 'Update users', 'resource': 'users', 'action': 'update'},
            {'name': 'users.delete', 'description': 'Delete users', 'resource': 'users', 'action': 'delete'},
            
            # Roles permissions
            {'name': 'roles.read', 'description': 'View roles', 'resource': 'roles', 'action': 'read'},
            {'name': 'roles.create', 'description': 'Create roles', 'resource': 'roles', 'action': 'create'},
            {'name': 'roles.update', 'description': 'Update roles', 'resource': 'roles', 'action': 'update'},
            {'name': 'roles.delete', 'description': 'Delete roles', 'resource': 'roles', 'action': 'delete'},
            
            # Permissions permissions
            {'name': 'permissions.read', 'description': 'View permissions', 'resource': 'permissions', 'action': 'read'},
            {'name': 'permissions.create', 'description': 'Create permissions', 'resource': 'permissions', 'action': 'create'},
            {'name': 'permissions.update', 'description': 'Update permissions', 'resource': 'permissions', 'action': 'update'},
            {'name': 'permissions.delete', 'description': 'Delete permissions', 'resource': 'permissions', 'action': 'delete'},
            
            # Projects permissions
            {'name': 'projects.read', 'description': 'View projects', 'resource': 'projects', 'action': 'read'},
            {'name': 'projects.create', 'description': 'Create projects', 'resource': 'projects', 'action': 'create'},
            {'name': 'projects.update', 'description': 'Update projects', 'resource': 'projects', 'action': 'update'},
            {'name': 'projects.delete', 'description': 'Delete projects', 'resource': 'projects', 'action': 'delete'},
            
            # Playbooks permissions
            {'name': 'playbooks.read', 'description': 'View playbooks', 'resource': 'playbooks', 'action': 'read'},
            {'name': 'playbooks.create', 'description': 'Create playbooks', 'resource': 'playbooks', 'action': 'create'},
            {'name': 'playbooks.update', 'description': 'Update playbooks', 'resource': 'playbooks', 'action': 'update'},
            {'name': 'playbooks.delete', 'description': 'Delete playbooks', 'resource': 'playbooks', 'action': 'delete'},
            {'name': 'playbooks.execute', 'description': 'Execute playbooks', 'resource': 'playbooks', 'action': 'execute'},
            
            # Inventory permissions
            {'name': 'inventory.read', 'description': 'View inventory', 'resource': 'inventory', 'action': 'read'},
            {'name': 'inventory.create', 'description': 'Create inventory', 'resource': 'inventory', 'action': 'create'},
            {'name': 'inventory.update', 'description': 'Update inventory', 'resource': 'inventory', 'action': 'update'},
            {'name': 'inventory.delete', 'description': 'Delete inventory', 'resource': 'inventory', 'action': 'delete'},
            
            # Secrets permissions
            {'name': 'secrets.read', 'description': 'View secrets', 'resource': 'secrets', 'action': 'read'},
            {'name': 'secrets.create', 'description': 'Create secrets', 'resource': 'secrets', 'action': 'create'},
            {'name': 'secrets.update', 'description': 'Update secrets', 'resource': 'secrets', 'action': 'update'},
            {'name': 'secrets.delete', 'description': 'Delete secrets', 'resource': 'secrets', 'action': 'delete'},
            
            # Settings permissions
            {'name': 'settings.read', 'description': 'View settings', 'resource': 'settings', 'action': 'read'},
            {'name': 'settings.update', 'description': 'Update settings', 'resource': 'settings', 'action': 'update'},
        ]
        
        # Создаем права доступа
        permission_ids = {}
        for perm_data in default_permissions:
            try:
                # Проверяем, существует ли уже такое право
                existing_perm = permission_service.get_permission_by_name(perm_data['name'])
                if existing_perm:
                    permission_ids[perm_data['name']] = existing_perm.id
                    continue
                
                # Создаем новое право
                perm = permission_service.create_permission(
                    name=perm_data['name'],
                    description=perm_data['description'],
                    resource=perm_data['resource'],
                    action=perm_data['action']
                )
                permission_ids[perm_data['name']] = perm.id
                created_any = True
                logger.info(f"Created permission: {perm.name}")
            except ValueError as e:
                # Право уже существует
                existing_perm = permission_service.get_permission_by_name(perm_data['name'])
                if existing_perm:
                    permission_ids[perm_data['name']] = existing_perm.id
                logger.debug(f"Permission {perm_data['name']} already exists: {e}")
            except Exception as e:
                logger.error(f"Error creating permission {perm_data['name']}: {e}", exc_info=True)
        
        # Создаем базовые роли
        default_roles = [
            {
                'name': 'admin',
                'description': 'Administrator - full access to all functions',
                'permissions': list(permission_ids.values())  # All permissions
            },
            {
                'name': 'user',
                'description': 'Regular user - basic access',
                'permissions': [
                    permission_ids.get('projects.read'),
                    permission_ids.get('playbooks.read'),
                    permission_ids.get('inventory.read'),
                    permission_ids.get('secrets.read'),
                    permission_ids.get('settings.read'),
                ]
            },
            {
                'name': 'operator',
                'description': 'Operator - can execute playbooks',
                'permissions': [
                    permission_ids.get('projects.read'),
                    permission_ids.get('playbooks.read'),
                    permission_ids.get('playbooks.execute'),
                    permission_ids.get('inventory.read'),
                    permission_ids.get('secrets.read'),
                ]
            },
            {
                'name': 'viewer',
                'description': 'Viewer - read-only access',
                'permissions': [
                    permission_ids.get('projects.read'),
                    permission_ids.get('playbooks.read'),
                    permission_ids.get('inventory.read'),
                    permission_ids.get('settings.read'),
                ]
            }
        ]
        
        # Фильтруем None из списков permissions
        for role_data in default_roles:
            role_data['permissions'] = [p for p in role_data['permissions'] if p is not None]
        
        # Создаем роли
        for role_data in default_roles:
            try:
                # Проверяем, существует ли уже такая роль
                existing_role = role_service.get_role_by_name(role_data['name'])
                if existing_role:
                    logger.debug(f"Role {role_data['name']} already exists")
                    continue
                
                # Создаем новую роль
                role = role_service.create_role(
                    name=role_data['name'],
                    description=role_data['description'],
                    permissions=role_data['permissions']
                )
                created_any = True
                logger.info(f"Created role: {role.name} with {len(role.permissions)} permissions")
            except ValueError as e:
                # Роль уже существует
                logger.debug(f"Role {role_data['name']} already exists: {e}")
            except Exception as e:
                logger.error(f"Error creating role {role_data['name']}: {e}", exc_info=True)
        
        # Назначаем роль admin пользователю admin, если он существует
        admin_user = user_service.get_user_by_username('admin')
        if admin_user:
            admin_role = role_service.get_role_by_name('admin')
            if admin_role and admin_role.id not in admin_user.roles:
                user_service.add_role_to_user(admin_user.id, admin_role.id)
                logger.info(f"Admin role assigned to admin user")
        
        return created_any
    except Exception as e:
        logger.error(f"Error seeding default roles: {e}", exc_info=True)
        return False
