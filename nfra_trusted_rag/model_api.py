"""Small shared client for the OpenAI-compatible model endpoints."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def api_endpoint(base_url: str, route: str) -> str:
    """Return one canonical endpoint regardless of the configured base suffix."""
    base = base_url.rstrip("/")
    for suffix in ("/chat/completions", "/embeddings"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/{route.lstrip('/')}"


def post_json(
    endpoint: str,
    *,
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
    service_name: str,
    error_type: type[Exception] = RuntimeError,
) -> dict[str, Any]:
    """POST JSON and normalize transport/protocol failures for model clients."""
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise error_type(
            f"{service_name}请求失败（HTTP {exc.code}）：{detail}"
        ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise error_type(f"{service_name}请求失败：{exc}") from exc
    if not isinstance(result, dict):
        raise error_type(f"{service_name}返回的 JSON 不是对象")
    return result
