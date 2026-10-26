from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx


class GraphError(RuntimeError):
    """Raised when the Graph API returns a 4xx/5xx response."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"Graph API {status}: {payload}")

    def to_dict(self) -> dict[str, Any]:
        err = self.payload.get("error", {}) if isinstance(self.payload, dict) else {}
        message = err.get("message") if isinstance(err, dict) else None
        return {
            "error": True,
            "status": self.status,
            "message": message if isinstance(message, str) else "Graph API request failed",
            "code": err.get("code"),
            "subcode": err.get("error_subcode"),
            "fbtrace_id": err.get("fbtrace_id"),
        }


_client: httpx.AsyncClient | None = None
MAX_RESPONSE_BYTES = 5_000_000
MAX_PAGES = 100


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )
    return _client


def _config() -> tuple[str, str]:
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("IG_ACCESS_TOKEN is not set")
    version = os.environ.get("IG_GRAPH_VERSION", "v21.0")
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise RuntimeError("IG_GRAPH_VERSION must match vN.N")
    host = os.environ.get("IG_GRAPH_HOST", "graph.facebook.com")
    if host not in {"graph.facebook.com", "graph.instagram.com"}:
        raise RuntimeError("IG_GRAPH_HOST is not an allowed Graph API host")
    return f"https://{host}/{version}", token


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _unwrap(r: httpx.Response) -> Any:
    if len(r.content) > MAX_RESPONSE_BYTES:
        raise GraphError(r.status_code, {"error": {"message": "Graph API response is too large"}})
    try:
        body = r.json()
    except ValueError:
        body = {"error": {"message": "Graph API returned invalid JSON"}}
    if r.status_code >= 400:
        safe_body = _redact(body, os.environ.get("IG_ACCESS_TOKEN"))
        if isinstance(safe_body, dict):
            error = safe_body.get("error")
            if isinstance(error, dict):
                for key in ("message", "error_user_title", "error_user_msg"):
                    value = error.get(key)
                    if isinstance(value, str) and ("http://" in value or "https://" in value):
                        error[key] = "Graph API request failed"
        raise GraphError(r.status_code, safe_body)
    if not isinstance(body, (dict, list)):
        raise GraphError(r.status_code, {"error": {"message": "Graph API returned invalid JSON"}})
    return body


def _redact(value: Any, token: str | None = None) -> Any:
    """Remove credential values and URL query data from API error content."""
    if isinstance(value, dict):
        return {
            str(k): (
                "[REDACTED]"
                if str(k).lower() in {"access_token", "client_secret"}
                else _redact(v, token)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, str):
        if token:
            value = value.replace(token, "[REDACTED]")
        return re.sub(
            r"(?i)(access_token|client_secret|fb_exchange_token)=([^&\s]+)",
            r"\1=[REDACTED]",
            value,
        )
    return value


def ig_user_id() -> str:
    val = os.environ.get("IG_USER_ID")
    if not val:
        raise RuntimeError("IG_USER_ID is not set")
    if not re.fullmatch(r"\d{1,64}", val):
        raise RuntimeError("IG_USER_ID must be a numeric Graph API ID")
    return val


async def get(path: str, **params: Any) -> Any:
    base, token = _config()
    q = _clean(params)
    q["access_token"] = token
    r = await _http().get(f"{base}/{path.lstrip('/')}", params=q)
    return _unwrap(r)


async def post(path: str, **fields: Any) -> Any:
    base, token = _config()
    r = await _http().post(
        f"{base}/{path.lstrip('/')}",
        params={"access_token": token},
        data=_clean(fields),
    )
    return _unwrap(r)


async def delete(path: str, **params: Any) -> Any:
    base, token = _config()
    q = _clean(params)
    q["access_token"] = token
    r = await _http().delete(f"{base}/{path.lstrip('/')}", params=q)
    return _unwrap(r)


async def paginate(path: str, max_pages: int = 5, **params: Any) -> dict[str, Any]:
    """Follow validated Graph paging URLs and concatenate data arrays."""
    if not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_PAGES}")
    results: list[Any] = []
    pages = 0
    next_url: str | None = None
    requested: set[str] = set()
    base, token = _config()
    expected = urlsplit(base)

    while pages < max_pages:
        if next_url is None:
            res = await get(path, **params)
        else:
            parsed = urlsplit(next_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != expected.hostname
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or not parsed.path.startswith(expected.path + "/")
            ):
                raise ValueError("Graph API returned an untrusted pagination URL")
            normalized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{parsed.query}"
            if normalized_url in requested:
                raise ValueError("Graph API returned a repeated pagination URL")
            requested.add(normalized_url)
            # Discard API-provided query credentials. Keep pagination parameters only.
            query = parse_qs(parsed.query, keep_blank_values=True)
            allowed = {"after", "before", "limit", "fields", "since", "until"}
            safe_params = {k: v[-1] for k, v in query.items() if k in allowed}
            safe_params["access_token"] = token
            clean_path = parsed.path
            page_url = f"{expected.scheme}://{expected.netloc}{clean_path}"
            r = await _http().get(page_url, params=safe_params)
            res = _unwrap(r)
        pages += 1
        if not isinstance(res, dict) or not isinstance(res.get("data", []), list):
            raise ValueError("Graph API returned malformed pagination data")
        results.extend(res.get("data", []))
        paging = res.get("paging", {})
        if not isinstance(paging, dict):
            raise ValueError("Graph API returned malformed pagination data")
        next_url = paging.get("next")
        if next_url is not None and not isinstance(next_url, str):
            raise ValueError("Graph API returned malformed pagination data")
        if next_url:
            parsed_next = urlsplit(next_url)
            next_key = f"{parsed_next.scheme}://{parsed_next.netloc}{parsed_next.path}?{parsed_next.query}"
            if next_key in requested:
                raise ValueError("Graph API returned a repeated pagination URL")
        if not next_url:
            break

    return {"data": results, "pages_fetched": pages, "has_more": bool(next_url)}
