"""Centralized logging configuration and request/tool call tracking for SkyDemon MCP."""
from __future__ import annotations

import functools
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

_LOGGING_INITIALIZED = False
LOGGER_NAME = "skydeamon"

logger = logging.getLogger(LOGGER_NAME)
tool_logger = logging.getLogger(f"{LOGGER_NAME}.tools")
http_logger = logging.getLogger(f"{LOGGER_NAME}.http")


def get_default_log_dir() -> Path:
    """Resolve the default logs directory in the project root."""
    root = Path(__file__).resolve().parent.parent
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def setup_logging(
    log_level: str | None = None,
    log_file: Path | str | None = None,
    console: bool = True,
) -> None:
    """Initialize structured logging to console and rotating log file.

    Reads LOG_LEVEL from environment (default: INFO).
    Logs to logs/skydemon-mcp.log with rotating backups.
    """
    global _LOGGING_INITIALIZED
    if _LOGGING_INITIALIZED:
        return

    level_name = (log_level or os.environ.get("LOG_LEVEL", "INFO")).strip().upper()
    numeric_level = getattr(logging, level_name, logging.INFO)

    log_format = "[%(asctime)s] [%(levelname)-7s] [%(name)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt=log_format, datefmt=date_format)

    # Resolve target log file
    if log_file is None:
        env_log_file = os.environ.get("SKYDEMON_LOG_FILE", "").strip()
        if env_log_file:
            target_path = Path(env_log_file)
        else:
            target_path = get_default_log_dir() / "skydemon-mcp.log"
    else:
        target_path = Path(log_file)

    target_path.parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = []

    # 1. Rotating File Handler (10MB max, 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(target_path),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    handlers.append(file_handler)

    # Also duplicate into tmp/mcp-server.log for backward-compatibility
    tmp_path = target_path.parent.parent / "tmp" / "mcp-server.log"
    try:
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_handler = logging.handlers.RotatingFileHandler(
            filename=str(tmp_path),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        tmp_handler.setLevel(numeric_level)
        tmp_handler.setFormatter(formatter)
        handlers.append(tmp_handler)
    except Exception:
        pass

    # 2. Console Handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove pre-existing handlers to prevent duplicated output
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    for h in handlers:
        root_logger.addHandler(h)

    # Apply to specific logger namespaces
    for name in ("skydeamon", "uvicorn", "uvicorn.error", "uvicorn.access", "mcp"):
        l = logging.getLogger(name)
        l.setLevel(numeric_level)
        l.propagate = True

    _LOGGING_INITIALIZED = True
    logger.info(f"Logging initialized: level={level_name}, file={target_path}")


def sanitize_value(key: str, val: Any) -> Any:
    """Redact sensitive keys like password, token, secret."""
    lower_k = key.lower()
    if any(secret_term in lower_k for secret_term in ("password", "secret", "token", "authorization")):
        return "***REDACTED***"
    if isinstance(val, dict):
        return {k: sanitize_value(k, v) for k, v in val.items()}
    if isinstance(val, str) and len(val) > 200:
        return f"{val[:100]}... [len={len(val)}]"
    return val


def summarize_result(result: Any) -> str:
    """Return a concise summary of a tool execution result for logging."""
    if not isinstance(result, dict):
        return str(type(result).__name__)
    parts = []
    if "ok" in result:
        parts.append("ok" if result["ok"] else f"error: {result.get('error', 'fail')}")
    if "count" in result:
        parts.append(f"count={result['count']}")
    if "results" in result and isinstance(result["results"], list):
        parts.append(f"results={len(result['results'])}")
    if "plans" in result and isinstance(result["plans"], list):
        parts.append(f"plans={len(result['plans'])}")
    if "files" in result and isinstance(result["files"], list):
        parts.append(f"files={len(result['files'])}")
    if not parts:
        keys = list(result.keys())[:4]
        parts.append(f"keys={keys}")
    return ", ".join(parts)


def log_tool_call(func: Callable) -> Callable:
    """Decorator for FastMCP tools to log execution, arguments, duration, and results."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        tool_name = func.__name__
        safe_kwargs = {k: sanitize_value(k, v) for k, v in kwargs.items()}
        args_str = ", ".join([f"{k}={v!r}" for k, v in safe_kwargs.items()])

        tool_logger.info(f"[tool:start] {tool_name}({args_str})")
        start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000
            summary = summarize_result(result)
            tool_logger.info(f"[tool:done] {tool_name} -> {summary} in {elapsed_ms:.1f}ms")
            return result
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            tool_logger.error(f"[tool:fail] {tool_name} error after {elapsed_ms:.1f}ms: {exc}", exc_info=True)
            raise

    return wrapper


class RequestLoggingMiddleware:
    """ASGI Middleware to log incoming HTTP requests and response times."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        headers = Headers(scope=scope)

        # Client IP extraction (respect Cloudflare Tunnel X-Forwarded-For)
        forwarded = headers.get("x-forwarded-for") or headers.get("cf-connecting-ip")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client = scope.get("client")
            client_ip = client[0] if client else "unknown"

        ua = headers.get("user-agent", "-")
        # Abbreviate long user agents
        if len(ua) > 60:
            ua = f"{ua[:57]}..."

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            # Don't spam favicon / trivial endpoints if desired, but log all MCP/OAuth routes
            http_logger.info(
                f"[http] {method} {path} -> {status_code} ({elapsed_ms:.1f}ms) "
                f"from {client_ip} [ua: {ua}]"
            )
