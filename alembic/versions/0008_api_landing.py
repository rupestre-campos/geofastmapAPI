"""Add api_landing table (single row: title, description, contact for landing page).

Revision ID: 0008
Revises: 0007
Create Date: 2026-02-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_DEFAULT_DESC = (
    "OGC API – Features and Tiles. Browse feature collections and items (GeoJSON), "
    "view and edit on maps, and use vector tiles (static and dynamic) per collection."
)
_DEFAULT_CONTACT = (
    "API owner and contact information can be edited from the landing page. "
    "Click **Edit API info** to set title, description, and contact details."
)


def upgrade() -> None:
    op.create_table(
        "api_landing",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    conn = op.get_bind()
    conn.execute(
        text(
            "INSERT INTO api_landing (id, title, description, contact) "
            "VALUES (:id, :title, :desc, :contact)"
        ),
        {"id": "default", "title": "GeoFast API", "desc": _DEFAULT_DESC, "contact": _DEFAULT_CONTACT},
    )


def downgrade() -> None:
    op.drop_table("api_landing")
