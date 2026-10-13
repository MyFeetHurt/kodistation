"""Backlog 013: program guide logic (resources/lib/epgstation/guide.py), against the shared live fixtures."""
import json
import os
import re
import time

import pytest
from conftest import FIXTURES, ROOT
from resources.lib.epgstation import api as api_mod
from resources.lib.epgstation import epgmeta, guide, items, live, reserves
from resources.lib.epgstation.api import ApiError
from resources.lib.epgstation.genres import genre_name

LIVE = os.path.join(FIXTURES, "live")
MIN = 60 * 1000
HOUR = 60 * MIN
JA = {"whats_on": "放送中", "by_genre": "ジャンル別", "schedule": "番組表", "now": "現在", "today": "今日",
      "tomorrow": "明日", "weekdays": "日,月,火,水,木,金,土", "channel_schedule": "{0} 番組表", "left": "残り{0}分",
      "gone": "番組表にない番組です"}
EN = {"whats_on": "What's on", "by_genre": "By genre", "schedule": "Schedule", "now": "Now", "today": "Today",
      "tomorrow": "Tomorrow", "weekdays": "Sun,Mon,Tue,Wed,Thu,Fri,Sat", "channel_schedule": "{0} schedule",
      "left": "{0} min left", "gone": "Program no longer in the guide"}


def _load(name):
    with open(os.path.join(LIVE, name), encoding="utf-8") as f:
        return json.load(f)


def local(y, mo, d, h=0, mi=0, s=0):
    """Local time -> ms (tests are independent of the time zone)."""
    return int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)) * 1000)


def api(username=""):
    return api_mod.EPGStation("http://epgs:8888", username, "pw")


def chan(cid=1, name="ＣＨ１", ctype="GR", stype=1, logo=True):
    return {"id": cid, "name": name, "channelType": ctype, "type": stype, "hasLogoData": logo}


def prog(pid, start, end, name="P", cid=1, **kw):
    return dict({"id": pid, "channelId": cid, "startAt": start, "endAt": end, "isFree": True, "name": name}, **kw)


@pytest.fixture
def fx():
    """schedules.json with guide.json merged, as the fake server serves them (at now = capturedAt)."""
    import fake_epgstation

    return {"schedules": fake_epgstation.merged_live("schedules"), "now": _load("live_meta.json")["capturedAt"]}


# --- fixtures --------------------------------------------------------------------------------------------------

def test_guide_fixture_budget_and_shape():
    assert os.path.getsize(os.path.join(LIVE, "guide.json")) <= 100 * 1024
    assert sum(os.path.getsize(os.path.join(LIVE, f)) for f in os.listdir(LIVE)) <= 330 * 1024
    g = _load("guide.json")
    assert all(set(s) == {"channel", "programs"} for s in g)
    nhk = [s for s in g if s["channel"]["id"] == 3211240960]
    assert nhk and len(nhk[0]["programs"]) > 20  # 8 days x up to 6 programs
    days = {items.fmt_time(p["startAt"], "%Y-%m-%d") for p in nhk[0]["programs"]}
    assert len(days) >= 7
    assert any(p.get("extended") for s in g for p in s["programs"])  # epgmeta's cast path stays covered
    for name in ("dev", "dev2"):  # one shared copy
        assert not os.path.exists(os.path.join(FIXTURES, name, "guide.json"))


# --- days and time slots ---------------------------------------------------------------------------------------

def test_local_midnight_and_day_label():
    now = local(2026, 10, 11, 15, 28)
    assert guide.local_midnight(now) == local(2026, 10, 11)
    assert guide.local_midnight(local(2026, 10, 11, 23, 59, 59)) == local(2026, 10, 11)
    assert guide.local_midnight(local(2026, 10, 12)) == local(2026, 10, 12)
    assert guide.day_label(local(2026, 10, 11), now, JA) == "今日 10/11(日)"
    assert guide.day_label(local(2026, 10, 12), now, JA) == "明日 10/12(月)"
    assert guide.day_label(local(2026, 10, 13), now, JA) == "10/13(火)"
    assert guide.day_label(local(2026, 10, 13), now, EN) == "Tue 10/13"
    assert guide.day_label(local(2026, 10, 11), now, EN) == "Today Sun 10/11"
    assert guide.day_label(local(2026, 10, 12), now, EN) == "Tomorrow Mon 10/12"
    assert guide.day_index(local(2026, 10, 11, 23, 59), now) == 0
    assert guide.day_index(local(2026, 10, 12, 0, 0), now) == 1
    assert guide.day_index(local(2026, 10, 10, 23, 30), now) == -1


