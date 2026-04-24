"""Observability models for request-level telemetry and aggregates."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text

from app.db.base import Base


class RequestEvent(Base):
    __tablename__ = "request_events"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at: datetime = Column(DateTime(timezone=True), nullable=False, index=True)
    method: str = Column(String(16), nullable=False)
    path: str = Column(String(1024), nullable=False, index=True)
    route_template: str = Column(String(1024), nullable=False, index=True)
    full_url: str = Column(Text, nullable=False)
    query_string: str = Column(Text, nullable=False, default="", server_default="")
    client_ip: str = Column(String(128), nullable=False, default="unknown", server_default="unknown")
    status_code: int = Column(Integer, nullable=False, index=True)
    latency_ms: int = Column(Integer, nullable=False, index=True)
    user_id: int | None = Column(Integer, nullable=True, index=True)
    username: str | None = Column(String(255), nullable=True, index=True)
    is_error: bool = Column(Boolean, nullable=False, default=False, server_default="false")
    request_body: str | None = Column(Text, nullable=True)
    request_headers: str | None = Column(Text, nullable=True)


class RequestMetricMinute(Base):
    __tablename__ = "request_metrics_minute"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    bucket_minute: datetime = Column(DateTime(timezone=True), nullable=False, index=True)
    route_template: str = Column(String(1024), nullable=False, index=True)
    request_count: int = Column(Integer, nullable=False, default=0, server_default="0")
    mean_ms: int = Column(Integer, nullable=False, default=0, server_default="0")
    p50_ms: int = Column(Integer, nullable=False, default=0, server_default="0")
    p90_ms: int = Column(Integer, nullable=False, default=0, server_default="0")
    status_2xx: int = Column(Integer, nullable=False, default=0, server_default="0")
    status_3xx: int = Column(Integer, nullable=False, default=0, server_default="0")
    status_4xx: int = Column(Integer, nullable=False, default=0, server_default="0")
    status_5xx: int = Column(Integer, nullable=False, default=0, server_default="0")
