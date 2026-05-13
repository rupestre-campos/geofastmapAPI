from __future__ import annotations

import os
import tempfile
from os import PathLike
from pathlib import Path

import rasterio
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.shutil import copy as rio_copy
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import box, mapping

_DST_CRS = CRS.from_epsg(4326)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def cog_path_for(storage_root: str, collection_id: str, feature_id: str) -> Path:
    # Keep it filesystem-friendly; collection ids are already constrained in API usage.
    root = Path(storage_root)
    return root / collection_id / f"{feature_id}.tif"


class CogPathOutsideStorageError(ValueError):
    """``properties.raster.cog_path`` must resolve under ``raster_storage_path``."""


def resolve_stored_cog_path(cog_path_str: str, storage_root: str) -> Path:
    """
    Resolve a stored COG filesystem path and ensure it stays under ``storage_root``
    (prevents arbitrary file read via ``raster.cog_path``).
    """
    if not cog_path_str or not isinstance(cog_path_str, str) or not cog_path_str.strip():
        raise CogPathOutsideStorageError("cog_path is empty")
    root = Path(storage_root).resolve()
    raw = Path(cog_path_str).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise CogPathOutsideStorageError("cog_path must be under raster storage root") from e
    return candidate


def parse_source_crs(override: str | None) -> CRS | None:
    """Parse optional EPSG code, proj4, or WKT from client. Empty / None -> None."""
    if override is None:
        return None
    s = override.strip()
    if not s:
        return None
    try:
        return CRS.from_user_input(s)
    except Exception as e:
        raise ValueError(f"Invalid source_crs (use EPSG:xxxx, proj4, or WKT): {e}") from e


def _crs_equal(a: CRS, b: CRS) -> bool:
    try:
        ea, eb = a.to_epsg(), b.to_epsg()
        if ea is not None and eb is not None:
            return ea == eb
    except Exception:
        pass
    try:
        return bool(a == b)
    except Exception:
        return False


def _assign_crs_and_copy_cog(src: rasterio.io.DatasetReader, dst_path: Path, crs: CRS) -> None:
    """File georeferencing is already in `crs` space; write pixels unchanged as COG."""
    profile = src.profile.copy()
    profile["crs"] = crs
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(src.read())
            if src.descriptions:
                for i, desc in enumerate(src.descriptions, start=1):
                    if desc:
                        tmp.set_band_description(i, desc)
        with mem.open() as tmp_src:
            rio_copy(
                tmp_src,
                dst_path,
                driver="COG",
                compress="deflate",
                blocksize=512,
            )


def _warp_to_cog_4326(src: rasterio.io.DatasetReader, dst_path: Path, src_crs: CRS) -> None:
    """Reproject to EPSG:4326 and write Cloud Optimized GeoTIFF."""
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs,
        _DST_CRS,
        src.width,
        src.height,
        *src.bounds,
    )
    tmp_fd, tmp_gtiff = tempfile.mkstemp(suffix=".tif")
    os.close(tmp_fd)
    tmp_path = Path(tmp_gtiff)
    try:
        profile = {
            "driver": "GTiff",
            "height": dst_height,
            "width": dst_width,
            "count": src.count,
            "crs": _DST_CRS,
            "transform": dst_transform,
            "dtype": src.dtypes[0],
            "compress": "deflate",
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
        }
        if src.nodata is not None:
            profile["nodata"] = src.nodata
        with rasterio.open(tmp_path, "w", **profile) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src_crs,
                    dst_transform=dst_transform,
                    dst_crs=_DST_CRS,
                    resampling=Resampling.bilinear,
                    src_nodata=src.nodata,
                    dst_nodata=src.nodata,
                )
            if src.descriptions:
                for i, desc in enumerate(src.descriptions, start=1):
                    if desc:
                        dst.set_band_description(i, desc)
        with rasterio.open(tmp_path) as warped:
            rio_copy(
                warped,
                dst_path,
                driver="COG",
                compress="deflate",
                blocksize=512,
            )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _meta_and_footprint_from_raster(path: Path) -> tuple[dict, dict]:
    with rasterio.open(path) as out:
        bounds = out.bounds
        footprint = mapping(box(bounds.left, bounds.bottom, bounds.right, bounds.top))
        tags = {}
        try:
            tags = out.tags() or {}
        except Exception:
            tags = {}
        band_tags = []
        for i in range(1, out.count + 1):
            try:
                band_tags.append({"band": i, "tags": out.tags(i) or {}})
            except Exception:
                band_tags.append({"band": i, "tags": {}})
        meta = {
            "driver": out.driver,
            "crs": "EPSG:4326",
            "width": int(out.width),
            "height": int(out.height),
            "count": int(out.count),
            "dtype": str(out.dtypes[0]) if out.dtypes else None,
            "bounds": [bounds.left, bounds.bottom, bounds.right, bounds.top],
            "nodata": out.nodata,
            "transform": list(out.transform) if out.transform is not None else None,
            "res": list(getattr(out, "res", (None, None))),
            "descriptions": list(out.descriptions) if out.descriptions else None,
            "colorinterp": [ci.name for ci in out.colorinterp] if getattr(out, "colorinterp", None) else None,
            "tags": tags,
            "band_tags": band_tags,
        }
    return meta, footprint


def convert_geotiff_to_cog_4326(
    src_path: str | PathLike[str],
    dst_path: Path,
    *,
    source_crs: str | None = None,
) -> dict:
    """
    Convert a GeoTIFF to a Cloud Optimized GeoTIFF in EPSG:4326.

    CRS handling:

    - If ``source_crs`` is set (EPSG:xxxx, proj4, or WKT), it is the CRS of the file's
      georeferencing (affine + bounds), overriding any embedded CRS when they disagree.
    - If the file has no CRS and ``source_crs`` is omitted, georeferencing is assumed to
      already be WGS84 lon/lat (typical for tfw-only or broken GeoTIFF tags); pixels are
      not resampled, only CRS metadata is attached.
    - If the file has a CRS and ``source_crs`` is omitted, that CRS is used; if it is not
      EPSG:4326, the raster is reprojected to EPSG:4326.
    """
    _ensure_dir(dst_path.parent)
    override = parse_source_crs(source_crs)

    with rasterio.open(src_path) as src:
        if override is not None:
            src_crs = override
        elif src.crs is not None:
            src_crs = src.crs
        else:
            src_crs = _DST_CRS

        if _crs_equal(src_crs, _DST_CRS) and src.crs is None:
            _assign_crs_and_copy_cog(src, dst_path, _DST_CRS)
        elif _crs_equal(src_crs, _DST_CRS) and src.crs is not None and _crs_equal(src.crs, _DST_CRS):
            rio_copy(
                src,
                dst_path,
                driver="COG",
                compress="deflate",
                blocksize=512,
            )
        else:
            _warp_to_cog_4326(src, dst_path, src_crs)

    meta, footprint = _meta_and_footprint_from_raster(dst_path)
    return {
        "cog_path": os.fspath(dst_path),
        "footprint_geojson": footprint,
        "meta": meta,
    }