def test_time_slots():
    now = local(2026, 10, 11, 15, 28, 10)
    slots = guide.time_slots(now)
    assert len(slots) == 25 and slots[0] == now
    assert slots[1] == local(2026, 10, 11, 16) and slots[-1] == local(2026, 10, 12, 15)
    assert guide.time_slots(local(2026, 10, 11, 15))[1] == local(2026, 10, 11, 16)  # exactly 15:00 -> 16:00 next
    folders = guide.slot_folders(now, JA)
    labels = [f["label"] for f in folders]
    assert labels[0] == "現在 (15:28)" and labels[1] == "16:00" and labels[8] == "23:00"
    assert labels[9] == "明日 0:00" and labels[-1] == "明日 15:00"
    assert folders[0]["path"].endswith("action=guide_now&t=now")
    assert folders[1]["path"].endswith(f"action=guide_now&t={local(2026, 10, 11, 16)}")
    assert all(f["folder"] for f in folders)
    assert guide.slot_folders(now, EN)[9]["label"] == "Tomorrow 0:00"
    assert guide.slot_header(local(2026, 10, 11, 21), now, JA) == "放送中 21:00"
    assert guide.slot_header(local(2026, 10, 12, 1), now, JA) == "放送中 明日 1:00"
    assert guide.slot_header(now, now, JA) == "放送中 15:28"


@pytest.fixture(params=["Pacific/Chatham", "Asia/Kolkata", "Asia/Tokyo"])
def zone(request, monkeypatch):
    """L6: run in zones with :45 and :30 offsets (and Tokyo)."""
    if not os.path.exists(os.path.join("/usr/share/zoneinfo", request.param)):
        pytest.skip(f"no tzdata for {request.param}")
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


def test_time_slots_in_fractional_offset_zones(zone):
    test_time_slots()
    test_local_midnight_and_day_label()
    now = local(2026, 10, 11, 15, 28, 10)
    assert all(time.localtime(t / 1000)[4:6] == (0, 0) for t in guide.time_slots(now)[1:])  # local full hours


def test_menu():
    m = guide.menu(JA)
    assert [f["label"] for f in m] == ["放送中", "ジャンル別"]
    assert m[0]["path"].endswith("action=guide_now") and m[1]["path"].endswith("action=guide_genres")


# --- on air at t -----------------------------------------------------------------------------------------------

def test_on_air_at_boundaries_and_filters():
    a, b = chan(1), chan(2, "ＣＨ２")
    radio, data = chan(3, "ラジオ", stype=2), chan(4, "携帯", stype=0xC0)
    scheds = [{"channel": a, "programs": [prog(1, 0, 10 * MIN), prog(2, 10 * MIN, 20 * MIN)]},
              {"channel": radio, "programs": [prog(3, 0, 20 * MIN, cid=3)]},
              {"channel": b, "programs": [prog(4, 5 * MIN, 30 * MIN, cid=2)]},
              {"channel": data, "programs": [prog(5, 0, 20 * MIN, cid=4)]}]
    assert [(c["id"], p["id"]) for c, p in guide.on_air_at(scheds, 10 * MIN)] == [(1, 2), (2, 4)]  # endAt == t not
    assert [(c["id"], p["id"]) for c, p in guide.on_air_at(scheds, 5 * MIN)] == [(1, 1), (2, 4)]  # startAt == t
    assert guide.on_air_at(scheds, 40 * MIN) == []


