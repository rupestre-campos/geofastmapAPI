import zipfile
from pathlib import Path

from app.services.raster_batch import build_raster_batch_zip_from_staged_path


def test_build_raster_batch_zip_from_staged_geotiff(tmp_path):
    src = tmp_path / "test.tif"
    src.write_bytes(b"not-a-real-tiff")
    dest = tmp_path / "batch.zip"
    n, entries = build_raster_batch_zip_from_staged_path(
        src_path=src,
        original_filename="layer.tif",
        dest_path=str(dest),
        is_dem=False,
        dem_encoding=None,
        source_crs=None,
    )
    assert n == 1
    assert entries[0]["kind"] == "geotiff"
    with zipfile.ZipFile(dest) as zf:
        assert "manifest.json" in zf.namelist()
        assert any(n.startswith("files/") for n in zf.namelist())
