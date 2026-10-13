"""Backlog 007: scheduled recordings logic (resources/lib/epgstation/reserves.py), against the captured
reservation fixtures (tools/snapshot_api.py --reserves). Conflicts, manual and time-specified reservations exist
on neither server: they are derived from captured items (flags flipped, ruleId / programId dropped)."""
import json
import os
import re
import time

import pytest
from conftest import FIXTURES, ROOT
from resources.lib.epgstation import api as api_mod
from resources.lib.epgstation import epgmeta, items, reserves
from resources.lib.epgstation.genres import item_genres

SETS = ["dev", "dev2"]
MIN = 60 * 1000
JA = {"reserves": "録画予約", "conflict": "競合", "overlap": "重複", "skip": "除外", "delete": "予約を削除",
      "skip_this": "この回を除外", "stop": "録画を停止", "unskip": "除外を解除", "unoverlap": "重複を解除して録画",
      "q_delete": "この予約を削除しますか?",
      "q_skip": "この回を録画しません。ルールはそのままです。",
      "q_stop": "録画を停止しますか? ここまでの録画は残ります。", "q_unskip": "除外を解除して、この回を録画しますか?",
      "q_unoverlap": "以前に録画済みの番組ですが、録画しますか?", "q_moves": "この回は録画されません。",
      "done_delete": "予約を削除しました", "done_skip": "この回は録画しません", "done_stop": "録画を停止しました",
      "done_unskip": "除外を解除しました", "done_unoverlap": "重複を解除しました", "failed": "失敗しました: {0}",
      "failed_net": "失敗しました (通信エラー)",
      "gone": "この予約はもうありません", "changed": "予約の状態が変わりました", "rule": "ルール: {0}",
      "manual": "手動予約", "line_conflict": "競合のため録画されません", "line_overlap": "録画済みのため録画しません",
      "line_skip": "除外: 録画しません", "recording": "録画中", "weekdays": "日,月,火,水,木,金,土",
      "q_airing": "放送中のため途中から録画されます", "q_stop_rest": "この回の残りは録画されません。"}


def _load(fixture_set, *path):
    full = os.path.join(FIXTURES, fixture_set, *path)
    if not os.path.isfile(full):
        pytest.skip(f"no {full}")
    with open(full, encoding="utf-8") as f:
        return json.load(f)


def _reserves(fixture_set):
    return _load(fixture_set, "api", "reserves.json")["reserves"]


def _now(fixture_set):
    return _load(fixture_set, "reserves_meta.json")["capturedAt"]


def _recording(fixture_set):
    return _load(fixture_set, "api", "recording.json")["records"]


def _channels(fixture_set):
    return _load(fixture_set, "api", "channels.json")


def api(username=""):
    return api_mod.EPGStation("http://epgs:8888", username, "pw")


def local(y, mo, d, h=0, mi=0):
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)) * 1000)


def res(rid=1, start=None, minutes=30, rule=1, state="normal", program=True, channel=3211240960, **kw):
    """A reservation derived from a captured dev one (texts and genres real), with the given id/time/flags."""
    base = dict(_reserves("dev")[0])
    start = local(2026, 10, 12, 21, 0) if start is None else start
    base.update(id=rid, startAt=start, endAt=start + minutes * MIN, channelId=channel, isConflict=state == "conflict",
                isSkip=state == "skip", isOverlap=state == "overlap", ruleId=rule)
    if rule is None:
        base.pop("ruleId")
    if not program:
        base.pop("programId", None)
        base["isTimeSpecified"] = True
    base.update(kw)
    return base


# --- fixtures ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_set", SETS)
def test_reserve_fixtures_within_budget(fixture_set):
    path = os.path.join(FIXTURES, fixture_set, "api", "reserves.json")
    assert os.path.getsize(path) <= 60_000
    data = _reserves(fixture_set)
    assert 0 < len(data) <= 40
    assert all("rawExtended" not in r for r in data)  # captured full-width (isHalfWidth=false)
    assert [r["startAt"] for r in data] == sorted(r["startAt"] for r in data)


@pytest.mark.parametrize("fixture_set", SETS)
def test_every_fixture_recording_finds_its_reservation(fixture_set):
    rs = _reserves(fixture_set)
    for rec in _recording(fixture_set):
        r = reserves.match_recording(rs, rec)
        assert r is not None, rec["id"]
        assert r["channelId"] == rec["channelId"] and reserves.is_recording(r, _recording(fixture_set))
        assert r["startAt"] <= _now(fixture_set) < r["endAt"]


@pytest.mark.parametrize("fixture_set", SETS)
def test_counts_equal_captured_cnts_of_the_kept_states(fixture_set):
    """Non-normal states are kept whole (up to 10 each): their counts equal the server's cnts."""
    cnts = _load(fixture_set, "api", "reserves", "cnts.json")
    got = reserves.counts(reserves.upcoming(_reserves(fixture_set), _now(fixture_set)))
    for state, key in (("conflict", "conflicts"), ("skip", "skips"), ("overlap", "overlaps")):
        assert got[state] == min(cnts[key], 10), state


# --- classify / counts / split / upcoming -----------------------------------------------------------------------

def test_classify_priority_like_get_cnts():
    assert reserves.classify(res()) == "normal"
    assert reserves.classify(res(state="skip")) == "skip"
    assert reserves.classify(res(state="overlap")) == "overlap"
    assert reserves.classify(res(state="conflict")) == "conflict"
    assert reserves.classify(res(isConflict=True, isSkip=True, isOverlap=True)) == "conflict"
    assert reserves.classify(res(isSkip=True, isOverlap=True)) == "skip"


def test_counts_and_split():
    rs = [res(1), res(2, state="overlap"), res(3, state="skip"), res(4, state="conflict"), res(5, state="overlap")]
    assert reserves.counts(rs) == {"normal": 1, "conflict": 1, "skip": 1, "overlap": 2}
    assert [r["id"] for r in reserves.split(rs, "overlap")] == [2, 5]
    assert [r["id"] for r in reserves.split(rs, "normal")] == [1]


def test_upcoming_drops_ended():
    now = local(2026, 10, 12, 21, 0)
    ended_now = res(1, start=now - 30 * MIN)  # endAt == now
    starts_now = res(2, start=now)
    running = res(3, start=now - 10 * MIN)
    assert [r["id"] for r in reserves.upcoming([ended_now, starts_now, running], now)] == [2, 3]


# --- folders ----------------------------------------------------------------------------------------------------

def test_type_folders_order_and_counts():
    out = reserves.type_folders({"normal": 5, "skip": 2, "overlap": 13, "conflict": 1}, JA)
    assert [d["label"] for d in out] == ["競合 (1)", "重複 (13)", "除外 (2)"]
    assert all(d["folder"] for d in out)
    assert [d["path"].rsplit("type=", 1)[1] for d in out] == ["conflict", "overlap", "skip"]
    assert [d["label"] for d in reserves.type_folders({"normal": 5, "overlap": 13}, JA)] == ["重複 (13)"]
    assert [d["label"] for d in reserves.type_folders({"skip": 2}, {})] == ["Skipped (2)"]
    assert reserves.type_folders({"normal": 3}, JA) == []


# --- actions ----------------------------------------------------------------------------------------------------

def test_action_for_table():
    now = local(2026, 10, 12, 21, 10)
    future = local(2026, 10, 13, 21, 0)
    running = res(start=now - 10 * MIN)
    assert reserves.action_for(running, now, [rec_of(running)]) == "stop"
    assert reserves.action_for(running, now) == "cancel"  # in its window but not in /recording (failed, PREP_TIME)
    assert reserves.action_for(dict(running, ruleId=None), now) == "cancel"
    assert reserves.action_for(res(start=future), now) == "cancel"
    assert reserves.action_for(res(start=future, rule=None), now) == "cancel"
    assert reserves.action_for(res(start=future, state="conflict"), now) == "cancel"
    conflict = res(start=now - 10 * MIN, state="conflict")
    assert reserves.action_for(conflict, now, [rec_of(conflict)]) == "cancel"  # a conflict never records
    assert reserves.action_for(res(start=future, state="skip"), now) == "unskip"
    assert reserves.action_for(res(start=now - 10 * MIN, state="skip"), now) == "unskip"
    assert reserves.action_for(res(start=future, state="overlap"), now) == "unoverlap"
    assert reserves.action_for(res(start=now - 60 * MIN, state="skip"), now) is None  # ended
    assert reserves.action_for(res(start=now - 60 * MIN), now) is None
    assert reserves.action_for(res(start=future, rule=None, program=False), now) == "cancel"


def rec_of(r, **kw):
    """The /recording item EPGStation makes from reservation r (the recorder copies these fields)."""
    rec = {k: r[k] for k in ("channelId", "programId", "startAt", "endAt") if k in r}
    rec.update(id=900, isRecording=True, **kw)
    return rec


def test_is_recording_follows_the_recording_list():
    """L3: recording = listed in /recording, not "inside the time window"."""
    now = local(2026, 10, 12, 21, 10)
    r = res(start=now - MIN)
    assert reserves.is_recording(r, [rec_of(r)])
    assert not reserves.is_recording(r, [])
    assert not reserves.is_recording(r, [rec_of(r, programId=1)])
    assert not reserves.is_recording(res(start=now - MIN, state="skip"), [rec_of(r)])
    assert not reserves.is_recording(res(start=now - MIN, state="conflict"), [rec_of(r)])
    timed = res(start=now - MIN, program=False)
    assert reserves.is_recording(timed, [rec_of(timed)])