def test_on_air_now_matches_live_now_next(fx):
    out = guide.on_air_at(fx["schedules"], fx["now"])
    want = [s["channel"]["id"] for s in fx["schedules"]
            if live.watchable(s["channel"]) and live.now_next(s["programs"], fx["now"])[0]]
    assert [c["id"] for c, _ in out] == want
    assert len([c for c, _ in out if not c["name"].startswith("（合成）")]) >= 24  # 020: real (free) channels on air


# --- genres ----------------------------------------------------------------------------------------------------

def test_genre_counts_and_folders():
    now = 0
    a = chan(1)
    scheds = [{"channel": a, "programs": [
        prog(1, -5 * MIN, 10 * MIN, genre1=3, subGenre1=0, genre2=3, subGenre2=1),  # once for 3
        prog(2, 10 * MIN, 20 * MIN, genre1=6, genre2=3),
        prog(3, 20 * MIN, 30 * MIN),  # no genre: in none
        prog(4, -20 * MIN, -10 * MIN, genre1=0),  # ended
        prog(5, guide.WINDOW_MS, guide.WINDOW_MS + MIN, genre1=0),  # starts after the window
    ]}, {"channel": chan(9, stype=2), "programs": [prog(6, 0, MIN, cid=9, genre1=4)]}]  # radio
    counts = guide.genre_counts(scheds, now)
    assert counts == {3: 2, 6: 1}
    folders = guide.genre_folders(counts, "ja")
    assert [f["label"] for f in folders] == ["ドラマ (2)", "映画 (1)"]
    assert folders[0]["path"].endswith("action=guide_genre&genre=3")
    assert [f["label"] for f in guide.genre_folders(counts, "en")] == ["Drama (2)", "Movies (1)"]


def test_genre_programs_order_and_window():
    a, b = chan(1), chan(2, "ＣＨ２")
    scheds = [{"channel": a, "programs": [prog(1, 0, 10 * MIN, genre1=3), prog(2, 10 * MIN, 20 * MIN, genre3=3)]},
              {"channel": b, "programs": [prog(3, -5 * MIN, 30 * MIN, cid=2, genre2=3),
                                          prog(4, 10 * MIN, 20 * MIN, cid=2, genre1=3),
                                          prog(5, 10 * MIN, 20 * MIN, cid=2, genre1=1)]}]
    out = guide.genre_programs(scheds, 3, 1 * MIN)
    assert [p["id"] for _, p in out] == [3, 1, 2, 4]  # (startAt, server order); on air first


def test_genre_counts_fixture(fx):
    counts = guide.genre_counts(fx["schedules"], fx["now"])
    assert counts and all(n > 0 for n in counts.values())
    for g, n in counts.items():
        assert len(guide.genre_programs(fx["schedules"], g, fx["now"])) == n


# --- channel days ----------------------------------------------------------------------------------------------

def test_day_buckets():
    now = local(2026, 10, 11, 15, 0)
    on_air = prog(1, local(2026, 10, 10, 23, 30), local(2026, 10, 11, 15, 30))  # started yesterday
    late = prog(2, local(2026, 10, 11, 23, 30), local(2026, 10, 12, 0, 30))  # crosses midnight: in both days
    day2 = prog(3, local(2026, 10, 13, 9), local(2026, 10, 13, 10))
    far = prog(4, local(2026, 10, 19, 9), local(2026, 10, 19, 10))  # today + 8: beyond
    ended = prog(5, local(2026, 10, 11, 9), local(2026, 10, 11, 10))
    days = [{"channel": chan(), "programs": [ended, on_air, late]}, {"channel": chan(), "programs": [late]},
            {"channel": chan(), "programs": [day2]}, {"channel": chan(), "programs": [far]}]
    buckets = guide.day_buckets(days, now)
    assert {k: [p["id"] for p in v] for k, v in buckets.items()} == {0: [1, 2], 2: [3]}
    folders = guide.day_folders(chan(7, "ＮＨＫ総合１"), buckets, now, JA)
    assert [f["label"] for f in folders] == ["今日 10/11(日) (2)", "10/13(火) (1)"]
    assert "action=guide_channel" in folders[1]["path"] and f"day={local(2026, 10, 13)}" in folders[1]["path"]
    assert "channelId=7" in folders[1]["path"]


