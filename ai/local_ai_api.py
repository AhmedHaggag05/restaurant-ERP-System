"""
LocalAIApi — lightweight Python client for the Flatlogic AI proxy.

Usage (inside the Django workspace):

    from ai.local_ai_api import LocalAIApi

    response = LocalAIApi.create_response({
        "input": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Summarise this text in two sentences."},
        ],
        "text": {"format": {"type": "json_object"}},
    })

    if response.get("success"):
        data = LocalAIApi.decode_json_from_response(response)
        # ...

# Typical successful payload (truncated):
# {
#   "id": "resp_xxx",
#   "status": "completed",
#   "output": [
#     {"type": "reasoning", "summary": []},
#     {"type": "message", "content": [{"type": "output_text", "text": "Your final answer here."}]}
#   ],
#   "usage": { "input_tokens": 123, "output_tokens": 456 }
# }

The helper automatically injects the project UUID header and falls back to
reading executor/.env if environment variables are missing.
"""

from __future__ import annotations

import json
import os
import time
import ssl
from typing import Any, Dict, Iterable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

__all__ = [
    "LocalAIApi",
    "create_response",
    "request",
    "fetch_status",
    "await_response",
    "extract_text",
    "decode_json_from_response",
]


_CONFIG_CACHE: Optional[Dict[str, Any]] = None


