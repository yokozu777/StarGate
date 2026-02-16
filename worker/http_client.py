#!/usr/bin/env python3
"""
HTTP клиент для общения worker с сервером через API
"""
import requests
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class WorkerHTTPClient:
    """HTTP клиент для worker API"""
    
    def __init__(self, server_url: str, worker_token: Optional[str] = None):
        """
        Args:
            server_url: URL сервера (например, http://localhost:5000)
            worker_token: Токен worker (если None, будет загружен из файла, env или получен при регистрации)
        """
        import os
        
        self.server_url = server_url.rstrip('/')
        self.worker_token = worker_token
        self.worker_id = None
        # WORKER_TOKEN_FILE — путь к файлу с токеном (для Docker: /app/data/worker.token)
        token_file_env = os.environ.get('WORKER_TOKEN_FILE')
        self.token_file = Path(token_file_env) if token_file_env else Path(__file__).parent / '.token'
        
        # Приоритет загрузки токена:
        # 1. Переданный параметр
        # 2. Переменная окружения WORKER_TOKEN
        # 3. Файл worker/.token
        
        if not self.worker_token:
            # Пробуем из переменной окружения
            self.worker_token = os.environ.get('WORKER_TOKEN')
            if self.worker_token:
                logger.info("Loaded worker token from WORKER_TOKEN environment variable")
        
        if not self.worker_token and self.token_file.exists():
            try:
                with open(self.token_file, 'r', encoding='utf-8') as f:
                    token_data = f.read().strip().split('\n')
                    if len(token_data) >= 2:
                        self.worker_id = token_data[0]
                        self.worker_token = token_data[1]
                        logger.info(f"Loaded worker token from {self.token_file}")
                    elif len(token_data) == 1:
                        # Только токен без worker_id
                        self.worker_token = token_data[0]
                        logger.info(f"Loaded worker token from {self.token_file} (worker_id will be determined from token)")
            except Exception as e:
                logger.warning(f"Error loading token from file: {e}")
    
    def _save_token(self, worker_id: str, worker_token: str):
        """Сохраняет токен в файл"""
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_file, 'w', encoding='utf-8') as f:
                f.write(f"{worker_id}\n{worker_token}\n")
            self.worker_id = worker_id
            self.worker_token = worker_token
            logger.info(f"Saved worker token to {self.token_file}")
        except Exception as e:
            logger.error(f"Error saving token: {e}")
    
    def _get_headers(self) -> Dict[str, str]:
        """Возвращает headers с авторизацией"""
        headers = {'Content-Type': 'application/json'}
        if self.worker_token:
            headers['Authorization'] = f'Bearer {self.worker_token}'
        return headers
    
    def register(self, name: str, capabilities: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Регистрирует worker на сервере
        
        Returns:
            dict с workerId и workerToken
        """
        url = f"{self.server_url}/api/worker/register"
        data = {
            'name': name,
            'capabilities': capabilities or {}
        }
        
        try:
            response = requests.post(url, json=data, headers={'Content-Type': 'application/json'}, timeout=10)
            response.raise_for_status()
            result = response.json()
            
            if result.get('success'):
                worker_id = result['workerId']
                worker_token = result['workerToken']
                self._save_token(worker_id, worker_token)
                return result
            else:
                raise Exception(f"Registration failed: {result.get('error')}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error registering worker: {e}")
            raise
    
    def claim(self, project_id: Optional[str] = None, max_concurrency: int = 1, 
              tags: Optional[list] = None) -> Optional[Dict[str, Any]]:
        """
        Запрашивает следующую задачу из очереди
        
        Args:
            project_id: Опциональный ID проекта для фильтрации
            max_concurrency: Максимальное количество параллельных runs (по умолчанию 1)
            tags: Опциональные tags worker для фильтрации по requirements
        
        Returns:
            dict с данными execution или None если очередь пуста
        """
        url = f"{self.server_url}/api/worker/claim"
        data = {
            'maxConcurrency': max_concurrency
        }
        if project_id:
            data['projectId'] = project_id
        if tags:
            data['tags'] = tags
        
        try:
            response = requests.post(url, json=data, headers=self._get_headers(), timeout=10)
            
            if response.status_code == 204:
                # No Content - очередь пуста
                return None
            
            # Проверяем 401 (Unauthorized) - токен невалиден, нужно перерегистрироваться
            if response.status_code == 401:
                raise requests.exceptions.HTTPError(
                    f"401 Client Error: UNAUTHORIZED - Invalid token, need re-registration",
                    response=response
                )
            
            # Проверяем 429 отдельно для лучшей обработки
            if response.status_code == 429:
                raise requests.exceptions.HTTPError(
                    f"429 Client Error: TOO MANY REQUESTS for url: {url}",
                    response=response
                )
            
            response.raise_for_status()
            result = response.json()
            
            if result.get('success'):
                return result
            else:
                raise Exception(f"Claim failed: {result.get('error')}")
        except requests.exceptions.RequestException as e:
            # Не логируем здесь - логирование будет в loop.py с правильной обработкой
            raise
    
    def send_log(self, execution_id: str, text: str, ts: Optional[float] = None) -> bool:
        """
        Отправляет лог в execution
        
        Args:
            execution_id: ID execution
            text: Текст лога
            ts: Timestamp (опционально)
        
        Returns:
            True если успешно
        """
        url = f"{self.server_url}/api/worker/executions/{execution_id}/log"
        data = {'text': text}
        if ts is not None:
            data['ts'] = ts
        
        try:
            response = requests.post(url, json=data, headers=self._get_headers(), timeout=10)
            response.raise_for_status()
            result = response.json()
            return result.get('success', False)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending log: {e}")
            return False
    
    def finish_execution(self, execution_id: str, status: str, finished_at: Optional[float] = None,
                        duration: Optional[int] = None, return_code: Optional[int] = None,
                        error: Optional[str] = None, result: Optional[dict] = None) -> bool:
        """
        Завершает execution
        
        Args:
            execution_id: ID execution
            status: 'SUCCESS', 'FAILED' или 'CANCELED'
            finished_at: Timestamp завершения
            duration: Длительность в секундах
            return_code: Код возврата ansible-playbook
            error: Сообщение об ошибке (опционально)
            result: Результат выполнения для HOST_CHECK и HOST_FACTS (опционально)
        
        Returns:
            True если успешно
        """
        url = f"{self.server_url}/api/worker/executions/{execution_id}/finish"
        data = {
            'status': status,
            'finishedAt': finished_at or time.time(),
            'duration': duration,
            'returnCode': return_code
        }
        if error:
            data['error'] = error
        if result:
            data['result'] = result
        
        try:
            response = requests.post(url, json=data, headers=self._get_headers(), timeout=10)
            response.raise_for_status()
            result = response.json()
            return result.get('success', False)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error finishing execution: {e}")
            return False
    
    def heartbeat(self, current_execution_id: Optional[str] = None) -> bool:
        """
        Отправляет heartbeat
        
        Args:
            current_execution_id: ID текущего execution (опционально)
        
        Returns:
            True если успешно, False если ошибка (включая 401)
        """
        url = f"{self.server_url}/api/worker/heartbeat"
        data = {
            'ts': time.time()
        }
        
        # Добавляем workerId только если он известен
        if self.worker_id:
            data['workerId'] = self.worker_id
        
        # Добавляем currentExecutionId если указан
        if current_execution_id:
            data['currentExecutionId'] = current_execution_id
        
        try:
            response = requests.post(url, json=data, headers=self._get_headers(), timeout=5)
            
            # Проверяем 401 (Unauthorized) - токен невалиден
            if response.status_code == 401:
                logger.warning("Heartbeat failed: 401 Unauthorized - token is invalid")
                return False
            
            response.raise_for_status()
            result = response.json()
            
            # Если сервер вернул workerId, сохраняем его
            if result.get('workerId') and not self.worker_id:
                self.worker_id = result['workerId']
                logger.info(f"Received workerId from server: {self.worker_id}")
            
            # Если сервер запросил systemInfo, отправляем его
            if result.get('requestSystemInfo'):
                from .system_info import collect_system_info
                system_info = collect_system_info()
                self.send_system_info(system_info)
                logger.info("Sent system info in response to server request")
            
            return result.get('success', False)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error sending heartbeat: {e}")
            return False
    
    def get_execution_status(self, execution_id: str, project_id: Optional[str] = None) -> Optional[str]:
        """
        Получает текущий статус execution
        
        Args:
            execution_id: ID execution
            project_id: ID проекта (опционально, для более быстрого поиска)
        
        Returns:
            Статус execution или None если не найден
        """
        # Используем стандартный API endpoint для получения execution
        url = f"{self.server_url}/api/executions/{execution_id}"
        params = {}
        if project_id:
            params['projectId'] = project_id
        
        try:
            response = requests.get(url, params=params, headers=self._get_headers(), timeout=5)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            result = response.json()
            
            if result.get('success') and result.get('execution'):
                return result['execution'].get('status')
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error getting execution status: {e}")
            return None
    
    def send_system_info(self, system_info: Dict[str, Any]) -> bool:
        """
        Отправляет системную информацию на сервер
        
        Args:
            system_info: Словарь с системной информацией
        
        Returns:
            True если успешно
        """
        url = f"{self.server_url}/api/worker/system-info"
        data = {
            'systemInfo': system_info
        }
        
        try:
            response = requests.post(url, json=data, headers=self._get_headers(), timeout=10)
            response.raise_for_status()
            # Ожидаем 204 No Content или 200 с success
            if response.status_code == 204:
                return True
            result = response.json()
            return result.get('success', False)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error sending system info: {e}")
            return False