def test_day_programs_by_absolute_day():
    """L1: a day folder names its local midnight, so a list made at 23:58 and opened after midnight is still right."""
    now = local(2026, 10, 11, 15, 0)
    on_air = prog(1, local(2026, 10, 10, 23, 30), local(2026, 10, 11, 15, 30))
    late = prog(2, local(2026, 10, 11, 23, 30), local(2026, 10, 12, 0, 30))
    day2 = prog(3, local(2026, 10, 12, 9), local(2026, 10, 12, 10))
    days = [{"channel": chan(), "programs": [on_air, late, day2]}, {"channel": chan(), "programs": [late, day2]}]
    assert [p["id"] for p in guide.day_programs(days, local(2026, 10, 11), now)] == [1, 2]  # today + the on-air one
    assert [p["id"] for p in guide.day_programs(days, local(2026, 10, 12), now)] == [3]
    after = local(2026, 10, 12, 0, 5)  # the 10/11 folder clicked after midnight: still 10/11's programs
    assert [p["id"] for p in guide.day_programs(days, local(2026, 10, 11), after)] == [2]
    assert [p["id"] for p in guide.day_programs(days, local(2026, 10, 12), after)] == [2, 3]  # 10/12 is today: on air


def test_day_buckets_fixture(fx):
    nhk = [s for s in fx["schedules"] if s["channel"]["id"] == 3211240960][0]
    buckets = guide.day_buckets([nhk], fx["now"])
    assert 0 in buckets and len(buckets) >= 7 and max(buckets) < guide.DAYS
    assert live.now_next(nhk["programs"], fx["now"])[0] in buckets[0]


# --- program items ---------------------------------------------------------------------------------------------

EXT = "◇出演者\n【司会】若林正恭，春日俊彰\n◇番組内容\n説明"


def _items(now, **kw):
    c = chan(3211240960, "ＮＨＫ総合１・大阪")
    cur = prog(10, now - 10 * MIN, now + 20 * MIN, "ニュース［字］", cid=c["id"], genre1=0, subGenre1=0,
               description="今日の　ニュース", extended=EXT)
    fut = prog(11, now + 2 * HOUR, now + 3 * HOUR, "ドラマ", cid=c["id"], genre1=3, subGenre1=0, isFree=False)
    return c, cur, fut


def test_program_item_on_air():
    now = local(2026, 10, 11, 15, 28)
    c, cur, _ = _items(now)
    d = guide.program_item(c, cur, api(), now, "ja", JA, "slot")
    assert d["label"] == "[COLOR yellow]▶[/COLOR] NHK総合1・大阪  ニュース[字]"
    assert "action=live_play" in d["path"] and "channelId=3211240960" in d["path"]
    assert d["playable"] is True and d["folder"] is False
    assert d["info"]["playcount"] == 0 and d["stream"]["codec"] == ""
    info = d["info"]
    assert info["mediatype"] == "video" and info["title"] == "ニュース[字]"
    assert info["genres"] == [genre_name(0, 0)] and info["studios"] == ["NHK総合1・大阪"]
    assert info["duration"] == 30 * 60
    assert info["firstaired"] == info["premiered"] == "2026-10-11"
    assert info["tagline"] == "NHK総合1・大阪  15:18-15:48 (残り20分)"
    assert info["plotoutline"] == "15:18-15:48 (残り20分)"
    assert info["plot"].startswith("15:18-15:48 (残り20分)\n\n")
    assert [a[0] for a in info["cast"]] == ["若林正恭", "春日俊彰"]
    p = d["props"]
    assert p["epgstation.programId"] == "10" and p["epgstation.channelId"] == "3211240960"
    assert p["epgstation.onAir"] == "true" and p["epgstation.progress"] == "33"
    assert p["epgstation.startAt"] == "2026-10-11 15:18" and p["epgstation.endAt"] == "2026-10-11 15:48"
    assert p["epgstation.isFree"] == "true" and p["epgstation.channelType"] == "GR"
    assert p["epgstation.extended.番組内容"] == "説明"
    assert d["art"] == {"icon": "http://epgs:8888/api/channels/3211240960/logo",
                        "thumb": "http://epgs:8888/api/channels/3211240960/logo"}
    label, url, mode = d["context"][0]
    assert label == "番組表" and mode == "update" and "action=guide_channel" in url and "channelId=3211240960" in url


