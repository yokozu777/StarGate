#!/bin/sh
# Substitutes API_URL into meta api-url before starting nginx
API_URL="${API_URL:-http://localhost:5000}"
sed -i "s|<meta name=\"api-url\" content=\"[^\"]*\">|<meta name=\"api-url\" content=\"$API_URL\">|" /usr/share/nginx/html/index.html
exec "$@"
