#!/usr/bin/env bash
# Wrapper that loads nest-mcp credentials from a secrets file, then starts the server.
# Credentials never appear in .mcp.json — only this script is referenced there.
#
# Secrets file location (in order of precedence):
#   $NEST_MCP_SECRETS  — override via env var
#   ~/.config/nest-mcp/secrets.env  — default

set -euo pipefail

SECRETS_FILE="${NEST_MCP_SECRETS:-$HOME/.config/nest-mcp/secrets.env}"

if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "nest-mcp: secrets file not found: $SECRETS_FILE" >&2
    echo "Copy mcp/secrets.env.example to $SECRETS_FILE and fill in credentials." >&2
    exit 1
fi

# shellcheck source=/dev/null
set -a
source "$SECRETS_FILE"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/.venv/bin/python" -m nest_mcp