def test_program_item_future_styles():
    now = local(2026, 10, 11, 15, 28)
    c, _, fut = _items(now)
    d = guide.program_item(c, fut, api(), now, "ja", JA, "genre")
    assert d["label"] == "17:28 NHK総合1・大阪  ドラマ"
    assert "action=guide_info" in d["path"] and "programId=11" in d["path"] and "channelId=3211240960" in d["path"]
    assert d["playable"] is False and d["folder"] is False
    assert "playcount" not in d["info"] and not d.get("stream")
    assert d["info"]["tagline"] == "NHK総合1・大阪  10/11(日) 17:28-18:28"
    assert d["props"]["epgstation.onAir"] == "false" and "epgstation.progress" not in d["props"]
    assert d["props"]["epgstation.isFree"] == "false"
    assert guide.program_item(c, fut, api(), now, "ja", JA, "day")["label"] == "17:28  ドラマ"
    assert guide.program_item(c, fut, api(), now, "ja", JA, "slot")["label"] == "NHK総合1・大阪  ドラマ"
    tomorrow = dict(fut, startAt=local(2026, 10, 12, 1), endAt=local(2026, 10, 12, 2))
    label = guide.program_item(c, tomorrow, api(), now, "ja", JA, "genre")["label"]
    assert label == "明日 01:00 NHK総合1・大阪  ドラマ"
    en = guide.program_item(c, tomorrow, api(), now, "en", EN, "genre")
    assert en["label"] == "Tomorrow 01:00 NHK総合1・大阪  ドラマ"
    assert en["info"]["genres"] == ["Drama / Japanese dramas"]  # subgenres (009)
    assert en["info"]["tagline"] == "NHK総合1・大阪  Mon 10/12 01:00-02:00"
    assert guide.program_item(c, fut, api(), now, "ja", JA, "day", context=False)["context"] == []


def test_program_item_logo_and_auth():
    now = local(2026, 10, 11, 15, 28)
    c, cur, _ = _items(now)
    assert guide.program_item(dict(c, hasLogoData=False), cur, api(), now, "ja", JA, "slot")["art"] == {}
    d = guide.program_item(c, cur, api("u"), now, "ja", JA, "slot")
    assert "|Authorization=" in d["art"]["thumb"]


def test_program_item_parser_failure(monkeypatch):
    now = local(2026, 10, 11, 15, 28)
    c, cur, _ = _items(now)

    def boom(rec, on_error=None):
        on_error(ValueError("x"))
        return None
    monkeypatch.setattr(epgmeta, "metadata", boom)
    errors = []
    d = guide.program_item(c, cur, api(), now, "ja", JA, "slot", on_error=lambda p, e: errors.append(p["id"]))
    assert errors == [10] and "今日の ニュース" in d["info"]["plot"] and "cast" not in d["info"]


def test_with_schedule_context(fx):
    out = live.channel_items(fx["schedules"], api(), fx["now"], "ja", JA)
    out = guide.with_schedule_context(out, JA)
    for d in out:
        label, url, mode = d["context"][-1]
        assert label == "番組表" and mode == "update"
        assert f"channelId={d['props']['epgstation.channelId']}" in url


# --- backlog 014: reservation markers and entries ------------------------------------------------------------------

