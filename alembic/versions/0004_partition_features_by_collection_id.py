"""Partition features table by collection_id (LIST)

Revision ID: 0004
Revises: 0003
Create Date: 2026-02-22

- Convert features to a partitioned table (PARTITION BY LIST (collection_id)).
- Primary key becomes (id, collection_id) as required by PostgreSQL for partitioned tables.
- Use a DEFAULT partition so any collection_id is accepted without pre-creating partitions.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 1. Rename existing table
    op.rename_table("features", "features_old")

    # 2. Create partitioned parent table (PK must include partition key)
    op.execute("""
        CREATE TABLE features (
            id character varying NOT NULL,
            collection_id character varying NOT NULL,
            geometry geometry(Geometry, 4326),
            properties jsonb,
            properties_flat text GENERATED ALWAYS AS (jsonb_flat_text(properties)) STORED,
            created_at timestamp with time zone NOT NULL,
            updated_at timestamp with time zone NOT NULL,
            PRIMARY KEY (id, collection_id),
            FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
        ) PARTITION BY LIST (collection_id)
    """)

    # 3. Create default partition (accepts any collection_id not in a specific partition)
    op.execute("""
        CREATE TABLE features_default PARTITION OF features DEFAULT
    """)

    # 4. Copy data (rows go to default partition)
    op.execute("""
        INSERT INTO features (id, collection_id, geometry, properties, created_at, updated_at)
        SELECT id, collection_id, geometry, properties, created_at, updated_at
        FROM features_old
    """)

    # 5. Drop old table
    op.drop_table("features_old")

    # 6. Create indexes on parent (they are created on all partitions)
    op.execute("""
        CREATE INDEX idx_features_geometry ON features USING GIST (geometry)
    """)
    op.execute("""
        CREATE INDEX idx_features_properties_gin ON features USING GIN (properties)
    """)
    op.execute("""
        CREATE INDEX idx_features_properties_flat_trgm ON features USING GIN (properties_flat gin_trgm_ops)
    """)


def downgrade() -> None:
    # 1. Rename partitioned table
    op.rename_table("features", "features_partitioned")

    # 2. Recreate non-partitioned features table (original PK id only)
    op.execute("""
        CREATE TABLE features (
            id character varying NOT NULL,
            collection_id character varying NOT NULL,
            geometry geometry(Geometry, 4326),
            properties jsonb,
            properties_flat text GENERATED ALWAYS AS (jsonb_flat_text(properties)) STORED,
            created_at timestamp with time zone NOT NULL,
            updated_at timestamp with time zone NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE INDEX idx_features_geometry ON features USING GIST (geometry)
    """)
    op.execute("""
        CREATE INDEX idx_features_properties_gin ON features USING GIN (properties)
    """)
    op.execute("""
        CREATE INDEX idx_features_properties_flat_trgm ON features USING GIN (properties_flat gin_trgm_ops)
    """)
    op.execute("""
        CREATE INDEX idx_features_collection_id ON features (collection_id)
    """)

    # 3. Copy data from all partitions (query the partitioned table)
    op.execute("""
        INSERT INTO features (id, collection_id, geometry, properties, created_at, updated_at)
        SELECT id, collection_id, geometry, properties, created_at, updated_at
        FROM features_partitioned
    """)

    # 4. Drop partitioned table (drops all partitions)
    op.drop_table("features_partitioned")
