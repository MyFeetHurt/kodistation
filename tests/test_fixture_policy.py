"""Backlog 020: the fixtures are free-channel dev captures plus clearly marked synthetic data, nothing else.

No production data, no paid channel, no disk size: the repository is published (019). New fixtures come from
`make snapshot-dev`, `snapshot-dev2`, `snapshot-schedules`, `snapshot-live`, `snapshot-reserves`, `snapshot-stream`
(dev EPGStation, free channels only); a shape the free channels lack goes into tests/fixtures/synthetic (marked
`_synthetic`).
"""
import glob
import json
import os
import re
import subprocess

import pytest
import snapshot_api
from conftest import FIXTURES, ROOT

SETS = ("dev", "dev2")
DIRS = {"dev", "dev2", "live", "synthetic", "streams"}  # streams: 011
TOP_FILES = {"epgmeta_expected.json", "live_errors.json"}
OPTIONAL = {"sample.ts"}  # a local sample video the fake server streams when present (never committed)
# The free channels, pinned here on purpose (not imported from tools/snapshot_api.py, so a change there has to be
# made twice): every GR service, and these BS services (NHK BS, BS日テレ, BS朝日, BS-TBS, BSテレ東, BSフジ, BS10,
# BS11, BS12, 放送大学, J:COM BS, BSよしもと). Never CS, never BS10プレミアム (201, 800).
FREE_BS = {101, 102, 141, 142, 143, 151, 152, 153, 161, 162, 163, 171, 172, 173, 181, 182, 183, 200, 211, 222, 231,
           232, 260, 265}
# Every file a fixture set may hold (paths relative to the set, N = digits).
SET_FILES = [r"api/(version|config|channels|encode|rules|recorded|recording|reserves)\.json",
             r"api/recorded/(options|\d+)\.json", r"api/reserves/cnts\.json", r"api/videos/\d+/duration\.json",
             r"api/thumbnails/\d+\.jpg", r"api/dropLogs/\d+\.txt",
             r"(halfwidth|reserves_meta|schedule_texts|schedule_details)\.json"]
LIVE_FILES = {"schedules.json", "broadcasting.json", "guide.json", "live_meta.json"}
SYNTHETIC_GLOBS = [os.path.join(FIXTURES, "synthetic", "*"), os.path.join(FIXTURES, "live", "synthetic_*")]
SYNTHETIC = sorted(p for g in SYNTHETIC_GLOBS for p in glob.glob(g))


