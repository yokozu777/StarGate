FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    ansible \
    sshpass \
    bash \
    net-tools \
    iputils-ping \
    python3-passlib \
    && rm -rf /var/lib/apt/lists/*

COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY worker/ /app/worker/

RUN mkdir -p /app/data

ENV PYTHONPATH=/app
# Default for bridge network; with network_mode: host in compose set WORKER_SERVER_URL=http://127.0.0.1:5000
ENV WORKER_SERVER_URL=http://backend:5000

CMD ["python", "-m", "worker.main", "--poll-interval", "5", "--max-concurrency", "1"]
