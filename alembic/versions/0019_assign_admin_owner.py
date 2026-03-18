"""Assign admin as owner for existing collections, maps, and styles with NULL owner_id.

Revision ID: 0019
Revises: 0018
Create Date: 2026-03-02

Assumes all existing objects without an owner belong to an admin user.
Uses the first admin user (is_admin = true); if none, no change.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Assign first admin user as owner for any collections/maps/styles with NULL owner_id.
    admin_subquery = "SELECT id FROM users WHERE is_admin = true ORDER BY id LIMIT 1"
    op.execute(text(
        f"UPDATE collections SET owner_id = ({admin_subquery}) WHERE owner_id IS NULL"
    ))
    op.execute(text(
        f"UPDATE maps SET owner_id = ({admin_subquery}) WHERE owner_id IS NULL"
    ))
    op.execute(text(
        f"UPDATE styles SET owner_id = ({admin_subquery}) WHERE owner_id IS NULL"
    ))


def downgrade() -> None:
    # No-op: we do not clear owner_id on downgrade (would leave objects without owner).
    pass