def test_entry_labels():
    assert reserves.entry_label("stop", res(), JA) == "録画を停止"
    assert reserves.entry_label("cancel", res(rule=None), JA) == "予約を削除"
    assert reserves.entry_label("cancel", res(), JA) == "この回を除外"
    assert reserves.entry_label("unskip", res(state="skip"), JA) == "除外を解除"
    assert reserves.entry_label("unoverlap", res(state="overlap"), JA) == "重複を解除して録画"
    assert reserves.entry_label("cancel", res(rule=None), {}) == "Delete reservation"
    assert reserves.entry_label("cancel", res(), {}) == "Skip this airing"


def test_confirm_messages():
    r = res(name="ＮＨＫニュース７", start=local(2026, 10, 12, 19, 0))
    lines = reserves.confirm_message("cancel", r, "NHK総合1", JA).split("\n")
    assert lines[0] == "NHKニュース7"
    assert lines[1] == "10/12(月) 19:00-19:30 NHK総合1"
    assert lines[2] == JA["q_skip"]
    assert reserves.confirm_message("cancel", dict(r, ruleId=None), "x", JA).endswith(JA["q_delete"])
    assert reserves.confirm_message("stop", r, "x", JA).endswith(JA["q_stop"] + " " + JA["q_stop_rest"])
    manual = dict(r)
    manual.pop("ruleId")
    assert reserves.confirm_message("stop", manual, "x", JA).endswith(JA["q_stop"])
    assert reserves.confirm_message("unskip", r, "x", JA).endswith(JA["q_unskip"])
    assert reserves.confirm_message("unoverlap", r, "x", JA).endswith(JA["q_unoverlap"])
    en = reserves.confirm_message("cancel", r, "NHK", {}).split("\n")
    assert en[1] == "Mon 10/12 19:00-19:30 NHK"
    assert en[2].startswith("Do not record this airing?")


def test_confirm_warns_when_already_airing():
    """M2: undoing a skip / clearing an overlap of a program on air records from now on."""
    now = local(2026, 10, 12, 19, 10)
    for op, state in (("unskip", "skip"), ("unoverlap", "overlap")):
        airing = res(start=local(2026, 10, 12, 19, 0), state=state)
        lines = reserves.confirm_message(op, airing, "x", JA, now).split("\n")
        assert lines[-1] == "放送中のため途中から録画されます" and lines[-2] == JA["q_" + op]
        future = res(start=local(2026, 10, 12, 20, 0), state=state)
        assert "放送中" not in reserves.confirm_message(op, future, "x", JA, now)
        assert reserves.confirm_message(op, airing, "x", {}, now).endswith(
            "Already airing: recording starts now")
    assert "放送中" not in reserves.confirm_message("cancel", res(start=local(2026, 10, 12, 19, 0)), "x", JA, now)


def test_error_messages():
    assert reserves.error_message(500, "ReservationIsNotFound", JA) == "この予約はもうありません"
    assert reserves.error_message(None, None, JA) == "失敗しました (通信エラー)"
    assert reserves.error_message(500, "X", JA) == "失敗しました: X"
    assert reserves.error_message(503, None, {}) == "Failed: HTTP 503"
    assert reserves.error_message(None, None, {}) == "Failed (connection error)"


def test_rule_cancel_wording_holds_for_skip_and_event_relay_delete():
    """L2: EPGStation deletes (not skips) a rule's event-relay reservation and the API cannot tell: the texts of a
    rule cancel say "will not record", never "moves to 除外 / Skipped"."""
    for strings in (JA, {}):
        texts = [reserves.confirm_message(op, res(), "x", strings) for op in ("cancel", "stop")]
        texts.append(reserves.done_message("cancel", res(), strings))
        assert not any("除外" in t or "Skipped" in t for t in texts)


def test_done_messages():
    assert reserves.done_message("cancel", res(rule=None), JA) == "予約を削除しました"
    assert reserves.done_message("cancel", res(), JA) == "この回は録画しません"
    assert reserves.done_message("stop", res(), JA) == "録画を停止しました"
    assert reserves.done_message("unskip", res(), JA) == "除外を解除しました"
    assert reserves.done_message("unoverlap", res(), JA) == "重複を解除しました"
    assert reserves.done_message("stop", res(), {}) == "Recording stopped"


def test_date_time():
    t = local(2026, 10, 12, 9, 5)
    assert reserves.date_time(t, JA) == "10/12(月) 09:05"
    assert reserves.date_time(t, {}) == "Mon 10/12 09:05"


# --- items ------------------------------------------------------------------------------------------------------

CH = {3211240960: "NHK総合1・大阪", 400191: "BS-TBS"}
RULES = {1: "アニメ"}


def _item(r, now, in_folder=False, logos=(3211240960,), lang="ja", strings=JA, on_error=None, a=None, records=()):
    return reserves.reserve_item(r, CH, set(logos), RULES, a or api(), now, lang, strings, on_error, in_folder,
                                 records)


def test_reserve_item_label_and_markers():
    now = local(2026, 10, 12, 21, 10)
    r = res(name="ドラマ　＃１", start=local(2026, 10, 12, 22, 0))
    assert _item(r, now)["label"] == "10/12(月) 22:00 NHK総合1・大阪  ドラマ #1"
    rec = res(start=now - MIN, name="x")
    assert _item(rec, now, records=[rec_of(rec)])["label"].startswith("[COLOR red]●[/COLOR] 10/12(月) 21:09 ")
    assert not _item(rec, now)["label"].startswith("[COLOR red]●")  # in its window, not recording
    assert "recording=1" in _item(rec, now, records=[rec_of(rec)])["path"]
    for state, marker in (("conflict", "[COLOR red][競合][/COLOR] "), ("overlap", "[COLOR grey][重複][/COLOR] "),
                          ("skip", "[COLOR grey][除外][/COLOR] ")):
        r = res(state=state, start=local(2026, 10, 12, 22, 0))
        assert _item(r, now)["label"].startswith(marker)
        assert not _item(r, now, in_folder=True)["label"].startswith("[COLOR")
    conflict = res(start=now - MIN, state="conflict")
    assert "●" not in _item(conflict, now, records=[rec_of(conflict)])["label"]


def test_reserve_item_info_props_and_path():
    now = local(2026, 10, 12, 21, 10)
    start = local(2026, 10, 12, 22, 0)
    r = res(rid=42, start=start, minutes=54)
    d = _item(r, now)
    info = d["info"]
    assert info["mediatype"] == "video" and info["title"] == items.to_half(r["name"])
    assert info["genres"] == item_genres(r, "ja")
    assert info["studios"] == ["NHK総合1・大阪"]
    assert info["duration"] == 54 * 60
    assert info["firstaired"] == info["premiered"] == "2026-10-12"
    assert info["tagline"] == "NHK総合1・大阪  10/12(月) 22:00-22:54"
    assert info["plotoutline"] == "10/12(月) 22:00-22:54"
    head = info["plot"].split("\n\n")[0].split("\n")
    assert head == ["10/12(月) 22:00-22:54", "ルール: アニメ"]
    assert d["props"]["epgstation.reserveId"] == "42"
    assert d["props"]["epgstation.ruleId"] == "1"
    assert d["props"]["epgstation.state"] == "normal"
    assert d["props"]["epgstation.recording"] == "false"
    assert d["props"]["epgstation.channelId"] == "3211240960"
    assert d["props"]["epgstation.programId"] == str(r["programId"])
    assert d["props"]["epgstation.startAt"] == "2026-10-12 22:00"
    assert d["folder"] is False and d["playable"] is False
    assert "action=reserve_info" in d["path"] and "reserveId=42" in d["path"] and "logo=1" in d["path"]
    assert "rule=" in d["path"]
    logo = "http://epgs:8888/api/channels/3211240960/logo"
    assert d["art"] == {"icon": logo, "thumb": logo}
    assert _item(r, now, logos=())["art"] == {}
    auth = _item(r, now, a=api("user"))["art"]["icon"]
    assert auth.startswith(logo + "|Authorization=")
    assert d["context"] == [("この回を除外", items.plugin_url(action="reserve_action", op="cancel", reserveId=42))]


def test_reserve_item_plot_lines_per_state():
    now = local(2026, 10, 12, 21, 10)
    start = local(2026, 10, 12, 22, 0)

    def head(r):
        return _item(r, now)["info"]["plot"].split("\n\n")[0].split("\n")

    manual = res(start=start, rule=None)
    assert head(manual)[1] == "手動予約"
    assert _item(manual, now)["props"]["epgstation.ruleId"] == ""
    assert head(res(start=start, state="conflict"))[2] == "競合のため録画されません"
    assert head(res(start=start, state="overlap"))[2] == "録画済みのため録画しません"
    assert head(res(start=start, state="skip"))[2] == "除外: 録画しません"
    running = res(start=now - MIN)
    assert _item(running, now, records=[rec_of(running)])["info"]["plot"].split("\n")[2] == "録画中"
    assert len(head(running)) == 2  # not in /recording: no 録画中 line
    assert len(head(res(start=start))) == 2
    assert head(res(start=start, rule=99))[1] == "ルール: 99"  # rule not in /rules


