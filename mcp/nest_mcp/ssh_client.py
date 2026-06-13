import asyncio
import os


async def ssh_run(host: str, cmd: str, user: str = "root", key: str = "~/.ssh/ansible-on-nest", timeout: int = 15) -> str:
    key = os.path.expanduser(key)
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-i", key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or f"SSH exited {proc.returncode}")
    return stdout.decode().strip()
