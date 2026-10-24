"""
src/routers/auth_router.py
--------------------------
Authentication router providing login, logout, me, and change-password endpoints.
"""
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.config import settings
from src.core.security import verify_password, hash_password, create_access_token
from src.core.auth import get_current_user
from src.core.request_context import AuthUser
from src.core.logger import get_logger
from src.models.users import User
from src.schemas.auth import (
    LoginRequest,
    ChangePasswordRequest,
    MeResponse,
    AuthStatusResponse,
)

logger = get_logger()
router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


@router.post("/login", status_code=status.HTTP_200_OK)
def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Authenticates username and password, sets HttpOnly session cookie,
    and returns token and user profile.
    """
    clean_username = payload.username.strip().lower()
    user = db.query(User).filter(User.username == clean_username).first()

    if not user or not user.is_active or not verify_password(payload.password, user.hashed_password):
        logger.log_security(
            "auth_router", "login_failed", "warning",
            username=clean_username,
            ip=request.headers.get("X-Forwarded-For") or (request.client.host if request.client else "unknown"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide"
        )

    token = create_access_token(
        user_id=str(user.id),
        username=user.username,
        role=user.role,
        token_version=user.token_version,
    )

    max_age_seconds = settings.AUTH_TOKEN_TTL_DAYS * 86400
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.AUTH_COOKIE_SECURE,
        path="/",
    )

    logger.log_security(
        "auth_router", "login_success", "ok",
        username=user.username,
        role=user.role,
    )

    return {
        "status": "success",
        "message": "Login effettuato con successo",
        "token": token,
        "user": {
            "id": str(user.id),
            "username": user.username,
            "role": user.role,
        }
    }


@router.post("/logout", response_model=AuthStatusResponse, status_code=status.HTTP_200_OK)
def logout(
    response: Response,
    everywhere: bool = False,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Clears the authentication cookie. If everywhere=true, increments token_version to invalidate all devices.
    """
    if everywhere:
        user = db.query(User).filter(User.id == current_user.id).first()
        if user:
            user.token_version += 1
            db.commit()
            logger.log_security("auth_router", "logout_everywhere", "ok", user=current_user.username)

    response.delete_cookie(key=settings.AUTH_COOKIE_NAME, path="/")
    logger.log_security("auth_router", "logout", "ok", user=current_user.username)
    return AuthStatusResponse(status="success", message="Logout effettuato con successo")


@router.get("/me", response_model=MeResponse, status_code=status.HTTP_200_OK)
def get_me(current_user: AuthUser = Depends(get_current_user)):
    """
    Returns the currently authenticated user's profile.
    """
    return MeResponse(
        id=current_user.id,
        username=current_user.username,
        role=current_user.role,
    )


@router.post("/change-password", response_model=AuthStatusResponse, status_code=status.HTTP_200_OK)
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Allows the logged-in user to change their password by providing current and new password.
    Increments token_version (invalidating other sessions) and re-issues a new cookie for the current session.
    """
    if len(payload.new_password) < settings.AUTH_MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"La nuova password deve contenere almeno {settings.AUTH_MIN_PASSWORD_LENGTH} caratteri"
        )

    user = db.query(User).filter(User.id == current_user.id).first()
    if not user or not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La password attuale non è corretta"
        )

    user.hashed_password = hash_password(payload.new_password)
    user.token_version += 1
    db.commit()
    db.refresh(user)

    # Re-issue cookie for this device with new token_version
    new_token = create_access_token(
        user_id=str(user.id),
        username=user.username,
        role=user.role,
        token_version=user.token_version,
    )

    max_age_seconds = settings.AUTH_TOKEN_TTL_DAYS * 86400
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=new_token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.AUTH_COOKIE_SECURE,
        path="/",
    )

    logger.log_security("auth_router", "password_changed", "ok", user=user.username)
    return AuthStatusResponse(status="success", message="Password modificata con successo")
