"""Block-ownership math behind the disk screen (no btrfs needed)."""
from __future__ import annotations

from app.services import divergence as d

B = d.BLOCK


def runs(*pairs):
    """Block runs given as (start_block, end_block) -> byte extents."""
    return [(s * B, (e - s) * B) for s, e in pairs]


def test_merge_blocks_joins_overlapping_and_touching_extents():
    flat = d.merge_blocks(runs((5, 8), (0, 2), (2, 3), (7, 10)))
    assert list(flat) == [0, 3, 5, 10]


def test_merge_blocks_rounds_partial_blocks_outward():
    flat = d.merge_blocks([(B + 10, 100)])
    assert list(flat) == [1, 2]


def test_sweep_splits_disk_by_referencing_set():
    live = d.merge_blocks(runs((0, 10)))
    a = d.merge_blocks(runs((0, 6), (20, 24)))   # 6 shared w/ live, 4 own
    b = d.merge_blocks(runs((0, 4), (20, 22), (30, 31)))  # 2 shared w/ a only, 1 own
    rs = d.sweep([live, a, b])
    assert rs == {
        0b111: 4 * B,
        0b011: 2 * B,
        0b001: 4 * B,
        0b110: 2 * B,
        0b010: 2 * B,
        0b100: 1 * B,
    }


def test_sweep_is_independent_of_window_size():
    live = d.merge_blocks(runs((0, 10), (13, 40)))
    a = d.merge_blocks(runs((0, 6), (20, 24), (35, 50)))
    b = d.merge_blocks(runs((3, 4), (20, 22), (30, 31), (39, 41)))
    whole = d.sweep([live, a, b], window=1 << 20)
    for w in (1, 3, 7, 16):
        assert d.sweep([live, a, b], window=w) == whole


def test_summarize_separates_exclusive_from_shared_divergence():
    live = d.merge_blocks(runs((0, 10)))
    a = d.merge_blocks(runs((0, 6), (20, 24)))
    b = d.merge_blocks(runs((0, 4), (20, 22), (30, 31)))
    names = ["replica", "clone-a", "clone-b"]
    s = d.summarize(names, d.sweep([live, a, b]), "replica")

    assert s["live_bytes"] == 10 * B
    assert s["referenced_bytes"] == 15 * B
    assert s["not_in_live_bytes"] == 5 * B

    ca, cb = s["subvolumes"]["clone-a"], s["subvolumes"]["clone-b"]
    # a diverged on 4 blocks, but 2 of them b also holds: only 2 are a's alone.
    assert ca["not_in_live_bytes"] == 4 * B
    assert ca["exclusive_bytes"] == 2 * B
    assert cb["not_in_live_bytes"] == 3 * B
    assert cb["exclusive_bytes"] == 1 * B
    # Live blocks are never offered for freeing.
    assert all("replica" not in g["names"] for g in s["groups"])


def test_freed_by_counts_blocks_shared_only_inside_the_selection():
    live = d.merge_blocks(runs((0, 10)))
    a = d.merge_blocks(runs((20, 24)))
    b = d.merge_blocks(runs((20, 22), (30, 31)))
    s = d.summarize(["replica", "a", "b"], d.sweep([live, a, b]), "replica")
    assert d.freed_by(s["groups"], ["a"]) == 2 * B
    assert d.freed_by(s["groups"], ["b"]) == 1 * B
    # Deleting both also frees what they shared — more than the two alone.
    assert d.freed_by(s["groups"], ["a", "b"]) == 5 * B


def test_file_extents_reads_a_real_file(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x" * (3 * B))
    import os
    os.sync()
    ext = d.file_extents(str(p))
    # FIEMAP is Linux-only; elsewhere the ioctl fails and nothing is returned.
    assert isinstance(ext, list)
    assert sum(length for _, length in ext) in (0, 3 * B)


def test_cache_roundtrip_and_staleness(tmp_path, monkeypatch):
    monkeypatch.setenv("DIVERGENCE_CACHE_PATH", str(tmp_path / "c.json"))
    assert d.load_cached() is None
    assert d.is_stale(None)
    d._store({"measured_at": "2000-01-01T00:00:00", "groups": []})
    data = d.load_cached()
    assert data["groups"] == []
    assert d.is_stale(data)


def test_live_name_accepts_relative_and_absolute_main_dir():
    assert d.live_name("/data/snaplicator", "replica") == "replica"
    assert d.live_name("/data/snaplicator", "/data/snaplicator/replica/pgdata") == "replica"


def test_describe_reads_kind_and_creation_time_from_the_name():
    assert d.describe("replica", "replica") == {"kind": "live", "created_at": None}
    assert d.describe("replica-clone-20261120-180355-a4105f", "replica") == {
        "kind": "clone", "created_at": "2026-11-20T18:03:55",
    }
    # A snapshot of a clone carries two stamps; its own is the last.
    assert d.describe("replica-clone-20260901-044310-snapshot-20260930-083947", "replica") == {
        "kind": "snapshot", "created_at": "2026-09-30T08:39:47",
    }
    assert d.describe("replica-snapshot-20261029-033357", "replica")["kind"] == "snapshot"


def test_build_response_sorts_by_divergence_and_survives_no_cache():
    empty = d.build_response(None, "replica", 10, 5, {"running": True, "error": None})
    assert empty["items"] == [] and empty["refreshing"] is True and empty["measured_at"] is None

    data = {
        "measured_at": "2026-11-22T10:00:00",
        "subvolumes": {
            "replica": {"exclusive_bytes": 1, "not_in_live_bytes": 0, "referenced_bytes": 9},
            "replica-clone-20261120-180355-a": {"exclusive_bytes": 1, "not_in_live_bytes": 2, "referenced_bytes": 9},
            "replica-clone-20260901-044310": {"exclusive_bytes": 3, "not_in_live_bytes": 7, "referenced_bytes": 9},
        },
        "groups": [],
    }
    r = d.build_response(data, "replica", 10, 5, {"running": False, "error": None})
    assert [i["name"] for i in r["items"]] == [
        "replica-clone-20260901-044310", "replica-clone-20261120-180355-a", "replica",
    ]
    assert r["items"][0]["kind"] == "clone"


def test_labels_come_from_each_subvolumes_metadata(tmp_path):
    import json as _json
    (tmp_path / "replica-clone-a").mkdir()
    (tmp_path / "replica-clone-a" / ".snaplicator.json").write_text(_json.dumps({"display_name": "brand-split", "description": "x"}))
    (tmp_path / "replica-clone-b-snapshot-20260101-000000").mkdir()
    (tmp_path / "replica-clone-b-snapshot-20260101-000000" / ".snaplicator.json").write_text(_json.dumps({"description": " test snap "}))
    (tmp_path / "replica-clone-c").mkdir()  # no metadata
    labels = d.read_labels(str(tmp_path), ["replica-clone-a", "replica-clone-b-snapshot-20260101-000000", "replica-clone-c", "gone"])
    assert labels == {"replica-clone-a": "brand-split", "replica-clone-b-snapshot-20260101-000000": "test snap"}
    data = {"subvolumes": {"replica-clone-a": {"exclusive_bytes": 1, "not_in_live_bytes": 2, "referenced_bytes": 3}}}
    r = d.build_response(data, "replica", 10, 5, {"running": False, "error": None}, labels)
    assert r["items"][0]["label"] == "brand-split"
