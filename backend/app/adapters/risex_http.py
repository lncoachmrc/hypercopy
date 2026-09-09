from __future__ import annotations

from typing import Any, NoReturn

import httpx

from app.adapters.risex_types import ProviderReadUnavailable, ProviderWriteDisabled


class RISExReadOnlyHTTPTransport:
    """HTTP transport for public RISEx reads; all mutations are locally disabled."""

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except ProviderReadUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderReadUnavailable(f'RISEx GET failed at {path}: {exc}') from exc

        if not isinstance(payload, dict):
            raise ProviderReadUnavailable(
                f'RISEx GET at {path} did not return a JSON object'
            )
        return payload

    async def post_json(
        self,
        _path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> NoReturn:
        del json
        raise ProviderWriteDisabled(
            'RISEx HTTP transport is read-only; POST is disabled before network I/O'
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> 'RISExReadOnlyHTTPTransport':
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.aclose()
