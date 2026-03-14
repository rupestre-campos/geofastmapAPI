"""Make all existing feature geometries valid with ST_MakeValid.

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    # Normalize any NULL or empty geometries implicitly; then fix invalid ones.
    conn.execute(
        sa.text(
            """
            UPDATE features
            SET geometry = NULL
            WHERE geometry IS NOT NULL AND ST_IsEmpty(geometry);
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE features
            SET geometry = ST_MakeValid(geometry)
            WHERE geometry IS NOT NULL AND NOT ST_IsValid(geometry);
            """
        )
    )


def downgrade() -> None:
    # Geometry validity is an improvement; no straightforward automatic downgrade.
    pass

