#!/usr/bin/env python3
"""
TOPODATA (INPE) ZIP → GeoFastMap collection ``topodata``.

1. Load scripts/.env
2. Read scripts/tiles_topodata.geojson — each feature ``properties.scene`` (e.g. 20S525) builds:
   http://www.dsr.inpe.br/topodata/data/geotiff/{scene}ZN.zip
3. Ensure collection exists (raster DEM), then download each ZIP and POST …/rasters/batch
   Retries on HTTP 500 for download and upload.

Needs: httpx, bulk worker for ingest.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
TILES_GEOJSON = SCRIPT_DIR / "tiles_topodata.geojson"

INPE_GEOTIFF_BASE = "http://www.dsr.inpe.br/topodata/data/geotiff/"
MAX_RETRIES = 5
RETRY_BASE_SLEEP = 3.0


def load_env() -> None:
    if not ENV_PATH.is_file():
        return
    try:
        raw = ENV_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def scene_to_url(scene: str) -> str:
    s = (scene or "").strip()
    return f"{INPE_GEOTIFF_BASE}{s}ZN.zip"


def _parse_feature_collection_file(raw: str) -> dict:
    """Accept normal GeoJSON or a copied JS bundle (`…};varpt_topodata={…}`)."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if "varpt_topodata=" in raw:
            return json.loads(raw.split("varpt_topodata=", 1)[1].strip())
        cut = raw.find("};var")
        if cut != -1:
            return json.loads(raw[: cut + 1])
        raise


def load_scenes_from_geojson(path: Path) -> list[str]:
    if not path.is_file():
        sys.exit(f"Missing {path} — add a FeatureCollection with properties.scene per tile.")
    data = _parse_feature_collection_file(path.read_text(encoding="utf-8"))
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        sys.exit(f"Invalid GeoJSON: expected FeatureCollection in {path}")
    scenes: list[str] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        if not isinstance(props, dict):
            continue
        sc = props.get("scene")
        if isinstance(sc, str) and sc.strip():
            scenes.append(sc.strip())
    if not scenes:
        sys.exit(f"No properties.scene values in {path}")
    # stable order, unique
    seen: set[str] = set()
    out: list[str] = []
    for s in scenes:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def ensure_collection(client: httpx.Client, api_base: str, collection_id: str) -> None:
    url = f"{api_base}/collections/{collection_id}"
    r = client.get(url)
    if r.status_code == 200:
        print(f"collection ok: {collection_id}")
        return
    if r.status_code != 404:
        r.raise_for_status()

    body = {
        "id": collection_id,
        "title": collection_id,
        "description": "TOPODATA (INPE) DEM tiles",
        "collection_type": "raster",
        "raster_settings": {"is_dem": True, "dem_encoding": "terrainrgb"},
    }
    r2 = client.post(f"{api_base}/collections", json=body)
    if r2.status_code == 400 and "already exists" in (r2.text or "").lower():
        print(f"collection ok: {collection_id}")
        return
    r2.raise_for_status()
    print(f"created collection: {collection_id}")


def download_with_retries(client: httpx.Client, url: str, dest: Path) -> None:
    last_err: str | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=600.0) as r:
                if r.status_code == 500:
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(RETRY_BASE_SLEEP * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    r.raise_for_status()
                with dest.open("wb") as out:
                    for chunk in r.iter_bytes(1024 * 1024):
                        out.write(chunk)
                return
        except httpx.HTTPStatusError as e:
            sc = e.response.status_code if e.response is not None else None
            if sc == 500 and attempt < MAX_RETRIES - 1:
                last_err = str(e)
                time.sleep(RETRY_BASE_SLEEP * (attempt + 1))
                continue
            raise
        except httpx.RequestError as e:
            if attempt < MAX_RETRIES - 1:
                last_err = str(e)
                time.sleep(RETRY_BASE_SLEEP * (attempt + 1))
                continue
            raise
    sys.exit(f"download failed after retries {url}: {last_err}")


def upload_zip_with_retries(
    client: httpx.Client,
    api_base: str,
    collection_id: str,
    zip_path: Path,
    filename: str,
    *,
    source_crs: str | None,
) -> None:
    url = f"{api_base}/collections/{collection_id}/rasters/batch"
    # Dict form fields (not list of tuples) — avoids httpx multipart TypeError with files=.
    form: dict[str, str] = {
        "is_dem": "true",
        "dem_encoding": "terrainrgb",
    }
    if source_crs:
        form["source_crs"] = source_crs

    for attempt in range(MAX_RETRIES):
        try:
            with zip_path.open("rb") as f:
                r = client.post(
                    url,
                    data=form,
                    files={"files": (filename, f, "application/zip")},
                    timeout=600.0,
                )
            if r.status_code == 500 and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_SLEEP * (attempt + 1))
                continue
            r.raise_for_status()
            j = r.json()
            print(f"  job_id={j.get('job_id')} {j.get('job_url', '')}")
            return
        except httpx.HTTPStatusError as e:
            sc = e.response.status_code if e.response is not None else None
            if sc == 500 and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_SLEEP * (attempt + 1))
                continue
            raise
        except httpx.RequestError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_SLEEP * (attempt + 1))
                continue
            raise
    sys.exit("upload failed after retries")


def main() -> None:
    load_env()

    api_base = (os.environ.get("GEOFASTMAP_API_BASE") or "").strip().rstrip("/")
    user = os.environ.get("GEOFASTMAP_USER", "").strip()
    password = os.environ.get("GEOFASTMAP_PASSWORD", "")
    collection_id = (os.environ.get("GEOFASTMAP_COLLECTION") or "topodata").strip()
    source_crs = (os.environ.get("GEOFASTMAP_SOURCE_CRS") or "").strip() or None
    tg = (os.environ.get("TOPODATA_TILES_GEOJSON") or "").strip()
    tiles_path = Path(tg) if tg else TILES_GEOJSON

    if not api_base or not user:
        sys.exit("Set GEOFASTMAP_API_BASE and GEOFASTMAP_USER (and password) in scripts/.env")

    scenes = load_scenes_from_geojson(tiles_path)
    print(f"{len(scenes)} scenes from {tiles_path}", file=sys.stderr)

    auth = (user, password)

    with httpx.Client(timeout=600.0, follow_redirects=True) as public_client:
        with httpx.Client(auth=auth, headers={"Accept": "application/json"}, timeout=600.0) as api_client:
            ensure_collection(api_client, api_base, collection_id)

            for i, scene in enumerate(scenes):
                url = scene_to_url(scene)
                name = f"{scene}ZN.zip"
                print(f"[{i + 1}/{len(scenes)}] {url}")
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    download_with_retries(public_client, url, tmp_path)
                    upload_zip_with_retries(
                        api_client,
                        api_base,
                        collection_id,
                        tmp_path,
                        name,
                        source_crs=source_crs,
                    )
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass


if __name__ == "__main__":
    main()
