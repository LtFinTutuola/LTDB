"""
src/core/request_context.py
---------------------------
ContextVar for propagating current authenticated user identity
across asynchronous and synchronous call stacks (services, agents, loggers).
"""
from dataclasses import dataclass
from typing import Optional
from contextvars import ContextVar


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str
    role: str
    token_version: int = 1


current_user_var: ContextVar[Optional[AuthUser]] = ContextVar("current_user", default=None)
