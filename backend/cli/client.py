"""Snaplicator API client - thin HTTP wrapper."""
from __future__ import annotations
import json
import sys
from typing import Any, Optional
try:
    import httpx
    def _get(url: str, timeout: float = 30) -> httpx.Response:
        return httpx.get(url, timeout=timeout)
    def _post(url: str, json_data: Any = None, timeout: float = 60) -> httpx.Response:
        return httpx.post(url, json=json_data, timeout=timeout)
    def _delete(url: str, json_data: Any = None, timeout: float = 30) -> httpx.Response:
        return httpx.delete(url, json=json_data, timeout=timeout)
except ImportError:
    import urllib.request
    import urllib.error

    class _FakeResponse:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text
        def json(self) -> Any:
            return json.loads(self.text)
        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def _urllib_request(url: str, method: str = "GET", json_data: Any = None, timeout: float = 30) -> _FakeResponse:
        data = None
        headers = {}
        if json_data is not None:
            data = json.dumps(json_data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return _FakeResponse(resp.status, body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8") if e.fp else str(e)
            return _FakeResponse(e.code, body)

    def _get(url: str, timeout: float = 30) -> _FakeResponse:
        return _urllib_request(url, "GET", timeout=timeout)
    def _post(url: str, json_data: Any = None, timeout: float = 60) -> _FakeResponse:
        return _urllib_request(url, "POST", json_data=json_data, timeout=timeout)
    def _delete(url: str, json_data: Any = None, timeout: float = 30) -> _FakeResponse:
        return _urllib_request(url, "DELETE", json_data=json_data, timeout=timeout)


class SnaplicatorClient:
    """HTTP client for Snaplicator API."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def get(self, path: str, timeout: float = 30) -> Any:
        r = _get(f"{self.base}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any = None, timeout: float = 60) -> Any:
        r = _post(f"{self.base}{path}", json_data=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def delete(self, path: str, body: Any = None, timeout: float = 30) -> Any:
        r = _delete(f"{self.base}{path}", json_data=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
