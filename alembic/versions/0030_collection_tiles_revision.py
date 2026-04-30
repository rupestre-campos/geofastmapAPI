"""Add tiles_revision to collection_tiles and backfill from MBTiles files.

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _derive_tiles_revision(collection_id: str, pmtiles_path: str | None) -> str | None:
    if not pmtiles_path:
        return None
    p = Path(pmtiles_path)
    if not p.exists():
        return None
    st = p.stat()
    base = f"{collection_id}:{p}:{st.st_mtime}:{st.st_size}"
    return hashlib.sha256(base.encode()).hexdigest()


def upgrade() -> None:
    op.add_column("collection_tiles", sa.Column("tiles_revision", sa.String(length=64), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT collection_id, pmtiles_path FROM collection_tiles")).fetchall()
    for row in rows:
        rev = _derive_tiles_revision(row.collection_id, row.pmtiles_path)
        bind.execute(
            sa.text(
                "UPDATE collection_tiles SET tiles_revision = :rev WHERE collection_id = :cid"
            ),
            {"cid": row.collection_id, "rev": rev},
        )


def downgrade() -> None:
    op.drop_column("collection_tiles", "tiles_revision")
