"""Schemas for user and auth."""

from datetime import datetime

from pydantic import BaseModel, Field


class UserRead(BaseModel):
    id: int
    username: str
    is_admin: bool = False
    must_change_password: bool = False
    created_at: datetime | None = None

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)
    is_admin: bool = False


class UserUpdate(BaseModel):
    is_admin: bool | None = None
    must_change_password: bool | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1)
