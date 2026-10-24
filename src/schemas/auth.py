"""
src/schemas/auth.py
-------------------
Pydantic schemas for authentication and password management.
"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=4)


class MeResponse(BaseModel):
    id: str
    username: str
    role: str


class AuthStatusResponse(BaseModel):
    status: str
    message: str