def test_reserve_item_context_per_state():
    now = local(2026, 10, 12, 21, 10)
    start = local(2026, 10, 12, 22, 0)

    def ctx(r):
        return _item(r, now)["context"]

    running = res(start=now - MIN)
    stop = _item(running, now, records=[rec_of(running)])["context"][0]
    assert stop[0] == "録画を停止" and "op=stop" in stop[1]
    assert ctx(running)[0][0] == "この回を除外"  # inside its window but not recording
    assert ctx(res(start=start, rule=None))[0][0] == "予約を削除"
    assert ctx(res(start=start, state="skip"))[0][0] == "除外を解除"
    assert "op=unskip" in ctx(res(start=start, state="skip"))[0][1]
    assert ctx(res(start=start, state="overlap"))[0][0] == "重複を解除して録画"
    assert "op=unoverlap" in ctx(res(start=start, state="overlap"))[0][1]
    assert all(len(ctx(r)) == 1 for r in (res(start=start), res(start=start, state="conflict")))


def test_reserve_item_metadata_and_parser_failure(monkeypatch):
    now = _now("dev")
    with_cast = [r for r in _reserves("dev") if (epgmeta.metadata(r) or {}).get("cast")]
    if not with_cast:
        pytest.skip("no dev reservation with credits")
    d = _item(with_cast[0], now)
    assert d["info"]["cast"]

    def boom(rec, on_error=None):
        if on_error:
            on_error(ValueError("x"))
        return None

    errors = []
    monkeypatch.setattr(epgmeta, "metadata", boom)
    r = with_cast[0]
    d = _item(r, now, on_error=lambda rec, e: errors.append(rec["id"]))
    assert errors == [r["id"]]
    assert "cast" not in d["info"]
    assert items.to_half(r["description"]) in d["info"]["plot"]


def test_reserve_item_english():
    now = local(2026, 10, 12, 21, 10)
    d = _item(res(start=local(2026, 10, 12, 22, 0)), now, lang="en", strings={})
    assert d["label"].startswith("Mon 10/12 22:00 ")
    assert d["info"]["plot"].split("\n")[1] == "Rule: アニメ"
    assert d["info"]["genres"] == item_genres(res(), "en")
    assert d["context"][0][0] == "Skip this airing"


@pytest.mark.parametrize("fixture_set", SETS)
def test_every_fixture_reservation_converts(fixture_set):
    now = _now(fixture_set)
    channels = items.channel_names(_channels(fixture_set))
    logos = {c["id"] for c in _channels(fixture_set) if c.get("hasLogoData")}
    rules = {r["id"]: items.rule_name(r) for r in _load(fixture_set, "api", "rules.json")["rules"]}
    for r in _reserves(fixture_set):
        d = reserves.reserve_item(r, channels, logos, rules, api(), now, "ja", JA, None, False,
                                  _recording(fixture_set))
        assert d["label"] and d["info"]["plot"]
        assert len(d["context"]) == 1


@pytest.mark.parametrize("fixture_set", SETS)
def test_listing_main_and_folders(fixture_set):
    now = _now(fixture_set)
    rs = _reserves(fixture_set)
    channels = items.channel_names(_channels(fixture_set))
    out = reserves.listing(rs, None, channels, set(), {}, api(), now, "ja", JA, None, _recording(fixture_set))
    up = reserves.upcoming(rs, now)
    folders = [d for d in out if d["folder"]]
    assert folders == reserves.type_folders(reserves.counts(up), JA)
    ids = [d["props"]["epgstation.reserveId"] for d in out if not d["folder"]]
    assert ids == [str(r["id"]) for r in reserves.split(up, "normal")]
    for state in reserves.STATES:
        sub = reserves.listing(rs, state, channels, set(), {}, api(), now, "ja", JA)
        assert [d["props"]["epgstation.reserveId"] for d in sub] == [str(r["id"]) for r in reserves.split(up, state)]
        assert not any(d["label"].startswith("[COLOR grey]") for d in sub)


# --- recordings -------------------------------------------------------------------------------------------------

def test_match_recording():
    a = res(1, program=True, programId=11, channel=5)
    b = res(2, program=False, channel=5, start=local(2026, 10, 12, 20, 0))
    c = res(3, programId=11, channel=6)
    assert reserves.match_recording([a, b, c], {"channelId": 5, "programId": 11, "startAt": 0})["id"] == 1
    assert reserves.match_recording([a, b, c], {"channelId": 6, "programId": 11, "startAt": 0})["id"] == 3
    assert reserves.match_recording([a, b, c], {"channelId": 5, "startAt": local(2026, 10, 12, 20, 0)})["id"] == 2
    assert reserves.match_recording([a, b, c], {"channelId": 7, "programId": 11, "startAt": 0}) is None
    assert reserves.match_recording([a], {"channelId": 5, "programId": 12, "startAt": a["startAt"]}) is None
    assert reserves.recording_still_listed([{"channelId": 5, "programId": 11, "startAt": 0}], a)
    assert not reserves.recording_still_listed([{"channelId": 5, "programId": 12, "startAt": 0}], a)
    assert not reserves.recording_still_listed([], a)


def test_context_stop():
    label, url = reserves.context_stop(63, JA)
    assert label == "録画を停止"
    assert url == items.plugin_url(action="reserve_action", op="stop", recordedId=63)


def test_recorded_item_stop_entry_only_while_recording():
    rec = {"id": 7, "channelId": 5, "startAt": 0, "endAt": 600000, "name": "Live", "isRecording": True}
    d = items.recorded_item(rec, api(), {}, "ja", {"stop": "録画を停止"})
    assert ("録画を停止", items.plugin_url(action="reserve_action", op="stop", recordedId=7)) in d["context"]
    d = items.recorded_item(dict(rec, isRecording=False), api(), {}, "ja", {"stop": "録画を停止"})
    assert not any("reserve_action" in c[1] for c in d["context"])
    d = items.recorded_item(rec, api(), {}, "en")
    assert [c[0] for c in d["context"] if "reserve_action" in c[1]] == ["Stop recording"]


def test_rule_name_keeps_rule_folder_naming():
    rules = {"rules": [{"id": 1, "reserveOption": {"name": "名前"}}, {"id": 2, "searchOption": {"keyword": "kw"}},
                       {"id": 3, "saveOption": {"directory": "dir"}}, {"id": 4}]}
    assert [items.rule_name(r) for r in rules["rules"]] == ["名前", "kw", "dir", "Rule 4"]
    folders = items.rule_folders(rules, {1: 1, 2: 1, 3: 1, 4: 1})
    assert [f["label"] for f in folders] == ["名前 (1)", "kw (1)", "dir (1)", "Rule 4 (1)"]


# --- layout rules (AC 1) ----------------------------------------------------------------------------------------

LIB = os.path.join(ROOT, "plugin.video.epgstation", "resources", "lib", "epgstation")


def _src(name):
    with open(os.path.join(LIB, name), encoding="utf-8") as f:
        return f.read()


def test_reserves_module_has_no_kodi_or_guide_imports():
    src = _src("reserves.py")
    assert not re.search(r"^\s*(import|from)\s+xbmc", src, re.M)
    assert not re.search(r"^\s*from\s+\.\s+import\s+.*\b(guide|live)\b", src, re.M)
    assert not re.search(r"^\s*from\s+\.(guide|live)\s+import", src, re.M)


def _api_methods(src):
    """{method name: body} of api.py's top-level and class functions."""
    out = {}
    for block in re.split(r"\n(?=    def |def |class )", src):
        m = re.match(r"\s*def (\w+)\(", block)
        if m:
            out[m.group(1)] = block.split("\n", 1)[1] if "\n" in block else ""
    return out


def _callers(src, pattern):
    """Names of the functions whose body matches pattern; "<module>" for code outside a function."""
    callers = set()
    for block in re.split(r"\n(?=    def |def |class )", src):
        m = re.match(r"\s*def (\w+)\(", block)
        body = block.split("\n", 1)[1] if "\n" in block else ""
        if m and re.search(pattern, body):
            callers.add(m.group(1))
        elif not m and re.search(pattern, block):
            callers.add("<module>")
    return callers


def test_api_writes_section_is_exactly_the_audited_writes():
    """007 + 014 + 006 + 018 + 011: the writes section holds six DELETEs, one POST, three PUTs and the GET that starts
    a transcode (011), nothing else writes."""
    src = _src("api.py")
    section = src.split("# --- writes (006, 007, 014, 018)", 1)
    assert len(section) == 2
    body = section[1].split("\n    # --- end of writes", 1)[0]
    writes = re.findall(r"^    def (\w+)\(", body, re.M)
    assert writes == ["delete", "cancel_reserve", "remove_skip", "remove_overlap", "post", "create_reserve", "put",
                      "protect_recorded", "unprotect_recorded", "delete_recorded", "delete_video_file",
                      # transcoded playback (011)
                      "start_recorded_hls", "streams", "keep_stream", "stop_stream", "stream_playlist_url"]
    assert '"PATCH"' not in src and "method=\"POST\"" not in src
    assert src.count('"POST"') == 1 and '"POST"' in _api_methods(src)["post"]
    assert src.count('"DELETE"') == 1 and '"DELETE"' in _api_methods(src)["delete"]
    assert src.count('"PUT"') == 1 and '"PUT"' in _api_methods(src)["put"]  # protect / delete (006)
    # 3 reservation DELETEs + delete_recorded (006) + delete_video_file (018) + stop_stream (011)
    assert src.count("self.delete(") == 6
    assert _callers(src, r"\bself\.delete\(") == {"cancel_reserve", "remove_skip", "remove_overlap",
                                                    "delete_recorded", "delete_video_file", "stop_stream"}
    assert _callers(src, r"\bself\.post\(") == {"create_reserve"} and src.count("self.post(") == 1
    assert _callers(src, r"\bself\.put\(") == {"protect_recorded", "unprotect_recorded", "keep_stream"}  # (011)
    assert src.count("self.put(") == 3


