#!/bin/bash

# Ensure we are in the project root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( dirname "$SCRIPT_DIR" )"
cd "$PROJECT_ROOT"

echo "--- AlgoSoft Server Restart Tool v2 ---"

# 1. Kill existing server process by name
echo "Stopping existing server processes by name..."
PID=$(pgrep -f "python3 web/server.py")

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
# Aggressively kill anything on port 5000
fuser -k 5000/tcp > /dev/null 2>&1
lsof -t -i :5000 | xargs kill -9 > /dev/null 2>&1
sleep 2

# Check with lsof and loop until clear
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

# Additional wait to ensure socket is released by OS
sleep 2
echo "Port 5000 is clear."

# 3. Start server
echo "Starting web server from $PROJECT_ROOT..."
export PYTHONPATH=.
# Clear old log
> server.log
nohup python3 web/server.py > server.log 2>&1 &

# 4. Check status
sleep 5
NEW_PID=$(pgrep -f "python3 web/server.py")
if [ -n "$NEW_PID" ] && [ -n "$(lsof -t -i :5000)" ]; then
    echo "SUCCESS: Server started with PID: $NEW_PID"
    echo "URL: http://127.0.0.1:5000"
    echo "Log file: $PROJECT_ROOT/server.log"
else
    echo "ERROR: Server failed to start or port 5000 not bound. Check server.log for details."
    echo "--- Last 20 lines of server.log ---"
    cat server.log | tail -n 20
    exit 1
fi
