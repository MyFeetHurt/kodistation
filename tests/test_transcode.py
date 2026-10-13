"""Transcoded playback (backlog 011): transcode.py helpers (no Kodi, no server)."""
import json
import os
import re

import pytest
from conftest import FIXTURES
from resources.lib.epgstation import transcode

STREAMS = os.path.join(FIXTURES, "streams")


def _load(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("name", ["dev", "dev2"])
def test_modes_and_enabled_from_fixture_configs(name):
    config = _load(FIXTURES, name, "api", "config.json")
    for kind in ("ts", "encoded"):
        assert transcode.recorded_enabled(config, kind)
        assert transcode.hls_modes(config, kind) == ["720p", "480p"]


def test_modes_follow_the_file_type():
    config = {"isEnableTSRecordedStream": True, "isEnableEncodedRecordedStream": False,
              "streamConfig": {"recorded": {"ts": {"hls": ["a", "b", "c"]}, "encoded": {"hls": ["x"]}}}}
    assert transcode.hls_modes(config, "ts") == ["a", "b", "c"]
    assert transcode.hls_modes(config, "encoded") == ["x"]
    assert transcode.recorded_enabled(config, "ts") and not transcode.recorded_enabled(config, "encoded")
    assert transcode.hls_modes(config, None) == ["a", "b", "c"]  # no type: the TS


@pytest.mark.parametrize("config", [{}, None, {"streamConfig": {}}, {"streamConfig": {"recorded": {"ts": {}}}},
                                    {"streamConfig": {"recorded": {"ts": {"hls": "720p"}}}}])
def test_modes_missing(config):
    assert transcode.hls_modes(config, "ts") == []
    assert not transcode.recorded_enabled(config, "ts")


def test_openapi_config_has_the_keys():
    spec = _load(os.path.dirname(FIXTURES), "..", "docs", "api", "openapi.json")
    props = spec["components"]["schemas"]["Config"]["properties"]
    assert "isEnableTSRecordedStream" in props and "isEnableEncodedRecordedStream" in props and "streamConfig" in props


@pytest.mark.parametrize("modes,choice,want", [
    (["720p", "480p"], 0, 0), (["720p", "480p"], 1, 1), (["720p", "480p"], 2, None),
    ([], 0, None), ([], 1, None), (["only"], 1, 0), (["only"], 0, 0), (["a", "b", "c"], 1, 2), (["a"], 7, 0)])
def test_mode_index(modes, choice, want):
    assert transcode.mode_index(modes, choice) == want


def test_stream_state_with_the_captured_streams():
    streams = _load(STREAMS, "streams.json")
    item = streams["items"][0]
    assert "viodeFileId" in item  # 2.10.0's typo, kept by the capture
    sid, vid, mode = item["streamId"], item["viodeFileId"], item["mode"]
    assert transcode.stream_state(streams, sid) == "ready" and transcode.is_ready(streams, sid)
    assert transcode.stream_state(streams, sid, vid, mode) == "ready"
    assert transcode.stream_state(streams, sid + 1, vid, mode) == "gone"
    off = {"items": [dict(item, isEnable=False)]}
    assert transcode.stream_state(off, sid, vid, mode) == "waiting" and not transcode.is_ready(off, sid)
    assert transcode.stream_state(_load(STREAMS, "streams_empty.json"), sid) == "gone"
    for junk in (None, [], {"items": None}, {"items": ["x"]}):
        assert transcode.stream_state(junk, sid) == "gone"


def test_stream_state_other_client_with_a_reused_id():
    """011 review L2: our id listed with another file or mode is someone else's stream."""
    streams = _load(STREAMS, "streams.json")
    item = streams["items"][0]
    sid, vid, mode = item["streamId"], item["viodeFileId"], item["mode"]
    assert transcode.stream_state(streams, sid, vid + 1, mode) == "other"
    assert transcode.stream_state(streams, sid, vid, mode + 1) == "other"
    spec = {"items": [{k: v for k, v in dict(item, videoFileId=vid).items() if k != "viodeFileId"}]}
    assert transcode.stream_state(spec, sid, vid, mode) == "ready"  # the spec's spelling works too


def test_stream_state_compares_only_what_the_item_has():
    """Review round 2 L-1: an item without the file id or the mode is not "another client"."""
    bare = {"items": [{"streamId": 0, "isEnable": True}]}
    assert transcode.stream_state(bare, 0, 132, 1) == "ready"
    no_mode = {"items": [{"streamId": 0, "isEnable": False, "viodeFileId": 132}]}
    assert transcode.stream_state(no_mode, 0, 132, 1) == "waiting"
    assert transcode.stream_state(no_mode, 0, 133, 1) == "other"


@pytest.mark.parametrize("status,detail,want", [
    (500, "EncodeProcessManageModelCreateError", "No free encoder on the server"),
    (500, "CreateStreamProcessError", "No free encoder on the server"),
    (500, "OutOfRange", "Start position is past the end"),
    (500, "ConfigIsUndefined", "Server: streaming off"),
    (500, "VideoFileIsNotFound", "Could not start transcoded stream (HTTP 500)"),
    (500, "VideoIsNull", "Could not start transcoded stream (HTTP 500)"),
    (500, "SomethingNew", "Could not start transcoded stream (HTTP 500)"),
    (404, None, "Could not start transcoded stream (HTTP 404)"),
    (None, None, "Could not start transcoded stream"),
    (200, "unexpected answer", "Could not start transcoded stream (HTTP 200)"),
])
def test_error_message(status, detail, want):
    assert transcode.error_message(status, detail) == want


def test_error_message_localized_and_empty_strings():
    assert transcode.error_message(500, "OutOfRange", {"out_of_range": "範囲外"}) == "範囲外"
    assert transcode.error_message(500, "OutOfRange", {"out_of_range": ""}) == "Start position is past the end"


def test_fmt_position():
    assert [transcode.fmt_position(s) for s in (0, 59, 60, 754, 3599, 3600, 5025, -3)] == \
        ["0:00", "0:59", "1:00", "12:34", "59:59", "1:00:00", "1:23:45", "0:00"]


def _labels(choices):
    return [(label, ss) for label, ss in choices]


def test_start_choices():
    s = {"from_start": "最初から", "from_pos": "{0} から", "from_custom": "位置を指定…"}
    assert _labels(transcode.start_choices(None, 1800, s)) == [("最初から", 0), ("位置を指定…", None)]
    assert _labels(transcode.start_choices(59, 1800, s)) == [("最初から", 0), ("位置を指定…", None)]
    assert _labels(transcode.start_choices(60, 1800, s)) == [("最初から", 0), ("1:00 から", 60), ("位置を指定…", None)]
    assert _labels(transcode.start_choices(754.6, 1800, s))[1] == ("12:34 から", 754)
    assert len(transcode.start_choices(1739, 1800, s)) == 3  # 61 s before the end
    assert len(transcode.start_choices(1740, 1800, s)) == 2  # duration - 60: watched to the end
    assert len(transcode.start_choices(5000, None, s)) == 3  # unknown duration (in progress)
    assert transcode.start_choices(None, None)[0] == ("From the beginning", 0)


@pytest.mark.parametrize("text,duration,want", [
    ("00:05", 1800, 300), ("0:05", None, 300), ("01:30", 7200, 5400), ("01:30", 3600, 3570), ("10:00", 1800, 1770),
    ("00:00", 1800, 0), ("", 1800, None), (None, 1800, None), ("ab", 1800, None), ("00:60", 1800, None),
    (" 1 : 05 ", None, 3900), ("00:05", 10, 0), ("00:05", 0, 300)])
def test_parse_position(text, duration, want):
    assert transcode.parse_position(text, duration) == want


# --- KeepAlive --------------------------------------------------------------------------------------------------

PATH = "/streamfiles/stream0.m3u8"
URL = "http://srv:8888/streamfiles/stream0.m3u8?k=abc"


def _keep(previous="", ss=0):
    return transcode.KeepAlive("abc", PATH, ss, 100.0, previous)  # the start GET answered at 100


def _simulate(slow_verify_at, hung_keeps, until=60):
    """Drive a KeepAlive like plugin._keep with a 1 s loop: PUTs take 0.1 s, except the hung ones (KEEP_TIMEOUT, then
    an error); a verify GET takes 0.1 s, the slow one STREAMS_TIMEOUT. Returns the send times of the good keeps and the
    longest gap between them (the server's timer must never see SERVER_TIMER)."""
    k = transcode.KeepAlive("abc", PATH, 0, 0.0)
    now, sends, good, n = 0.0, [], [0.0], 0
    while now < until:
        now += 1
        if not k.keep_due(now):
            continue
        n += 1
        sent = now
        if n in hung_keeps:
            now += transcode.KEEP_TIMEOUT
            k.keep_failed(None, None, now)
            continue
        now += 0.1
        good.append(sent)
        k.keep_ok(sent, now)
        if k.needs_verify():
            now += transcode.STREAMS_TIMEOUT if n == slow_verify_at else 0.1
            k.verified(sent)
        sends.append(sent)
    return good, max(b - a for a, b in zip(good, good[1:]))


@pytest.mark.parametrize("slow_verify_at,hung", [(None, {2}), (1, {2}), (4, {5}), (1, {2, 3}), (None, {1})])
def test_keep_timing_survives_a_slow_verify_and_a_hung_keep(slow_verify_at, hung):
    """011 review M1 / round 2 L-3: a slow ownership check, then keeps that hang for their whole timeout: the retry
    is scheduled from the last good keep, so the server's 15 s timer never runs out."""
    good, gap = _simulate(slow_verify_at, hung)
    assert gap < transcode.SERVER_TIMER, (good, gap)


def test_late_failure_retries_at_once():
    """Round 2 L-3: a keep that fails close to the server's deadline is retried at once, not KEEP_RETRY s later."""
    k = _keep()  # last good: the start at 100
    k.keep_failed(None, None, 110.5)
    assert k.next_keep == 100 + transcode.SERVER_TIMER - transcode.KEEP_TIMEOUT - 1 == 111
    k.keep_failed(None, None, 113)
    assert k.next_keep <= 113  # due now


def test_keep_chain_and_trust():
    """Round 2 L-2: good keeps within the server's timer keep the chain (trusted for TRUST_WINDOW s); a gap breaks
    it until a verify."""
    k = _keep()
    assert k.trusted(111.9) and not k.trusted(112)
    k.keep_ok(105, 105.1)
    assert k.last_good == 105 and k.trusted(116)
    k.keep_ok(125, 125.1)  # 20 s later: a gap
    assert k.last_good is None and not k.trusted(125.2) and k.needs_verify()
    k.verified(125)
    assert k.trusted(126) and k.last_good == 125


def test_verify_every_third_keep():
    k = _keep()
    flags = []
    for t in range(105, 150, 5):
        assert k.keep_due(t)
        k.keep_ok(t, t + 0.1)
        flags.append(k.needs_verify())
    assert flags == [True, False, False, True, False, False, True, False, False]


def test_keep_cadence_and_position():
    k = _keep(ss=300)
    out = [k.tick(100.0 + t, URL, "", t) for t in range(1, 32)]
    assert [i + 1 for i, a in enumerate(out) if a == "keep"] == [5, 10, 15, 20, 25, 30]
    assert k.started and k.position == 331 and k.reason is None


def test_keep_counts_from_the_last_keep_not_the_tick_rate():
    k = _keep()
    assert k.tick(104.5, URL) is None
    assert k.tick(105.2, URL) == "keep"
    assert k.tick(110.1, URL) is None
    assert k.tick(110.3, URL) == "keep"


def test_failed_keep_is_retried_soon():
    k = _keep()
    assert k.tick(105, URL) == "keep"
    assert k.keep_failed(None, None, 108) is None and k.reason is None  # timed out after 3 s
    assert k.tick(109, URL) is None
    assert k.tick(110, URL) == "keep"  # KEEP_RETRY s after the failure, 10 s after the last good keep
    assert k.tick(114, URL) is None and k.tick(115, URL) == "keep"  # then the usual cadence again
    assert k.keep_failed(500, "Other", 115) is None and k.tick(117, URL) == "keep"


def test_keeps_count_from_the_start_not_from_play():
    """Measured on the dev Kodi: the server's timer runs from the start GET, so the first keep is due KEEP_EVERY s
    after it, also when play() came later."""
    k = _keep()  # created at 100 (the start GET)
    assert not k.keep_due(104)
    k.playing_from(104, "http://srv/api/videos/1")
    assert k.tick(104.5, "http://srv/api/videos/1") is None
    assert k.tick(105, URL) == "keep"
    assert k.previous_file == "http://srv/api/videos/1"


def test_keep_due_while_waiting():
    k = _keep()
    assert [t for t in range(101, 113) if k.keep_due(t)] == [105, 110]
    k.keep_failed(500, "StreamIsUndefined", 111)
    assert not k.keep_due(200) and k.reason == "gone"


def test_start_timeout_counts_from_play():
    k = _keep()
    k.playing_from(112)
    k.tick(131, "")
    assert k.reason is None
    assert k.tick(132, "") == "stop" and k.reason == "timeout"


def test_stop_when_our_file_is_gone():
    k = _keep()
    k.tick(101, URL, "", 1.0)
    assert k.tick(102, "") is None
    assert k.tick(103, "") is None
    assert k.tick(104, "") == "stop" and k.reason == "ended" and k.position == 1.0
    assert k.tick(105, URL) == "stop"  # stays stopped


def test_stop_at_once_after_a_stop_callback():
    k = _keep()
    k.tick(101, URL, "", 1.0)
    k.on_stopped()
    assert k.tick(102, "") == "stop" and k.reason == "stopped"


def test_a_stop_callback_while_ours_still_plays_is_not_ours():
    k = _keep()
    k.tick(101, URL, "", 0.5)
    k.on_stopped()  # a late stop of something else
    assert k.tick(102, URL, "", 1.5) is None and k.reason is None


def test_failed_start():
    """011 review L1: a stop before ours played, then nothing of ours for FAIL_WAIT s: "failed" (not after 20 s)."""
    k = _keep()
    k.playing_from(100)
    k.on_started()
    k.on_stopped()
    assert k.tick(101, "") is None
    assert k.tick(103, "") is None
    assert k.tick(103.5, "") == "stop" and k.reason == "failed" and not k.started


def test_stop_without_our_start_callback_waits_for_the_timeout():
    """Review round 2 L-4: a stop Kodi never started our item for is left to START_TIMEOUT."""
    k = _keep()
    k.on_started()  # before play(): not ours
    k.playing_from(100)
    k.on_stopped()
    assert all(k.tick(100 + t, "") in (None, "keep") for t in range(1, 20)) and not k.stop_seen
    assert k.tick(120, "") == "stop" and k.reason == "timeout"


def test_failed_start_after_switching_from_another_file():
    """Kodi 21.3 sends one stop for the file ours replaced: not a failure; a stop after our start callback is."""
    k = _keep()
    k.playing_from(100, "http://srv/api/videos/1")
    k.on_stopped()  # the previous file's stop
    assert k.tick(104, "") is None and not k.stop_seen
    k.on_started()
    k.on_stopped()  # ours failed
    k.tick(105, "")
    assert k.tick(107.5, "") == "stop" and k.reason == "failed"


def test_late_start_after_a_stop_is_still_ours():
    k = _keep()
    k.playing_from(100)
    k.on_started()
    k.on_stopped()
    k.tick(101, "")
    assert k.tick(102, URL, "", 0.0) is None and k.started and k.reason is None


def test_paused_too_long():
    """011 review L3: the player time stands still for PAUSE_LIMIT s -> "paused" at the paused position."""
    k = _keep(ss=60)
    k.tick(101, URL, "", 10.0)
    k.tick(102, URL, "", 11.0)
    assert k.tick(102 + transcode.PAUSE_LIMIT - 1, URL, "", 11.2) in (None, "keep")
    assert k.tick(102 + transcode.PAUSE_LIMIT, URL, "", 11.2) == "stop"
    assert k.reason == "paused" and k.position == 71.2


def test_playing_resets_the_pause_clock():
    k = _keep()
    for t in range(0, 3 * transcode.PAUSE_LIMIT, 600):
        k.tick(100 + t, URL, "", float(t))
    assert k.reason is None


def test_stop_when_another_file_plays():
    k = _keep()
    k.tick(101, URL)
    assert k.tick(102, "http://srv/api/videos/9") == "stop" and k.reason == "other"


def test_previous_file_before_the_start_is_not_another_file():
    k = _keep()
    k.playing_from(100, "http://srv/api/videos/1")
    assert k.tick(101, "http://srv/api/videos/1") is None
    assert k.tick(102, URL) is None and k.started


def test_another_file_before_the_start():
    k = _keep()
    k.playing_from(100, "http://srv/api/videos/1")
    assert k.tick(101, "http://srv/api/videos/2") == "stop" and k.reason == "other" and not k.started


def test_never_started():
    k = _keep()
    assert all(k.tick(100.0 + t, "") in (None, "keep") for t in range(1, 20))
    assert k.tick(120, "") == "stop" and k.reason == "timeout"


def test_keep_failed_gone_and_server_lost():
    k = _keep()
    assert k.keep_failed(None, None, 101) is None and k.reason is None  # connection error: retried
    assert k.keep_failed(500, "StreamIsUndefined", 102) == "stop" and k.reason == "gone"
    assert _keep().keep_failed(404, None, 101) == "stop"
    lost = _keep()
    assert lost.server_lost() == "stop" and lost.reason == "gone" and lost.tick(101, URL) == "stop"


def test_gone_before_playing_is_a_failed_start():
    assert transcode.end_message("gone", None, started=False) == "Could not start transcoded stream"


@pytest.mark.parametrize("reason,want", [("failed", "Could not start transcoded stream"),
                                         ("timeout", "Could not start transcoded stream"),
                                         ("gone", "Transcoded stream ended"), ("paused", "Transcoded stream ended"),
                                         ("stopped", None), ("ended", None), ("other", None), (None, None)])
def test_end_message(reason, want):
    assert transcode.end_message(reason) == want


def test_nonce_matching_with_reused_ids():
    k = _keep()
    assert k.is_ours(URL) and k.is_ours(URL + "&x=1") and k.is_ours(URL + "|Authorization=Basic%20x")
    assert k.is_ours("http://srv:8888/streamfiles/stream0.m3u8")  # inputstream.adaptive: no query
    assert not k.is_ours("http://srv:8888/streamfiles/stream0.m3u8?k=old")  # an earlier stream with the same id
    assert not k.is_ours("http://srv:8888/streamfiles/stream10.m3u8?k=abc")
    assert not k.is_ours("") and not k.is_ours("http://srv/api/videos/1?k=abc")
    assert k.is_ours("anything", "abc") and not k.is_ours(URL, "old")  # the item's property wins


def test_another_stream_with_the_same_id_is_another_file():
    k = _keep()
    k.tick(101, URL)
    assert k.tick(102, "http://srv:8888/streamfiles/stream0.m3u8", "zzz") == "stop" and k.reason == "other"


# --- play item ---------------------------------------------------------------------------------------------------

def test_stream_item():
    d = {"label": "L", "path": "plugin://x", "playable": True, "info": {"title": "T", "duration": 1800, "plot": "P"},
         "art": {}, "props": {"a": "1"}, "stream": {"codec": "h264", "width": 1, "height": 1, "duration": 9},
         "context": [("x", "y")]}
    out = transcode.stream_item(d, "n1")
    assert out["info"] == {"title": "T", "plot": "P", "playcount": 0}
    assert out["props"] == {"a": "1", "epgstation.stream": "n1"} and out["mimetype"] == transcode.MIME
    assert out["stream"]["duration"] == 0 and not out["playable"] and out["context"] == []
    assert d["info"]["duration"] == 1800 and "epgstation.stream" not in d["props"]  # the input is unchanged
    assert transcode.stream_item(dict(d, stream=None), "n")["stream"] is None


def test_isa_properties():
    assert transcode.isa_properties() == {"inputstream": "inputstream.adaptive",
                                          "inputstream.adaptive.play_timeshift_buffer": "true"}
    props = transcode.isa_properties("Authorization=Basic%20eA%3D%3D")
    assert props["inputstream.adaptive.manifest_headers"] == props["inputstream.adaptive.stream_headers"] == \
        "Authorization=Basic%20eA%3D%3D"


# --- PositionStore ---------------------------------------------------------------------------------------------

def test_position_store_roundtrip(tmp_path):
    path = str(tmp_path / "p" / "stream_positions.json")
    store = transcode.PositionStore(path, "http://a")
    store.put(5, 754.7, 1800)
    store.put(6, 30, 1800)  # below RESUME_MIN: not kept
    assert store.save()
    again = transcode.PositionStore(path, "http://a")
    assert again.get(5) == 754 and again.get("5") == 754 and again.get(6) is None
    assert transcode.PositionStore(path, "http://b").get(5) is None  # another server starts empty


def test_position_store_cleared_near_the_end(tmp_path):
    store = transcode.PositionStore(str(tmp_path / "s.json"), "u")
    store.put(1, 600, 1800)
    store.put(1, 1745, 1800)
    assert store.get(1) is None
    store.put(2, 600, None)  # in progress: kept
    assert store.get(2) == 600


def test_position_store_cap(tmp_path):
    store = transcode.PositionStore(str(tmp_path / "s.json"), "u")
    for i in range(transcode.POSITIONS_MAX + 5):
        store.put(i, 100, None)
    store.put(3, 200, None)  # moves to the newest
    assert len(store.positions) == transcode.POSITIONS_MAX
    assert store.get(0) is None and store.get(5) is None and store.get(3) == 200 and store.get(6) == 100


@pytest.mark.parametrize("content", ["not json", "[]", '{"server_url": "u", "positions": []}',
                                     '{"server_url": "u", "positions": {"1": "x", "2": true, "3": 120}}'])
def test_position_store_unreadable(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content, encoding="utf-8")
    store = transcode.PositionStore(str(path), "u")
    assert store.get(1) is None and store.get(2) is None
    assert store.get(3) == (120 if '"3": 120' in content else None)


def test_removal_checklist_lists_every_hook():
    """transcode.py's docstring names every file with a transcoded playback (011) hook."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = [os.path.join(root, "Makefile")]
    for top in ("plugin.video.epgstation", "tools", "tests"):
        files += [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(root, top)) for f in fs
                  if f.endswith((".py", ".xml", ".po")) and "fixtures" not in d and "__pycache__" not in d]
    own = {os.path.abspath(transcode.__file__), os.path.abspath(__file__)}
    hooks = {os.path.basename(f) for f in files
             if os.path.abspath(f) not in own and "(011)" in open(f, encoding="utf-8").read()}
    assert {"plugin.py", "api.py", "items.py", "fake_epgstation.py", "settings.xml", "kodi_smoke.py"} <= hooks
    missing = sorted(h for h in hooks if h not in transcode.__doc__)
    assert not missing, missing



def test_removal_checklist_names_real_plugin_and_api_hooks():
    """011 review nit 1: every plugin.py / api.py name in the checklist exists, and every function of plugin.py's
    transcoded playback section is listed."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lib = os.path.join(root, "plugin.video.epgstation", "resources", "lib", "epgstation")
    plugin = open(os.path.join(lib, "plugin.py"), encoding="utf-8").read()
    api = open(os.path.join(lib, "api.py"), encoding="utf-8").read()
    named = set(re.findall(r"\b_?[a-z_]*[A-Za-z][A-Za-z0-9_]*\b", transcode.__doc__.split("- plugin.py:", 1)[1]
                           .split("\n- api.py:", 1)[0]))
    for name in named - {"stream", "timeout", "NON_DIRECTORY_ACTIONS"}:
        assert re.search(rf"\b(def|class) {name}\b", plugin), name
    assert "NON_DIRECTORY_ACTIONS" in plugin
    section = plugin.split("# --- transcoded playback (011) ---", 1)[1].split("\n    # --- ", 1)[0]
    for name in re.findall(r"^    def (\w+)\(", section, re.M):
        assert name in named, name
    for name in re.findall(r"(\w+)\(\)", transcode.__doc__.split("- api.py:", 1)[1].split("\n- ", 1)[0]):
        assert re.search(rf"    def {name}\(", api), name


# --- ActiveStreams (tester F1, final review round: one file per stream) -------------------------------------------

def test_active_streams_one_file_per_stream(tmp_path):
    d = str(tmp_path / "stream_active")
    store = transcode.ActiveStreams(d)
    assert store.of("http://a") == []  # no directory yet
    assert store.add("http://a", 0, 132, 1, 1000) and store.add("http://a", 1, 133, 0, 2000)
    assert store.add("http://b", 0, 9, 0, 3000)
    assert len(os.listdir(d)) == 3  # each stream its own file
    again = transcode.ActiveStreams(d)
    assert [e["streamId"] for e in again.of("http://a")] == [0, 1]
    assert again.of("http://a")[0] == {"server_url": "http://a", "streamId": 0, "videoFileId": 132, "mode": 1,
                                       "startedAt": 1000}
    assert again.remove("http://a", 0) and again.remove("http://a", 7)  # unknown: nothing to remove
    assert [e["streamId"] for e in again.of("http://a")] == [1] and len(again.of("http://b")) == 1
    assert again.add("http://a", 1, 140, 0, 5000)  # the id reused later: replaces its own entry
    assert again.of("http://a")[0]["videoFileId"] == 140 and len(os.listdir(d)) == 2


@pytest.mark.parametrize("content", ["not json", "[]", '{"server_url": "http://a", "streamId": "x"}',
                                     '{"server_url": "http://a", "streamId": true}'])
def test_active_streams_unreadable_entries_are_skipped(tmp_path, content):
    d = tmp_path / "stream_active"
    d.mkdir()
    (d / "bad.json").write_text(content, encoding="utf-8")
    store = transcode.ActiveStreams(str(d))
    store.add("http://a", 3, 1, 0, 1)
    assert [e["streamId"] for e in store.of("http://a")] == [3]


NOW = 1_800_000_000_000
ENTRY = {"server_url": "u", "streamId": 0, "videoFileId": 132, "mode": 1, "startedAt": NOW - 60_000}


def _listing(**item):
    return {"items": [dict({"streamId": 0, "isEnable": True, "viodeFileId": 132, "mode": 1}, **item)]}


@pytest.mark.parametrize("streams,want", [
    (_listing(), "check"),
    (_listing(isEnable=False), "live"),
    ({"items": []}, "forget"),
    (_listing(streamId=1), "forget"),
    (_listing(viodeFileId=133), "forget"),
    (_listing(mode=0), "forget"),
    ({"items": [{"streamId": 0, "isEnable": True, "mode": 1}]}, "forget"),  # no file id: strict, not "check"
    ({"items": [{"streamId": 0, "isEnable": True, "viodeFileId": 132}]}, "forget"),  # no mode
    ({"items": [{"streamId": 0, "isEnable": True, "videoFileId": 132, "mode": 1}]}, "check"),  # the spec's key
    (None, "forget")])
def test_stale_state(streams, want):
    assert transcode.stale_state(ENTRY, streams, NOW) == want


def test_stale_state_old_entries():
    old = dict(ENTRY, startedAt=NOW - transcode.STALE_MAX_AGE_MS - 1)
    assert transcode.stale_state(old, _listing(), NOW) == "old"
    assert transcode.stale_state(dict(ENTRY, startedAt=None), _listing(), NOW) == "old"
