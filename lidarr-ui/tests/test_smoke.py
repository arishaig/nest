"""Smoke test closing architecture-review.md G3: nothing previously gated a
broken commit from becoming a running container. This doesn't aim for
coverage parity with mcp/ — it just proves the app still starts and serves
its index route.
"""

from fastapi.testclient import TestClient

from lidarr_ui.main import app


def test_index_returns_200():
    # Lidarr isn't reachable in CI; lifespan's startup call catches that and
    # falls back to empty defaults, so the app still comes up.
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
