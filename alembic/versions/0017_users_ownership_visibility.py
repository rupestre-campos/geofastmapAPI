"""Users, resource_shares, owner_id and visibility on collections, maps, styles.

Revision ID: 0017
Revises: 0016
Create Date: 2026-03-02

- users table (id, username, password_hash, is_admin, must_change_password, created_at, updated_at)
- resource_shares table (resource_type, resource_id, username, role)
- collections: owner_id (FK users), visibility (default 'private')
- maps: owner_id (FK users), visibility (default 'private')
- styles: owner_id (FK users), visibility (default 'private')
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "resource_shares",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(512), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_type", "resource_id", "username", name="uq_resource_share_resource_username"),
    )
    op.create_index("ix_resource_shares_resource_type", "resource_shares", ["resource_type"])
    op.create_index("ix_resource_shares_resource_id", "resource_shares", ["resource_id"])
    op.create_index("ix_resource_shares_username", "resource_shares", ["username"])

    op.add_column("collections", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.add_column("collections", sa.Column("visibility", sa.String(32), nullable=False, server_default="private"))
    op.create_foreign_key("fk_collections_owner_id", "collections", "users", ["owner_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_collections_owner_id", "collections", ["owner_id"])

    op.add_column("maps", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.add_column("maps", sa.Column("visibility", sa.String(32), nullable=False, server_default="private"))
    op.create_foreign_key("fk_maps_owner_id", "maps", "users", ["owner_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_maps_owner_id", "maps", ["owner_id"])

    op.add_column("styles", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.add_column("styles", sa.Column("visibility", sa.String(32), nullable=False, server_default="private"))
    op.create_foreign_key("fk_styles_owner_id", "styles", "users", ["owner_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_styles_owner_id", "styles", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_styles_owner_id", "styles")
    op.drop_constraint("fk_styles_owner_id", "styles", type_="foreignkey")
    op.drop_column("styles", "visibility")
    op.drop_column("styles", "owner_id")

    op.drop_index("ix_maps_owner_id", "maps")
    op.drop_constraint("fk_maps_owner_id", "maps", type_="foreignkey")
    op.drop_column("maps", "visibility")
    op.drop_column("maps", "owner_id")

    op.drop_index("ix_collections_owner_id", "collections")
    op.drop_constraint("fk_collections_owner_id", "collections", type_="foreignkey")
    op.drop_column("collections", "visibility")
    op.drop_column("collections", "owner_id")

    op.drop_index("ix_resource_shares_username", "resource_shares")
    op.drop_index("ix_resource_shares_resource_id", "resource_shares")
    op.drop_index("ix_resource_shares_resource_type", "resource_shares")
    op.drop_table("resource_shares")

    op.drop_index("ix_users_username", "users")
    op.drop_table("users")
