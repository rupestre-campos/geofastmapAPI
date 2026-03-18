"""Schemas for resource shares (list, add)."""

from pydantic import BaseModel, Field


class ShareAdd(BaseModel):
    username: str = Field(..., min_length=1, description="Username to grant access")
    role: str = Field(default="viewer", description="viewer | editor")


class ShareRead(BaseModel):
    username: str
    role: str
