#!/bin/bash
# MyCloud startup script
# Starts API and dashboard servers together

echo ""
echo "  ╔══════════════════════════════╗"
echo "  ║     MyCloud Starting...      ║"
echo "  ╚══════════════════════════════╝"
echo ""

# Kill any existing instances
pkill -f "python3 api.py"    2>/dev/null
pkill -f "python3 serve.py"  2>/dev/null
sleep 1

# Start API in background
echo "  [1/2] Starting API server..."
cd ~/cloud-system/api
python3 api.py &> ~/cloud-system/logs/api.log &
API_PID=$!
echo "        API PID: $API_PID"

# Wait for API to be ready
sleep 2

# Start dashboard in background
echo "  [2/2] Starting Dashboard server..."
cd ~/cloud-system/dashboard
python3 serve.py &> ~/cloud-system/logs/dashboard.log &
DASH_PID=$!
echo "        Dashboard PID: $DASH_PID"

sleep 1

# Start MyDrive (user-facing storage app)
echo "  [3/4] Starting MyDrive app..."
cd ~/cloud-system/drive-app
python3 serve.py &> ~/cloud-system/logs/drive.log &
DRIVE_PID=$!
echo "        MyDrive PID: $DRIVE_PID"
echo $DRIVE_PID > ~/cloud-system/logs/drive.pid

sleep 1

echo ""
echo "  ✅ MyCloud is running!"
echo ""
echo "  Admin Dashboard  : http://localhost:8080"
echo "  MyDrive (users)  : http://localhost:8081"
echo "  API              : http://localhost:5000"
echo ""
echo "  Login     : admin / admin123"
echo ""
echo "  To stop   : bash ~/cloud-system/stop.sh"
echo ""


# Start Ngrok tunnel
echo "  [3/3] Starting Ngrok tunnel..."
cd ~
ngrok http 8081 --log=stdout &> ~/cloud-system/logs/ngrok.log &
NGROK_PID=$!
echo "        Ngrok PID: $NGROK_PID"
echo $NGROK_PID > ~/cloud-system/logs/ngrok.pid

sleep 3

# Get the public URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
tunnels = data.get('tunnels', [])
if tunnels:
    print(tunnels[0]['public_url'])
else:
    print('URL not available yet')
")

echo ""
echo "  ✅ MyCloud is running!"
echo ""
echo "  Admin Dashboard  : http://localhost:8080  (local only — not exposed)"
echo "  MyDrive (local)  : http://localhost:8081"
echo "  MyDrive (public) : $NGROK_URL"
echo "  API              : http://localhost:5000  (local only — not exposed)"
echo ""
echo "  Login : admin / admin123"
echo ""

# Save PIDs for stop script
echo $API_PID  > ~/cloud-system/logs/api.pid
echo $DASH_PID > ~/cloud-system/logs/dashboard.pid