JA14 = dict(JA, record="録画", by_rule="録画予約: ルール", by_manual="録画予約: 手動", delete="予約を削除",
            skip_this="この回を除外", stop="録画を停止", unskip="除外を解除", unoverlap="重複を解除して録画",
            conflict="競合", overlap="重複", skip="除外", line_overlap="録画済みのため録画しません",
            line_skip="除外: 録画しません", line_conflict="競合のため録画されません")


def _idx(state="normal", rid=7, pid=11, rule=3):
    item = {k: v for k, v in (("reserveId", rid), ("programId", pid), ("ruleId", rule)) if v is not None}
    key = {"normal": "normal", "conflict": "conflicts", "skip": "skips", "overlap": "overlaps"}[state]
    return reserves.lists_index({key: [item]})


def _rec_of(p, cid=3211240960):
    return {"id": 900, "channelId": cid, "programId": p["id"], "startAt": p["startAt"], "endAt": p["endAt"],
            "isRecording": True}


def test_program_item_without_index_has_no_marker_or_entry():
    now = local(2026, 10, 11, 15, 28)
    c, cur, fut = _items(now)
    d = guide.program_item(c, fut, api(), now, "ja", JA14, "genre")
    assert d["label"] == "17:28 NHK総合1・大阪  ドラマ" and len(d["context"]) == 1  # 番組表 only
    assert guide.program_item(c, fut, api(), now, "ja", JA14, "day", context=False, index=None)["context"] == []


def test_program_item_unreserved_offers_record_after_schedule():
    now = local(2026, 10, 11, 15, 28)
    c, cur, fut = _items(now)
    d = guide.program_item(c, fut, api(), now, "ja", JA14, "genre", index={})
    assert d["label"] == "17:28 NHK総合1・大阪  ドラマ"
    assert [x[0] for x in d["context"]] == ["番組表", "録画"]
    url = d["context"][1][1]
    assert "action=record" in url and "programId=11" in url and "channelId=3211240960" in url
    assert "name=NHK%E7%B7%8F%E5%90%881%E3%83%BB%E5%A4%A7%E9%98%AA" in url and len(d["context"][1]) == 2
    day = guide.program_item(c, fut, api(), now, "ja", JA14, "day", context=False, index={})
    assert [x[0] for x in day["context"]] == ["録画"]  # inside its own 番組表: only the reservation entry
    on_air = guide.program_item(c, cur, api(), now, "ja", JA14, "slot", index={})
    assert on_air["label"].startswith("[COLOR yellow]▶[/COLOR] NHK") and on_air["context"][1][0] == "録画"


@pytest.mark.parametrize("state,rule,marker,entry,op,line", [
    ("normal", 3, "[COLOR red]○[/COLOR] ", "この回を除外", "cancel", "録画予約: ルール"),
    ("normal", None, "[COLOR red]○[/COLOR] ", "予約を削除", "cancel", "録画予約: 手動"),
    ("overlap", 3, "[COLOR grey][重複][/COLOR] ", "重複を解除して録画", "unoverlap",
     "録画予約: ルール\n録画済みのため録画しません"),
    ("skip", 3, "[COLOR grey][除外][/COLOR] ", "除外を解除", "unskip", "録画予約: ルール\n除外: 録画しません"),
    ("conflict", 3, "[COLOR red][競合][/COLOR] ", "この回を除外", "cancel",
     "録画予約: ルール\n競合のため録画されません"),
])
def test_program_item_reserved(state, rule, marker, entry, op, line):
    now = local(2026, 10, 11, 15, 28)
    c, cur, fut = _items(now)
    d = guide.program_item(c, fut, api(), now, "ja", JA14, "genre", index=_idx(state, rule=rule))
    assert d["label"] == marker + "17:28 NHK総合1・大阪  ドラマ"
    assert [x[0] for x in d["context"]] == ["番組表", entry]
    assert d["context"][1][1] == f"plugin://plugin.video.epgstation/?action=reserve_action&op={op}&reserveId=7"
    plot = d["info"]["plot"].split("\n")
    assert plot[0] == "10/11(日) 17:28-18:28" and "\n".join(plot[1:1 + line.count("\n") + 1]) == line


