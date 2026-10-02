from __future__ import annotations

import socket
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol

from .errors import TransportError
from .models import OutboundRequest, TransportResponse


class Transport(Protocol):
    def send(self, request: OutboundRequest) -> TransportResponse: ...


class UrllibTransport:
    """Small production transport; tests inject a deterministic fake."""

    def send(self, request: OutboundRequest) -> TransportResponse:
        raw = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(
                raw, timeout=request.timeout_seconds
            ) as response:
                return TransportResponse(
                    status_code=response.status,
                    headers=_headers(response.headers),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return TransportResponse(
                status_code=exc.code,
                headers=_headers(exc.headers),
                body=exc.read(),
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise TransportError(type(exc).__name__) from exc


def _headers(value: Mapping[str, str]) -> dict[str, str]:
    return {str(key): str(item) for key, item in value.items()}

