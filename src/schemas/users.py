"""
src/schemas/users.py
--------------------
Pydantic schemas for user management (IAM).
"""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str = Field(..., min_length=4, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(..., min_length=4)
    role: str = Field(default="base", pattern=r"^(admin|base)$")


class UserUpdate(BaseModel):
    role: Optional[str] = Field(None, pattern=r"^(admin|base)$")
    is_active: Optional[bool] = None


class SetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=4)
