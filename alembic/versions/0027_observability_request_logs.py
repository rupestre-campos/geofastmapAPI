"""Observability request logs and minute metrics.

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "request_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("route_template", sa.String(length=1024), nullable=False),
        sa.Column("full_url", sa.Text(), nullable=False),
        sa.Column("query_string", sa.Text(), nullable=False, server_default=""),
        sa.Column("client_ip", sa.String(length=128), nullable=False, server_default="unknown"),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("request_body", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_request_events_created_at", "request_events", ["created_at"], unique=False)
    op.create_index("ix_request_events_path", "request_events", ["path"], unique=False)
    op.create_index("ix_request_events_route_template", "request_events", ["route_template"], unique=False)
    op.create_index("ix_request_events_status_code", "request_events", ["status_code"], unique=False)
    op.create_index("ix_request_events_latency_ms", "request_events", ["latency_ms"], unique=False)
    op.create_index("ix_request_events_user_id", "request_events", ["user_id"], unique=False)
    op.create_index("ix_request_events_username", "request_events", ["username"], unique=False)
    op.create_index(
        "ix_request_events_created_status_route",
        "request_events",
        ["created_at", "status_code", "route_template"],
        unique=False,
    )

    op.create_table(
        "request_metrics_minute",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("bucket_minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column("route_template", sa.String(length=1024), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mean_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("p50_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("p90_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_2xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_3xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_4xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_5xx", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_request_metrics_minute_bucket", "request_metrics_minute", ["bucket_minute"], unique=False)
    op.create_index(
        "ix_request_metrics_minute_route",
        "request_metrics_minute",
        ["route_template"],
        unique=False,
    )
    op.create_index(
        "ix_request_metrics_minute_bucket_route",
        "request_metrics_minute",
        ["bucket_minute", "route_template"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_request_metrics_minute_bucket_route", table_name="request_metrics_minute")
    op.drop_index("ix_request_metrics_minute_route", table_name="request_metrics_minute")
    op.drop_index("ix_request_metrics_minute_bucket", table_name="request_metrics_minute")
    op.drop_table("request_metrics_minute")

    op.drop_index("ix_request_events_created_status_route", table_name="request_events")
    op.drop_index("ix_request_events_username", table_name="request_events")
    op.drop_index("ix_request_events_user_id", table_name="request_events")
    op.drop_index("ix_request_events_latency_ms", table_name="request_events")
    op.drop_index("ix_request_events_status_code", table_name="request_events")
    op.drop_index("ix_request_events_route_template", table_name="request_events")
    op.drop_index("ix_request_events_path", table_name="request_events")
    op.drop_index("ix_request_events_created_at", table_name="request_events")
    op.drop_table("request_events")
