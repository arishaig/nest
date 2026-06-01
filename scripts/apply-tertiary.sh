#!/bin/bash
set -e

VPS="root@66.42.79.175"
KEY="$HOME/.ssh/ansible-on-nest"
LOCAL_PORT=13000
REMOTE_PORT=3000

# Start tunnel in background, kill it on exit
ssh -f -N -L "${LOCAL_PORT}:localhost:${REMOTE_PORT}" \
    -i "$KEY" \
    -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    "$VPS"

TUNNEL_PID=$(pgrep -fn "ssh -f -N -L ${LOCAL_PORT}")
trap 'kill "$TUNNEL_PID" 2>/dev/null' EXIT

# Wait for tunnel to be ready
for _ in $(seq 1 10); do
  nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null && break
  sleep 1
done

cd "$(dirname "$0")/../terraform"
terraform apply --var-file=secrets.tfvars "$@"
