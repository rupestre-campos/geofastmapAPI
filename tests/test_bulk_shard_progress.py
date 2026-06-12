"""Tests for sharded bulk import parent job progress reporting."""

from app.services import bulk_worker as bw


def test_parent_shard_progress_message_includes_totals():
    msg, total = bw._parent_shard_progress_message(
        shard_index=1,
        shard_total=2,
        status="running",
        shard_created=5000,
        parent_state={"items_created": 12000, "completed_shards": 0},
    )
    assert "Shard 1/2" in msg
    assert "5000 in this shard" in msg
    assert "17000 features total" in msg
    assert total == 17000


def test_shard_progress_cb_updates_parent_job(monkeypatch):
    updates = []

    monkeypatch.setattr(
        bw,
        "get_parent_shard_state",
        lambda _pid: {"items_created": 100, "completed_shards": 0},
    )
    monkeypatch.setattr(
        bw,
        "update_job",
        lambda job_id, **kw: updates.append((job_id, kw)),
    )
    monkeypatch.setattr(bw, "_shard_progress_heartbeat_seconds", lambda: 0.0)

    cb = bw._make_parent_shard_progress_cb("parent-1", 2, 3)
    cb("running", 250, None)

    assert len(updates) == 1
    assert updates[0][0] == "parent-1"
    assert updates[0][1]["items_created"] == 350
    assert "Shard 2/3" in updates[0][1]["message"]


def test_notify_parent_shard_started(monkeypatch):
    updates = []

    monkeypatch.setattr(
        bw,
        "get_parent_shard_state",
        lambda _pid: {"items_created": 40000, "completed_shards": 1},
    )
    monkeypatch.setattr(
        bw,
        "update_job",
        lambda job_id, **kw: updates.append((job_id, kw)),
    )

    payload = bw.BulkJobPayload(
        job_id="parent:shard:2",
        collection_id="c1",
        storage_key="x.geojsonl",
        mode="append",
        batch_size=1000,
        job_kind="shard",
        parent_job_id="parent",
        shard_index=2,
        shard_total=2,
    )
    bw._notify_parent_shard_started(payload)

    assert updates[0][0] == "parent"
    assert "Processing shard 2/2" in updates[0][1]["message"]
    assert updates[0][1]["items_created"] == 40000
