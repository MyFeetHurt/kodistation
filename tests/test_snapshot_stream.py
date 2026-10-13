"""Transcoded playback (011): tools/dev/snapshot_stream.py is dev-only and picks a free finished TS recording."""
import importlib.util
import os

import pytest
from conftest import ROOT


def _tool():
    path = os.path.join(ROOT, "tools", "dev", "snapshot_stream.py")
    spec = importlib.util.spec_from_file_location("snapshot_stream_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_refuses_production_and_unknown_servers(monkeypatch):
    tool = _tool()
    monkeypatch.setenv("EPGS_DEV_URL", "http://192.0.2.10:8888")
    monkeypatch.setenv("EPGS_PROD_URL", "http://192.0.2.20:9000")
    sent = []
    monkeypatch.setattr(tool, "request", lambda *a, **k: sent.append(a))
    for base in ("http://192.0.2.20:9000", "http://192.0.2.30:8888", ""):
        with pytest.raises(SystemExit):
            tool.main(["--base", base])
    assert sent == []


def test_refuses_without_prod_url(monkeypatch):
    tool = _tool()
    monkeypatch.setenv("EPGS_DEV_URL", "http://192.0.2.10:8888")
    monkeypatch.delenv("EPGS_PROD_URL", raising=False)
    with pytest.raises(SystemExit):
        tool.main(["--base", "http://192.0.2.10:8888"])


def test_pick():
    tool = _tool()
    channels = [{"id": 1, "channelType": "GR"}, {"id": 2, "channelType": "CS"}]
    ts, enc = [{"id": 9, "type": "ts"}], [{"id": 8, "type": "encoded"}]
    recs = [
        {"id": 1, "channelId": 2, "startAt": 9_000_000, "endAt": 9_600_000, "videoFiles": ts},  # paid
        {"id": 2, "channelId": 1, "startAt": 8_000_000, "endAt": 8_600_000, "videoFiles": ts, "isRecording": True},
        {"id": 3, "channelId": 1, "startAt": 7_000_000, "endAt": 7_060_000, "videoFiles": ts},  # 1 min
        {"id": 4, "channelId": 1, "startAt": 6_000_000, "endAt": 6_600_000, "videoFiles": enc},
        {"id": 5, "channelId": 1, "startAt": 5_000_000, "endAt": 5_600_000, "videoFiles": ts},
    ]
    rec, video = tool.pick(recs, channels)
    assert rec["id"] == 5 and video["id"] == 9
    with pytest.raises(SystemExit):
        tool.pick(recs[:4], channels)


# --- 011 review L4e / nit 2: the DELETE on every failure path ------------------------------------------------------

class _Server:
    """A scripted dev EPGStation for capture(): answers by (method, path); every request recorded."""

    def __init__(self, ready_after=2, start=(200, b'{"streamId": 0}'), playlist=200, start_error=None, timer=None):
        self.ready_after, self.start, self.playlist, self.start_error = ready_after, start, playlist, start_error
        self.sent, self.polls, self.running = [], 0, False
        self.timer, self.clock, self.last = timer, None, 0.0  # EPGStation's 15 s stop timer on the test clock

    def request(self, base, method, path, params=None, timeout=10):
        import json

        self.sent.append((method, path))
        if self.timer and self.running and self.clock[0] - self.last > self.timer:
            self.running = False  # the server's timer stopped it
        if path.endswith("/hls"):
            self.last = self.clock[0] if self.clock else 0.0
            if self.start_error:
                self.running = True  # the server started it, the answer got lost
                raise self.start_error
            self.running = True
            return self.start
        if method == "DELETE":
            self.running = False
            return 200, b'{"code":200}'
        if method == "PUT":
            if not self.running:
                return 500, b'{"errors": "StreamIsUndefined"}'
            self.last = self.clock[0]
            return 200, b'{"code":200}'
        if path == "/api/streams":
            self.polls += 1
            item = {"streamId": 0, "isEnable": self.ready_after is not None and self.polls > self.ready_after,
                    "viodeFileId": 9, "mode": 1, "channelId": 1}
            return 200, json.dumps({"items": [item] if self.running else []}).encode()
        if path == "/api/channels":
            return 200, b'[{"id": 1, "channelType": "GR"}]'
        if path == "/api/recorded":
            return 200, json.dumps({"records": [{"id": 5, "channelId": 1, "startAt": 0, "endAt": 600000,
                                                 "videoFiles": [{"id": 9, "type": "ts"}]}]}).encode()
        if path == "/api/config":
            return 200, b'{"streamConfig": {"recorded": {"ts": {"hls": ["720p", "480p"]}}}}'
        if path.startswith("/streamfiles/"):
            return self.playlist, b"#EXTM3U\n" if self.playlist == 200 else b"Not Found"
        raise AssertionError(path)


@pytest.fixture
def capture(monkeypatch, tmp_path):
    tool = _tool()
    clock = [0.0]
    monkeypatch.setattr(tool.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tool.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    def run(server):
        server.clock = clock
        monkeypatch.setattr(tool, "request", server.request)
        out = tmp_path / "out"
        try:
            tool.capture("http://dev", str(out))
            return out, None
        except SystemExit as e:
            return out, e
    return run


def test_capture_writes_and_deletes(capture):
    server = _Server()
    out, err = capture(server)
    assert err is None and server.sent.count(("DELETE", "/api/streams/0")) == 1 and not server.running
    assert sorted(p.name for p in out.iterdir()) == ["playlist.txt", "stream_meta.json", "streams.json",
                                                     "streams_empty.json"]


@pytest.mark.parametrize("server", [_Server(ready_after=None), _Server(playlist=404)],
                         ids=["never-ready", "no-playlist"])
def test_capture_deletes_on_failure(capture, server):
    out, err = capture(server)
    assert err is not None and ("DELETE", "/api/streams/0") in server.sent and not server.running
    assert not out.exists()


def test_capture_never_waits_past_max_seconds(capture):
    tool = _tool()
    server = _Server(ready_after=None)
    capture(server)
    assert server.polls <= 2 * (tool.MAX_SECONDS - tool.STOP_RESERVE) + 2


@pytest.mark.parametrize("server", [_Server(start=(500, b'{"errors": "x"}')), _Server(start=(200, b"{}")),
                                    _Server(start_error=OSError("timed out"))], ids=["500", "no-id", "timeout"])
def test_capture_cleans_up_without_a_stream_id(capture, server):
    out, err = capture(server)
    assert err is not None and not out.exists()
    assert ("DELETE", "/api/streams/0") in server.sent and not server.running


def test_capture_keeps_a_slow_start_alive(capture):
    """Codex round 2: ready only after 18 s (longer than the server's 15 s timer): keeps every 5 s keep it."""
    server = _Server(ready_after=36, timer=15)  # polls every 0.5 s
    out, err = capture(server)
    assert err is None and out.exists()
    keeps = [i for i, (m, _) in enumerate(server.sent) if m == "PUT"]
    assert len(keeps) == 3 and ("DELETE", "/api/streams/0") in server.sent
