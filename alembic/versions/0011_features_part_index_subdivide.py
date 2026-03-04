"""Add part_index to features for ST_Subdivide (max 256 vertices per row).

Revision ID: 0011
Revises: 0010
Create Date: 2026-03-02

- One logical feature can have multiple rows (same id, collection_id; part_index 0,1,2,...).
- At insert we use ST_Subdivide(geometry, 256) so no row has more than 256 vertices.
- API and tiles return one feature per id (ST_Union(geometry) grouped by id).
- Counts use COUNT(DISTINCT id).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Add column (idempotent for re-runs)
    op.execute(
        "ALTER TABLE features ADD COLUMN IF NOT EXISTS part_index integer NOT NULL DEFAULT 0"
    )
    # Drop existing PK if present (partitioned table may use different constraint name)
    op.execute("""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN
                SELECT c.conname FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'features' AND c.contype = 'p'
            LOOP
                EXECUTE format('ALTER TABLE features DROP CONSTRAINT IF EXISTS %I', r.conname);
            END LOOP;
        END $$;
    """)
    # Add new PK only if no primary key exists (idempotent for re-runs)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'features' AND c.contype = 'p'
            ) THEN
                ALTER TABLE features ADD PRIMARY KEY (id, collection_id, part_index);
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Drop current PK (whatever its name)
    op.execute("""
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN
                SELECT c.conname FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'features' AND c.contype = 'p'
            LOOP
                EXECUTE format('ALTER TABLE features DROP CONSTRAINT IF EXISTS %I', r.conname);
            END LOOP;
        END $$;
    """)
    op.execute("DELETE FROM features WHERE part_index <> 0")
    op.execute("ALTER TABLE features ADD PRIMARY KEY (id, collection_id)")
    op.execute("ALTER TABLE features DROP COLUMN part_index")
