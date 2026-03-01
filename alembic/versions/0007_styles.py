"""Add styles table (OGC API - Styles): public and collection-specific layer styles.

Revision ID: 0007
Revises: 0006
Create Date: 2026-02-13

- styles: (collection_id, id) PK; collection_id='' = public style.
- style_spec JSON: fillColor, lineColor, fillOpacity, lineOpacity, lineWidth, linePattern, fillEnabled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "styles",
        sa.Column("collection_id", sa.String(), primary_key=True),  # '' = public style
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("style_spec", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_styles_collection_id", "styles", ["collection_id"], unique=False)
    op.create_index("ix_styles_id", "styles", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_styles_id", table_name="styles")
    op.drop_index("ix_styles_collection_id", table_name="styles")
    op.drop_table("styles")