def test_api_request_is_called_only_from_get_delete_and_post():
    """L5 / 014 / 006: the generic _request (any method) is reached only through get(), delete(), post() and put()."""
    src = _src("api.py")
    assert _callers(src, r"\b_request\(") - {"_request"} == {"get", "delete", "post", "put"}
    assert src.count("_request(") == 5  # the definition + the four calls


def _addon_sources():
    return {name: _src(name) for name in os.listdir(LIB) if name.endswith(".py") and name != "api.py"}


def _method_body(src, name):
    return src.split(f"    def {name}(self):", 1)[1].split("\n    def ", 1)[0]


def test_write_methods_are_called_only_from_do_reserve_action():
    """The 007 DELETEs only in do_reserve_action (006's delete_recorded is test_recorded's)."""
    callers = {}
    for name, src in _addon_sources().items():
        for m in re.finditer(r"\.(cancel_reserve|remove_skip|remove_overlap|delete)\(", src):
            callers.setdefault(name, []).append(m.group(1))
    assert set(callers) == {"plugin.py"}
    plugin = _src("plugin.py")
    outside = plugin.replace(_method_body(plugin, "do_reserve_action"), "")
    assert not re.search(r"\.(cancel_reserve|remove_skip|remove_overlap|delete)\(", outside)


def test_create_reserve_is_called_only_from_do_record():
    """014: exactly one create_reserve call in the addon, in plugin.do_record; nothing calls api.post directly."""
    hits = {name: len(re.findall(r"\.create_reserve\(", src)) for name, src in _addon_sources().items()}
    assert {k: v for k, v in hits.items() if v} == {"plugin.py": 1}
    plugin = _src("plugin.py")
    assert re.search(r"\.create_reserve\(", _method_body(plugin, "do_record"))
    assert not any(re.search(r"\.post\(", src) for src in _addon_sources().values())


def test_guide_live_items_have_no_api_write_call():
    for name in ("guide.py", "live.py", "items.py", "reserves.py", "recorded.py", "library.py"):
        src = _src(name)
        assert not re.search(r"\.(cancel_reserve|remove_skip|remove_overlap|delete|post|create_reserve|put|"
                             r"protect_recorded|unprotect_recorded|delete_recorded|delete_video_file)\(", src), name
        assert "urlopen" not in src and '"POST"' not in src, name


# --- tools/dev/test_reserve.py (DEV-only write tool) -------------------------------------------------------------

