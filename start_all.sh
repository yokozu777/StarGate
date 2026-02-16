#!/bin/bash
# Script to start all services: Backend, Frontend and Worker

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WEB_DIR="$SCRIPT_DIR"
BACKEND_DIR="$WEB_DIR/backend"
FRONTEND_DIR="$WEB_DIR/frontend"
WORKER_DIR="$WEB_DIR/worker"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# PID files for process tracking
PID_DIR="$WEB_DIR/.pids"
mkdir -p "$PID_DIR"
BACKEND_PID="$PID_DIR/backend.pid"
FRONTEND_PID="$PID_DIR/frontend.pid"
WORKER_PID="$PID_DIR/worker.pid"

# Startup logs (stdout/stderr of launch scripts)
# Note: Applications write their own logs to data/logs/ via Python logging
# These files contain only launch script output for debugging
LOG_DIR="$WEB_DIR/../data/logs"
mkdir -p "$LOG_DIR"
BACKEND_STARTUP_LOG="$LOG_DIR/backend-startup.log"
FRONTEND_STARTUP_LOG="$LOG_DIR/frontend-startup.log"
WORKER_STARTUP_LOG="$LOG_DIR/worker-startup.log"

# Main application logs (for reference)
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"
WORKER_LOG="$LOG_DIR/worker.log"

# Cleanup function on exit
cleanup() {
    echo -e "\n${YELLOW}Stopping all services...${NC}"
    
    if [ -f "$BACKEND_PID" ]; then
        BACKEND_PID_VALUE=$(cat "$BACKEND_PID")
        if kill -0 "$BACKEND_PID_VALUE" 2>/dev/null; then
            echo -e "${BLUE}Stopping Backend (PID: $BACKEND_PID_VALUE)...${NC}"
            kill "$BACKEND_PID_VALUE" 2>/dev/null || true
        fi
        rm -f "$BACKEND_PID"
    fi
    
    if [ -f "$FRONTEND_PID" ]; then
        FRONTEND_PID_VALUE=$(cat "$FRONTEND_PID")
        if kill -0 "$FRONTEND_PID_VALUE" 2>/dev/null; then
            echo -e "${BLUE}Stopping Frontend (PID: $FRONTEND_PID_VALUE)...${NC}"
            kill "$FRONTEND_PID_VALUE" 2>/dev/null || true
        fi
        rm -f "$FRONTEND_PID"
    fi
    
    if [ -f "$WORKER_PID" ]; then
        WORKER_PID_VALUE=$(cat "$WORKER_PID")
        if kill -0 "$WORKER_PID_VALUE" 2>/dev/null; then
            echo -e "${BLUE}Stopping Worker (PID: $WORKER_PID_VALUE)...${NC}"
            kill "$WORKER_PID_VALUE" 2>/dev/null || true
        fi
        rm -f "$WORKER_PID"
    fi
    
    # Kill child processes
    pkill -P $$ 2>/dev/null || true
    
    echo -e "${GREEN}All services stopped${NC}"
    exit 0
}

# Set signal handler
trap cleanup SIGINT SIGTERM

# Function to check port availability
check_port() {
    local port=$1
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1 ; then
        return 0
    else
        return 1
    fi
}

# Function to wait for port availability
wait_for_port() {
    local port=$1
    local service=$2
    local max_attempts=30
    local attempt=0
    
    echo -e "${YELLOW}Waiting for $service on port $port...${NC}"
    while [ $attempt -lt $max_attempts ]; do
        if check_port $port; then
            echo -e "${GREEN}$service started on port $port${NC}"
            return 0
        fi
        sleep 1
        attempt=$((attempt + 1))
    done
    
    echo -e "${RED}Timeout waiting for $service on port $port${NC}"
    return 1
}

echo -e "${BLUE}=========================================="
echo "Ansible Variables Configurator"
echo "Starting all services"
echo "==========================================${NC}"
echo ""

