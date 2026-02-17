#!/usr/bin/env python3
"""
Worker для обработки очереди выполнения playbook runs
Thin-wrapper для обратной совместимости - делегирует в worker.main
"""
import sys
from pathlib import Path

# Добавляем путь к web для импорта worker модуля
web_dir = Path(__file__).parent.parent
sys.path.insert(0, str(web_dir))

# Импортируем и запускаем worker из нового модуля
if __name__ == '__main__':
    from worker.main import main
    main()
