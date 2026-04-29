from pathlib import Path

from app.services.bulk_storage import FilesystemStorage


def test_chunk_part_assembly_and_cleanup(tmp_path: Path):
    s = FilesystemStorage(str(tmp_path))
    p1 = s.get_chunk_part_path("u1", 1)
    p2 = s.get_chunk_part_path("u1", 2)
    Path(p1).write_bytes(b"abc")
    Path(p2).write_bytes(b"def")
    out = s.assemble_chunk_parts("u1", [1, 2], "final.bin")
    assert Path(out).read_bytes() == b"abcdef"
    s.delete_upload_parts("u1")
    assert not Path(p1).exists()
