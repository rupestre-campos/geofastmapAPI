"""Create named partitions per collection_id and move data from default.

Revision ID: 0005
Revises: 0004
Create Date: 2026-02-22

- For each distinct collection_id in features_default: create a dedicated LIST partition
  and move rows from features_default into it.
- After this migration, new collections should get a named partition at creation time
  (see app.db.features_partitions.ensure_features_partition in create_collection).
"""

from collections.abc import Sequence
import hashlib
import re

from alembic import op
from sqlalchemy import text

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _safe_partition_name(collection_id: str) -> str:
    """Valid, unique table name for a features partition (max 63 chars)."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", collection_id)[:45].strip("_") or "default"
    h = hashlib.sha256(collection_id.encode()).hexdigest()[:8]
    return f"features_{safe}_{h}"


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(text("SELECT DISTINCT collection_id FROM features_default"))
    cids = [row[0] for row in result]

    for cid in cids:
        name = _safe_partition_name(cid)
        cid_escaped = cid.replace("'", "''")
        # PostgreSQL forbids creating a new partition if the default partition still
        # contains rows that would belong to it (default constraint would be violated).
        # So: copy rows to a temp table, delete from default, then create partition and copy back.
        temp_name = "_mig_0005_" + hashlib.sha256(cid.encode()).hexdigest()[:12]
        op.execute(
            f"""
            CREATE TABLE "{temp_name}" (
                id character varying NOT NULL,
                collection_id character varying NOT NULL,
                geometry geometry(Geometry, 4326),
                properties jsonb,
                created_at timestamp with time zone NOT NULL,
                updated_at timestamp with time zone NOT NULL
            )
            """
        )
        conn.execute(
            text(f"""
                INSERT INTO "{temp_name}" (id, collection_id, geometry, properties, created_at, updated_at)
                SELECT id, collection_id, geometry, properties, created_at, updated_at
                FROM features_default
                WHERE collection_id = :cid
            """),
            {"cid": cid},
        )
        conn.execute(
            text("DELETE FROM features_default WHERE collection_id = :cid"),
            {"cid": cid},
        )
        op.execute(
            f'CREATE TABLE "{name}" PARTITION OF features FOR VALUES IN (\'{cid_escaped}\')'
        )
        conn.execute(
            text(f"""
                INSERT INTO features (id, collection_id, geometry, properties, created_at, updated_at)
                SELECT id, collection_id, geometry, properties, created_at, updated_at
                FROM "{temp_name}"
            """)
        )
        op.execute(f'DROP TABLE "{temp_name}"')


def downgrade() -> None:
    # Detach each named partition, copy its rows into features (they go to default), then drop the table.
    conn = op.get_bind()
    result = conn.execute(
        text("""
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'features'
              AND c.relname != 'features_default'
        """)
    )
    part_names = [row[0] for row in result]
    for part_name in part_names:
        op.execute(f'ALTER TABLE features DETACH PARTITION "{part_name}"')
        # Rows go to default partition (no other partition for this collection_id after detach)
        quoted = '"' + part_name.replace('"', '""') + '"'
        conn.execute(
            text(
                "INSERT INTO features (id, collection_id, geometry, properties, created_at, updated_at) "
                "SELECT id, collection_id, geometry, properties, created_at, updated_at FROM " + quoted
            )
        )
        op.execute(f'DROP TABLE "{part_name}"')
