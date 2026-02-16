#!/usr/bin/env python3
"""
Entrypoint CLI для worker
"""
import argparse
import logging
import os
# Инициализируем логирование при импорте
from . import logging_setup  # noqa: F401
from .loop import worker_loop

logger = logging.getLogger(__name__)


def main():
    """CLI entrypoint для worker"""
    parser = argparse.ArgumentParser(description='Ansible AWX Worker (HTTP API mode)')
    parser.add_argument('--server-url', type=str, default=os.environ.get('WORKER_SERVER_URL', 'http://localhost:5000'),
                        help='Server URL (default: http://localhost:5000 or WORKER_SERVER_URL env)')
    parser.add_argument('--poll-interval', type=int, default=3, help='Poll interval in seconds (default: 3, minimum recommended: 2)')
    parser.add_argument('--project-id', help='Process only specific project')
    parser.add_argument('--worker-name', help='Worker name (default: hostname)')
    parser.add_argument('--max-concurrency', type=int, default=1, help='Maximum concurrent runs (default: 1)')
    parser.add_argument('--tags', nargs='+', help='Worker tags for filtering (e.g., --tags docker winrm)')
    
    args = parser.parse_args()
    
    worker_loop(
        server_url=args.server_url,
        poll_interval=args.poll_interval,
        project_id=args.project_id,
        worker_name=args.worker_name,
        max_concurrency=args.max_concurrency,
        tags=args.tags
    )


if __name__ == '__main__':
    main()
