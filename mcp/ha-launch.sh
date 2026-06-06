#!/usr/bin/env bash
# Loads NEST_HA_TOKEN from the shared secrets file, then proxies the
# Home Assistant native MCP SSE server as a stdio transport for Claude Code.

set -euo pipefail

SECRETS_FILE="${NEST_MCP_SECRETS:-$HOME/.config/nest-mcp/secrets.env}"

if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "ha-launch: secrets file not found: $SECRETS_FILE" >&2
    exit 1
fi

# shellcheck source=/dev/null
set -a
source "$SECRETS_FILE"
set +a

exec /usr/bin/npx mcp-remote \
    "http://192.168.4.50:8123/mcp_server/sse" \
    --header "Authorization: Bearer ${NEST_HA_TOKEN}" \
    --allow-http
