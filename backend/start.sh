#!/bin/sh
echo "🚀 Starting Lovedogs 360 Backend on port $PORT"
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
