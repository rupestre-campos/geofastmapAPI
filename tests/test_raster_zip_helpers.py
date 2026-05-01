from pathlib import Path
import zipfile

from app.api.routes import rasters


def test_zip_tiff_members_filters_tiffs(tmp_path):
    zpath = tmp_path / "rasters.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("a.tif", b"dummy")
        zf.writestr("b.tfw", b"dummy")
        zf.writestr("nested/c.TIFF", b"dummy")
        zf.writestr("nested/readme.txt", b"dummy")
        zf.writestr("empty_dir/", b"")

    members = rasters._zip_tiff_members(zpath)
    assert members == ["a.tif", "nested/c.TIFF"]


def test_vsizip_member_path_uses_gdal_virtual_prefix(tmp_path):
    zpath = tmp_path / "input.zip"
    got = rasters._vsizip_member_path(zpath, "/nested/dem.tif")
    assert got.startswith("/vsizip/")
    assert got.endswith("/nested/dem.tif")
    assert Path(zpath).as_posix() in got
