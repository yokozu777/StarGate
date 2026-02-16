#!/usr/bin/env python3
"""
HTTP сервер для encrypt/decrypt через ansible-vault.
Запускается в worker (где установлен ansible).
"""
import json
import logging
import os
from pathlib import Path

from flask import Flask, request, jsonify

from .config import PROJECTS_DIR, get_project_vault_keys_dir, get_project_vaults_file
from .vault_utils import (
    is_ansible_vault_encrypted,
    parse_vault_id_from_header,
    ansible_vault_encrypt,
    ansible_vault_decrypt,
    get_vault_name_for_vault_id,
)

logger = logging.getLogger(__name__)

app = Flask(__name__)


def _load_vaults(project_id):
    """Загружает vaults из secrets/vault/vaults.json"""
    vaults_file = get_project_vaults_file(project_id)
    if not vaults_file.exists():
        return []
    try:
        with open(vaults_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Error loading vaults {project_id}: {e}")
        return []


def _get_key_password_for_vault(project_id, vault_uuid):
    """Получить пароль ключа по uuid vault."""
    vaults = _load_vaults(project_id)
    vault = next((v for v in vaults if v.get('id') == vault_uuid), None)
    if not vault:
        return None
    key_id = vault.get('keyId')
    if not key_id:
        return None
    vault_keys_dir = get_project_vault_keys_dir(project_id)
    pass_file = vault_keys_dir / f'{key_id}.pass'
    if not pass_file.exists():
        return None
    try:
        with open(pass_file, 'r', encoding='utf-8') as f:
            return f.read().rstrip('\n')
    except Exception:
        return None


@app.route('/encrypt', methods=['POST'])
def encrypt():
    """POST { project_id, vault_id, content } -> { content }"""
    try:
        data = request.json or {}
        project_id = data.get('project_id')
        vault_id = data.get('vault_id')
        content = data.get('content', '')
        if not project_id or not vault_id:
            return jsonify({'success': False, 'error': 'project_id and vault_id required'}), 400
        if not (PROJECTS_DIR / project_id).exists():
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        password = _get_key_password_for_vault(project_id, vault_id)
        if not password:
            return jsonify({'success': False, 'error': 'Vault or key not found'}), 404
        if is_ansible_vault_encrypted(content):
            return jsonify({'success': False, 'error': 'Content is already encrypted'}), 400
        vault_label = get_vault_name_for_vault_id(project_id, vault_id)
        ok, result = ansible_vault_encrypt(content, password, vault_id=vault_label)
        if not ok:
            return jsonify({'success': False, 'error': result or 'Encryption failed'}), 500
        return jsonify({'success': True, 'content': result})
    except Exception as e:
        logger.exception("Encrypt error")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/decrypt', methods=['POST'])
def decrypt():
    """POST { project_id, vault_id, content } -> { content }"""
    try:
        data = request.json or {}
        project_id = data.get('project_id')
        vault_id = data.get('vault_id')
        content = data.get('content', '')
        if not project_id or not vault_id:
            return jsonify({'success': False, 'error': 'project_id and vault_id required'}), 400
        if not (PROJECTS_DIR / project_id).exists():
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        if not is_ansible_vault_encrypted(content):
            return jsonify({'success': True, 'content': content})
        password = _get_key_password_for_vault(project_id, vault_id)
        if not password:
            return jsonify({'success': False, 'error': 'Vault or key not found'}), 404
        vault_id_header = parse_vault_id_from_header(content)
        use_vault_id = vault_id_header if vault_id_header else None
        ok, result = ansible_vault_decrypt(content, password, use_vault_id)
        if not ok:
            return jsonify({'success': False, 'error': result or 'Decryption failed'}), 500
        return jsonify({'success': True, 'content': result})
    except Exception as e:
        logger.exception("Decrypt error")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True})


def run_vault_server():
    """Запуск vault HTTP сервера. Слушает 0.0.0.0 для доступа из контейнеров."""
    port = int(os.environ.get('VAULT_SERVER_PORT', '9999'))
    host = os.environ.get('VAULT_SERVER_HOST', '0.0.0.0')
    logger.info(f"Vault server starting on {host}:{port}")
    app.run(host=host, port=port, threaded=True, use_reloader=False)