def test_program_item_on_air_recording_and_reserved():
    now = local(2026, 10, 11, 15, 28)
    c, cur, _ = _items(now)
    idx = _idx(pid=10)
    rec = guide.program_item(c, cur, api(), now, "ja", JA14, "slot", index=idx, records=[_rec_of(cur)])
    assert rec["label"] == "[COLOR yellow]▶[/COLOR] [COLOR red]●[/COLOR] NHK総合1・大阪  ニュース[字]"
    assert rec["context"][1][0] == "録画を停止" and "op=stop" in rec["context"][1][1]
    waiting = guide.program_item(c, cur, api(), now, "ja", JA14, "slot", index=idx)
    assert waiting["label"] == "[COLOR yellow]▶[/COLOR] [COLOR red]○[/COLOR] NHK総合1・大阪  ニュース[字]"
    assert waiting["context"][1][0] == "この回を除外"


def test_program_item_ended_in_stale_list_has_no_record():
    now = local(2026, 10, 11, 15, 28)
    c, cur, fut = _items(now)
    later = fut["endAt"] + MIN
    d = guide.program_item(c, fut, api(), later, "ja", JA14, "genre", index={})
    assert [x[0] for x in d["context"]] == ["番組表"]


def test_program_items_pass_index_and_records():
    now = local(2026, 10, 11, 15, 28)
    c, cur, fut = _items(now)
    out = guide.program_items([(c, cur), (c, fut)], api(), now, "ja", JA14, "genre", index=_idx(pid=11),
                              records=())
    assert [d["context"][-1][0] for d in out] == ["録画", "この回を除外"]


def test_mark_channels(fx):
    now = fx["now"]
    out = guide.with_schedule_context(live.channel_items(fx["schedules"], api(), now, "ja", JA14), JA14)
    programs = {}
    for s in fx["schedules"]:
        cur, _ = live.now_next(s["programs"], now)
        if cur is not None:
            programs[cur["id"]] = dict(cur, channelId=s["channel"]["id"])
    first, second = out[0], out[1]
    pid = int(first["props"]["epgstation.programId"])
    labels = [d["label"] for d in out]
    guide.mark_channels(out, programs, _idx(pid=pid), [], now, JA14)
    assert first["label"] == "[COLOR red]○[/COLOR] " + labels[0]
    assert [x[0] for x in first["context"]] == ["番組表", "この回を除外"]
    assert second["label"] == labels[1] and [x[0] for x in second["context"]] == ["番組表", "録画"]
    assert "action=record" in second["context"][1][1]
    assert f"programId={second['props']['epgstation.programId']}" in second["context"][1][1]
    again = guide.with_schedule_context(live.channel_items(fx["schedules"], api(), now, "ja", JA14), JA14)
    guide.mark_channels(again, programs, None, [], now, JA14)  # lists failed: unchanged
    assert [d["label"] for d in again] == labels and all(len(d["context"]) == 1 for d in again)


def test_guide_has_no_kodi_import_and_no_post():
    src = open(guide.__file__, encoding="utf-8").read()
    assert "import xbmc" not in src and "from xbmc" not in src
    assert "POST" not in src and "urlopen" not in src  # all requests go through api.py's GET
    snap = open(os.path.join(ROOT, "tools", "snapshot_api.py"), encoding="utf-8").read()
    assert 'method="GET"' in snap and 'method="POST"' not in snap and "data=" not in snap


# --- cache -----------------------------------------------------------------------------------------------------

class FakeApi:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def schedules(self, start, end, types, half_width=False, timeout=None):
        self.calls.append((start, end, tuple(types), timeout))
        if self.fail:
            raise ApiError("down")
        return [{"channel": chan(), "programs": [prog(1, start, start + MIN)]}]


