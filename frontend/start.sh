#!/bin/bash
# Скрипт для запуска фронтенда (статический сервер)

cd "$(dirname "$0")"

echo "=========================================="
echo "Ansible Variables Configurator - Frontend"
echo "=========================================="
echo ""

# Проверка наличия Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 not found. Please install Python3."
    exit 1
fi

# Проверка переменной API_URL
if [ -z "$API_URL" ]; then
    echo "⚠️  API_URL not set. Using default: http://localhost:5000"
    echo "   Set API_URL environment variable to change backend URL"
    export API_URL="http://localhost:5000"
fi

echo "📡 Backend API URL: $API_URL"
echo ""

# Создаем временный HTML с установкой API_URL через meta tag
# Обновляем meta tag в index.html напрямую
sed -i "s|<meta name=\"api-url\" content=\"[^\"]*\">|<meta name=\"api-url\" content=\"$API_URL\">|" index.html

echo "✅ Starting frontend server..."
echo "🌐 Open in browser: http://localhost:8080"
echo "📡 Backend API: $API_URL"
echo ""
echo "Press Ctrl+C to stop"
echo ""

# Запускаем простой HTTP сервер
python3 -m http.server 8080