def _test_reserve_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("test_reserve_tool", os.path.join(ROOT, "tools", "dev",
                                                                                   "test_reserve.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DEV_URL = "http://epgs-dev.test:8888"
PROD_URL = "http://epgs-prod.test:9984"
_ADDRS = {"epgs-dev.test": "192.0.2.20", "epgs-prod.test": "192.0.2.18", "prod-alias.test": "192.0.2.18",
          "localhost": "127.0.0.1", "example": "192.0.2.99"}


@pytest.fixture
def no_dns(monkeypatch):
    """DNS for the example names only (no real lookup in tests)."""
    import socket

    def getaddrinfo(host, port, *a, **kw):
        if host not in _ADDRS and not host.replace(".", "").isdigit():
            raise OSError("unknown host")
        return [(2, 1, 6, "", (_ADDRS.get(host, host), 0))]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("base", [PROD_URL, "http://epgs-prod.test:8888", "http://example:8888",
                                  "http://epgs-dev.test:8889", "http://127.0.0.1:1"])
def test_dev_tool_refuses_other_servers(base, monkeypatch, no_dns):
    monkeypatch.setenv("EPGS_DEV_URL", DEV_URL)
    monkeypatch.setenv("EPGS_PROD_URL", PROD_URL)
    tool = _test_reserve_tool()
    with pytest.raises(SystemExit):
        tool.check_base(base)
    with pytest.raises(SystemExit):
        tool.Dev(base)


def test_dev_tool_refuses_everything_without_a_dev_url(monkeypatch, no_dns):
    """019: EPGS_DEV_URL unset (no local.env) -> even localhost is refused; no request is sent."""
    tool = _test_reserve_tool()
    monkeypatch.setattr(tool.urllib.request, "urlopen", lambda *a, **k: pytest.fail("request sent"))
    for base in (DEV_URL, "http://localhost:8888", ""):
        with pytest.raises(SystemExit):
            tool.Dev(base)
    monkeypatch.setenv("EPGS_PROD_URL", PROD_URL)
    monkeypatch.setenv("EPGS_DEV_URL", "http://epgs-prod.test:8888")  # misconfigured: the production host
    with pytest.raises(SystemExit):
        tool.check_base("http://epgs-prod.test:8888")
    with pytest.raises(SystemExit):
        tool.vm_ssh()  # KODI_HOST unset


def test_dev_tool_refuses_without_prod_url_and_by_dns(monkeypatch, no_dns):
    """019 L2: EPGS_PROD_URL must be set too; a name that resolves to the production address is refused."""
    tool = _test_reserve_tool()
    monkeypatch.setenv("EPGS_DEV_URL", DEV_URL)
    with pytest.raises(SystemExit):
        tool.check_base(DEV_URL)
    monkeypatch.setenv("EPGS_PROD_URL", PROD_URL)
    assert tool.check_base(DEV_URL) == DEV_URL
    monkeypatch.setenv("EPGS_DEV_URL", "http://prod-alias.test:8888")
    with pytest.raises(SystemExit):
        tool.check_base("http://prod-alias.test:8888")


def test_dev_tool_accepts_dev_and_counts_concurrency(monkeypatch, no_dns):
    monkeypatch.setenv("EPGS_DEV_URL", DEV_URL)
    monkeypatch.setenv("EPGS_PROD_URL", PROD_URL)
    monkeypatch.setenv("KODI_HOST", "epgs-dev.test")
    monkeypatch.setenv("KODI_SSH_USER", "kodi")
    tool = _test_reserve_tool()
    assert tool.check_base(DEV_URL + "/") == DEV_URL
    monkeypatch.setattr(tool, "_on_vm", lambda: True)  # on the VM itself
    assert tool.check_base("http://localhost:8888") == "http://localhost:8888"
    monkeypatch.setattr(tool, "_on_vm", lambda: False)  # 011 review: elsewhere localhost may tunnel to production
    with pytest.raises(SystemExit):
        tool.check_base("http://localhost:8888")
    assert tool.vm_ssh() == "kodi@epgs-dev.test"
    rs = [{"startAt": 0, "endAt": 10}, {"startAt": 5, "endAt": 15}, {"startAt": 0, "endAt": 20, "isSkip": True}]
    assert tool.others_at(rs, 0, 20) == 2
    assert tool.others_at(rs, 10, 20) == 1
    assert tool.others_at([], 0, 10) == 0


def test_on_vm_only_on_kodi_host(monkeypatch):
    """011 review nit 6: _on_vm is True only when this machine owns KODI_HOST's address."""
    tool = _test_reserve_tool()
    monkeypatch.delenv("KODI_HOST", raising=False)
    assert not tool._on_vm()
    monkeypatch.setenv("KODI_HOST", "127.0.0.1")  # this machine
    assert tool._on_vm()
    monkeypatch.setenv("KODI_HOST", "192.0.2.1")  # TEST-NET: never this machine
    assert not tool._on_vm()


class _FakeDev:
    """test_reserve.Dev stand-in: GET answers from dicts, every call recorded, 404 as urllib does."""

    def __init__(self, reserves=None, recorded=None):
        self.reserves, self.recorded, self.sent = reserves or {}, recorded or {}, []

    def call(self, method, path, body=None, **params):
        import urllib.error

        self.sent.append((method, path))
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/recorded":
            return {"records": [r for r in self.recorded.values() if r["channelId"] == params.get("channelId")]}
        table = self.reserves if parts[0] == "reserves" else self.recorded
        if method == "GET":
            if int(parts[1]) not in table:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return table[int(parts[1])]
        table.pop(int(parts[1]), None)
        return None


MINE = {"id": 5, "channelId": 3211240960, "programId": None, "startAt": 1000 * MIN, "endAt": 1003 * MIN}


def _tool_with_state(tmp_path, monkeypatch, entries=(MINE,)):
    tool = _test_reserve_tool()
    monkeypatch.setattr(tool, "STATE", str(tmp_path / "state.json"))
    tool.save_state({"reserves": [dict(e) for e in entries]})
    return tool


def _reserve_of(mine, **kw):
    r = {k: mine[k] for k in ("id", "channelId", "startAt", "endAt")}
    r.update(name="KODIstation test", **kw)
    return r


def _rec(mine, rid=70, **kw):
    rec = {"id": rid, "channelId": mine["channelId"], "startAt": mine["startAt"], "endAt": mine["endAt"],
           "isRecording": False, "isProtected": False}
    rec.update(kw)
    return {k: v for k, v in rec.items() if v is not None}


def test_dev_tool_delete_reserve_only_its_own(tmp_path, monkeypatch):
    tool = _tool_with_state(tmp_path, monkeypatch)
    dev = _FakeDev({5: _reserve_of(MINE), 6: _reserve_of(MINE, id=6)})
    with pytest.raises(SystemExit):
        tool.delete_reserve(dev, 6)  # not created by the tool
    assert not [c for c in dev.sent if c[0] == "DELETE"]
    dev.reserves[5] = _reserve_of(MINE, ruleId=1)  # the id now is a rule reservation
    with pytest.raises(SystemExit):
        tool.delete_reserve(dev, 5)
    dev.reserves[5] = _reserve_of(MINE, startAt=MINE["startAt"] + MIN)  # another time
    with pytest.raises(SystemExit):
        tool.delete_reserve(dev, 5)
    assert not [c for c in dev.sent if c[0] == "DELETE"]
    dev.reserves[5] = _reserve_of(MINE)
    tool.delete_reserve(dev, 5)
    assert [c for c in dev.sent if c[0] == "DELETE"] == [("DELETE", "/reserves/5")]
    assert tool.load_state()["reserves"] == []  # gone without a recording: pruned


def test_dev_tool_delete_recorded_refuses_a_rule_recording_at_the_same_minute(tmp_path, monkeypatch):
    tool = _tool_with_state(tmp_path, monkeypatch)
    rule_rec = _rec(MINE, 71, ruleId=4, programId=321124096000282)  # NHK総合 rule, same channel and minute
    other_end = _rec(MINE, 72, endAt=MINE["endAt"] + 27 * MIN)
    with_program = _rec(MINE, 73, programId=321124096000282)
    dev = _FakeDev({}, {71: rule_rec, 72: other_end, 73: with_program, 74: _rec(MINE, 74, isProtected=True),
                        75: _rec(MINE, 75, isRecording=True), 70: _rec(MINE)})
    for rid in (71, 72, 73, 74, 75):
        with pytest.raises(SystemExit):
            tool.delete_recorded(dev, rid)
    assert not [c for c in dev.sent if c[0] == "DELETE"]
    tool.delete_recorded(dev, 70)
    assert [c for c in dev.sent if c[0] == "DELETE"] == [("DELETE", "/recorded/70")]


def test_dev_tool_program_reservation_needs_the_same_program(tmp_path, monkeypatch):
    mine = dict(MINE, programId=327210205631831)
    tool = _tool_with_state(tmp_path, monkeypatch, [mine])
    dev = _FakeDev({}, {80: _rec(mine, 80), 81: _rec(mine, 81, programId=327210205631831)})
    with pytest.raises(SystemExit):
        tool.delete_recorded(dev, 80)  # no programId: a time-specified recording, not ours
    tool.delete_recorded(dev, 81)
    assert ("DELETE", "/recorded/81") in dev.sent


def test_dev_tool_prunes_stale_ids_but_keeps_ones_with_a_recording(tmp_path, monkeypatch):
    stale = dict(MINE, id=517, programId=327210205631831)
    stopped = dict(MINE, id=6, startAt=2000 * MIN, endAt=2003 * MIN)  # stopped: reservation gone, recording left
    tool = _tool_with_state(tmp_path, monkeypatch, [stale, stopped, dict(MINE, id=7)])
    dev = _FakeDev({7: _reserve_of(MINE, id=7)}, {90: _rec(stopped, 90)})
    tool.prune(dev)
    assert [x["id"] for x in tool.load_state()["reserves"]] == [6, 7]
    assert all(c[0] == "GET" for c in dev.sent)
    tool.delete_recorded(dev, 90)
    assert [x["id"] for x in tool.load_state()["reserves"]] == [7]


# --- backlog 014: record from the guide --------------------------------------------------------------------------

JA14 = dict(JA, record="録画", q_record="この番組を録画予約しますか?",
            q_record_airing="放送中: 今から途中から録画します",
            encode="エンコード: {0}", done_record="録画予約しました", done_record_now="録画を開始しました",
            already="すでに予約されています", add_conflict="チューナーが足りないため予約できません (競合)",
            ended="放送は終了しました", by_rule="録画予約: ルール", by_manual="録画予約: 手動",
            program_gone="番組表にない番組です")


def lists(normal=(), conflicts=(), skips=(), overlaps=()):
    """A /reserves/lists answer (arrays of {reserveId, programId?, ruleId?}, like the real server)."""
    return {"normal": list(normal), "conflicts": list(conflicts), "skips": list(skips), "overlaps": list(overlaps)}


def entry(rid, pid=None, rule=None):
    return {k: v for k, v in (("reserveId", rid), ("programId", pid), ("ruleId", rule)) if v is not None}


def gprog(pid=100, start=None, minutes=30, channel=3211240960, **kw):
    """A guide program derived from a captured live one (texts real), with the given id/time."""
    start = local(2026, 10, 12, 21, 0) if start is None else start
    with open(os.path.join(FIXTURES, "live", "schedules.json"), encoding="utf-8") as f:
        base = dict(json.load(f)[0]["programs"][0])
    base.update(id=pid, startAt=start, endAt=start + minutes * MIN, channelId=channel, **kw)
    return base


def test_lists_index_precedence_and_ignores_items_without_program():
    idx = reserves.lists_index(lists(normal=[entry(1, 10, 3), entry(2, 11), entry(9)],
                                     conflicts=[entry(3, 12, 3)], skips=[entry(4, 13, 3), entry(5, 10, 4)],
                                     overlaps=[entry(6, 14, 3), entry(7, 11, 5)]))
    assert set(idx) == {10, 11, 12, 13, 14}
    assert idx[10]["id"] == 1 and idx[10]["ruleId"] == 3 and reserves.classify(idx[10]) == "normal"  # normal > skip
    assert idx[11]["id"] == 2 and "ruleId" not in idx[11]  # ruleId absent = manual
    assert reserves.classify(idx[12]) == "conflict" and idx[12]["isConflict"] is True
    assert reserves.classify(idx[13]) == "skip" and reserves.classify(idx[14]) == "overlap"
    # several for one programId in the same state: manual before rule
    both = reserves.lists_index(lists(normal=[entry(1, 10, 3), entry(2, 10)]))
    assert both[10]["id"] == 2
    assert reserves.lists_index(lists(conflicts=[entry(1, 10, 3)], normal=[entry(2, 10, 3)]))[10]["id"] == 2
    assert reserves.lists_index({}) == {} and reserves.lists_index(None) == {}


def test_program_reserve_and_action_for_each_state():
    now = local(2026, 10, 12, 20, 0)
    p = gprog(start=local(2026, 10, 12, 21, 0))
    idx = reserves.lists_index(lists(normal=[entry(1, 100, 3)]))
    r = reserves.program_reserve(idx[100], p)
    assert (r["channelId"], r["startAt"], r["endAt"], r["programId"]) == (3211240960, p["startAt"], p["endAt"], 100)
    assert reserves.action_for(r, now) == "cancel" and reserves.entry_label("cancel", r, JA14) == "この回を除外"
    manual = reserves.program_reserve(reserves.lists_index(lists(normal=[entry(2, 100)]))[100], p)
    assert reserves.entry_label(reserves.action_for(manual, now), manual, JA14) == "予約を削除"
    skip = reserves.program_reserve(reserves.lists_index(lists(skips=[entry(3, 100, 3)]))[100], p)
    assert reserves.action_for(skip, now) == "unskip"
    over = reserves.program_reserve(reserves.lists_index(lists(overlaps=[entry(4, 100, 3)]))[100], p)
    assert reserves.action_for(over, now) == "unoverlap"
    airing = dict(r, startAt=now - MIN)
    assert reserves.action_for(airing, now, [rec_of(airing)]) == "stop"


def test_marker_per_state():
    p = gprog(start=local(2026, 10, 12, 21, 0))
    r = reserves.program_reserve(reserves.lists_index(lists(normal=[entry(1, 100, 3)]))[100], p)
    assert reserves.RESERVED == "[COLOR red]○[/COLOR] "
    assert reserves.marker(r, [], JA14) == reserves.RESERVED  # on air but not in /recording: ○
    assert reserves.marker(r, [rec_of(r)], JA14) == "[COLOR red]●[/COLOR] "
    for state, text in (("conflict", "[COLOR red][競合][/COLOR] "), ("skip", "[COLOR grey][除外][/COLOR] "),
                        ("overlap", "[COLOR grey][重複][/COLOR] ")):
        assert reserves.marker(dict(r, **{"is" + state.capitalize(): True}), [rec_of(r)], JA14) == text
    assert reserves.marker(dict(r, isOverlap=True), [], {}) == "[COLOR grey][Duplicates][/COLOR] "


def test_reserve_line_ja_en():
    p = gprog()
    rule = reserves.program_reserve(reserves.lists_index(lists(normal=[entry(1, 100, 3)]))[100], p)
    manual = reserves.program_reserve(reserves.lists_index(lists(normal=[entry(1, 100)]))[100], p)
    assert reserves.reserve_line(rule, JA14) == "録画予約: ルール"
    assert reserves.reserve_line(manual, JA14) == "録画予約: 手動"
    assert reserves.reserve_line(rule, {}) == "Scheduled: rule"
    assert reserves.reserve_line(manual, {}) == "Scheduled: manual"
    assert reserves.reserve_line(dict(rule, isOverlap=True), JA14) == "録画予約: ルール\n録画済みのため録画しません"
    assert reserves.reserve_line(dict(rule, isSkip=True), {}) == "Scheduled: rule\nSkipped: will not record"


def test_needs_records_and_lists_window():
    now = local(2026, 10, 12, 21, 10)
    on_air, fut = gprog(100, local(2026, 10, 12, 21, 0)), gprog(101, local(2026, 10, 12, 22, 0))
    normal = reserves.lists_index(lists(normal=[entry(1, 100, 3)]))
    assert reserves.needs_records(normal, [on_air, fut], now) is True
    assert reserves.needs_records(normal, [fut], now) is False
    assert reserves.needs_records(reserves.lists_index(lists(skips=[entry(1, 100, 3)])), [on_air], now) is False
    assert reserves.needs_records(reserves.lists_index(lists(normal=[entry(1, 101, 3)])), [on_air, fut], now) is False
    assert reserves.needs_records({}, [on_air], now) is False
    assert reserves.needs_records(None, [on_air], now) is False
    assert reserves.lists_window([fut, on_air]) == (on_air["startAt"], fut["endAt"])
    assert reserves.lists_window([]) is None


def test_can_record():
    now = local(2026, 10, 12, 21, 30)
    assert reserves.can_record(gprog(start=local(2026, 10, 12, 22, 0)), now)
    assert reserves.can_record(gprog(start=local(2026, 10, 12, 21, 10)), now)  # airing
    assert not reserves.can_record(gprog(start=local(2026, 10, 12, 21, 0)), now)  # endAt == now
    assert not reserves.can_record(gprog(start=local(2026, 10, 12, 20, 0)), now)


def test_record_context():
    label, url = reserves.record_context(gprog(100), "NHK総合1・大阪", JA14)
    assert label == "録画"
    assert url.startswith("plugin://plugin.video.epgstation/?action=record&")
    q = dict(__import__("urllib.parse").parse.parse_qsl(url.split("?", 1)[1]))
    assert q == {"action": "record", "programId": "100", "channelId": "3211240960", "name": "NHK総合1・大阪"}
    assert reserves.record_context(gprog(100), "x", {})[0] == "Record"


def test_guide_mark_entry_per_case():
    now = local(2026, 10, 12, 20, 0)
    p = gprog(100, local(2026, 10, 12, 21, 0))
    assert reserves.guide_mark(p, "NHK", None, (), now, JA14) == ("", "", None)  # lists failed: nothing
    marker, line, ctx = reserves.guide_mark(p, "NHK", {}, (), now, JA14)
    assert (marker, line) == ("", "") and ctx[0] == "録画" and "action=record" in ctx[1]
    marker, line, ctx = reserves.guide_mark(p, "NHK", reserves.lists_index(lists(normal=[entry(7, 100, 3)])), (),
                                            now, JA14)
    assert marker == reserves.RESERVED and line == "録画予約: ルール"
    assert ctx == ("この回を除外", "plugin://plugin.video.epgstation/?action=reserve_action&op=cancel&reserveId=7")
    ended = gprog(100, local(2026, 10, 12, 19, 0))
    assert reserves.guide_mark(ended, "NHK", {}, (), now, JA14)[2] is None  # stale list: no 録画 on an ended one


def test_record_message():
    now = local(2026, 10, 12, 20, 0)
    p = gprog(100, local(2026, 10, 12, 21, 0), name="ＮＨＫニュース７")
    lines = reserves.record_message(p, "NHK総合1", now, None, JA14).split("\n")
    assert lines == ["NHKニュース7", "10/12(月) 21:00-21:30 NHK総合1", "この番組を録画予約しますか?"]
    lines = reserves.record_message(p, "NHK総合1", now, "H.264", JA14).split("\n")
    assert lines[-1] == "エンコード: H.264" and len(lines) == 4
    airing = reserves.record_message(p, "NHK総合1", p["startAt"] + MIN, None, JA14).split("\n")
    assert airing[-1] == "放送中: 今から途中から録画します" and airing[2] == "この番組を録画予約しますか?"
    both = reserves.record_message(p, "x", p["startAt"], "H.264", {}).split("\n")
    assert both[-2:] == ["On air: recording starts now, partway in", "Encode: H.264"]
    assert both[1] == "Mon 10/12 21:00-21:30 x"


def test_record_done():
    p = gprog(100, local(2026, 10, 12, 21, 0))
    assert reserves.record_done(p, p["startAt"] - 1, JA14) == "録画予約しました"
    assert reserves.record_done(p, p["startAt"], JA14) == "録画を開始しました"
    assert reserves.record_done(p, p["startAt"] - 1, {}) == "Recording scheduled"


def test_add_error():
    assert reserves.add_error(500, "ReservationManageModelAddReserveConflict", JA14) == JA14["add_conflict"]
    assert reserves.add_error(500, "ReservationManageModelCheckReservedProgramError", JA14) == "すでに予約されています"
    assert reserves.add_error(500, "ProgramIsNotFound", JA14) == "番組表にない番組です"
    assert reserves.add_error(None, None, JA14) == "失敗しました (通信エラー)"
    assert reserves.add_error(500, "X", JA14) == "失敗しました: X"
    assert reserves.add_error(400, None, {}) == "Failed: HTTP 400"
    assert reserves.add_error(500, "ProgramIsNotFound", {}) == "Program no longer in the guide"


# --- tools/dev/test_reserve.py pick / snapshot / adopt (014) -----------------------------------------------------

GR_CH = {"id": 3211240960, "name": "NHK総合1", "channelType": "GR", "type": 1}
BS_CH = {"id": 400101, "name": "BS", "channelType": "BS", "type": 1}


class _GuideDev(_FakeDev):
    """GETs of /channels, /schedules, /schedules/detail/{id}, /reserves (type=all) and /reserves/{id}; no writes."""

    def __init__(self, programs, reserves=None, channels=(GR_CH, BS_CH)):
        super().__init__({r["id"]: r for r in reserves or []})
        self.programs, self.channels = programs, list(channels)

    def call(self, method, path, body=None, **params):
        import urllib.error

        self.sent.append((method, path))
        if path == "/channels":
            return self.channels
        if path == "/reserves":
            return {"reserves": list(self.reserves.values()), "total": len(self.reserves)}
        if path == "/schedules":
            out = []
            for ch in self.channels:
                progs = [p for p in self.programs if p["channelId"] == ch["id"] and p["endAt"] > params["startAt"]
                         and p["startAt"] <= params["endAt"]]
                out.append({"channel": ch, "programs": progs})
            return out
        if path.startswith("/schedules/detail/"):
            p = next((p for p in self.programs if str(p["id"]) == path.rsplit("/", 1)[1]), None)
            if p is None:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return p
        return super().call(method, path, body, **params)


def _gp(pid, start_min, minutes, channel=GR_CH["id"], free=True):
    return {"id": pid, "channelId": channel, "startAt": start_min * MIN, "endAt": (start_min + minutes) * MIN,
            "isFree": free, "name": f"P{pid}"}


def test_dev_tool_pick_future_and_airing():
    tool = _test_reserve_tool()
    now = 1000 * MIN
    progs = [_gp(1, 1030, 5), _gp(2, 1070, 15), _gp(3, 1080, 5, free=False), _gp(4, 1090, 5, BS_CH["id"]),
             _gp(5, 1100, 5), _gp(6, 995, 10), _gp(7, 999, 3), _gp(8, 1065, 5)]
    dev = _GuideDev(progs, [{"id": 1, "programId": 8, "startAt": 1065 * MIN, "endAt": 1070 * MIN}])
    p, ch = tool.pick(dev, now)
    assert p["id"] == 5 and ch["id"] == GR_CH["id"]  # >= 60 min ahead, <= 10 min, free GR, not reserved
    p, _ = tool.pick(dev, now, airing=True)
    assert p["id"] == 6  # on air with >= 3 min left (7 has 2 min left)
    busy = [{"id": 10 + i, "startAt": 1100 * MIN, "endAt": 1105 * MIN} for i in range(7)]
    with pytest.raises(SystemExit):
        tool.pick(_GuideDev([_gp(5, 1100, 5)], busy), now)  # 7 others at that time
    assert all(c[0] == "GET" for c in dev.sent)


def _kodi_line(ms, pid, rid, name="P"):
    """kodi.log line of plugin.do_record's success log (exact format)."""
    lt = time.localtime(ms / 1000)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", lt) + f".{int(ms % 1000):03d}"
    return f"{stamp} T:1234    info <general>: [epgstation] reserve: add program {pid} ({name}) done: reserve {rid}"


def _adopt_setup(tmp_path, monkeypatch, log_lines=None, snap_age_min=5, now=None):
    tool = _tool_with_state(tmp_path, monkeypatch, [])
    now = time.time() * 1000 if now is None else now
    progs = [_gp(5, 1100, 5), _gp(6, 1100, 30), _gp(7, 1100, 5, free=False), _gp(8, 1100, 5, BS_CH["id"])]
    old = {"id": 40, "programId": 9, "ruleId": 2, "channelId": GR_CH["id"], "startAt": 1100 * MIN,
           "endAt": 1105 * MIN}
    dev = _GuideDev(progs, [old])
    tool.snapshot(dev)
    state = tool.load_state()
    state["snapshotAt"] = int(now - snap_age_min * MIN)
    tool.save_state(state)
    lines = log_lines if log_lines is not None else [_kodi_line(now - MIN, 5, 41)]
    monkeypatch.setattr(tool, "read_kodi_log", lambda dev: "\n".join(lines) + "\n")
    mine = {"id": 41, "programId": 5, "channelId": GR_CH["id"], "startAt": 1100 * MIN, "endAt": 1105 * MIN}
    return tool, dev, progs, mine, now


def test_dev_tool_snapshot_and_adopt(tmp_path, monkeypatch):
    tool, dev, progs, mine, now = _adopt_setup(tmp_path, monkeypatch)
    assert tool.load_state()["snapshot"] == [40]
    with pytest.raises(SystemExit):
        tool.adopt(dev, 40, 9)  # in the snapshot (and a rule reservation): not ours
    for bad, pid in ((dict(mine, ruleId=3), 5), (dict(mine, programId=6), 5), (mine, 6)):
        dev.reserves[41] = bad
        with pytest.raises(SystemExit):
            tool.adopt(dev, 41, pid)
    for pid in (6, 7, 8):  # too long, not free, not GR
        p = next(x for x in progs if x["id"] == pid)
        dev.reserves[41] = dict(mine, programId=pid, channelId=p["channelId"], startAt=p["startAt"], endAt=p["endAt"])
        with pytest.raises(SystemExit):
            tool.adopt(dev, 41, pid)
    with pytest.raises(SystemExit):
        tool.adopt(dev, 99, 5)  # no such reservation
    assert tool.load_state()["reserves"] == []
    dev.reserves[41] = mine
    tool.adopt(dev, 41, 5)
    state = tool.load_state()
    assert [(x["id"], x["programId"], x["channelId"], x["startAt"], x["endAt"]) for x in state["reserves"]] == \
        [(41, 5, GR_CH["id"], 1100 * MIN, 1105 * MIN)]
    assert "snapshot" not in state and "snapshotAt" not in state  # M2: cleared after a successful adopt
    assert all(c[0] == "GET" for c in dev.sent)
    tool.delete_reserve(dev, 41)  # now the existing delete accepts it
    assert ("DELETE", "/reserves/41") in dev.sent


def test_dev_tool_adopt_refuses_a_stale_snapshot(tmp_path, monkeypatch):
    tool, dev, _, mine, _ = _adopt_setup(tmp_path, monkeypatch, snap_age_min=121)
    dev.reserves[41] = mine
    with pytest.raises(SystemExit):
        tool.adopt(dev, 41, 5)
    assert tool.load_state()["reserves"] == []


def test_dev_tool_adopt_refuses_ids_not_above_the_snapshot(tmp_path, monkeypatch):
    tool, dev, _, mine, now = _adopt_setup(tmp_path, monkeypatch, [_kodi_line(time.time() * 1000 - MIN, 5, 39)])
    dev.reserves[39] = dict(mine, id=39)  # an older id that was not in the snapshot (deleted and re-listed?)
    with pytest.raises(SystemExit):
        tool.adopt(dev, 39, 5)
    assert tool.load_state()["reserves"] == []


@pytest.mark.parametrize("lines", [
    [],  # Kodi never logged it
    ["{old}"],  # logged before the snapshot
    ["{other_program}"], ["{other_reserve}"],
    ["{no_done}"],  # the attempt, not the success
])
def test_dev_tool_adopt_needs_kodis_log_line_after_the_snapshot(tmp_path, monkeypatch, lines):
    now = time.time() * 1000
    stamp = _kodi_line(now - MIN, 5, 41).split(" T:", 1)[0]
    lines = [x.format(old=_kodi_line(now - 10 * MIN, 5, 41), other_program=_kodi_line(now - MIN, 6, 41),
                      other_reserve=_kodi_line(now - MIN, 5, 42),
                      no_done=f"{stamp} T:1 info <general>: [epgstation] reserve: add program 5 (P)") for x in lines]
    tool, dev, _, mine, _ = _adopt_setup(tmp_path, monkeypatch, lines, now=now)
    dev.reserves[41] = mine
    with pytest.raises(SystemExit):
        tool.adopt(dev, 41, 5)
    assert tool.load_state()["reserves"] == [] and tool.load_state()["snapshot"] == [40]


def test_dev_tool_adopt_without_snapshot_refuses(tmp_path, monkeypatch):
    tool = _tool_with_state(tmp_path, monkeypatch, [])
    dev = _GuideDev([_gp(5, 1100, 5)], [{"id": 41, "programId": 5, "channelId": GR_CH["id"],
                                          "startAt": 1100 * MIN, "endAt": 1105 * MIN}])
    monkeypatch.setattr(tool, "read_kodi_log", lambda dev: _kodi_line(time.time() * 1000, 5, 41))
    with pytest.raises(SystemExit):
        tool.adopt(dev, 41, 5)


def test_dev_tool_kodi_log_matches_do_record_format():
    """The pattern adopt looks for is the exact line plugin.do_record logs."""
    tool = _test_reserve_tool()
    plugin = _src("plugin.py")
    assert 'f"[epgstation] reserve: {what} done: reserve {reserve_id}"' in plugin
    assert 'what = f"add program {program_id} ({items.to_half(program[\'name\'])})"' in plugin
    found = tool.kodi_adds(_kodi_line(local(2026, 10, 12, 21, 0, ) + 5, 327220206442898, 520, "東京空色さんぽ (再)"))
    assert found == [(local(2026, 10, 12, 21, 0) + 5, 327220206442898, 520)]


def test_record_from_the_guide_removal_checklist_names_every_hook():
    """reserves.py's docstring names every file with a "record from the guide (014)" hook."""
    files = []
    for top in ("plugin.video.epgstation", "tools", "tests"):
        files += [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(ROOT, top)) for f in fs
                  if f.endswith((".py", ".xml", ".po")) and "fixtures" not in d and "__pycache__" not in d]
    me = os.path.abspath(reserves.__file__)
    hooks = {os.path.basename(f) for f in files
             if os.path.abspath(f) != me and "record from the guide (014)" in open(f, encoding="utf-8").read()}
    assert {"plugin.py", "guide.py", "api.py", "settings.xml", "fake_epgstation.py", "kodi_smoke.py",
            "test_reserve.py"} <= hooks
    missing = sorted(h for h in hooks if h not in reserves.__doc__)
    assert not missing, missing


def test_add_error_ipc_timeout_is_no_answer():
    """L1: EPGStation's IPC gives up after 5 s but still completes the add: like no answer."""
    assert reserves.add_error(500, "IPCTimeout", JA14) == "失敗しました (通信エラー)"
    assert reserves.add_error(500, "IPCTimeout", {}) == "Failed (connection error)"
    assert reserves.maybe_applied(None, None) and reserves.maybe_applied(500, "IPCTimeout")
    assert not reserves.maybe_applied(500, "ProgramIsNotFound") and not reserves.maybe_applied(503, None)


def test_urlopen_and_request_only_in_request_and_live_probe():
    """L3: every HTTP request of the addon goes through api._request, the GET-only live_probe or the HEAD-only
    stream_playlist_status (011: a static playlist file, no side effect)."""
    src = _src("api.py")
    assert _callers(src, r"\burlopen\(") == {"_request", "live_probe", "stream_playlist_status"}
    assert _callers(src, r"\bRequest\(") == {"_request", "live_probe", "stream_playlist_status"}
    assert src.count("urlopen(") == 3 and src.count("Request(") == 3
    probe = _api_methods(src)["live_probe"]
    assert 'method="GET"' in probe and "data=" not in probe
    head = _api_methods(src)["stream_playlist_status"]
    assert 'method="HEAD"' in head and "data=" not in head and "/streamfiles/" in head
    for name, other in _addon_sources().items():
        assert "urlopen" not in other and "Request(" not in other, name


# --- tools/dev/test_reserve.py demo-target / demo-audit / demo-restore (demo video (021)) ------------------------

DNOW = int(time.time() * 1000)
P = 327230207212427  # a free GR program of 4 min
CH_GR = {"id": 3272302072, "name": "ABCテレビ1", "channelType": "GR", "type": 1}
CH_PAID = {"id": 400191, "name": "WOWOW", "channelType": "BS", "serviceId": 191, "type": 1}


def _drec(rid, channel=3211240960, **kw):
    return dict({"id": rid, "channelId": channel, "name": f"R{rid}", "startAt": DNOW - 900 * MIN,
                 "endAt": DNOW - 870 * MIN, "ruleId": 4, "isRecording": False, "isProtected": False,
                 "isEncoding": False, "videoFiles": [{"id": rid * 10, "size": 1000}]}, **kw)


class _DemoDev:
    """GETs of /recorded(/id), /recording, /channels, /rules, /reserves(/id), /schedules/detail/id; PUT protect."""

    def __init__(self):
        self.recorded = {1: _drec(1), 2: _drec(2, isProtected=True), 69: _drec(69)}
        self.reserves = {40: {"id": 40, "endAt": DNOW + 60 * MIN}, 41: {"id": 41, "endAt": DNOW + 5 * MIN}}
        self.rules = [{"id": 1, "searchOption": {"keyword": "x"}}]
        self.programs = {P: {"id": P, "channelId": CH_GR["id"], "startAt": DNOW + 70 * MIN,
                             "endAt": DNOW + 74 * MIN, "isFree": True, "name": "P"}}
        self.sent = []

    def call(self, method, path, body=None, **params):
        import urllib.error

        self.sent.append((method, path))
        parts = path.strip("/").split("/")

        def get(table, key):
            if key not in table:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return table[key]

        if method == "PUT":
            self.recorded[int(parts[1])]["isProtected"] = parts[2] == "protect"
            return {"code": 200}
        if path == "/recorded":
            recs = [r for r in self.recorded.values() if "channelId" not in params
                    or r["channelId"] == params["channelId"]]
            return {"records": recs, "total": len(recs)}
        if path == "/recording":
            return {"records": [], "total": 0}
        if path == "/channels":
            return [{"id": 3211240960, "channelType": "GR", "name": "NHK"}, CH_GR, CH_PAID]
        if path == "/rules":
            return {"rules": json.loads(json.dumps(self.rules)), "total": len(self.rules)}
        if path == "/reserves":
            return {"reserves": list(self.reserves.values()), "total": len(self.reserves)}
        if parts[0] == "reserves":
            return get(self.reserves, int(parts[1]))
        if parts[0] == "schedules":
            return get(self.programs, int(parts[2]))
        return get(self.recorded, int(parts[1]))


def _dts(ms, iso=False):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S" if iso else "%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))
    return stamp + ".000"


