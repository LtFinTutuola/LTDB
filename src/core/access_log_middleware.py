"""
src/core/access_log_middleware.py
---------------------------------
Starlette middleware that logs every HTTP request with execution timing,
client IP, response status, and authenticated user identity.
"""
import time
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request

from src.core.logger import get_logger

logger = get_logger()


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip static assets to avoid log spam
        if request.url.path.startswith("/static"):
            return await call_next(request)

        start_time = time.time()
        response = await call_next(request)
        latency_ms = int((time.time() - start_time) * 1000)

        # Extract client IP respecting reverse proxies
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()
        else:
            client_ip = request.headers.get("X-Real-IP") or (request.client.host if request.client else "unknown")

        user_obj = getattr(request.state, "user", None)
        username = user_obj.username if user_obj else None

        # Check edge agent endpoints
        if not username and request.url.path.startswith("/api/v1/excel-synchronization/"):
            if "agent-config" in request.url.path or "push-batch" in request.url.path:
                username = "edge-agent"

        logger.log_execution(
            component="http_access",
            event="request",
            status="ok" if response.status_code < 400 else "warning",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=latency_ms,
            ip=client_ip,
            user_agent=request.headers.get("user-agent", "unknown"),
            user=username or "anonymous",
        )

        return response
