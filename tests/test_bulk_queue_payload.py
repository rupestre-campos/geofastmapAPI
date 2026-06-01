from app.services.bulk_queue import BulkJobPayload


def test_bulk_payload_roundtrip_parent_and_shard_fields():
    p = BulkJobPayload(
        job_id="j1",
        collection_id="c1",
        storage_key="f.geojsonl",
        mode="append",
        batch_size=1000,
        job_kind="shard",
        parent_job_id="p0",
        shard_index=2,
        shard_total=5,
        finalize_collection=False,
    )
    out = BulkJobPayload.from_json(p.to_json())
    assert out.job_kind == "shard"
    assert out.parent_job_id == "p0"
    assert out.shard_index == 2
    assert out.shard_total == 5
    assert out.finalize_collection is False


def test_bulk_payload_roundtrip_replace_filters():
    p = BulkJobPayload(
        job_id="j2",
        collection_id="c1",
        storage_key="f.geojson",
        mode="replace_filtered",
        batch_size=500,
        replace_filters=["a:eq:1"],
    )
    out = BulkJobPayload.from_json(p.to_json())
    assert out.replace_filters == ["a:eq:1"]