class LocalAIApi:
    """Static helpers mirroring the PHP implementation."""

    @staticmethod
    def create_response(params: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return create_response(params, options or {})

    @staticmethod
    def request(path: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
                options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return request(path, payload or {}, options or {})

    @staticmethod
    def extract_text(response: Dict[str, Any]) -> str:
        return extract_text(response)

    @staticmethod
    def decode_json_from_response(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return decode_json_from_response(response)


def create_response(params: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Signature compatible with the OpenAI Responses API."""
    options = options or {}
    payload = dict(params)

    if not isinstance(payload.get("input"), list) or not payload["input"]:
        return {
            "success": False,
            "error": "input_missing",
            "message": 'Parameter "input" is required and must be a non-empty list.',
        }

    cfg = _config()
    if not payload.get("model"):
        payload["model"] = cfg["default_model"]

    initial = request(options.get("path"), payload, options)
    if not initial.get("success"):
        return initial

    data = initial.get("data")
    if isinstance(data, dict) and "ai_request_id" in data:
        ai_request_id = data["ai_request_id"]
        poll_timeout = int(options.get("poll_timeout", 300))
        poll_interval = int(options.get("poll_interval", 5))
        return await_response(ai_request_id, {
            "interval": poll_interval,
            "timeout": poll_timeout,
            "headers": options.get("headers"),
            "timeout_per_call": options.get("timeout"),
        })

    return initial


def request(path: Optional[str], payload: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Perform a raw request to the AI proxy."""
    cfg = _config()
    options = options or {}

    resolved_path = path or options.get("path") or cfg["responses_path"]
    if not resolved_path:
        return {
            "success": False,
            "error": "project_id_missing",
            "message": "PROJECT_ID is not defined; cannot resolve AI proxy endpoint.",
        }

    project_uuid = cfg["project_uuid"]
    if not project_uuid:
        return {
            "success": False,
            "error": "project_uuid_missing",
            "message": "PROJECT_UUID is not defined; aborting AI request.",
        }

    if "project_uuid" not in payload and project_uuid:
        payload["project_uuid"] = project_uuid

    url = _build_url(resolved_path, cfg["base_url"])
    opt_timeout = options.get("timeout")
    timeout = int(cfg["timeout"] if opt_timeout is None else opt_timeout)
    verify_tls = options.get("verify_tls", cfg["verify_tls"])

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        cfg["project_header"]: project_uuid,
    }
    extra_headers = options.get("headers")
    if isinstance(extra_headers, Iterable):
        for header in extra_headers:
            if isinstance(header, str) and ":" in header:
                name, value = header.split(":", 1)
                headers[name.strip()] = value.strip()

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _http_request(url, "POST", body, headers, timeout, verify_tls)


def fetch_status(ai_request_id: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fetch status for a queued AI request."""
    cfg = _config()
    options = options or {}

    project_uuid = cfg["project_uuid"]
    if not project_uuid:
        return {
            "success": False,
            "error": "project_uuid_missing",
            "message": "PROJECT_UUID is not defined; aborting status check.",
        }

    status_path = _resolve_status_path(ai_request_id, cfg)
    url = _build_url(status_path, cfg["base_url"])

    opt_timeout = options.get("timeout")
    timeout = int(cfg["timeout"] if opt_timeout is None else opt_timeout)
    verify_tls = options.get("verify_tls", cfg["verify_tls"])

    headers: Dict[str, str] = {
        "Accept": "application/json",
        cfg["project_header"]: project_uuid,
    }
    extra_headers = options.get("headers")
    if isinstance(extra_headers, Iterable):
        for header in extra_headers:
            if isinstance(header, str) and ":" in header:
                name, value = header.split(":", 1)
                headers[name.strip()] = value.strip()

    return _http_request(url, "GET", None, headers, timeout, verify_tls)


def await_response(ai_request_id: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Poll status endpoint until the request is complete or timed out."""
    options = options or {}
    timeout = int(options.get("timeout", 300))
    interval = int(options.get("interval", 5))
    if interval <= 0:
        interval = 5
    per_call_timeout = options.get("timeout_per_call")

    deadline = time.time() + max(timeout, interval)

    while True:
        status_resp = fetch_status(ai_request_id, {
            "headers": options.get("headers"),
            "timeout": per_call_timeout,
            "verify_tls": options.get("verify_tls"),
        })
        if status_resp.get("success"):
            data = status_resp.get("data") or {}
            if isinstance(data, dict):
                status_value = data.get("status")
                if status_value == "success":
                    return {
                        "success": True,
                        "status": 200,
                        "data": data.get("response", data),
                    }
                if status_value == "failed":
                    return {
                        "success": False,
                        "status": 500,
                        "error": str(data.get("error") or "AI request failed"),
                        "data": data,
                    }
        else:
            return status_resp

        if time.time() >= deadline:
            return {
                "success": False,
                "error": "timeout",
                "message": "Timed out waiting for AI response.",
            }
        time.sleep(interval)


def extract_text(response: Dict[str, Any]) -> str:
    """Public helper to extract plain text from a Responses payload."""
    return _extract_text(response)


def decode_json_from_response(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attempt to decode JSON emitted by the model (handles markdown fences)."""
    text = _extract_text(response)
    if text == "":
        return None

    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return decoded
    except json.JSONDecodeError:
        pass

    stripped = text.strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    stripped = stripped.strip()
    if stripped and stripped != text:
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            return None
    return None


def _extract_text(response: Dict[str, Any]) -> str:
    payload = response.get("data") if response.get("success") else response.get("response")
    if isinstance(payload, dict):
        output = payload.get("output")
        if isinstance(output, list):
            combined = ""
            for item in output:
                content = item.get("content") if isinstance(item, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "output_text" and block.get("text"):
                            combined += str(block["text"])
                if combined:
                    return combined
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message")
            if isinstance(message, dict) and message.get("content"):
                return str(message["content"])
    if isinstance(payload, str):
        return payload
    return ""


def _config() -> Dict[str, Any]:
    global _CONFIG_CACHE  # noqa: PLW0603
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    _ensure_env_loaded()

    base_url = os.getenv("AI_PROXY_BASE_URL", "https://flatlogic.com")
    project_id = os.getenv("PROJECT_ID") or None
    responses_path = os.getenv("AI_RESPONSES_PATH")
    if not responses_path and project_id:
        responses_path = f"/projects/{project_id}/ai-request"

    _CONFIG_CACHE = {
        "base_url": base_url,
        "responses_path": responses_path,
        "project_id": project_id,
        "project_uuid": os.getenv("PROJECT_UUID"),
        "project_header": os.getenv("AI_PROJECT_HEADER", "project-uuid"),
        "default_model": os.getenv("AI_DEFAULT_MODEL", "gpt-5-mini"),
        "timeout": int(os.getenv("AI_TIMEOUT", "30")),
        "verify_tls": os.getenv("AI_VERIFY_TLS", "true").lower() not in {"0", "false", "no"},
    }
    return _CONFIG_CACHE


def _build_url(path: str, base_url: str) -> str:
    trimmed = path.strip()
    if trimmed.startswith("http://") or trimmed.startswith("https://"):
        return trimmed
    if trimmed.startswith("/"):
        return f"{base_url}{trimmed}"
    return f"{base_url}/{trimmed}"


def _resolve_status_path(ai_request_id: Any, cfg: Dict[str, Any]) -> str:
    base_path = (cfg.get("responses_path") or "").rstrip("/")
    if not base_path:
        return f"/ai-request/{ai_request_id}/status"
    if not base_path.endswith("/ai-request"):
        base_path = f"{base_path}/ai-request"
    return f"{base_path}/{ai_request_id}/status"


def _http_request(url: str, method: str, body: Optional[bytes], headers: Dict[str, str],
                  timeout: int, verify_tls: bool) -> Dict[str, Any]:
    """
    Shared HTTP helper for GET/POST requests.
    """
    req = urlrequest.Request(url, data=body, method=method.upper())
    for name, value in headers.items():
        req.add_header(name, value)

    context = None
    if not verify_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    try:
        with urlrequest.urlopen(req, timeout=timeout, context=context) as resp:
            status = resp.getcode()
            response_body = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        status = exc.getcode()
        response_body = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "success": False,
            "error": "request_failed",
            "message": str(exc),
        }

    decoded = None
    if response_body:
        try:
            decoded = json.loads(response_body)
        except json.JSONDecodeError:
            decoded = None

    if 200 <= status < 300:
        return {
            "success": True,
            "status": status,
            "data": decoded if decoded is not None else response_body,
        }

    error_message = "AI proxy request failed"
    if isinstance(decoded, dict):
        error_message = decoded.get("error") or decoded.get("message") or error_message
    elif response_body:
        error_message = response_body

    return {
        "success": False,
        "status": status,
        "error": error_message,
        "response": decoded if decoded is not None else response_body,
    }


def _ensure_env_loaded() -> None:
    """Populate os.environ from executor/.env if variables are missing."""
    if os.getenv("PROJECT_UUID") and os.getenv("PROJECT_ID"):
        return

    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = value.strip().strip('\'"')
                if key and not os.getenv(key):
                    os.environ[key] = value
    except OSError:
        pass
