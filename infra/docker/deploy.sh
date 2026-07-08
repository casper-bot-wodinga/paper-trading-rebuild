#!/bin/bash
# Deploy Canvas + Dashboard to Docker host (192.168.1.179)
# Prerequisites: source code must be present in ./canvas/ and ./dashboard/
# Usage: ./deploy.sh

set -e
DOCKER_HOST="raf@192.168.1.179"

echo "=== Building and deploying trading stack ==="

# Sync source from OpenClaw VM (if available) or use local copies
if [ -d "./canvas/app.py" ] || [ -f "./canvas/app.py" ]; then
  echo "Canvas source found locally"
else
  echo "Canvas source NOT found — please copy from OpenClaw VM first:"
  echo "  scp -r openclaw@192.168.1.41:~/projects/canvas/* ./canvas/"
  exit 1
fi

if [ -f "./dashboard/src/leaderboard_api.py" ]; then
  echo "Dashboard source found locally"
else
  echo "Dashboard source NOT found — please copy from OpenClaw VM first:"
  echo "  scp -r openclaw@192.168.1.41:~/projects/paper-trading-teams/src/leaderboard_api.py ./dashboard/"
  exit 1
fi

# Copy to Docker host
echo "=== Syncing to Docker host ==="
rsync -avz --delete ./canvas/ "$DOCKER_HOST:~/trading-stack/canvas/"
rsync -avz --delete ./dashboard/ "$DOCKER_HOST:~/trading-stack/dashboard/"
scp docker-compose.trading.yml "$DOCKER_HOST:~/trading-stack/"

# Deploy
echo "=== Deploying ==="
ssh "$DOCKER_HOST" "
  cd ~/trading-stack
  docker compose -f docker-compose.trading.yml build --no-cache
  docker compose -f docker-compose.trading.yml up -d
  echo '=== Status ==='
  docker compose -f docker-compose.trading.yml ps
"

echo "=== Done ==="
echo "Canvas:  https://canvas.wodinga.studio"
echo "Dashboard: https://dashboard.wodinga.studio"
