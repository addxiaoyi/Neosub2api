"""2-D7：视频任务本地台账的状态机与计费钩子。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.services import video_tasks


def _conn(tmp_path):
    return Database(Path(tmp_path) / "unified.db").connect()


def test_register_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    first = video_tasks.register(
        conn, provider="sub2api", upstream_task_id="vid-1", model="sora-2", key_id=7,
        key_name="k7", request_id="req-1", ts=100,
    )
    assert first["status"] == "pending"
    assert first["model"] == "sora-2"

    again = video_tasks.register(
        conn, provider="sub2api", upstream_task_id="vid-1", model="sora-2", ts=200
    )
    assert again["id"] == first["id"]
    assert again["created_at"] == 100 and again["updated_at"] == 100
    assert conn.execute("SELECT COUNT(*) AS n FROM video_tasks").fetchone()["n"] == 1


def test_status_runs_forward_and_records_billing(tmp_path):
    conn = _conn(tmp_path)
    task = video_tasks.register(conn, provider="sub2api", upstream_task_id="vid-2", ts=100)
    running = video_tasks.mark(
        conn, provider="sub2api", upstream_task_id="vid-2", status="running", ts=120
    )
    assert running["status"] == "running"

    done = video_tasks.mark(
        conn,
        provider="sub2api",
        upstream_task_id="vid-2",
        status="succeeded",
        ts=300,
        seconds=8,
        billed_micro=480000,
        snapshot_id=42,
    )
    assert done["status"] == "succeeded"
    assert done["seconds"] == 8
    assert done["billed_micro"] == 480000
    assert done["snapshot_id"] == 42
    assert done["updated_at"] == 300


def test_terminal_state_cannot_be_rewritten(tmp_path):
    conn = _conn(tmp_path)
    video_tasks.register(conn, provider="sub2api", upstream_task_id="vid-3", ts=100)
    video_tasks.mark(
        conn, provider="sub2api", upstream_task_id="vid-3", status="failed", ts=200, error="upstream 500"
    )
    assert video_tasks.mark(
        conn, provider="sub2api", upstream_task_id="vid-3", status="failed", ts=300
    )["status"] == "failed"

    with pytest.raises(video_tasks.VideoTaskError):
        video_tasks.mark(
            conn, provider="sub2api", upstream_task_id="vid-3", status="succeeded", ts=400
        )


def test_unknown_task_and_bad_status_are_rejected(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(video_tasks.VideoTaskError):
        video_tasks.mark(conn, provider="sub2api", upstream_task_id="ghost", status="running", ts=1)
    with pytest.raises(video_tasks.VideoTaskError):
        video_tasks.mark(
            conn, provider="sub2api", upstream_task_id="ghost", status="weird", ts=1
        )
    with pytest.raises(video_tasks.VideoTaskError):
        video_tasks.register(conn, provider="", upstream_task_id="x", ts=1)


def test_list_filters_by_status(tmp_path):
    conn = _conn(tmp_path)
    video_tasks.register(conn, provider="sub2api", upstream_task_id="a", ts=1)
    video_tasks.register(conn, provider="sub2api", upstream_task_id="b", ts=2)
    video_tasks.mark(conn, provider="sub2api", upstream_task_id="b", status="succeeded", ts=3)
    assert [task["upstream_task_id"] for task in video_tasks.list_tasks(conn)] == ["b", "a"]
    assert [task["upstream_task_id"] for task in video_tasks.list_tasks(conn, status="pending")] == ["a"]


def test_same_task_id_from_different_providers_are_separate(tmp_path):
    conn = _conn(tmp_path)
    one = video_tasks.register(conn, provider="sub2api", upstream_task_id="same", ts=1)
    two = video_tasks.register(conn, provider="zcode", upstream_task_id="same", ts=2)
    assert one["id"] != two["id"]


def test_parse_snapshot_handles_aliases_and_garbage():
    snap = video_tasks.parse_snapshot(b'{"id":"vt-1","status":"completed","seconds":4}')
    assert snap == {"task_id": "vt-1", "status": "succeeded", "seconds": 4}

    alias = video_tasks.parse_snapshot(b'{"task_id":"vt-2","status":"in_progress","duration":7}')
    assert alias["task_id"] == "vt-2"
    assert alias["status"] == "running"
    assert alias["seconds"] == 7

    assert video_tasks.parse_snapshot(b"not json")["task_id"] == ""
    assert video_tasks.parse_snapshot(b'{"status":"weird"}')["status"] == "pending"
    assert video_tasks.parse_snapshot(b'{"id":"x","seconds":-3}')["seconds"] == 0


def test_ledger_view_is_explicit_about_source(tmp_path):
    conn = _conn(tmp_path)
    task = video_tasks.register(conn, provider="sub2api", upstream_task_id="vt-9", ts=100)
    view = video_tasks.ledger_view(task)
    assert view["id"] == "vt-9"
    assert view["object"] == "video.task"
    assert view["status"] == "pending"
    assert view["source"] == "local_ledger"
    assert view["billed_micro"] == 0


def test_book_only_applies_to_terminal_tasks(tmp_path):
    conn = _conn(tmp_path)
    video_tasks.register(conn, provider="sub2api", upstream_task_id="b-1", ts=100)
    with pytest.raises(video_tasks.VideoTaskError):
        video_tasks.book(conn, provider="sub2api", upstream_task_id="b-1", billed_micro=1, ts=110)

    video_tasks.mark(conn, provider="sub2api", upstream_task_id="b-1", status="succeeded", ts=120, seconds=2)
    booked = video_tasks.book(
        conn, provider="sub2api", upstream_task_id="b-1", billed_micro=200000, snapshot_id=5, ts=130
    )
    assert booked["billed_micro"] == 200000 and booked["snapshot_id"] == 5
    assert booked["status"] == "succeeded"

    # 重复回填同一金额幂等；状态/错误不被改写
    again = video_tasks.book(
        conn, provider="sub2api", upstream_task_id="b-1", billed_micro=200000, ts=140
    )
    assert again["billed_micro"] == 200000 and again["updated_at"] == 140

    with pytest.raises(video_tasks.VideoTaskError):
        video_tasks.book(conn, provider="sub2api", upstream_task_id="b-1", billed_micro=-1, ts=150)
