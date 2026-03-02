from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "GeoFast API"
    environment: str = "development"

    # Example: postgresql+asyncpg://user:password@localhost:5432/geofast
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/geofast"
    )
    # Connection pool: increase for heavy concurrent load (e.g. dynamic tiles). Default 5+10 is too low for maps.
    database_pool_size: int = 20
    database_pool_max_overflow: int = 20
    database_pool_timeout: float = 30.0  # seconds to wait for a connection from the pool

    # OGC API Features items: pagination limits
    items_default_limit: int = 100
    items_max_limit: int = 1000

    # Bulk import: batch size for DB inserts (background job)
    bulk_import_batch_size: int = 1000

    # Bulk storage: where uploaded files go (shared path for API and worker). Future: s3.
    bulk_storage_type: str = "filesystem"  # filesystem | s3 (s3 reserved)
    bulk_storage_path: str = "/data/bulk-uploads"  # for filesystem; create if missing

    # Bulk queue: memory = in-process consumer; redis = separate worker(s), scalable.
    bulk_queue_type: str = "redis"  # memory | redis
    redis_url: str = "redis://localhost:6379/0"  # used when bulk_queue_type=redis

    # OGC API - Processes: geometric operations (intersection, erase) between collections.
    process_queue_type: str = "redis"  # redis | memory (memory = no separate worker)
    process_max_concurrent: int = 1  # max process jobs running at once per worker

    # Tiles: static MBTiles storage; dynamic tiles are served by FastAPI from PostGIS.
    tiles_storage_path: str = "/data/tiles"
    # Max features per MVT tile to avoid overloading the database (default 200k).
    tiles_mvt_max_features: int = 10_000
    # Redis cache TTL for dynamic tiles (seconds). 0 = no cache.
    tiles_dynamic_cache_ttl_seconds: int = 60
    # Also cache tiles with query params (limit, offset, bbox, etc.) to reduce DB load when panning/zooming.
    tiles_dynamic_cache_with_params: bool = True
    # TTL for parametrized tile cache (can be longer since same search = same tiles for a while).
    tiles_dynamic_cache_params_ttl_seconds: int = 120
    # Max time (seconds) for a single dynamic tile query; 0 = disabled. Helps avoid holding pool connections.
    tiles_dynamic_statement_timeout_seconds: int = 15
    tippecanoe_minzoom: int = 0
    tippecanoe_maxzoom: int = 16
    google_maps_api_key: str = ""  # optional; for Google Satellite/Hybrid basemaps in HTML

    @property
    def database_sync_url(self) -> str:
        """URL for sync SQLAlchemy (background threads). Replaces asyncpg with psycopg2."""
        if "+asyncpg" in self.database_url:
            return self.database_url.replace("+asyncpg", "+psycopg2", 1)
        if self.database_url.startswith("postgresql://"):
            return self.database_url  # psycopg2 default
        return self.database_url

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

