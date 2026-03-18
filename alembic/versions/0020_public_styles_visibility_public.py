"""Make global public styles visible to everyone.

Revision ID: 0020
Revises: 0019
Create Date: 2026-03-18
"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # "Public styles" are the styles with collection_id == ''.
    # They are meant to be selectable by everyone, including anonymous users.
    op.execute(
        """
        UPDATE styles
        SET visibility = 'public'
        WHERE collection_id = ''
          AND (visibility IS NULL OR visibility <> 'public')
        """
    )


def downgrade() -> None:
    # No-op: we don't want to accidentally hide styles again.
    pass

