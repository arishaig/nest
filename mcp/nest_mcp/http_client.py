import httpx
from contextlib import asynccontextmanager
from typing import AsyncGenerator


def make_client(
    base_url: str,
    *,
    verify: bool = True,
    headers: dict | None = None,
    timeout: float = 10.0,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        verify=verify,
        headers=headers or {},
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    )


class UniFiSession:
    """Stateful UniFi client that handles cookie-based auth with CSRF tokens."""

    def __init__(self, url: str, username: str, password: str, verify_tls: bool = False):
        self._url = url
        self._username = username
        self._password = password
        self._verify = verify_tls
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._url,
                verify=self._verify,
                timeout=httpx.Timeout(10.0),
                follow_redirects=True,
            )
            await self._login()
        return self._client

    async def _login(self) -> None:
        assert self._client is not None
        resp = await self._client.post(
            "/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        csrf = resp.headers.get("x-csrf-token", "")
        if csrf:
            self._client.headers.update({"x-csrf-token": csrf})

    async def get(self, path: str, **kwargs) -> httpx.Response:
        client = await self._ensure_client()
        resp = await client.get(path, **kwargs)
        if resp.status_code == 401:
            await self._login()
            resp = await client.get(path, **kwargs)
        resp.raise_for_status()
        return resp

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
