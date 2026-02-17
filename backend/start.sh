#!/bin/bash
# Скрипт для запуска веб-сервиса конфигурации Ansible переменных (Backend API)

cd "$(dirname "$0")"

echo "=========================================="
echo "Ansible Variables Configurator - Backend"
echo "=========================================="
echo ""

# Проверка наличия Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 not found. Please install Python3."
    exit 1
fi

# Проверка и установка зависимостей
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

echo "📦 Activating virtual environment..."
source venv/bin/activate

echo "📦 Installing dependencies..."
pip install -q -r requirements.txt

echo ""
echo "✅ Starting backend API service..."
echo "🌐 API available at: http://localhost:5000"
echo "📡 CORS enabled for frontend integration"
echo ""
echo "Press Ctrl+C to stop"
echo ""

python3 app.py
