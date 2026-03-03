"""Schemas for API landing page (user-editable title, description, contact)."""

from pydantic import BaseModel, Field


class ApiLandingUpdate(BaseModel):
    """Body for PATCH /api-info. Omitted fields are left unchanged."""

    title: str | None = Field(None, min_length=1, max_length=500, description="Landing page title")
    description: str | None = Field(None, max_length=10000, description="Short description of the API")
    contact: str | None = Field(None, max_length=10000, description="Owner and contact information (plain text or markdown)")


class ApiLandingRead(BaseModel):
    """API landing content (for JSON response)."""

    title: str
    description: str | None
    contact: str | None
