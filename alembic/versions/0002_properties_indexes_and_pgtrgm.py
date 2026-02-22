"""Add properties GIN index and full-text trigram index (pg_trgm)

Revision ID: 0002
Revises: 0001
Create Date: 2026-02-20

- Enable pg_trgm for trigram similarity/ILIKE.
- GIN index on properties (JSONB) for containment (e.g. @>).
- Function + generated column to flatten properties to text for full-text search.
- GIN trigram index on that column.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Flatten JSONB properties to single text for full-text/trigram search across all values
    op.execute("""
        CREATE OR REPLACE FUNCTION jsonb_flat_text(j jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT coalesce(string_agg(value, ' ' ORDER BY key), '')
            FROM jsonb_each_text(coalesce(j, '{}'::jsonb))
        $$
    """)

    op.execute("""
        ALTER TABLE features
        ADD COLUMN IF NOT EXISTS properties_flat text
        GENERATED ALWAYS AS (jsonb_flat_text(properties)) STORED
    """)

    # GIN on properties for containment (e.g. properties @> '{"car_code": "x"}')
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_features_properties_gin
        ON features USING gin (properties)
    """)

    # Trigram index on flattened text for ILIKE / full-text search
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_features_properties_flat_trgm
        ON features USING gin (properties_flat gin_trgm_ops)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_features_properties_flat_trgm")
    op.execute("DROP INDEX IF EXISTS idx_features_properties_gin")
    op.execute("ALTER TABLE features DROP COLUMN IF EXISTS properties_flat")
    op.execute("DROP FUNCTION IF EXISTS jsonb_flat_text(jsonb)")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
