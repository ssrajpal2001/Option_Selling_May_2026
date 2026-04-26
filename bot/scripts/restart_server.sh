#!/bin/bash

# Ensure we are in the project root (bot/)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( dirname "$SCRIPT_DIR" )"
cd "$PROJECT_ROOT"

PYENV_PYTHON="$HOME/.pyenv/versions/3.11.15/bin/python"

echo "--- AlgoSoft Server Restart Tool v3 ---"

# 0. Self-heal: install deps if uvicorn is not importable under Python 3.11
if ! "$PYENV_PYTHON" -c "import uvicorn" 2>/dev/null; then
    echo "uvicorn not found in Python 3.11 env — running setup_env.sh first..."
    bash "$PROJECT_ROOT/setup_env.sh" || { echo "FATAL: setup_env.sh failed. Aborting."; exit 1; }
fi

# 1. Kill existing server process by name
echo "Stopping existing server processes by name..."
PID=$(pgrep -f "python.*web/server.py")

if [ -n "$PID" ]; then
    echo "Found server process at PID: $PID. Terminating..."
    kill -9 $PID
    sleep 2
    echo "Process terminated."
else
    echo "No running server process found by name."
fi

# 2. Force clear Port 5000
echo "Ensuring Port 5000 is clear..."
fuser -k 5000/tcp > /dev/null 2>&1
lsof -t -i :5000 | xargs kill -9 > /dev/null 2>&1
sleep 2

MAX_RETRIES=10
RETRY_COUNT=0
while [ -n "$(lsof -t -i :5000)" ] && [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    PORT_PID=$(lsof -t -i :5000)
    echo "Port 5000 still occupied by PID: $PORT_PID. Retrying kill..."
    kill -9 $PORT_PID > /dev/null 2>&1
    sleep 2
    RETRY_COUNT=$((RETRY_COUNT+1))
done

if [ -n "$(lsof -t -i :5000)" ]; then
    echo "FATAL ERROR: Could not clear Port 5000 after $MAX_RETRIES attempts."
    exit 1
fi

sleep 2
echo "Port 5000 is clear."

# 3. Start server using the pyenv Python 3.11 interpreter (preserves reload=True in server.py __main__)
echo "Starting web server from $PROJECT_ROOT..."
export PYTHONPATH=.
> server.log
nohup "$PYENV_PYTHON" web/server.py > server.log 2>&1 &

# 4. Check status
sleep 5
NEW_PID=$(pgrep -f "python.*web/server.py")
if [ -n "$NEW_PID" ] && [ -n "$(lsof -t -i :5000)" ]; then
    echo "SUCCESS: Server started with PID: $NEW_PID"
    echo "URL: http://127.0.0.1:5000"
    echo "Log file: $PROJECT_ROOT/server.log"
else
    echo "ERROR: Server failed to start or port 5000 not bound. Check server.log for details."
    echo "--- Last 20 lines of server.log ---"
    tail -n 20 server.log
    exit 1
fi
