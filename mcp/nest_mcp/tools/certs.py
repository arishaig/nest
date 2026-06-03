import asyncio
import ssl
import socket
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP

# Key external-facing endpoints — all use *.arishaig.site wildcard so one
# subdomain tells you all expiries, but checking a few catches per-cert splits.
_DEFAULT_HOSTS = [
    "arishaig.site",
    "mcp.arishaig.site",
    "jellyfin.arishaig.site",
    "torrent.arishaig.site",
]


def _check_cert_sync(host: str, port: int, timeout: float) -> dict:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        not_after_str = cert["notAfter"]
        not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_left = (not_after - datetime.now(timezone.utc)).days
        san = [v for _, v in cert.get("subjectAltName", [])]
        return {
            "host": host,
            "port": port,
            "ok": True,
            "days_left": days_left,
            "expires": not_after_str,
            "warning": days_left < 30,
            "subject": dict(x[0] for x in cert.get("subject", [])),
            "san": san,
        }
    except ssl.CertificateError as e:
        return {"host": host, "port": port, "ok": False, "error": f"cert error: {e}"}
    except (OSError, TimeoutError) as e:
        return {"host": host, "port": port, "ok": False, "error": str(e)}
    except Exception as e:
        return {"host": host, "port": port, "ok": False, "error": str(e)}


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def cert_expiry(
        hosts: list[str] | None = None,
        port: int = 443,
        timeout: float = 10.0,
    ) -> list[dict]:
        """Check TLS certificate expiry for one or more HTTPS hosts.

        Args:
            hosts: List of hostnames to check. Defaults to key arishaig.site endpoints.
            port: TCP port (default 443).
            timeout: Connection timeout in seconds (default 10).

        Returns a list of results with days_left, expiry date, SANs, and a warning
        flag when fewer than 30 days remain.
        """
        check_hosts = hosts if hosts is not None else _DEFAULT_HOSTS
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(None, _check_cert_sync, h, port, timeout)
            for h in check_hosts
        ]
        results = await asyncio.gather(*tasks)
        return sorted(results, key=lambda r: r.get("days_left", 9999))