def _dacc(ms, method, path, ua="KODIstation/1 (plugin.video.epgstation)"):
    return f'[{_dts(ms, True)}] [INFO] access - ::ffff:127.0.0.1 - - "{method} {path} HTTP/1.1" 200 12 "" "{ua}"'


def _dkodi(ms, text):
    return f"{_dts(ms)} T:1    info <general>: [epgstation] {text}"


def _demo_setup(tmp_path, monkeypatch):
    tool = _tool_with_state(tmp_path, monkeypatch, [])
    dev = _DemoDev()
    tool.recorded_snapshot(dev)
    state = tool.load_state()
    state["recordedSnapshot"]["at"] = DNOW - 20 * MIN
    tool.save_state(state)
    tool.demo_target(dev, 69)
    return tool, dev


def _demo_logs(tool, monkeypatch, access=None, kodi=None):
    t = DNOW - 10 * MIN
    access = access if access is not None else [
        _dacc(t, "POST", "/api/reserves"), _dacc(t + MIN, "DELETE", "/api/reserves/579"),
        _dacc(t + 2 * MIN, "PUT", "/api/recorded/69/protect"), _dacc(t + 3 * MIN, "PUT", "/api/recorded/69/unprotect")]
    access = list(access) + [_dacc(t, "GET", "/api/recorded?limit=1"),
                             _dacc(DNOW - 60 * MIN, "DELETE", "/api/recorded/1")]  # before the snapshot: ignored
    kodi = kodi if kodi is not None else [
        _dkodi(t, f"reserve: add program {P} (P (再)) done: reserve 579"),
        _dkodi(t, f"reserve: add program {P} (P (再)) done: reserve 579"),  # Kodi writes it twice
        _dkodi(t + MIN, "reserve: cancel reserve 579 (P)"), _dkodi(t + MIN, "reserve: cancel reserve 579 (P) done"),
        _dkodi(t + 2 * MIN, "recorded: protect recorded 69 (R69) done"),
        _dkodi(t + 3 * MIN, "recorded: unprotect recorded 69 (R69) done")]
    monkeypatch.setattr(tool, "read_access_log", lambda dev, since_ms=0: "\n".join(access) + "\n")
    monkeypatch.setattr(tool, "read_kodi_log", lambda dev: "\n".join(kodi) + "\n")