def _load(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return json.load(f)


def _free(channel):
    kind = channel.get("channelType")
    return kind == "GR" or (kind == "BS" and channel.get("serviceId") in FREE_BS)


def _real_channels():
    """{id: channel} of every real channels.json and every captured live channel."""
    out = {}
    for name in SETS:
        out.update({c["id"]: c for c in _load(FIXTURES, name, "api", "channels.json")})
    for name in ("schedules.json", "guide.json"):
        out.update({s["channel"]["id"]: s["channel"] for s in _load(FIXTURES, "live", name)})
    return out


def _set_files(name):
    root = os.path.join(FIXTURES, name)
    return sorted(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                  for d, _, files in os.walk(root) for f in files)


def test_fixture_tree_is_dev_dev2_live_synthetic():
    entries = set(os.listdir(FIXTURES)) - OPTIONAL
    assert entries == DIRS | TOP_FILES
    for dirpath, _, files in os.walk(FIXTURES):
        assert "storages.json" not in files, dirpath  # the disk size stays out
        assert os.path.basename(dirpath) not in ("pr" + "od", "production"), dirpath


@pytest.mark.parametrize("name", SETS)
def test_set_holds_only_known_files(name):
    pattern = re.compile("(" + "|".join(SET_FILES) + ")$")
    assert [f for f in _set_files(name) if not pattern.match(f)] == []


def test_live_and_synthetic_dirs_hold_only_known_files():
    live = set(_set_files("live"))
    assert live - LIVE_FILES == {os.path.basename(p) for p in SYNTHETIC if os.sep + "live" + os.sep in p}
    assert all(f.endswith(".json") for f in _set_files("synthetic"))


@pytest.mark.parametrize("name", SETS)
def test_files_belong_to_the_sets_recordings(name):
    """Thumbnails, durations, recording details, drop logs and half-width texts only of the set's own recordings
    (recorded + in progress): nothing left over from a recording that was dropped (e.g. on a paid channel)."""
    api = os.path.join(FIXTURES, name, "api")
    recorded = _load(api, "recorded.json")["records"]
    recs = recorded + _load(api, "recording.json")["records"]
    files = _set_files(name)

    def ids(prefix, suffix):
        return {int(f[len(prefix):-len(suffix)]) for f in files if f.startswith(prefix) and f.endswith(suffix)
                and f[len(prefix):-len(suffix)].isdigit()}

    assert ids("api/thumbnails/", ".jpg") <= {t for r in recs for t in r.get("thumbnails") or []}
    assert ids("api/videos/", "/duration.json") <= {v["id"] for r in recs for v in r.get("videoFiles") or []}
    assert ids("api/recorded/", ".json") <= {r["id"] for r in recs}
    assert ids("api/dropLogs/", ".txt") <= {r["dropLogFile"]["id"] for r in recs
                                            if isinstance(r.get("dropLogFile"), dict)}
    assert {int(k) for k in _load(FIXTURES, name, "halfwidth.json")} <= {r["id"] for r in recs}
    assert len(recorded) == _load(api, "recorded.json")["total"]


def test_sets_are_disjoint():
    dev, dev2 = ({r["id"] for r in _load(FIXTURES, n, "api", "recorded.json")["records"]} for n in SETS)
    assert dev and dev2 and not dev & dev2


def test_every_channel_in_every_fixture_is_free():
    channels = _real_channels()
    for cid, channel in channels.items():
        assert _free(channel) and snapshot_api.is_free_channel(channel), (cid, channel.get("name"))
    for name in SETS:
        api = os.path.join(FIXTURES, name, "api")
        used = [r["channelId"] for r in _load(api, "recorded.json")["records"]]
        used += [r["channelId"] for r in _load(api, "recording.json")["records"]]
        used += [r["channelId"] for r in _load(api, "reserves.json")["reserves"]]
        recorded = os.path.join(api, "recorded")
        used += [_load(recorded, f)["channelId"] for f in os.listdir(recorded) if f[:-5].isdigit()]
        used += [c["channelId"] for c in _load(recorded, "options.json")["channels"]]
        for rule in _load(api, "rules.json")["rules"]:
            search = rule.get("searchOption") or {}
            assert not search.get("CS") and not search.get("SKY"), (name, rule["id"])  # no paid broadcast type
            used += search.get("channelIds") or []
        for cid in used:
            assert cid in channels and _free(channels[cid]), (name, cid)
    for s in _load(FIXTURES, "live", "broadcasting.json"):
        assert _free(channels[s["channel"]["id"]])
    for s in _load(FIXTURES, "live", "schedules.json") + _load(FIXTURES, "live", "guide.json"):
        assert all(p.get("channelId", s["channel"]["id"]) == s["channel"]["id"] for p in s["programs"])


def test_every_live_file_is_known_and_free():
    """Every tests/fixtures/live/*.json: the captured ones hold free channels only, synthetic ones are marked."""
    channels = _real_channels()
    for path in glob.glob(os.path.join(FIXTURES, "live", "*.json")):
        name = os.path.basename(path)
        data = _load(path)
        if name.startswith("synthetic_"):
            assert list(data)[0] == "_synthetic", name
        elif name == "live_meta.json":
            assert set(data) == {"capturedAt"}
        else:
            assert name in LIVE_FILES and all(_free(channels[s["channel"]["id"]]) for s in data), name


@pytest.mark.parametrize("path", SYNTHETIC)
def test_synthetic_files_are_marked_and_invented(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert re.match(r'\{\s*"_synthetic"', text)  # the marker is the first thing in the file
    data = json.loads(text)
    assert list(data)[0] == "_synthetic" and data["_synthetic"]


def test_synthetic_paid_channels_exist_nowhere_else():
    paid = _load(FIXTURES, "live", "synthetic_paid.json")
    real = _real_channels()
    ids = {c["id"] for c in paid["channels"]}
    assert ids == {s["channel"]["id"] for s in paid["schedules"]}
    assert {c["channelType"] for c in paid["channels"]} == {"BS", "CS"}
    for c in paid["channels"]:
        assert c["id"] not in real and c["name"].startswith("（合成）")
        assert not snapshot_api.is_free_channel(c)
    assert all(p["isFree"] is False for s in paid["schedules"] for p in s["programs"])


def test_synthetic_texts_have_unique_keys():
    items = _load(FIXTURES, "synthetic", "epg_texts.json")["items"]
    keys = [i["key"] for i in items]
    assert len(keys) == len(set(keys)) and all(i.get("why") and i.get("name") for i in items)


def test_no_production_set_referenced_in_code():
    """AC 3: tests, tools and the addon name no production fixture set or recording (a set name or "<set> <id>").

    The pattern is assembled so that this file does not match it either."""
    name = "pr" + "od"
    pattern = re.compile(rf'fixtures/{name}|"{name}"|\b{name} [0-9]+|\b{name}/[0-9]')
    hits = []
    for top in ("tests", "tools", "plugin.video.epgstation", "repository.kodistation"):
        for dirpath, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "fixtures")]
            for f in files:
                if f == "epgxport_baseline.js" or not f.endswith((".py", ".sh", ".js", ".xml", ".po", ".json")):
                    continue
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if pattern.search(line):
                            hits.append(f"{os.path.relpath(path, ROOT)}:{n}: {line.strip()}")
    assert not hits, "\n".join(hits)


def test_tracked_fixtures_match_the_tree():
    """git knows no fixture outside the allowed dirs (skipped outside a git checkout)."""
    try:
        out = subprocess.run(["git", "ls-files", "tests/fixtures"], cwd=ROOT, capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no git")
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    for path in out.stdout.split():
        rest = path.split("/", 2)[2]
        assert rest in TOP_FILES or rest.split("/", 1)[0] in DIRS, path
        assert not path.endswith("storages.json"), path


def test_stream_fixtures_are_free_and_hold_no_video():
    """Transcoded playback (011): make snapshot-stream writes these four files (no .ts / .m3u8 file)."""
    root = os.path.join(FIXTURES, "streams")
    assert sorted(os.listdir(root)) == ["playlist.txt", "stream_meta.json", "streams.json", "streams_empty.json"]
    assert _load(root, "streams_empty.json") == {"items": []}
    channels = _real_channels()
    for item in _load(root, "streams.json")["items"]:
        cid = item["channelId"]
        assert cid in channels and _free(channels[cid]), cid
    with open(os.path.join(root, "playlist.txt"), encoding="utf-8") as f:
        assert f.read().startswith("#EXTM3U")
