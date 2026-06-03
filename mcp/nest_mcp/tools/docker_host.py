import json
import re

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run

_TRAEFIK_CONFIG_DIR = "/mnt/app_config/traefik/dynamic"

# Script run on docker host to scrape all Traefik routes (labels + file provider)
_TRAEFIK_SCRIPT = r"""
import json, subprocess, os

routes = []

# Docker-label routes
result = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True)
for container in result.stdout.strip().splitlines():
    if not container:
        continue
    r2 = subprocess.run(['docker', 'inspect', container, '--format', '{{json .Config.Labels}}'],
                        capture_output=True, text=True)
    try:
        labels = json.loads(r2.stdout)
    except Exception:
        continue
    for k, v in labels.items():
        if k.startswith('traefik.http.routers.') and k.endswith('.rule'):
            router = k.split('.')[3]
            mw = labels.get(f'traefik.http.routers.{router}.middlewares', '')
            routes.append({
                'source': 'docker',
                'container': container,
                'router': router,
                'rule': v,
                'middlewares': [m.strip() for m in mw.split(',') if m.strip()],
            })

# File-provider routes
try:
    import yaml
    for fname in sorted(os.listdir('""" + _TRAEFIK_CONFIG_DIR + r"""')):
        if not (fname.endswith('.yml') or fname.endswith('.yaml')):
            continue
        try:
            with open(os.path.join('""" + _TRAEFIK_CONFIG_DIR + r"""', fname)) as f:
                data = yaml.safe_load(f)
            for name, r in (data or {}).get('http', {}).get('routers', {}).items():
                routes.append({
                    'source': 'file:' + fname,
                    'container': None,
                    'router': name,
                    'rule': r.get('rule', ''),
                    'middlewares': r.get('middlewares', []),
                })
        except Exception:
            pass
except ImportError:
    routes.append({'source': 'note', 'error': 'pyyaml not installed on host; file routes skipped'})

print(json.dumps(routes))
"""


async def _ssh(cmd: str) -> str:
    return await ssh_run(config.docker_host.host, cmd, key=config.docker_host.ssh_key)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def docker_containers() -> list[dict]:
        """List all Docker containers on the main docker LXC (running and stopped) with name, image, status, and ports."""
        output = await _ssh(
            r"""docker ps -a --format '{"name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}","ports":"{{.Ports}}"}'"""
        )
        containers = []
        for line in output.splitlines():
            line = line.strip()
            if line:
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    containers.append({"raw": line})
        return containers

    @mcp.tool()
    async def docker_logs(container: str, lines: int = 50) -> dict:
        """Fetch the most recent log lines from a named Docker container on the main docker LXC.

        Args:
            container: Exact container name (e.g. "jellyfin", "traefik").
            lines: Number of tail lines to return (default 50, max 500).
        """
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$', container):
            return {"error": "Invalid container name — only alphanumerics, hyphens, dots, and underscores allowed."}
        lines = min(max(1, lines), 500)
        output = await _ssh(f"docker logs --tail {lines} --timestamps {container} 2>&1")
        return {"container": container, "lines_requested": lines, "logs": output}

    @mcp.tool()
    async def traefik_routes() -> list[dict]:
        """List all active Traefik HTTP routes from both Docker container labels and file provider configs.

        Returns router name, the Host/path match rule, source (docker container or config file),
        and any middlewares applied (e.g. authelia).
        """
        script = _TRAEFIK_SCRIPT.replace('\n', '; ').replace('"', '\\"')
        # Run as a here-doc to avoid quoting hell
        output = await _ssh(f"python3 << 'PYEOF'\n{_TRAEFIK_SCRIPT}\nPYEOF")
        try:
            return json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return [{"error": "Failed to parse route output", "raw": output[:500]}]

    @mcp.tool()
    async def wg_tunnel_status() -> dict:
        """Show WireGuard tunnel status on the docker LXC: peer handshake age, endpoint, and traffic counters."""
        output = await _ssh("wg show 2>/dev/null || echo 'wg not available'")
        return {"wg_show": output}