def test_demo_target_stores_prior_state_rules_and_reserves(tmp_path, monkeypatch):
    tool, dev = _demo_setup(tmp_path, monkeypatch)
    demo = tool.load_state()["demo"]
    assert demo["rec"] == 69 and demo["prior"] == {"isProtected": False, "files": [690]}
    assert demo["rules"] == dev.rules and set(demo["reserves"]) == {"40", "41"}
    assert all(c[0] == "GET" for c in dev.sent)


@pytest.mark.parametrize("change", ["protected", "recording", "encoding", "paid", "missing", "no_snapshot"])
def test_demo_target_refusals(tmp_path, monkeypatch, change):
    tool = _tool_with_state(tmp_path, monkeypatch, [])
    dev = _DemoDev()
    dev.recorded[70] = _drec(70)
    if change != "no_snapshot":
        tool.recorded_snapshot(dev)
    rec = 70
    if change == "protected":
        dev.recorded[70]["isProtected"] = True
    elif change == "recording":
        dev.recorded[70]["isRecording"] = True
    elif change == "encoding":
        dev.recorded[70]["isEncoding"] = True
    elif change == "paid":
        dev.recorded[70]["channelId"] = CH_PAID["id"]
    elif change == "missing":
        rec = 71
        dev.recorded[71] = _drec(71)  # not in the snapshot
    with pytest.raises(SystemExit):
        tool.demo_target(dev, rec)
    assert "demo" not in tool.load_state()


