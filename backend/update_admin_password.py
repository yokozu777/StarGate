#!/usr/bin/env python3
"""
Скрипт для обновления пароля пользователя admin
"""
import os
import sys
from pathlib import Path

# Добавляем текущую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from user_service import UserService

# Определяем DATA_DIR (из env или project_root/data)
_project_root = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', str(_project_root / 'data')))

# Создаем сервис
user_service = UserService(DATA_DIR)

# Находим пользователя admin
admin_user = user_service.get_user_by_username('admin')

if not admin_user:
    print("❌ Пользователь admin не найден!")
    sys.exit(1)

# Обновляем пароль
try:
    success = user_service.set_password(admin_user.id, 'admin123')
    if success:
        print("✅ Пароль пользователя admin успешно обновлен!")
        print("   Новый пароль: admin123")
    else:
        print("❌ Не удалось обновить пароль")
        sys.exit(1)
except Exception as e:
    print(f"❌ Ошибка при обновлении пароля: {e}")
    sys.exit(1)
