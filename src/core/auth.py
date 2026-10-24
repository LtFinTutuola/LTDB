"""
src/core/auth.py
----------------
Authentication dependencies and bootstrap procedures for LTDB.
Provides JWT validation, role checks, and database session synchronization.
"""
from typing import Optional
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.config import settings
from src.core.security import decode_access_token, hash_password, ensure_jwt_secret
from src.core.request_context import AuthUser, current_user_var
from src.core.logger import get_logger
from src.models.users import User, UserRole
from src.models.sales import SalesChatSession

logger = get_logger()


class LoginRedirect(Exception):
    """Exception raised when an unauthenticated HTML request must be redirected to /login."""
    pass


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db)
) -> AuthUser:
    """
    FastAPI dependency that extracts and validates the JWT from cookie or Bearer header,
    verifies user activity and token_version against the database, and injects AuthUser.
    """
    token: Optional[str] = request.cookies.get(settings.AUTH_COOKIE_NAME)

    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    is_api = request.url.path.startswith("/api/")

    if not token:
        if is_api:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Non autenticato"
            )
        raise LoginRedirect()

    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        token_version = payload.get("tv")
    except Exception as exc:
        logger.log_security("auth", "token_invalid", "warning", error=str(exc), path=request.url.path)
        if is_api:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token non valido o scaduto"
            )
        raise LoginRedirect()

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active or user.token_version != token_version:
        logger.log_security(
            "auth", "session_revoked", "warning",
            user_id=str(user_id) if user_id else "unknown",
            path=request.url.path
        )
        if is_api:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sessione revocata o utente disabilitato"
            )
        raise LoginRedirect()

    auth_user = AuthUser(
        id=str(user.id),
        username=user.username,
        role=user.role,
        token_version=user.token_version,
    )
    request.state.user = auth_user
    current_user_var.set(auth_user)
    return auth_user


async def require_admin(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
) -> AuthUser:
    """
    FastAPI dependency ensuring the authenticated user has the 'admin' role.
    """
    if current_user.role != UserRole.ADMIN.value:
        logger.log_security(
            "auth", "forbidden_role", "warning",
            user=current_user.username,
            role=current_user.role,
            path=request.url.path
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permesso negato: operazione riservata agli amministratori"
        )
    return current_user


def bootstrap_auth(db: Session) -> None:
    """
    Initializes JWT secret if missing, ensures default admin user exists,
    and associates orphan legacy sales chat sessions to the admin user.
    """
    ensure_jwt_secret()

    admin_user = db.query(User).filter(User.role == UserRole.ADMIN.value).first()
    if not admin_user:
        admin_pass = settings.AUTH_BOOTSTRAP_ADMIN_PASSWORD
        admin_user = User(
            username="admin",
            hashed_password=hash_password(admin_pass),
            role=UserRole.ADMIN.value,
            is_active=True,
            token_version=1,
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)
        logger.log_security("auth", "bootstrap_admin_created", "ok", username="admin")

    # Backfill orphan chat sessions to an admin
    orphan_count = db.query(SalesChatSession).filter(SalesChatSession.user_id.is_(None)).count()
    if orphan_count > 0:
        db.query(SalesChatSession).filter(SalesChatSession.user_id.is_(None)).update(
            {SalesChatSession.user_id: admin_user.id},
            synchronize_session=False,
        )
        db.commit()
        logger.log_execution("auth", "backfill_orphan_chat_sessions", "ok", count=orphan_count)
