# Backend API (Flask)
# Build context: project root (docker build -f backend/Dockerfile .)
FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    ansible \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend /app

RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python", "app.py"]
