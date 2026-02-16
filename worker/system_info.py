#!/usr/bin/env python3
"""
Сбор системной информации о worker (OS, Python, Ansible, CPU, Memory, Hostname)
"""
import platform
import sys
import socket
import subprocess
import logging
import os
import re
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


def get_os_info() -> Dict[str, str]:
    """Собирает информацию об ОС"""
    try:
        return {
            'name': platform.system(),
            'version': platform.release(),
            'kernel': platform.version(),
            'arch': platform.machine()
        }
    except Exception as e:
        logger.warning(f"Error getting OS info: {e}")
        return {
            'name': 'Unknown',
            'version': 'Unknown',
            'kernel': 'Unknown',
            'arch': 'Unknown'
        }


def get_python_info() -> Dict[str, str]:
    """Собирает информацию о Python"""
    try:
        return {
            'version': sys.version.split()[0],  # Версия без дополнительной информации
            'implementation': platform.python_implementation(),
            'path': sys.executable
        }
    except Exception as e:
        logger.warning(f"Error getting Python info: {e}")
        return {
            'version': 'Unknown',
            'implementation': 'Unknown',
            'path': 'Unknown'
        }


def get_ansible_info() -> Dict[str, str]:
    """Собирает информацию об Ansible"""
    try:
        # Пробуем получить версию ansible-playbook
        result = subprocess.run(
            ['ansible-playbook', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            # Парсим первую строку вывода: "ansible-playbook [core 2.15.8]"
            first_line = result.stdout.split('\n')[0] if result.stdout else ''
            version = 'Unknown'
            
            # Извлекаем версию из строки вида "ansible-playbook [core 2.15.8]"
            if '[' in first_line and ']' in first_line:
                version_part = first_line.split('[')[1].split(']')[0]
                # Извлекаем версию (последнее число)
                parts = version_part.split()
                if len(parts) > 1:
                    version = parts[-1]
            
            # Определяем путь к ansible-playbook
            executable = 'ansible-playbook'
            try:
                which_result = subprocess.run(
                    ['which', 'ansible-playbook'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if which_result.returncode == 0:
                    executable = which_result.stdout.strip()
            except Exception:
                pass
            
            return {
                'version': version,
                'executable': executable
            }
    except FileNotFoundError:
        logger.warning("ansible-playbook not found in PATH")
    except subprocess.TimeoutExpired:
        logger.warning("Timeout getting ansible version")
    except Exception as e:
        logger.warning(f"Error getting Ansible info: {e}")
    
    return {
        'version': 'Not installed',
        'executable': 'Not found'
    }


def get_sshpass_info() -> Dict[str, str]:
    """Собирает информацию о sshpass"""
    try:
        result = subprocess.run(
            ['sshpass', '-V'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            # Парсим вывод вида "sshpass 1.10" или "sshpass version 1.10"
            output = result.stdout.strip() if result.stdout else result.stderr.strip()
            version = 'Unknown'
            
            # Пробуем извлечь версию
            version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', output)
            if version_match:
                version = version_match.group(1)
            
            # Определяем путь к sshpass
            executable = 'sshpass'
            try:
                which_result = subprocess.run(
                    ['which', 'sshpass'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if which_result.returncode == 0:
                    executable = which_result.stdout.strip()
            except Exception:
                pass
            
            return {
                'version': version,
                'executable': executable
            }
    except FileNotFoundError:
        logger.warning("sshpass not found in PATH")
    except subprocess.TimeoutExpired:
        logger.warning("Timeout getting sshpass version")
    except Exception as e:
        logger.warning(f"Error getting sshpass info: {e}")
    
    return {
        'version': 'Not installed',
        'executable': 'Not found'
    }


def get_openssh_info() -> Dict[str, str]:
    """Собирает информацию об OpenSSH"""
    try:
        result = subprocess.run(
            ['ssh', '-V'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0 or result.stderr:
            # Версия обычно в stderr: "OpenSSH_8.9p1 Ubuntu-3ubuntu0.1, OpenSSL 3.0.2 15 Mar 2022"
            output = (result.stderr.strip() if result.stderr else result.stdout.strip()) or ''
            version = 'Unknown'
            
            # Парсим версию вида "OpenSSH_8.9p1"
            version_match = re.search(r'OpenSSH[_-](\d+\.\d+(?:p\d+)?)', output)
            if version_match:
                version = version_match.group(1)
            
            # Определяем путь к ssh
            executable = 'ssh'
            try:
                which_result = subprocess.run(
                    ['which', 'ssh'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if which_result.returncode == 0:
                    executable = which_result.stdout.strip()
            except Exception:
                pass
            
            return {
                'version': version,
                'executable': executable
            }
    except FileNotFoundError:
        logger.warning("ssh not found in PATH")
    except subprocess.TimeoutExpired:
        logger.warning("Timeout getting OpenSSH version")
    except Exception as e:
        logger.warning(f"Error getting OpenSSH info: {e}")
    
    return {
        'version': 'Not installed',
        'executable': 'Not found'
    }


def get_mitogen_info() -> Dict[str, str]:
    """Собирает информацию о Mitogen"""
    try:
        # Пробуем импортировать mitogen и получить версию
        try:
            import mitogen
            version = getattr(mitogen, '__version__', None)
            if not version:
                # Пробуем получить версию из __init__.py
                try:
                    mitogen_path = mitogen.__file__
                    if mitogen_path:
                        # Читаем __init__.py и ищем __version__
                        init_path = os.path.join(os.path.dirname(mitogen_path), '__init__.py')
                        if os.path.exists(init_path):
                            with open(init_path, 'r') as f:
                                for line in f:
                                    if '__version__' in line:
                                        # Извлекаем версию из строки вида "__version__ = '0.3.3'"
                                        match = re.search(r"['\"]([^'\"]+)['\"]", line)
                                        if match:
                                            version = match.group(1)
                                        break
                except Exception:
                    pass
            
            if version:
                return {
                    'version': version,
                    'executable': 'Python module'
                }
        except ImportError:
            pass
        
        # Если не удалось импортировать, пробуем через pip show
        try:
            result = subprocess.run(
                ['pip', 'show', 'mitogen'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                # Парсим вывод pip show
                for line in result.stdout.split('\n'):
                    if line.startswith('Version:'):
                        version = line.split(':', 1)[1].strip()
                        return {
                            'version': version,
                            'executable': 'Python module'
                        }
        except FileNotFoundError:
            pass
        except Exception:
            pass
        
    except Exception as e:
        logger.warning(f"Error getting Mitogen info: {e}")
    
    return {
        'version': 'Not installed',
        'executable': 'Not found'
    }


def get_mitogen_strategy_plugins_path() -> Optional[str]:
    """
    Возвращает путь к директории strategy plugins для Ansible (ansible_mitogen).
    Нужен для ANSIBLE_STRATEGY_PLUGINS при использовании mitogen_linear, mitogen_free, mitogen_host_pinned.
    """
    try:
        # 1) Пакет ansible_mitogen (отдельный или внутри mitogen)
        try:
            import ansible_mitogen
            path = os.path.join(os.path.dirname(ansible_mitogen.__file__), 'plugins', 'strategy')
            if os.path.isdir(path):
                return path
        except ImportError:
            pass
        # 2) Внутри пакета mitogen
        try:
            import mitogen
            path = os.path.join(os.path.dirname(mitogen.__file__), 'ansible_mitogen', 'plugins', 'strategy')
            if os.path.isdir(path):
                return path
        except ImportError:
            pass
        # 3) Поиск в site-packages (путь вида .../python3.X/site-packages/ansible_mitogen/plugins/strategy)
        try:
            import site
            for sp in (site.getsitepackages() if hasattr(site, 'getsitepackages') else []):
                path = os.path.join(sp, 'ansible_mitogen', 'plugins', 'strategy')
                if os.path.isdir(path):
                    return path
            # Типичный путь при установке в систему
            import sys
            base = f"/usr/local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            path = os.path.join(base, 'ansible_mitogen', 'plugins', 'strategy')
            if os.path.isdir(path):
                return path
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"Could not resolve Mitogen strategy_plugins path: {e}")
    return None


def get_cpu_info() -> Dict[str, Any]:
    """Собирает информацию о CPU"""
    try:
        cores = os.cpu_count() or 1
        processor = platform.processor()
        
        # Пробуем получить более детальную информацию о CPU (Linux)
        model = processor
        if platform.system() == 'Linux':
            try:
                with open('/proc/cpuinfo', 'r') as f:
                    for line in f:
                        if line.startswith('model name'):
                            model = line.split(':', 1)[1].strip()
                            break
            except Exception:
                pass
        
        return {
            'cores': cores,
            'model': model
        }
    except Exception as e:
        logger.warning(f"Error getting CPU info: {e}")
        return {
            'cores': 1,
            'model': 'Unknown'
        }


def get_memory_info() -> Dict[str, int]:
    """Собирает информацию о памяти"""
    try:
        # Пробуем использовать psutil если доступен
        try:
            import psutil
            total_bytes = psutil.virtual_memory().total
            total_mb = int(total_bytes / (1024 * 1024))
            return {
                'total_mb': total_mb
            }
        except ImportError:
            # Fallback: читаем /proc/meminfo (Linux)
            if platform.system() == 'Linux':
                try:
                    with open('/proc/meminfo', 'r') as f:
                        for line in f:
                            if line.startswith('MemTotal:'):
                                # Формат: "MemTotal:       32768 kB"
                                parts = line.split()
                                if len(parts) >= 2:
                                    total_kb = int(parts[1])
                                    total_mb = int(total_kb / 1024)
                                    return {
                                        'total_mb': total_mb
                                    }
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Error getting memory info: {e}")
    
    return {
        'total_mb': 0  # Unknown
    }


def get_hostname() -> str:
    """Получает hostname"""
    try:
        return socket.gethostname()
    except Exception as e:
        logger.warning(f"Error getting hostname: {e}")
        return 'Unknown'


def collect_system_info() -> Dict[str, Any]:
    """
    Собирает всю системную информацию
    
    Returns:
        dict с полной информацией о системе
    """
    return {
        'os': get_os_info(),
        'python': get_python_info(),
        'ansible': get_ansible_info(),
        'sshpass': get_sshpass_info(),
        'openssh': get_openssh_info(),
        'mitogen': get_mitogen_info(),
        'cpu': get_cpu_info(),
        'memory': get_memory_info(),
        'hostname': get_hostname()
    }
