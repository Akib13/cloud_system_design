#!/bin/bash
echo "  Stopping MyCloud..."
pkill -f "python3 api.py"
pkill -f "python3 serve.py"
pkill -f "ngrok"
echo "  ✅ Stopped."