# Check required files exist
if [ ! -f "$BACKEND_DIR/start.sh" ]; then
    echo -e "${RED}❌ Backend start.sh not found: $BACKEND_DIR/start.sh${NC}"
    exit 1
fi

if [ ! -f "$FRONTEND_DIR/start.sh" ]; then
    echo -e "${RED}❌ Frontend start.sh not found: $FRONTEND_DIR/start.sh${NC}"
    exit 1
fi

if [ ! -f "$WORKER_DIR/main.py" ]; then
    echo -e "${RED}❌ Worker main.py not found: $WORKER_DIR/main.py${NC}"
    exit 1
fi

# Clean old PID files
rm -f "$BACKEND_PID" "$FRONTEND_PID" "$WORKER_PID"

# Free ports if occupied
echo -e "${YELLOW}Checking and freeing ports...${NC}"
for port in 5000 8080; do
    PIDS=$(lsof -ti:$port 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo -e "${YELLOW}⚠️  Port $port occupied by processes: $PIDS${NC}"
        # Try to stop all processes on port
        for pid in $PIDS; do
            echo -e "${BLUE}   Stopping process $pid...${NC}"
            kill "$pid" 2>/dev/null || true
        done
        sleep 2
        # Check again and force kill if needed
        PIDS2=$(lsof -ti:$port 2>/dev/null || true)
        if [ -n "$PIDS2" ]; then
            echo -e "${YELLOW}   Force stopping processes: $PIDS2...${NC}"
            for pid in $PIDS2; do
                kill -9 "$pid" 2>/dev/null || true
            done
            sleep 1
        fi
        # Final check
        PIDS3=$(lsof -ti:$port 2>/dev/null || true)
        if [ -n "$PIDS3" ]; then
            echo -e "${RED}❌ Failed to free port $port. Processes: $PIDS3${NC}"
            echo -e "${RED}   Try manually: kill -9 $PIDS3${NC}"
            exit 1
        else
            echo -e "${GREEN}   ✓ Port $port freed${NC}"
        fi
    else
        echo -e "${GREEN}   ✓ Port $port free${NC}"
    fi
done
echo ""

# Start Backend
echo -e "${GREEN}🚀 Starting Backend...${NC}"
cd "$BACKEND_DIR"
nohup bash start.sh > "$BACKEND_STARTUP_LOG" 2>&1 &
BACKEND_PID_VALUE=$!
echo $BACKEND_PID_VALUE > "$BACKEND_PID"
echo -e "${BLUE}   Backend PID: $BACKEND_PID_VALUE${NC}"
echo -e "${BLUE}   App logs: $BACKEND_LOG${NC}"
echo -e "${BLUE}   Startup logs: $BACKEND_STARTUP_LOG${NC}"

# Wait for Backend to start
if wait_for_port 5000 "Backend"; then
    echo ""
else
    echo -e "${RED}❌ Backend failed to start${NC}"
    cleanup
    exit 1
fi

# Start Frontend
echo -e "${GREEN}🚀 Starting Frontend...${NC}"
cd "$FRONTEND_DIR"
nohup bash start.sh > "$FRONTEND_STARTUP_LOG" 2>&1 &
FRONTEND_PID_VALUE=$!
echo $FRONTEND_PID_VALUE > "$FRONTEND_PID"
echo -e "${BLUE}   Frontend PID: $FRONTEND_PID_VALUE${NC}"
echo -e "${BLUE}   App logs: $FRONTEND_LOG${NC}"
echo -e "${BLUE}   Startup logs: $FRONTEND_STARTUP_LOG${NC}"

# Wait for Frontend to start
if wait_for_port 8080 "Frontend"; then
    echo ""
else
    echo -e "${YELLOW}⚠️  Frontend may still be starting...${NC}"
    echo ""
fi

# Function to ensure worker token exists
ensure_worker_token() {
    local WORKER_TOKEN_FILE="$WEB_DIR/worker/.token"
    local WORKER_NAME="local-worker"
    
    # Check token file exists
    if [ -f "$WORKER_TOKEN_FILE" ] && [ -r "$WORKER_TOKEN_FILE" ]; then
        echo -e "${GREEN}✓ Worker token found: $WORKER_TOKEN_FILE${NC}"
        # Load token from file
        # File format: first line - worker_id (optional), second line - token
        # Or single line with token only
        local line_count=$(wc -l < "$WORKER_TOKEN_FILE" | tr -d ' ')
        local token_from_file=""
        
        if [ "$line_count" -ge 2 ]; then
            # File contains worker_id and token
            token_from_file=$(sed -n '2p' "$WORKER_TOKEN_FILE" | tr -d '\n\r' | tr -d ' ')
        elif [ "$line_count" -eq 1 ]; then
            # File contains token only
            token_from_file=$(head -n 1 "$WORKER_TOKEN_FILE" | tr -d '\n\r' | tr -d ' ')
        fi
        
        if [ -n "$token_from_file" ] && [ "$token_from_file" != "" ]; then
            export WORKER_TOKEN="$token_from_file"
            echo -e "${BLUE}   Using existing worker token${NC}"
            return 0
        else
            echo -e "${YELLOW}⚠️  Token file empty or corrupted. Creating new worker.${NC}"
        fi
    fi
    
    # If no token, create worker via API
    echo -e "${YELLOW}⚠️  Worker token not found. Creating new worker...${NC}"
    
    # Wait for backend to be ready to accept requests (check port)
    local max_attempts=30
    local attempt=0
    while [ $attempt -lt $max_attempts ]; do
        if check_port 5000; then
            # Also verify API responds
            if curl -s -f "http://localhost:5000/api/admin/workers" > /dev/null 2>&1 || \
               curl -s -f "http://localhost:5000/api/worker/register" -X POST -H "Content-Type: application/json" -d '{}' > /dev/null 2>&1; then
                break
            fi
        fi
        sleep 1
        attempt=$((attempt + 1))
    done
    
    if [ $attempt -eq $max_attempts ]; then
        echo -e "${RED}❌ Backend not responding. Failed to create worker.${NC}"
        return 1
    fi
    
    # Create worker via API registration
    echo -e "${BLUE}   Registering worker '$WORKER_NAME'...${NC}"
    local response=$(curl -s -X POST "http://localhost:5000/api/worker/register" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"$WORKER_NAME\", \"capabilities\": {}, \"tags\": [\"default\", \"local\"]}" 2>&1)
    
    if echo "$response" | grep -q '"success":true'; then
        # Extract workerId and workerToken from response
        local worker_id=$(echo "$response" | grep -o '"workerId":"[^"]*"' | cut -d'"' -f4)
        local worker_token=$(echo "$response" | grep -o '"workerToken":"[^"]*"' | cut -d'"' -f4)
        
        if [ -n "$worker_id" ] && [ -n "$worker_token" ]; then
            # Save token to file
            mkdir -p "$(dirname "$WORKER_TOKEN_FILE")"
            echo "$worker_id" > "$WORKER_TOKEN_FILE"
            echo "$worker_token" >> "$WORKER_TOKEN_FILE"
            export WORKER_TOKEN="$worker_token"
            echo -e "${GREEN}✓ Worker created and token saved: $WORKER_TOKEN_FILE${NC}"
            echo -e "${BLUE}   Worker ID: $worker_id${NC}"
            return 0
        else
            echo -e "${RED}❌ Failed to extract token from API response${NC}"
            echo -e "${YELLOW}   Response: $response${NC}"
            return 1
        fi
    else
        echo -e "${RED}❌ Worker creation failed${NC}"
        echo -e "${YELLOW}   Response: $response${NC}"
        return 1
    fi
}

# Start Worker
echo -e "${GREEN}🚀 Starting Worker...${NC}"

# Ensure worker token exists
if ! ensure_worker_token; then
    echo -e "${YELLOW}⚠️  Continuing without token. Worker will self-register.${NC}"
fi

WORKER_SERVER_URL="${WORKER_SERVER_URL:-http://localhost:5000}"
export WORKER_SERVER_URL
[ -n "$WORKER_TOKEN" ] && export WORKER_TOKEN

# venv and worker dependencies (from project root)
(
    cd "$WEB_DIR"
    [ ! -d "venv" ] && python3 -m venv venv
    source venv/bin/activate
    pip install -q -r worker/requirements.txt 2>/dev/null || pip install -q requests 2>/dev/null || true
    export PYTHONPATH="$WEB_DIR"
    exec python3 -m worker.main --server-url "$WORKER_SERVER_URL" --poll-interval 5 --max-concurrency 1
) >> "$WORKER_STARTUP_LOG" 2>&1 &
WORKER_PID_VALUE=$!
echo $WORKER_PID_VALUE > "$WORKER_PID"
echo -e "${BLUE}   Worker PID: $WORKER_PID_VALUE${NC}"
echo -e "${BLUE}   App logs: $WORKER_LOG${NC}"
echo -e "${BLUE}   Startup logs: $WORKER_STARTUP_LOG${NC}"

echo ""
echo -e "${GREEN}=========================================="
echo "✅ All services started!"
echo "==========================================${NC}"
echo ""
echo -e "${BLUE}📍 Services:${NC}"
echo -e "   ${GREEN}Backend API:${NC}  http://localhost:5000"
echo -e "   ${GREEN}Frontend:${NC}     http://localhost:8080"
echo -e "   ${GREEN}Worker:${NC}       running"
echo ""
echo -e "${BLUE}📋 Application logs (data/logs/):${NC}"
echo -e "   Backend:  tail -f $BACKEND_LOG"
echo -e "   Frontend: tail -f $FRONTEND_LOG"
echo -e "   Worker:   tail -f $WORKER_LOG"
echo ""
echo -e "${BLUE}📋 Startup logs (for debugging):${NC}"
echo -e "   Backend:  tail -f $BACKEND_STARTUP_LOG"
echo -e "   Frontend: tail -f $FRONTEND_STARTUP_LOG"
echo -e "   Worker:   tail -f $WORKER_STARTUP_LOG"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop all services${NC}"
echo ""

# Function to show last log lines
show_logs() {
    echo -e "\n${BLUE}=== Last application log lines ===${NC}"
    echo -e "${YELLOW}Backend:${NC}"
    tail -n 5 "$BACKEND_LOG" 2>/dev/null || echo "  (log empty)"
    echo ""
    echo -e "${YELLOW}Frontend:${NC}"
    tail -n 5 "$FRONTEND_LOG" 2>/dev/null || echo "  (log empty)"
    echo ""
    echo -e "${YELLOW}Worker:${NC}"
    tail -n 5 "$WORKER_LOG" 2>/dev/null || echo "  (log empty)"
    echo ""
}

# Periodically show status
while true; do
    sleep 10
    
    # Check processes are still alive
    if [ -f "$BACKEND_PID" ]; then
        BACKEND_PID_VALUE=$(cat "$BACKEND_PID")
        if ! kill -0 "$BACKEND_PID_VALUE" 2>/dev/null; then
            echo -e "${RED}❌ Backend process exited${NC}"
            cleanup
            exit 1
        fi
    fi
    
    if [ -f "$FRONTEND_PID" ]; then
        FRONTEND_PID_VALUE=$(cat "$FRONTEND_PID")
        if ! kill -0 "$FRONTEND_PID_VALUE" 2>/dev/null; then
            echo -e "${YELLOW}⚠️  Frontend process exited${NC}"
        fi
    fi
    
    if [ -f "$WORKER_PID" ]; then
        WORKER_PID_VALUE=$(cat "$WORKER_PID")
        if ! kill -0 "$WORKER_PID_VALUE" 2>/dev/null; then
            echo -e "${YELLOW}⚠️  Worker process exited${NC}"
        fi
    fi
done
