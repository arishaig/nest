from nest_mcp.tools import certs
from helpers import load_tools


async def test_cert_expiry_sorts_by_days_left(monkeypatch):
    fake = {
        "a.test": {"host": "a.test", "ok": True, "days_left": 90},
        "b.test": {"host": "b.test", "ok": True, "days_left": 10, "warning": True},
    }
    monkeypatch.setattr(certs, "_check_cert_sync", lambda host, port, timeout: fake[host])
    out = await load_tools(certs)["cert_expiry"](hosts=["a.test", "b.test"])
    assert [r["host"] for r in out] == ["b.test", "a.test"]  # soonest expiry first


async def test_cert_expiry_defaults_to_known_hosts(monkeypatch):
    seen = []
    monkeypatch.setattr(certs, "_check_cert_sync",
                        lambda host, port, timeout: seen.append(host) or {"host": host, "days_left": 1})
    await load_tools(certs)["cert_expiry"]()
    assert "mcp.arishaig.site" in seen


def test_check_cert_sync_handles_unreachable():
    # Port 1 on localhost refuses fast -> exercises the OSError branch.
    result = certs._check_cert_sync("127.0.0.1", 1, 0.5)
    assert result["ok"] is False
    assert "error" in result
