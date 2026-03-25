"""Add bulk_import_job_id to features for cancellable bulk imports.

Revision ID: 0023
Revises: 0022
Create Date: 2026-03-24

- Nullable bulk_import_job_id links rows to the job that created them (DELETE on cancel).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE features ADD COLUMN IF NOT EXISTS bulk_import_job_id character varying"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_features_bulk_import_job
        ON features (collection_id, bulk_import_job_id)
        WHERE bulk_import_job_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_features_bulk_import_job")
    op.drop_column("features", "bulk_import_job_id")
