"""Classify a terminal provider event, not text mentioned by a user or model."""
from __future__ import annotations

import json
from pathlib import Path
import re


DEFERRED_PROVIDER_KINDS = frozenset({"at_capacity", "rate_limit", "transport", "routing"})


def provider_failure_is_deferred(kind: str | None) -> bool:
    """Use the existing provider-wait lane only for recoverable service failures.

    Authentication stays on the gateway's ordinary failure/retry path. A 401
    alone cannot prove credentials are permanently invalid, and must not opt
    a request into unlimited provider-wait retries.
    """
    return kind in DEFERRED_PROVIDER_KINDS


def provider_failure_reason(kind: str, english: bool = False) -> str:
    """Safe public cause; never forward provider URLs, credentials or payloads."""
    reasons = {
        "at_capacity": (
            "当前选择的模型暂时满载。",
            "The selected model is temporarily at capacity.",
        ),
        "rate_limit": (
            "Codex 当前已达到用量或请求频率限制。",
            "Codex has reached a usage or request-rate limit.",
        ),
        "transport": (
            "Codex 与模型服务的连接中断或建立失败，本次调用未能完成。",
            "The connection between Codex and the model service failed; this call did not complete.",
        ),
        "routing": (
            "Codex 未能完成工作区路由发现，本次模型调用未能完成。",
            "Codex could not complete workspace routing discovery; this model call did not complete.",
        ),
        "authentication": (
            "模型服务拒绝了本次认证；可能是服务异常或登录已失效，需要检查登录状态，单凭此错误不能确定原因。",
            "The model service rejected authentication. This may be a service issue or an expired login; check the login state. The error alone does not establish the cause.",
        ),
    }
    pair = reasons.get(kind, ("本次模型调用未能完成。", "This model call did not complete."))
    return pair[int(english)]


class ProviderFailureError(RuntimeError):
    def __init__(self, event_log: Path, kind="at_capacity"):
        self.event_log = event_log
        self.kind = kind
        super().__init__(provider_failure_reason(kind, english=True))


class ProviderCapacityError(ProviderFailureError):
    """Compatibility name for callers handling the existing capacity lane."""


def _message_failure(message: str) -> str | None:
    # The remote-compaction prefix is native Codex transport context, not task
    # prose. Strip only this exact wrapper before checking explicit signatures.
    message = message.removeprefix("error running remote compact task: ")
    if message.startswith("selected model is at capacity"):
        return "at_capacity"
    if (message.startswith(("usage limit", "rate limit", "rate_limit", "quota exceeded",
                            "token limit", "plan limit", "too many requests", "429 too many requests"))
            or re.match(r"unexpected status(?: code)? 429\b", message)
            or re.match(r"you(?:'ve| have) (?:hit|reached) your (?:usage|rate|token|plan) limit\b", message)):
        return "rate_limit"
    if (re.match(r"unexpected status(?: code)? 401\b", message)
            or message.startswith(("401 unauthorized", "authentication failed",
                                   "your authentication token has expired"))):
        return "authentication"
    if message.startswith(("workspace routing discovery timed out", "workspace routing discovery failed")):
        return "routing"
    if message.startswith((
        "connection refused", "connection reset", "connection aborted",
        "failed to connect", "unable to connect", "network is unreachable",
        "name resolution", "dns lookup", "temporary failure in name resolution",
        "tls handshake", "ssl handshake", "proxy error", "stream disconnected",
        "http status 502", "http status 503", "http status 504",
        "unexpected status 502", "unexpected status 503", "unexpected status 504",
        "unexpected status code 502", "unexpected status code 503", "unexpected status code 504",
        "error sending request for url", "codex pc offline",
    )):
        return "transport"
    return None


def terminal_provider_failure(event_log: Path):
    # Called only after the native process exits. Intermediate errors may be
    # followed by native retries; the latest terminal event owns the outcome.
    with event_log.open("rb") as handle:
        handle.seek(max(0, event_log.stat().st_size - 65536))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            return None
        if event.get("type") != "turn.failed":
            continue
        error = event.get("error")
        message = event.get("message") or (error.get("message", "") if isinstance(error, dict) else error)
        code = str(error.get("code", "") if isinstance(error, dict) else "").lower()
        message = str(message).strip().lower()
        # Preserve the existing structured server categories ahead of message
        # signatures. New signatures come from terminal native errors only.
        if code in {"usage_limit_reached", "rate_limit_exceeded", "rate_limit"}:
            return "rate_limit"
        if code == "model_at_capacity":
            return "at_capacity"
        return _message_failure(message)
    return None


def terminal_capacity_failure(event_log: Path) -> bool:
    return terminal_provider_failure(event_log) == "at_capacity"
