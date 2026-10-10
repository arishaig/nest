import json
import os
from types import SimpleNamespace

from nest_mcp.tools import local
from helpers import load_tools


async def test_terraform_state_parses(monkeypatch):
    state = {"values": {"root_module": {
        "resources": [{"type": "proxmox_virtual_environment_vm", "name": "talos",
                       "provider_name": "registry.terraform.io/bpg/proxmox"}],
        "child_modules": [{"address": "module.dns",
                           "resources": [{"type": "adguard_rewrite", "name": "r",
                                          "provider_name": "x/adguard"}]}],
    }}}

    async def fake_run(cmd, cwd=None, timeout=None):
        return {"ok": True, "output": json.dumps(state)}
    monkeypatch.setattr(local, "_run", fake_run)

    out = await load_tools(local)["terraform_state"]()
    assert out["total"] == 2
    assert out["resources"][0]["provider"] == "proxmox"
    assert out["child_resources"][0]["module"] == "module.dns"


async def test_terraform_state_propagates_failure(monkeypatch):
    async def fake_run(cmd, cwd=None, timeout=None):
        return {"ok": False, "output": "boom"}
    monkeypatch.setattr(local, "_run", fake_run)
    out = await load_tools(local)["terraform_state"]()
    assert out["ok"] is False


async def test_terraform_plan(monkeypatch):
    seen = {}

    async def fake_run(cmd, cwd=None, timeout=None):
        if cmd[0].endswith("tofu-secrets.sh"):
            return {"ok": True, "output": 'pbs_password = "x"'}
        var_file = next(a for a in cmd if a.startswith("-var-file="))[len("-var-file="):]
        seen["path"] = var_file
        seen["content"] = open(var_file).read()
        seen["mode"] = os.stat(var_file).st_mode & 0o777
        return {"ok": True, "output": "No changes"}
    monkeypatch.setattr(local, "_run", fake_run)
    out = await load_tools(local)["terraform_plan"]()
    assert out["output"] == "No changes"
    assert seen["content"] == 'pbs_password = "x"\n'
    assert seen["mode"] == 0o600
    assert not os.path.exists(seen["path"])  # removed after the plan


async def test_terraform_plan_secret_generation_failure(monkeypatch):
    calls = []

    async def fake_run(cmd, cwd=None, timeout=None):
        calls.append(cmd[0])
        return {"ok": False, "output": "sops: no key"}
    monkeypatch.setattr(local, "_run", fake_run)
    out = await load_tools(local)["terraform_plan"]()
    assert out["ok"] is False and "sops: no key" in out["output"]
    assert calls == [calls[0]] and calls[0].endswith("tofu-secrets.sh")  # tofu never ran


async def test_lint_check_returns_run_url(monkeypatch):
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[:2] == ["gh", "workflow"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"url": "https://gh/run/1"}]), stderr="")
    monkeypatch.setattr(local.subprocess, "run", fake_subprocess_run)
    out = await load_tools(local)["lint_check"]()
    assert out["ok"] is True and out["url"] == "https://gh/run/1"


async def test_lint_check_handles_trigger_failure(monkeypatch):
    def fake_subprocess_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not authed")
    monkeypatch.setattr(local.subprocess, "run", fake_subprocess_run)
    out = await load_tools(local)["lint_check"]()
    assert out["ok"] is False and "not authed" in out["error"]
