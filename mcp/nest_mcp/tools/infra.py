import asyncio

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run


async def _pve(cmd: str) -> str:
    return await ssh_run(config.fileserver.host, cmd, key=config.fileserver.ssh_key)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def zfs_arc_stats() -> dict:
        """ZFS ARC cache statistics from PVE: hit rate, current size, and eviction pressure.

        ARC starvation (low hit rate, size well below max) causes NFS I/O stalls.
        This was the root cause of the Jun-2025 SABnzbd/Sonarr mount-freeze incident.
        """
        try:
            raw = await _pve("cat /proc/spl/kstat/zfs/arcstats")
        except Exception as e:
            return {"error": str(e)}

        vals: dict[str, int] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 3:
                try:
                    vals[parts[0]] = int(parts[2])
                except ValueError:
                    pass

        hits = vals.get("hits", 0)
        misses = vals.get("misses", 0)
        total = hits + misses
        hit_rate = round(hits / total * 100, 1) if total else 0.0

        def _gb(b: int) -> float:
            return round(b / 1024**3, 2)

        return {
            "hit_rate_pct": hit_rate,
            "size_gb": _gb(vals.get("size", 0)),
            "target_gb": _gb(vals.get("c", 0)),
            "max_gb": _gb(vals.get("c_max", 0)),
            "demand_data_hit_rate_pct": round(
                vals.get("demand_data_hits", 0)
                / max(vals.get("demand_data_hits", 0) + vals.get("demand_data_misses", 0), 1)
                * 100,
                1,
            ),
            "mfu_size_gb": _gb(vals.get("mfu_size", 0)),
            "mru_size_gb": _gb(vals.get("mru_size", 0)),
        }

    @mcp.tool()
    async def nfs_status() -> dict:
        """NFS server stats and active client mounts from PVE (the NFS source for the k8s media PVC).

        Shows which Talos nodes are connected and the current NFS op/error counters.
        """
        try:
            nfsstat_raw, showmount_raw = await asyncio.gather(
                _pve("nfsstat -s 2>/dev/null | head -30"),
                _pve("showmount -a 2>/dev/null || echo 'showmount unavailable'"),
            )
        except Exception as e:
            return {"error": str(e)}

        mounts = [
            line.strip()
            for line in showmount_raw.splitlines()
            if line.strip() and ":" in line and not line.startswith("All")
        ]

        return {
            "active_mounts": mounts,
            "nfsstat_server": nfsstat_raw.strip(),
        }
