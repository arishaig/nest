import asyncio
import os
import subprocess

from mcp.server.fastmcp import FastMCP

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))


async def _run(cmd: list[str], cwd: str = REPO_ROOT, timeout: int = 120) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "output": f"timed out after {timeout}s"}
    output = stdout.decode().strip()
    return {"ok": proc.returncode == 0, "output": output}


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def lint_check() -> dict:
        """Trigger the full lint suite (shellcheck, yamllint, ansible-lint, terraform, prometheus rules) via GitHub Actions and return the run URL. Results appear in GitHub within ~60s."""
        try:
            result = subprocess.run(
                ["gh", "workflow", "run", "lint.yml", "--ref", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return {"ok": False, "error": result.stderr.strip()}

            # Get the URL of the most recently queued run
            url_result = subprocess.run(
                ["gh", "run", "list", "--workflow", "lint.yml", "--limit", "1", "--json", "url,status,conclusion"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=15,
            )
            import json
            runs = json.loads(url_result.stdout) if url_result.returncode == 0 else []
            run_url = runs[0]["url"] if runs else "https://github.com"
            return {"ok": True, "message": "Lint workflow triggered", "url": run_url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    async def terraform_plan() -> dict:
        """Run terraform plan (read-only) against the live infrastructure and return the proposed changes. Calls provider APIs so may take 30-60s."""
        terraform_dir = os.path.join(REPO_ROOT, "terraform")
        secrets = os.path.join(terraform_dir, "secrets.tfvars")
        result = await _run(
            ["terraform", "plan", f"-var-file={secrets}", "-no-color", "-compact-warnings"],
            cwd=terraform_dir,
            timeout=120,
        )
        return result
