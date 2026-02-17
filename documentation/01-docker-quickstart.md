# Docker Quick Start

This guide explains how to run Stargate using Docker Compose.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) installed
- Git (for local build option)

---

## Option 1: Pre-built Images (Docker Hub)

Fastest way to run Stargate — pull images from Docker Hub.

### 1. Clone the repository

```bash
git clone git@github.com:yokozu777/StarGate.git
cd StarGate
```

### 2. Start the stack

The repo includes `docker-compose.hub.yml` with pre-built image references:

```bash
docker compose -f docker-compose.hub.yml up -d
```

### 3. Access the application

- **Web UI:** http://localhost:8080
- **Backend API:** http://localhost:5000

---

## Option 2: Local Build (from source)

Use this when you have the repository and want to build images locally.

### 1. Clone the repository

```bash
git clone git@github.com:yokozu777/StarGate.git
cd StarGate
```

### 2. Build and start

```bash
docker compose up -d --build
```

### 3. Access the application

- **Web UI:** http://localhost:8080
- **Backend API:** http://localhost:5000

---

## Common Commands

| Action | Command |
|--------|---------|
| Start in background | `docker compose up -d` |
| View logs | `docker compose logs -f` |
| Stop | `docker compose down` |
| Rebuild and start | `docker compose up -d --build` |

---

## Data Persistence

All persistent data is stored in the `./data` directory:

- Projects, inventory, playbooks
- Worker registration token
- SSH keys and vault secrets

Ensure this directory exists and has proper permissions.

---

## Worker Network Mode

The worker runs with `network_mode: host` so it can reach inventory hosts (e.g. `192.168.1.x`) via SSH. Without this, host connectivity checks would fail from inside the container.

---

## Troubleshooting

**Port already in use**

- Change ports in the compose file (e.g. `"8081:80"` for frontend).

**Worker cannot reach hosts**

- Ensure the worker has `network_mode: host` and can reach your inventory hosts from the host machine.

**First run**

- On first start, the worker self-registers with the backend and saves its token to `./data/worker.token`.
