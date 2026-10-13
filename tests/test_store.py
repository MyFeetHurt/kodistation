import json

from resources.lib.epgstation import store


def test_atomic_write_json_round_trip(tmp_path):
    path = tmp_path / "x.json"
    assert store.atomic_write_json(str(path), {"a": "映画", "n": [1, 2]}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "映画", "n": [1, 2]}
    assert "映画" in path.read_text(encoding="utf-8")  # utf-8, not \\u escapes: half the size for EPG texts
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.json"]


def test_atomic_write_json_failure_leaves_nothing(tmp_path):
    assert store.atomic_write_json(str(tmp_path / "missing" / "x.json"), {}) is False
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_json_unserialisable_cleans_up(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("old")
    assert store.atomic_write_json(str(path), {"bad": object()}) is False
    assert path.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.json"]
