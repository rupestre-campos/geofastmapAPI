from __future__ import annotations

import os
from pathlib import Path

import rasterio
from rasterio.crs import CRS
from rasterio.shutil import copy as rio_copy
from shapely.geometry import box, mapping


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def cog_path_for(storage_root: str, collection_id: str, feature_id: str) -> Path:
    # Keep it filesystem-friendly; collection ids are already constrained in API usage.
    root = Path(storage_root)
    return root / collection_id / f"{feature_id}.tif"


def convert_geotiff_to_cog_4326(src_path: Path, dst_path: Path) -> dict:
    """
    Convert a GeoTIFF to a Cloud Optimized GeoTIFF on disk.

    Assumptions:
    - Input is already EPSG:4326 as requested.
    - We store only metadata + footprint in DB; the COG stays on disk.
    """
    _ensure_dir(dst_path.parent)

    with rasterio.open(src_path) as src:
        if src.crs is None:
            raise ValueError("GeoTIFF has no CRS; expected EPSG:4326")
        if CRS.from_user_input(src.crs).to_epsg() != 4326:
            raise ValueError("GeoTIFF CRS must be EPSG:4326")

        # Build COG using GDAL COG driver via rasterio.
        # Note: overview generation is handled by GDAL driver.
        rio_copy(
            src,
            dst_path,
            driver="COG",
            compress="deflate",
            blocksize=512,
        )

        bounds = src.bounds  # left, bottom, right, top in EPSG:4326
        footprint = mapping(box(bounds.left, bounds.bottom, bounds.right, bounds.top))
        tags = {}
        try:
            tags = src.tags() or {}
        except Exception:
            tags = {}
        band_tags = []
        for i in range(1, src.count + 1):
            try:
                band_tags.append({"band": i, "tags": src.tags(i) or {}})
            except Exception:
                band_tags.append({"band": i, "tags": {}})

        meta = {
            "driver": src.driver,
            "crs": "EPSG:4326",
            "width": int(src.width),
            "height": int(src.height),
            "count": int(src.count),
            "dtype": str(src.dtypes[0]) if src.dtypes else None,
            "bounds": [bounds.left, bounds.bottom, bounds.right, bounds.top],
            "nodata": src.nodata,
            "transform": list(src.transform) if src.transform is not None else None,
            "res": list(getattr(src, "res", (None, None))),
            "descriptions": list(src.descriptions) if src.descriptions else None,
            "colorinterp": [ci.name for ci in src.colorinterp] if getattr(src, "colorinterp", None) else None,
            "tags": tags,
            "band_tags": band_tags,
        }

    # Relative path is safer to store (container path stability); caller can compute absolute.
    return {
        "cog_path": os.fspath(dst_path),
        "footprint_geojson": footprint,
        "meta": meta,
    }

