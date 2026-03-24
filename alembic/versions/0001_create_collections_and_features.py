"""Create collections and features tables

Revision ID: 0001
Revises:
Create Date: 2026-02-13
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
# Keep this short (<= 32 chars) to match Alembic's default alembic_version.version_num column.
revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", sa.String(), primary_key=True, index=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("extent", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # PostGIS required for geometry column
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "features",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("collection_id", sa.String(), nullable=False, index=True),
        sa.Column(
            "geometry",
            # Disable auto-GiST: GeoAlchemy2 would create a spatial index here; we add idx_features_geometry below.
            Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("properties", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )
    # GiST index for spatial queries (IF NOT EXISTS: safe if DB was partially migrated or index pre-exists)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_features_geometry ON features USING GIST (geometry)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_features_geometry")
    op.drop_table("features")
    op.drop_table("collections")
    op.execute("DROP EXTENSION IF EXISTS postgis")

