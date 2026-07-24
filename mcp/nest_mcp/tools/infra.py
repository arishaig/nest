import asyncio

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run


async def _pve(cmd: str, timeout: int = 15) -> str:
    return await ssh_run(config.fileserver.host, cmd, key=config.fileserver.ssh_key, timeout=timeout)


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
            nfsstat_raw, clients_raw = await asyncio.gather(
                _pve("nfsstat -s 2>/dev/null | head -30"),
                _pve("cat /proc/fs/nfsd/clients/*/info 2>/dev/null || echo ''"),
            )
        except Exception as e:
            return {"error": str(e)}

        # Parse NFSv4 client info blocks (showmount -a only shows NFSv3)
        mounts = []
        for block in clients_raw.split("\n\n"):
            lines = {k.strip(): v.strip() for k, v in
                     (l.split(":", 1) for l in block.splitlines() if ":" in l)}
            if lines.get("address"):
                entry = lines["address"]
                if lines.get("minor version"):
                    entry += f" (NFSv4.{lines['minor version']})"
                mounts.append(entry)

        return {
            "active_mounts": mounts,
            "nfsstat_server": nfsstat_raw.strip(),
        }

    @mcp.tool()
    async def proxmox_boot_diagnostics() -> dict:
        """Kernel/system journal from the PVE host's PREVIOUS boot — the boot that
        ended in whatever caused the last outage/reboot.

        Surfaces kernel warnings, OOM-killer events, and system-level errors from
        before the reboot, plus whether persistent journald logging is even
        enabled (Proxmox/Debian defaults to volatile logging, which is wiped on
        every reboot — if that's off, there's nothing to recover after a hard
        reset and it's worth turning on for next time).
        """
        cmd = (
            "echo '===PERSISTENT==='; "
            "test -d /var/log/journal && echo yes || echo no; "
            "echo '===BOOTS==='; journalctl --list-boots -o short-iso 2>&1 | tail -6; "
            "echo '===KERNEL_WARN==='; journalctl -b -1 -k -p warning --no-pager -n 200 2>&1; "
            "echo '===OOM==='; "
            "(journalctl -b -1 -k --no-pager 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process') "
            "|| echo '(none found)'; "
            "echo '===SYS_ERR==='; journalctl -b -1 -p err..alert --no-pager -n 100 2>&1; "
            "true"
        )
        try:
            raw = await _pve(cmd, timeout=30)
        except Exception as e:
            return {"error": str(e)}

        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in raw.splitlines():
            if line.startswith("===") and line.endswith("==="):
                current = line.strip("=")
                sections[current] = []
            elif current:
                sections[current].append(line)
        text = {k: "\n".join(v).strip() for k, v in sections.items()}

        no_prev_boot = "Failed to determine boot" in text.get("KERNEL_WARN", "") or \
            "Failed to determine boot" in text.get("SYS_ERR", "")

        return {
            "persistent_journal_enabled": text.get("PERSISTENT", "") == "yes",
            "previous_boot_log_available": not no_prev_boot,
            "boots": text.get("BOOTS", "") or "(none)",
            "kernel_warnings": text.get("KERNEL_WARN", "") or "(none)",
            "oom_events": text.get("OOM", "") or "(none)",
            "system_errors": text.get("SYS_ERR", "") or "(none)",
        }
