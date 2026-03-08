"""Add basemaps table and seed default basemaps.

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "basemaps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("copyright", sa.String(), nullable=True),
        sa.Column("min_zoom", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_zoom", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("tiles", sa.JSON(), nullable=False),
        sa.Column("labels", sa.String(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    basemaps_table = sa.table(
        "basemaps",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("copyright", sa.String()),
        sa.column("min_zoom", sa.Integer()),
        sa.column("max_zoom", sa.Integer()),
        sa.column("tiles", sa.JSON()),
        sa.column("labels", sa.String()),
        sa.column("sort_order", sa.Integer()),
    )
    op.bulk_insert(
        basemaps_table,
        [
            {"id": "osm", "name": "OpenStreetMap", "copyright": "© OpenStreetMap contributors", "min_zoom": 0, "max_zoom": 22, "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], "labels": None, "sort_order": 0},
            {"id": "streets", "name": "Esri Streets", "copyright": "Esri", "min_zoom": 0, "max_zoom": 22, "tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"], "labels": None, "sort_order": 1},
            {"id": "satellite", "name": "Esri Satellite", "copyright": "Esri", "min_zoom": 0, "max_zoom": 22, "tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], "labels": None, "sort_order": 2},
            {"id": "hybrid", "name": "Esri Hybrid", "copyright": "Esri", "min_zoom": 0, "max_zoom": 22, "tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], "labels": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Reference_Overlay/MapServer/tile/{z}/{y}/{x}", "sort_order": 3},
            {"id": "google_satellite", "name": "Google Satellite", "copyright": "© Google", "min_zoom": 0, "max_zoom": 22, "tiles": ["https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"], "labels": None, "sort_order": 4},
            {"id": "google_hybrid", "name": "Google Hybrid", "copyright": "© Google", "min_zoom": 0, "max_zoom": 22, "tiles": ["https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"], "labels": None, "sort_order": 5},
        ],
    )


def downgrade() -> None:
    op.drop_table("basemaps")
