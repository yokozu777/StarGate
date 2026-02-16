# Frontend — static files behind nginx
# Build context: project root (docker build -f frontend/Dockerfile .)
FROM nginx:alpine

COPY frontend/nginx-docker.conf /etc/nginx/conf.d/default.conf
COPY frontend/ /usr/share/nginx/html/
COPY frontend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