def test_guide_cache(tmp_path):
    clock = [1000.0]
    cache = guide.GuideCache(str(tmp_path), "http://epgs:8888", now=lambda: clock[0])
    a = FakeApi()
    first = cache.schedules(a, ["GR", "BS"])
    assert len(a.calls) == 1 and first[0]["programs"][0]["id"] == 1
    start, end, types, timeout = a.calls[0]
    assert start == 1000000 and end - start == guide.WINDOW_MS + guide.TTL * 1000 and timeout == guide.DEADLINE
    assert os.path.isfile(os.path.join(str(tmp_path), guide.CACHE_FILE))
    clock[0] += guide.TTL - 1
    assert cache.schedules(a, ["GR", "BS"]) == first and len(a.calls) == 1  # hit
    assert guide.GuideCache(str(tmp_path), "http://other:8888", now=lambda: clock[0]).schedules(a, ["GR", "BS"])
    assert len(a.calls) == 2  # other server
    cache.schedules(a, ["GR"])
    assert len(a.calls) == 3  # other types
    clock[0] += guide.TTL + 1
    cache.schedules(a, ["GR"])
    assert len(a.calls) == 4  # expired


def test_guide_cache_corrupt_unwritable_and_errors(tmp_path):
    path = os.path.join(str(tmp_path), guide.CACHE_FILE)
    with open(path, "w") as f:
        f.write("{not json")
    a = FakeApi()
    guide.GuideCache(str(tmp_path), "s", now=lambda: 1.0).schedules(a, ["GR"])
    assert len(a.calls) == 1
    with open(path, "w") as f:
        json.dump({"v": 99, "server": "s", "types": ["GR"], "t": 1000, "end": 10 ** 15, "schedules": []}, f)
    guide.GuideCache(str(tmp_path), "s", now=lambda: 1.0).schedules(a, ["GR"])
    assert len(a.calls) == 2  # other format
    for bad in ([{}], [{"channel": {"id": 1}, "programs": [{"id": 1}]}], [{"channel": [], "programs": []}], [1]):
        with open(path, "w") as f:  # L3: valid JSON, wrong shape -> fetch
            json.dump({"v": 1, "server": "s", "types": ["GR"], "t": 1000, "end": 10 ** 15, "schedules": bad}, f)
        n = len(a.calls)
        guide.GuideCache(str(tmp_path), "s", now=lambda: 1.0).schedules(a, ["GR"])
        assert len(a.calls) == n + 1, bad
    missing = os.path.join(str(tmp_path), "no", "such", "dir")
    assert guide.GuideCache(missing, "s", now=lambda: 1.0).schedules(a, ["GR"])  # still returns data
    with pytest.raises(ApiError):
        guide.GuideCache(str(tmp_path / "x"), "s", now=lambda: 1.0).schedules(FakeApi(fail=True), ["GR"])


def test_removal_checklist_lists_every_hook():
    """guide.py's docstring names every file with a program guide (013) hook."""
    files = [os.path.join(ROOT, "Makefile")]
    for top in ("plugin.video.epgstation", "tools", "tests"):
        files += [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(ROOT, top)) for f in fs
                  if f.endswith((".py", ".xml", ".po")) and "fixtures" not in d and "__pycache__" not in d]
    guide_py = os.path.abspath(guide.__file__)
    hooks = {os.path.basename(f) for f in files
             if os.path.abspath(f) != guide_py and "program guide (013)" in open(f, encoding="utf-8").read()}
    assert {"plugin.py", "api.py", "fake_epgstation.py", "snapshot_api.py"} <= hooks
    missing = sorted(h for h in hooks if h not in guide.__doc__)
    assert not missing, missing
    # L7: the import of guide in plugin.py carries the marker too
    plugin = os.path.join(ROOT, "plugin.video.epgstation", "resources", "lib", "epgstation", "plugin.py")
    # transcoded playback (011): ruff wraps the import in parentheses, the marker sits on its first line
    imports = re.findall(r"^from \. import (?:\((.*?)^\)|(.*?)$)", open(plugin, encoding="utf-8").read(), re.M | re.S)
    imports = [a or b for a, b in imports]
    assert any(re.search(r"\bguide\b", text) for text in imports)
    assert all("program guide (013)" in text for text in imports if re.search(r"\bguide\b", text)), imports
