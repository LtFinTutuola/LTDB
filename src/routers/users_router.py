"""
src/routers/users_router.py
---------------------------
User management router (IAM) restricted to administrators.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.config import settings
from src.core.security import hash_password
from src.core.auth import require_admin
from src.core.request_context import AuthUser
from src.core.logger import get_logger
from src.models.users import User, UserRole
from src.schemas.users import (
    UserOut,
    UserCreate,
    UserUpdate,
    SetPasswordRequest,
)

logger = get_logger()
router = APIRouter(
    prefix="/api/v1/users",
    tags=["User Management"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=List[UserOut], status_code=status.HTTP_200_OK)
def list_users(db: Session = Depends(get_db)):
    """
    Returns list of all registered users.
    """
    return db.query(User).order_by(User.username.asc()).all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """
    Creates a new user profile with role 'base' or 'admin'.
    """
    clean_username = payload.username.strip().lower()

    if len(payload.password) < settings.AUTH_MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"La password deve contenere almeno {settings.AUTH_MIN_PASSWORD_LENGTH} caratteri"
        )

    existing = db.query(User).filter(User.username == clean_username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"L'utente '{clean_username}' esiste già"
        )

    new_user = User(
        username=clean_username,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        is_active=True,
        token_version=1,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    logger.log_security(
        "users_router", "user_created", "ok",
        created_by=admin.username,
        new_username=new_user.username,
        role=new_user.role,
    )
    return new_user


@router.put("/{user_id}/password", status_code=status.HTTP_200_OK)
def reset_user_password(
    user_id: str,
    payload: SetPasswordRequest,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """
    Sets a new password for a user. Increments token_version to invalidate previous sessions.
    """
    if len(payload.new_password) < settings.AUTH_MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"La nuova password deve contenere almeno {settings.AUTH_MIN_PASSWORD_LENGTH} caratteri"
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")

    user.hashed_password = hash_password(payload.new_password)
    user.token_version += 1
    db.commit()

    logger.log_security(
        "users_router", "admin_reset_password", "ok",
        admin=admin.username,
        target_user=user.username,
    )
    return {"status": "success", "message": f"Password reimpostata per l'utente {user.username}"}


@router.patch("/{user_id}", response_model=UserOut, status_code=status.HTTP_200_OK)
def update_user(
    user_id: str,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """
    Updates user role and active state. Enforces that at least one active admin remains.
    Increments token_version on changes.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")

    # Guard: prevent deactivating or demoting the last active admin
    is_demoting = payload.role and payload.role != UserRole.ADMIN.value and user.role == UserRole.ADMIN.value
    is_deactivating = payload.is_active is False and user.role == UserRole.ADMIN.value

    if is_demoting or is_deactivating:
        active_admins = db.query(User).filter(
            User.role == UserRole.ADMIN.value,
            User.is_active.is_(True)
        ).count()
        if active_admins <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Operazione negata: impossibile disattivare o declassare l'unico amministratore attivo rimasto"
            )

    changed = False
    if payload.role is not None and payload.role != user.role:
        user.role = payload.role
        changed = True

    if payload.is_active is not None and payload.is_active != user.is_active:
        user.is_active = payload.is_active
        changed = True

    if changed:
        user.token_version += 1
        db.commit()
        db.refresh(user)

    logger.log_security(
        "users_router", "user_updated", "ok",
        admin=admin.username,
        target_user=user.username,
        role=user.role,
        is_active=user.is_active,
    )
    return user


@router.post("/revoke-all", status_code=status.HTTP_200_OK)
def revoke_all_sessions(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    """
    Hard reset: increments token_version for all users, instantly invalidating every active session.
    """
    db.query(User).update({User.token_version: User.token_version + 1}, synchronize_session=False)
    db.commit()

    logger.log_security("users_router", "hard_reset_global", "warning", admin=admin.username)
    return {"status": "success", "message": "Tutte le sessioni attive sono state revocate con successo"}
