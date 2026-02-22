-- Enable pg_trgm for trigram indexes (full-text search on properties_flat).
-- Migration 0002 also runs CREATE EXTENSION IF NOT EXISTS; this ensures it exists on first DB init.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