def test_demo_audit_passes_for_exactly_the_allowed_writes(tmp_path, monkeypatch, capsys):
    tool, dev = _demo_setup(tmp_path, monkeypatch)
    _demo_logs(tool, monkeypatch)
    del dev.reserves[41]  # a reservation may only be gone after its end: this one has ended
    state = tool.load_state()
    state["demo"]["reserves"]["41"] = DNOW - MIN
    tool.save_state(state)
    assert tool.demo_audit(dev) is True
    assert "PASS" in capsys.readouterr().out
    assert all(c[0] == "GET" for c in dev.sent)


@pytest.mark.parametrize("change", ["second_post", "delete_recorded", "protect_other", "left_protected", "rule",
                                    "other_recording", "rid_exists", "too_soon", "reserve_gone", "no_unprotect",
                                    "second_add", "kodi_delete", "other_delete", "no_evidence"])
def test_demo_audit_fails(tmp_path, monkeypatch, capsys, change):
    tool, dev = _demo_setup(tmp_path, monkeypatch)
    t = DNOW - 10 * MIN
    base = [_dacc(t, "POST", "/api/reserves"), _dacc(t + MIN, "DELETE", "/api/reserves/579"),
            _dacc(t + 2 * MIN, "PUT", "/api/recorded/69/protect"),
            _dacc(t + 3 * MIN, "PUT", "/api/recorded/69/unprotect")]
    access, kodi = list(base), None
    if change == "second_post":
        access.append(_dacc(t + 4 * MIN, "POST", "/api/reserves"))
    elif change == "delete_recorded":
        access.append(_dacc(t + 4 * MIN, "DELETE", "/api/recorded/1"))
    elif change == "protect_other":
        access.append(_dacc(t + 4 * MIN, "PUT", "/api/recorded/1/protect"))
    elif change == "left_protected":
        dev.recorded[69]["isProtected"] = True
    elif change == "rule":
        dev.rules[0]["searchOption"]["keyword"] = "y"
    elif change == "other_recording":
        dev.recorded[1]["videoFiles"] = []
    elif change == "rid_exists":
        dev.reserves[579] = {"id": 579, "endAt": DNOW + 74 * MIN}
    elif change == "too_soon":
        dev.programs[P]["startAt"] = t + 30 * MIN
        dev.programs[P]["endAt"] = t + 34 * MIN
    elif change == "reserve_gone":
        del dev.reserves[40]
    elif change == "no_unprotect":
        access = base[:3]
    elif change == "second_add":
        kodi = [_dkodi(t, f"reserve: add program {P} (P) done: reserve 579"),
                _dkodi(t, f"reserve: add program {P} (P) done: reserve 580")]
    elif change == "kodi_delete":
        kodi = [_dkodi(t, f"reserve: add program {P} (P) done: reserve 579"),
                _dkodi(t + 4 * MIN, "recorded: delete recorded 69 (R69) done")]
    elif change == "other_delete":
        access[1] = _dacc(t + MIN, "DELETE", "/api/reserves/40")
    _demo_logs(tool, monkeypatch, access, kodi)
    if change == "no_evidence":
        monkeypatch.setattr(tool, "read_kodi_log", lambda dev: None)
    assert tool.demo_audit(dev) is False
    assert "FAIL" in capsys.readouterr().out


def test_demo_audit_needs_demo_target(tmp_path, monkeypatch):
    tool = _tool_with_state(tmp_path, monkeypatch, [])
    dev = _DemoDev()
    tool.recorded_snapshot(dev)
    with pytest.raises(SystemExit):
        tool.demo_audit(dev)


def test_demo_restore_only_unprotects_the_stored_target(tmp_path, monkeypatch):
    tool = _tool_with_state(tmp_path, monkeypatch, [])
    dev = _DemoDev()
    with pytest.raises(SystemExit):
        tool.demo_restore(dev)  # no target stored
    tool.recorded_snapshot(dev)
    tool.demo_target(dev, 69)
    tool.demo_restore(dev)  # not protected: nothing sent
    assert not [c for c in dev.sent if c[0] != "GET"]
    dev.recorded[69]["isProtected"] = True
    dev.recorded[1]["isProtected"] = True
    tool.demo_restore(dev)
    assert [c for c in dev.sent if c[0] != "GET"] == [("PUT", "/recorded/69/unprotect")]
    assert dev.recorded[1]["isProtected"] is True  # never another recording
    dev.recorded[69].update(isProtected=True, videoFiles=[{"id": 999, "size": 1}])  # not the same recording
    with pytest.raises(SystemExit):
        tool.demo_restore(dev)
