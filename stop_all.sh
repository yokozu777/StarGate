#!/bin/bash
# Script to stop all services (backend, frontend, worker).
# Kills both shell (bash) and entire process tree so no python processes remain.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/.pids"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Kill process and all its children (process group)
kill_tree() {
    local pid=$1
    [ -z "$pid" ] && return 0
    if ! kill -0 "$pid" 2>/dev/null; then return 0; fi
    local pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -n "$pgid" ] && [ "$pgid" != "0" ]; then
        kill -TERM -"$pgid" 2>/dev/null || true
    else
        kill -TERM "$pid" 2>/dev/null || true
    fi
    # Child processes (bash runs python — kill those too)
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        kill_tree "$child"
    done
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    return 0
}

echo -e "${BLUE}Stopping all services...${NC}"
echo ""

stopped=0

# Stop Backend (process + child tree)
if [ -f "$PID_DIR/backend.pid" ]; then
    BACKEND_PID=$(cat "$PID_DIR/backend.pid")
    if kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo -e "${YELLOW}Stopping Backend (PID: $BACKEND_PID)...${NC}"
        kill_tree "$BACKEND_PID" && stopped=$((stopped + 1))
    fi
    rm -f "$PID_DIR/backend.pid"
fi

# Stop Frontend
if [ -f "$PID_DIR/frontend.pid" ]; then
    FRONTEND_PID=$(cat "$PID_DIR/frontend.pid")
    if kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo -e "${YELLOW}Stopping Frontend (PID: $FRONTEND_PID)...${NC}"
        kill_tree "$FRONTEND_PID" && stopped=$((stopped + 1))
    fi
    rm -f "$PID_DIR/frontend.pid"
fi

# Stop Worker
if [ -f "$PID_DIR/worker.pid" ]; then
    WORKER_PID=$(cat "$PID_DIR/worker.pid")
    if kill -0 "$WORKER_PID" 2>/dev/null; then
        echo -e "${YELLOW}Stopping Worker (PID: $WORKER_PID)...${NC}"
        kill_tree "$WORKER_PID" && stopped=$((stopped + 1))
    fi
    rm -f "$PID_DIR/worker.pid"
fi

# Stop processes on ports (fallback if PID files lost)
for port in 5000 8080; do
    PIDS=$(lsof -ti:$port 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        for pid in $PIDS; do
            echo -e "${YELLOW}Stopping process on port $port (PID: $pid)...${NC}"
            kill_tree "$pid" && stopped=$((stopped + 1))
        done
    fi
done

# Kill worker by process name (worker does not listen on port)
if pgrep -f "backend/worker.py|worker.main" >/dev/null 2>&1; then
    echo -e "${YELLOW}Stopping remaining Worker processes...${NC}"
    pkill -f "backend/worker.py|worker.main" 2>/dev/null && stopped=$((stopped + 1)) || true
    sleep 1
    pkill -9 -f "backend/worker.py|worker.main" 2>/dev/null || true
fi

if [ $stopped -gt 0 ]; then
    echo ""
    echo -e "${GREEN}✅ Services stopped: $stopped${NC}"
else
    echo -e "${BLUE}ℹ️  Services not running${NC}"
fi
