from __future__ import annotations

from typing import Any, Protocol


class ProviderWriteDisabled(RuntimeError):
    """A provider mutation is intentionally disabled before any external I/O."""


class ProviderReadUnavailable(RuntimeError):
    """Provider read state is unavailable and must never be inferred as zero."""


class ProviderDataMalformed(ProviderReadUnavailable):
    """Provider returned data that cannot be safely normalized."""


class RISExTransport(Protocol):
    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def post_json(self, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]: ...
