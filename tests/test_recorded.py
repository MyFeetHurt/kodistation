"""Backlog 006: protect, unprotect and delete recordings (recorded.py, the item hooks, the library changed-marker,
the api writes, plugin.do_recorded_action against the fake EPGStation, the grep rules and the DEV tool)."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from conftest import FIXTURES, ROOT, run_plugin
from resources.lib.epgstation import api as api_mod
from resources.lib.epgstation import items, library, recorded
from test_reserves import _api_methods, _callers, _FakeDev, _src, _test_reserve_tool, local

JA = {"protect": "保護", "unprotect": "保護を解除", "delete": "削除", "marker": "保護",
      "q_protect": "この録画を保護しますか? 自動削除されず、保護中は削除できません。",
      "q_unprotect": "保護を解除しますか? 解除後は削除や自動削除の対象になります。",
      "q_delete": "この録画を削除しますか? ファイルは元に戻せません。",
      "q_encoding": "エンコード中: 中止されます", "files": "ファイル: {0}",
      "done_protect": "保護しました", "done_unprotect": "保護を解除しました", "done_delete": "削除しました",
      "gone": "この録画はもうありません", "changed": "録画の状態が変わりました",
      "protected": "保護されているため削除できません", "protect_line": "保護中 (自動削除の対象外)",
      "failed": "失敗しました: {0}", "failed_net": "失敗しました (通信エラー)", "weekdays": "日,月,火,水,木,金,土",
      # delete a file (018)
      "delete_file": "ファイルを削除", "choose_file": "削除するファイルを選択",
      "q_delete_file": "このファイルを削除しますか? 元に戻せません。", "delete_keep": "削除: {0} / 残す: {1}",
      "done_delete_file": "ファイルを削除しました", "file_gone": "このファイルはもうありません"}
MIN = 60 * 1000


def _fixture(fixture_set, rid):
    with open(os.path.join(FIXTURES, fixture_set, "api", "recorded", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def rec_of(**kw):
    """A captured dev recording (texts, files real) with the given fields."""
    base = _fixture("dev", 9)
    base.update(kw)
    return base


def api(username=""):
    return api_mod.EPGStation("http://epgs:8888", username, "pw")


# --- recorded.py --------------------------------------------------------------------------------------------------

def test_actions_for_each_state():
    assert recorded.actions_for(rec_of()) == ["protect", "delete", "delete_file"]  # dev 9 has two files (018)
    assert recorded.actions_for(rec_of(videoFiles=rec_of()["videoFiles"][:1])) == ["protect", "delete"]
    assert recorded.actions_for(rec_of(isProtected=True)) == ["unprotect"]
    assert recorded.actions_for(rec_of(isRecording=True)) == ["protect"]
    assert recorded.actions_for(rec_of(isRecording=True, isProtected=True)) == ["unprotect"]


def test_context_entries_urls_and_labels():
    rec = rec_of(id=12)
    assert recorded.context_entries(rec, JA) == [
        ("保護", items.plugin_url(action="recorded_action", op="protect", id=12)),
        ("削除", items.plugin_url(action="recorded_action", op="delete", id=12)),
        ("ファイルを削除", items.plugin_url(action="recorded_action", op="delete_file", id=12))]
    assert recorded.context_entries(rec_of(id=12, isProtected=True)) == [
        ("Unprotect", items.plugin_url(action="recorded_action", op="unprotect", id=12))]


def test_marker_and_protect_line():
    assert recorded.marker(rec_of(), JA) == ""
    assert recorded.marker(rec_of(isProtected=True), JA) == " [COLOR gold][保護][/COLOR]"
    assert recorded.marker(rec_of(isProtected=True)) == " [COLOR gold][Protected][/COLOR]"
    assert recorded.marker(rec_of(isProtected=True), {"marker": ""}) == " [COLOR gold][Protected][/COLOR]"
    assert recorded.protect_line(JA) == "保護中 (自動削除の対象外)"
    assert recorded.protect_line() == "Protected (not auto-deleted)"


def test_can_and_refusal():
    free, prot, live = rec_of(), rec_of(isProtected=True), rec_of(isRecording=True)
    assert recorded.can("protect", free) and not recorded.can("protect", prot)
    assert recorded.can("unprotect", prot) and not recorded.can("unprotect", free)
    assert recorded.can("delete", free) and not recorded.can("delete", prot) and not recorded.can("delete", live)
    assert recorded.can("protect", live)
    assert recorded.refusal("delete", prot, JA) == "保護されているため削除できません"
    assert recorded.refusal("delete", live, JA) == "録画の状態が変わりました"
    assert recorded.refusal("protect", prot, JA) == "録画の状態が変わりました"
    assert recorded.refusal("unprotect", free) == "Recording has changed"


def test_files_line():
    assert recorded.files_line(rec_of(), JA) == "ファイル: TS 136.8 MB, H.264 5.0 MB"
    one = rec_of(videoFiles=[{"id": 1, "name": "TS", "type": "ts", "size": 1288490189}])
    assert recorded.files_line(one) == "Files: TS 1.2 GB"
    assert recorded.files_line(rec_of(videoFiles=[])) == ""
    assert items.fmt_size(143475584) == "136.8 MB"


def test_one_line():
    name = items.to_half(rec_of()["name"])  # dev recording 9
    assert recorded.one_line(name) == "おしゃべり小料理ゆみこ[字]★『豊臣兄弟』で注目の松下洸…"
    assert recorded.one_line("ニュース7") == "ニュース7"
    assert recorded.one_line("a" * 46) == "a" * 46 and recorded.one_line("a" * 47) == "a" * 46 + "…"
    assert recorded.one_line("あ" * 28) == "あ" * 28 and recorded.one_line("あ" * 29) == "あ" * 28 + "…"


def test_confirm_message_per_op():
    rec = rec_of(startAt=local(2026, 10, 12, 21, 0), endAt=local(2026, 10, 12, 21, 30))
    name = recorded.one_line(items.to_half(rec["name"]))
    msg = recorded.confirm_message("delete", rec, "NHK総合1", JA).split("\n")
    assert msg == ["この録画を削除しますか? ファイルは元に戻せません。", name, "10/12(月) 21:00-21:30 NHK総合1",
                   "ファイル: TS 136.8 MB, H.264 5.0 MB"]
    msg = recorded.confirm_message("protect", rec, "NHK総合1", JA).split("\n")
    assert msg == [JA["q_protect"], name, "10/12(月) 21:00-21:30 NHK総合1"]
    msg = recorded.confirm_message("unprotect", rec, "BS", {}).split("\n")
    assert msg == ["Remove protection? The recording can then be deleted, also automatically.", name,
                   "Mon 10/12 21:00-21:30 BS"]
    enc = recorded.confirm_message("delete", dict(rec, isEncoding=True), "X", JA).split("\n")
    assert len(enc) == 4 and enc[0] == JA["q_delete"]  # review L3: four lines, Estuary's box
    assert enc[-1] == "ファイル: TS 136.8 MB, H.264 5.0 MB (エンコード中: 中止されます)"
    enc = recorded.confirm_message("delete", dict(rec, isEncoding=True, videoFiles=[]), "X", {}).split("\n")
    assert enc[-1] == "Encoding: will be cancelled"
    assert "エンコード中" not in recorded.confirm_message("protect", dict(rec, isEncoding=True), "X", JA)


def test_entry_label_done_message():
    assert [recorded.entry_label(op, JA) for op in recorded.OPS] == ["保護", "保護を解除", "削除", "ファイルを削除"]
    assert [recorded.done_message(op, JA) for op in recorded.OPS] == ["保護しました", "保護を解除しました",
                                                                       "削除しました", "ファイルを削除しました"]
    assert [recorded.done_message(op) for op in recorded.OPS] == ["Protected", "Protection removed",
                                                                   "Recording deleted", "File deleted"]


@pytest.mark.parametrize("status,detail,want", [
    (500, "RecordedIsProtected", "保護されているため削除できません"),
    (500, "RecordedIdIsNotFound", "この録画はもうありません"),
    (500, "RecordedIsNull", "この録画はもうありません"),
    (500, "IPCTimeout", "失敗しました (通信エラー)"),
    (None, None, "失敗しました (通信エラー)"),
    (500, "Something", "失敗しました: Something"),
    (503, None, "失敗しました: HTTP 503"),
])
def test_error_message(status, detail, want):
    assert recorded.error_message(status, detail, JA) == want


def test_fingerprint():
    rec = rec_of()
    fp = recorded.fingerprint(rec)
    assert recorded.fingerprint(dict(rec, isProtected=True)) == fp  # the flag is checked by can(), not here
    files = list(reversed(rec["videoFiles"]))
    assert recorded.fingerprint(dict(rec, videoFiles=files)) == fp  # file order does not matter
    for change in ({"name": "x"}, {"startAt": 1}, {"channelId": 1}, {"isEncoding": True}, {"videoFiles": files[:1]},
                   {"videoFiles": [dict(files[0], size=1)] + files[1:]}):
        assert recorded.fingerprint(dict(rec, **change)) != fp, change
    live = dict(rec, isRecording=True)
    grown = dict(live, videoFiles=[dict(v, size=v["size"] + 1) for v in rec["videoFiles"]])
    assert recorded.fingerprint(grown) == recorded.fingerprint(live)  # in progress: the TS grows


def test_maybe_applied():
    assert recorded.maybe_applied(None, None) and recorded.maybe_applied(500, "IPCTimeout")
    assert not recorded.maybe_applied(500, "RecordedIsProtected") and not recorded.maybe_applied(503, None)


def test_removal_checklist_names_every_hook():
    """recorded.py's docstring names every file with a "protect / delete (006)" hook."""
    files = []
    for top in ("plugin.video.epgstation", "tools", "tests"):
        files += [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(ROOT, top)) for f in fs
                  if f.endswith((".py", ".xml", ".po")) and "fixtures" not in d and "__pycache__" not in d]
    me = os.path.abspath(recorded.__file__)
    hooks = {os.path.basename(f) for f in files
             if os.path.abspath(f) != me and "protect / delete (006)" in open(f, encoding="utf-8").read()}
    assert {"plugin.py", "items.py", "library.py", "api.py", "fake_epgstation.py", "kodi_smoke.py",
            "test_reserve.py"} <= hooks
    missing = sorted(h for h in hooks if h not in recorded.__doc__)
    assert not missing, missing


# --- items.recorded_item ------------------------------------------------------------------------------------------

def test_recorded_item_marker_plot_props_and_entries():
    rec = rec_of(isProtected=True)
    d = items.recorded_item(rec, api(), {}, "ja", JA, real_duration=60)  # 60 s of a 60-min slot: partial
    name = items.to_half(rec["name"])
    assert d["label"] == f"{name} [COLOR yellow][Partial][/COLOR] [COLOR gold][保護][/COLOR]"
    assert d["props"]["epgstation.isProtected"] == "true"
    assert d["info"]["plot"].endswith("保護中 (自動削除の対象外)")
    assert [c[0] for c in d["context"]] == ["Choose video file", "保護を解除"]
    free = items.recorded_item(rec_of(), api(), {}, "ja", JA)
    assert "[保護]" not in free["label"] and "保護中" not in free["info"]["plot"]
    assert [c[0] for c in free["context"]] == ["Choose video file", "保護", "削除", "ファイルを削除"]


def test_recorded_item_recording_entries_order():
    rec = rec_of(isRecording=True, videoFiles=[_fixture("dev", 9)["videoFiles"][0]])
    d = items.recorded_item(rec, api(), {}, "ja", dict(JA, stop="録画を停止"))
    assert [c[0] for c in d["context"]] == ["録画を停止", "保護"]
    d = items.recorded_item(dict(rec, isProtected=True), api(), {}, "en")
    assert [c[0] for c in d["context"]] == ["Stop recording", "Unprotect"]
    assert d["label"] == f"[COLOR red]●[/COLOR] {items.to_half(rec['name'])} [COLOR gold][Protected][/COLOR]"


def test_movie_and_episode_labels_keep_the_marker():
    rec = rec_of(isProtected=True)
    d = items.recorded_item(rec, api(), {}, "ja", JA)
    meta = {"version": "", "subtitle": "", "episode": None, "season": None, "show": "おしゃべり小料理ゆみこ"}
    assert items.movie_label(d, meta).endswith(" [COLOR gold][保護][/COLOR]")
    assert items.episode_label(d, meta).endswith(" [COLOR gold][保護][/COLOR]")
    plain = items.recorded_item(rec_of(), api(), {}, "ja", JA)
    assert items.movie_label(d, meta) == items.movie_label(plain, meta) + " [COLOR gold][保護][/COLOR]"


# --- library changed-marker ---------------------------------------------------------------------------------------

from test_library import ListApi, _r  # noqa: E402


def _lib(tmp_path, now=1000.0):
    return library.Library(str(tmp_path), "x", now=lambda: now)


def test_mark_changed_creates_and_merges(tmp_path):
    library.mark_changed(str(tmp_path), "x", 5, "protect", now=lambda: 10.0)
    library.mark_changed(str(tmp_path), "x", 6, "delete", now=lambda: 11.0)
    library.mark_changed(str(tmp_path), "x", 5, "unprotect", now=lambda: 12.0)
    library.mark_changed(str(tmp_path), "x", 7, None, now=lambda: 13.0)
    data = json.load(open(tmp_path / library.CHANGED_FILE, encoding="utf-8"))
    assert data == {"server": "x", "t": 13.0, "deleted": [6], "protect": {"5": False}}
    library.mark_changed(str(tmp_path), "other", 8, "protect", now=lambda: 14.0)  # another server: replaced
    data = json.load(open(tmp_path / library.CHANGED_FILE, encoding="utf-8"))
    assert data == {"server": "other", "t": 14.0, "deleted": [], "protect": {"8": True}}
    assert [p.name for p in tmp_path.iterdir()] == [library.CHANGED_FILE]  # atomic: no temp file left


def test_newer_marker_refreshes_once_and_save_removes_it(tmp_path):
    recs = [_r(1, 100, "A #1", isProtected=False), _r(2, 200, "B #1", isProtected=False)]
    _lib(tmp_path, 1000.0).current(ListApi(recs, []))
    library.mark_changed(str(tmp_path), "x", 2, "protect", now=lambda: 1001.0)
    recs[1]["isProtected"] = True
    api = ListApi(recs, [])
    snap = _lib(tmp_path, 1002.0).current(api)
    assert ("pages",) in api.calls and snap.records[2]["isProtected"] is True
    assert not (tmp_path / library.CHANGED_FILE).exists()
    api = ListApi(recs, [])
    _lib(tmp_path, 1003.0).current(api)
    assert ("pages",) not in api.calls  # warm again


def test_older_marker_is_ignored(tmp_path):
    recs = [_r(1, 100, "A #1"), _r(2, 200, "B #1")]
    library.mark_changed(str(tmp_path), "x", 2, "protect", now=lambda: 900.0)
    _lib(tmp_path, 1000.0).current(ListApi(recs, []))
    library.mark_changed(str(tmp_path), "x", 2, "protect", now=lambda: 999.0)  # written before the cache
    api = ListApi(recs, [])
    _lib(tmp_path, 1001.0).current(api)
    assert ("pages",) not in api.calls


def test_marker_of_another_server_is_ignored(tmp_path):
    recs = [_r(1, 100, "A #1"), _r(2, 200, "B #1")]
    _lib(tmp_path, 1000.0).current(ListApi(recs, []))
    library.mark_changed(str(tmp_path), "other", 2, "protect", now=lambda: 1001.0)
    api = ListApi(recs, [])
    _lib(tmp_path, 1002.0).current(api)
    assert ("pages",) not in api.calls


def test_marker_written_during_a_refresh_survives_and_forces_the_next(tmp_path):
    """A write while a refresh runs: the saved cache may predate it, so the marker stays (covered = refresh start)."""
    recs = [_r(1, 100, "A #1"), _r(2, 200, "B #1")]

    class WriteDuring(ListApi):
        def recorded_pages(self, total, page, workers, deadline):
            out = super().recorded_pages(total, page, workers, deadline)
            library.mark_changed(str(tmp_path), "x", 1, "delete", now=lambda: 1000.5)
            return out

    _lib(tmp_path, 1000.0).current(WriteDuring(recs, []))
    assert (tmp_path / library.CHANGED_FILE).exists()
    assert json.load(open(tmp_path / library.INDEX_FILE, encoding="utf-8"))["h"]["covered"] == 1000.0
    api = ListApi(recs[1:], [])
    snap = _lib(tmp_path, 1001.0).current(api)
    assert ("pages",) in api.calls and [e["id"] for e in snap.entries] == [2]
    assert not (tmp_path / library.CHANGED_FILE).exists()


def test_concurrent_mark_changed_loses_nothing(tmp_path, monkeypatch):
    """Review L2: two plugin runs marking at once (read, merge, write) both land."""
    import threading

    real = library._read_changed

    def slow(directory):
        out = real(directory)
        time.sleep(0.05)  # widen the read-merge-write window
        return out

    monkeypatch.setattr(library, "_read_changed", slow)
    threads = [threading.Thread(target=library.mark_changed, args=(str(tmp_path), "x", i, "delete")) for i in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.load(open(tmp_path / library.CHANGED_FILE, encoding="utf-8"))
    assert sorted(data["deleted"]) == [1, 2, 3]
    assert not (tmp_path / library.LOCK_FILE).exists()


def test_stale_or_stuck_lock_does_not_block(tmp_path, monkeypatch):
    lock = tmp_path / library.LOCK_FILE
    lock.write_text("", encoding="utf-8")
    old = time.time() - 60
    os.utime(lock, (old, old))  # left behind by a crashed run: taken over
    assert library.mark_changed(str(tmp_path), "x", 1, "delete")
    assert not lock.exists()
    lock.write_text("", encoding="utf-8")  # fresh and never released: write anyway after the wait
    monkeypatch.setattr(library, "LOCK_WAIT", 0.1)
    t = time.monotonic()
    assert library.mark_changed(str(tmp_path), "x", 2, "delete")
    assert time.monotonic() - t < 1.0
    assert json.load(open(tmp_path / library.CHANGED_FILE, encoding="utf-8"))["deleted"] == [1, 2]
    assert lock.exists()  # not ours: left alone


def test_stale_lock_that_cannot_be_removed_does_not_hang(tmp_path, monkeypatch):
    """Review N2: a stale lock that cannot be removed must not spin: give up after LOCK_WAIT and go on unlocked."""
    import threading

    lock = tmp_path / library.LOCK_FILE
    lock.write_text("", encoding="utf-8")
    os.utime(lock, (time.time() - 60, time.time() - 60))
    real = os.remove

    def remove(p):
        if str(p).endswith(library.LOCK_FILE):
            raise PermissionError(p)
        return real(p)

    monkeypatch.setattr(library.os, "remove", remove)
    done = []
    t = threading.Thread(target=lambda: done.append(library.mark_changed(str(tmp_path), "x", 1, "protect")),
                         daemon=True)
    t.start()
    t.join(3)  # timeout: LOCK_WAIT is 1 s
    assert done == [True], "mark_changed still spinning after 3 s"
    assert json.load(open(tmp_path / library.CHANGED_FILE, encoding="utf-8"))["protect"] == {"1": True}


class DownApi(ListApi):
    def recorded(self, *a, **kw):
        raise api_mod.ApiError("down")


class Tick:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_stale_fallback_after_write_during_refresh(tmp_path):
    """Review M1: a write during a refresh (header t after the marker) still shows in a stale fallback."""
    clock = Tick(1000.0)
    recs = [_r(1, 100, "A #1", isProtected=False), _r(2, 200, "B #1")]

    class WriteDuring(ListApi):
        def recorded_pages(self, total, page, workers, deadline):
            out = super().recorded_pages(total, page, workers, deadline)
            library.mark_changed(str(tmp_path), "x", 2, "delete", now=lambda: 1000.5)
            library.mark_changed(str(tmp_path), "x", 1, "protect", now=lambda: 1000.5)
            clock.t = 1001.0  # the refresh finishes later: header t = 1001
            return out

    library.Library(str(tmp_path), "x", now=clock).current(WriteDuring(recs, []))
    assert (tmp_path / library.CHANGED_FILE).exists()
    clock.t = 1002.0
    snap = library.Library(str(tmp_path), "x", now=clock).current(DownApi(recs, []))
    assert 2 not in snap.records, "deleted recording shown by the stale fallback"
    assert snap.records[1]["isProtected"] is True, "old protect flag shown by the stale fallback"


def test_marker_removed_by_concurrent_save(tmp_path, monkeypatch):
    """Review M1: a marker written while _save writes the records file is not removed."""
    clock = Tick(1000.0)
    recs = [_r(1, 100, "A #1", isProtected=False), _r(2, 200, "B #1")]
    real = library.store.atomic_write_json
    hit = []

    def write(path, obj):
        ok = real(path, obj)
        if path.endswith(library.RECORDS_FILE) and not hit:
            hit.append(1)
            library.mark_changed(str(tmp_path), "x", 1, "protect", now=lambda: 1000.9)
        return ok

    library.mark_changed(str(tmp_path), "x", 2, None, now=lambda: 999.0)  # an older marker exists
    monkeypatch.setattr(library.store, "atomic_write_json", write)
    library.Library(str(tmp_path), "x", now=clock).current(ListApi(recs, []))
    monkeypatch.setattr(library.store, "atomic_write_json", real)
    assert os.path.exists(tmp_path / library.CHANGED_FILE), "the protect marker of 1 was removed"


def test_out_of_order_refreshes(tmp_path):
    """Review N1: an older refresh that saves after a newer one must not bring its stale state back."""
    clock = Tick(1000.0)
    old = [_r(1, 100, "A #1", isProtected=False), _r(2, 200, "B #1")]
    new = [_r(1, 100, "A #1", isProtected=True)]  # 1 protected, 2 deleted at 1000.5

    class Slow(ListApi):  # R1 began at 1000, its pages predate the write; R2 runs and saves inside it
        def recorded_pages(self, total, page, workers, deadline):
            out = super().recorded_pages(total, page, workers, deadline)
            library.mark_changed(str(tmp_path), "x", 2, "delete", now=lambda: 1000.5)
            library.mark_changed(str(tmp_path), "x", 1, "protect", now=lambda: 1000.5)
            clock.t = 1001.0
            library.Library(str(tmp_path), "x", now=clock).current(ListApi(new, []))  # R2 covers the marker
            clock.t = 1002.0
            return out

    library.Library(str(tmp_path), "x", now=clock).current(Slow(old, []))  # R1 saves last
    assert json.load(open(tmp_path / library.INDEX_FILE, encoding="utf-8"))["h"]["covered"] == 1001.0
    clock.t = 1003.0
    snap = library.Library(str(tmp_path), "x", now=clock).current(DownApi(old, []))
    assert 2 not in snap.records and snap.records[1]["isProtected"] is True


def test_cache_without_covered_refreshes_on_any_marker(tmp_path):
    """A cache written before covered existed: any marker of this server means a refresh."""
    recs = [_r(1, 100, "A #1"), _r(2, 200, "B #1")]
    _lib(tmp_path, 1000.0).current(ListApi(recs, []))
    for name in (library.INDEX_FILE, library.RECORDS_FILE):
        data = json.load(open(tmp_path / name, encoding="utf-8"))
        del data["h"]["covered"]
        json.dump(data, open(tmp_path / name, "w", encoding="utf-8"))
    library.mark_changed(str(tmp_path), "x", 2, "protect", now=lambda: 10.0)
    api = ListApi(recs, [])
    _lib(tmp_path, 1001.0).current(api)
    assert ("pages",) in api.calls


def test_stale_fallback_applies_deletions_and_protect_flags(tmp_path):
    recs = [_r(1, 100, "A #1", isProtected=False), _r(2, 200, "B #1", isProtected=True), _r(3, 300, "C #1")]
    _lib(tmp_path, 1000.0).current(ListApi(recs, []))
    library.mark_changed(str(tmp_path), "x", 3, "delete", now=lambda: 1001.0)
    library.mark_changed(str(tmp_path), "x", 1, "protect", now=lambda: 1001.0)
    library.mark_changed(str(tmp_path), "x", 2, "unprotect", now=lambda: 1001.0)
    snap = _lib(tmp_path, 1002.0).current(DownApi(recs, []))
    assert snap.stale and [e["id"] for e in snap.entries] == [2, 1] and set(snap.records) == {1, 2}
    assert snap.records[1]["isProtected"] is True and snap.records[2]["isProtected"] is False
    snap = _lib(tmp_path, 1002.0).current(DownApi(recs, []), with_records=False)
    assert [e["id"] for e in snap.entries] == [2, 1]
    assert (tmp_path / library.CHANGED_FILE).exists()  # kept until a successful refresh


@pytest.mark.parametrize("content", ["{", "[]", '{"server": "x", "t": "soon"}', '{"server": "x", "t": 2000,'
                                     ' "deleted": "1", "protect": []}', '{"server": "x", "t": 2000, "deleted": ["a"]}'])
def test_bad_marker_file_is_ignored_safely(tmp_path, content):
    recs = [_r(1, 100, "A #1"), _r(2, 200, "B #1")]
    _lib(tmp_path, 1000.0).current(ListApi(recs, []))
    (tmp_path / library.CHANGED_FILE).write_text(content, encoding="utf-8")
    snap = _lib(tmp_path, 1001.0).current(ListApi(recs, []))
    assert [e["id"] for e in snap.entries] == [2, 1]
    snap = _lib(tmp_path, 1001.0).current(DownApi(recs, []))
    assert [e["id"] for e in snap.entries] == [2, 1]
    library.mark_changed(str(tmp_path), "x", 1, "delete", now=lambda: 1002.0)  # a bad file is replaced
    data = json.load(open(tmp_path / library.CHANGED_FILE, encoding="utf-8"))
    assert data["deleted"] == [1]


# --- api.py -------------------------------------------------------------------------------------------------------

def _spy(monkeypatch):
    import urllib.request as ur

    sent, real = [], ur.urlopen

    def spy(req, timeout=None):
        if not isinstance(req, str):
            sent.append((req.get_method(), req.full_url, req.get_header("Authorization"),
                         req.get_header("User-agent")))
        return real(req, timeout=timeout)

    monkeypatch.setattr(ur, "urlopen", spy)
    return sent


def _first_id(fake_server):
    return _get(fake_server, "/api/recorded?limit=1")["records"][0]["id"]


def test_api_protect_unprotect_delete(fake_server, handler, monkeypatch):
    sent = _spy(monkeypatch)
    client = api_mod.EPGStation(fake_server, "user", "pw")
    rid = _first_id(fake_server)
    assert client.protect_recorded(rid) == {"code": 200}
    assert sent[-1][0] == "PUT" and sent[-1][1] == f"{fake_server}/api/recorded/{rid}/protect"
    assert sent[-1][2].startswith("Basic ") and sent[-1][3] == api_mod.USER_AGENT
    with pytest.raises(api_mod.ApiError) as e:
        client.delete_recorded(rid)
    assert e.value.status == 500 and e.value.detail == "RecordedIsProtected"
    assert client.unprotect_recorded(rid) == {"code": 200}
    assert sent[-1][1].endswith(f"/api/recorded/{rid}/unprotect")
    assert client.delete_recorded(rid) == {"code": 200}
    assert sent[-1][0] == "DELETE" and sent[-1][1] == f"{fake_server}/api/recorded/{rid}"
    with pytest.raises(api_mod.ApiError) as e:
        client.delete_recorded(rid)
    assert e.value.detail == "RecordedIdIsNotFound"
    with pytest.raises(api_mod.ApiError) as e:
        client.protect_recorded(rid)
    assert e.value.detail == "RecordedIsNull"
    with pytest.raises(api_mod.ApiError) as e:
        client.recorded_item(rid)
    assert e.value.status == 404
    assert [w[:2] for w in handler.writes] == [
        ("PUT", f"/api/recorded/{rid}/protect"), ("DELETE", f"/api/recorded/{rid}"),
        ("PUT", f"/api/recorded/{rid}/unprotect"), ("DELETE", f"/api/recorded/{rid}"),
        ("DELETE", f"/api/recorded/{rid}"), ("PUT", f"/api/recorded/{rid}/protect")]


# --- fake server --------------------------------------------------------------------------------------------------

def _get(url, path):
    with urllib.request.urlopen(url + path) as resp:
        return json.loads(resp.read())


def _send(url, method, path):
    req = urllib.request.Request(url + path, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_fake_server_protect_and_delete_state(fake_server, handler):
    total = _get(fake_server, "/api/recorded?limit=1")["total"]
    rid = _first_id(fake_server)
    assert _send(fake_server, "PUT", f"/api/recorded/{rid}/protect") == (200, {"code": 200})
    assert _send(fake_server, "PUT", f"/api/recorded/{rid}/protect") == (200, {"code": 200})  # same state: no-op
    assert _get(fake_server, f"/api/recorded/{rid}")["isProtected"] is True
    assert _get(fake_server, "/api/recorded?limit=1")["records"][0]["isProtected"] is True
    err = _send(fake_server, "DELETE", f"/api/recorded/{rid}")
    assert err == (500, {"code": 500, "message": "Internal Server Error", "errors": "RecordedIsProtected"})
    assert _send(fake_server, "PUT", f"/api/recorded/{rid}/unprotect")[0] == 200
    assert _send(fake_server, "DELETE", f"/api/recorded/{rid}") == (200, {"code": 200})
    assert _get(fake_server, "/api/recorded?limit=1")["total"] == total - 1
    assert all(r["id"] != rid for r in _get(fake_server, "/api/recorded?limit=10000")["records"])
    assert _send(fake_server, "GET", f"/api/recorded/{rid}") == (404, {"code": 404, "message": "recorded is not Found"})
    assert _send(fake_server, "DELETE", f"/api/recorded/{rid}")[1]["errors"] == "RecordedIdIsNotFound"
    assert _send(fake_server, "PUT", "/api/recorded/999999/protect")[1]["errors"] == "RecordedIsNull"
    assert _send(fake_server, "DELETE", "/api/videos/999999")[1]["errors"] == "VideoFileIsNotFound"  # 018
    handler.write_error = (500, {"code": 500, "message": "Internal Server Error", "errors": "IPCTimeout"})
    assert _send(fake_server, "PUT", f"/api/recorded/{rid}/protect")[1]["errors"] == "IPCTimeout"
    handler.reset_reserves()
    assert _get(fake_server, "/api/recorded?limit=1")["total"] == total
    assert _get(fake_server, f"/api/recorded/{rid}")["isProtected"] is False


def test_fake_server_recording_protect_and_delete(fake_server, handler):
    rec = dict(_get(fake_server, "/api/recorded?limit=1")["records"][0], id=99901, isRecording=True)
    handler.recording_state.append(rec)
    assert _send(fake_server, "PUT", "/api/recorded/99901/protect")[0] == 200
    assert next(r for r in _get(fake_server, "/api/recording")["records"] if r["id"] == 99901)["isProtected"]
    assert _send(fake_server, "PUT", "/api/recorded/99901/unprotect")[0] == 200
    assert _send(fake_server, "DELETE", "/api/recorded/99901")[0] == 200  # stop and delete
    assert all(r["id"] != 99901 for r in _get(fake_server, "/api/recording")["records"])


# --- plugin.do_recorded_action ------------------------------------------------------------------------------------

def _action(op, rid):
    return f"action=recorded_action&op={op}&id={rid}"


def _target(fake_server, handler, protected=False):
    rec = _get(fake_server, "/api/recorded?limit=1")["records"][0]
    if protected:
        handler.recorded_protect[rec["id"]] = True
    return rec


OPS_CASES = [("protect", False, "PUT", "/protect", "Protect", "Protected"),
             ("unprotect", True, "PUT", "/unprotect", "Unprotect", "Protection removed"),
             ("delete", False, "DELETE", "", "Delete", "Recording deleted")]


@pytest.mark.parametrize("op,protected,method,suffix,label,done", OPS_CASES)
def test_recorded_action_no_writes_nothing(kodi, fake_server, handler, op, protected, method, suffix, label, done):
    import xbmc
    import xbmcgui

    rec = _target(fake_server, handler, protected)
    xbmcgui.yesno_answers.append(False)
    run_plugin(_action(op, rec["id"]))
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert not xbmcgui.notifications and xbmc.builtins == [] and kodi.ended is None


@pytest.mark.parametrize("op,protected,method,suffix,label,done", OPS_CASES)
def test_recorded_action_yes_writes_once(kodi, fake_server, handler, tmp_path, op, protected, method, suffix, label,
                                         done):
    import xbmc
    import xbmcgui
    import xbmcvfs

    rec = _target(fake_server, handler, protected)
    rid = rec["id"]
    total = _get(fake_server, "/api/recorded?limit=1")["total"]
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action(op, rid))
    y = xbmcgui.yesnos[-1]
    assert y["heading"] == label and y["yeslabel"] == label and y["nolabel"] == "Cancel"
    assert y["defaultbutton"] == xbmcgui.DLG_YESNO_NO_BTN
    lines = y["message"].split("\n")
    assert lines[0] == recorded.DEFAULT_STRINGS["q_" + op]
    assert lines[1] == recorded.one_line(items.to_half(rec["name"]))
    assert lines[2].startswith(recorded.time_line(rec))
    assert (op == "delete") == (len(lines) > 3 and lines[3].startswith("Files: "))
    assert handler.writes == [(method, f"/api/recorded/{rid}{suffix}", "")]
    assert xbmcgui.notifications[-1][1] == done and xbmcgui.notifications[-1][2] == xbmcgui.NOTIFICATION_INFO
    assert xbmc.builtins == ["Container.Refresh"]
    logs = [m for _, m in xbmc.logs if "[epgstation] recorded:" in m]
    assert logs == [f"[epgstation] recorded: {op} recorded {rid} ({items.to_half(rec['name'])})",
                    f"[epgstation] recorded: {op} recorded {rid} ({items.to_half(rec['name'])}) done"]
    marker = json.load(open(_changed_path(xbmcvfs), encoding="utf-8"))
    assert marker["server"] == fake_server
    if op == "delete":
        assert marker["deleted"] == [rid]
        assert _get(fake_server, "/api/recorded?limit=1")["total"] == total - 1
    else:
        assert marker["protect"] == {str(rid): op == "protect"}
        assert _get(fake_server, f"/api/recorded/{rid}")["isProtected"] is (op == "protect")


def _changed_path(xbmcvfs):
    return os.path.join(xbmcvfs.translatePath("special://profile/addon_data/plugin.video.epgstation/"),
                        library.CHANGED_FILE)


def _list_item(kodi, rid):
    return next((li for _, li, _ in kodi.items if li.props.get("epgstation.recordedId") == str(rid)), None)


def test_recorded_action_list_follows(kodi, fake_server, handler):
    """After a protect the list shows the marker and only 保護を解除; after unprotect + delete the item is gone."""
    import xbmcgui

    rid = _first_id(fake_server)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("protect", rid))
    kodi.reset()
    run_plugin("action=list")
    li = _list_item(kodi, rid)
    assert li.label.endswith("[COLOR gold][Protected][/COLOR]")
    assert [c[0] for c in li.context if "recorded_action" in c[1]] == ["Unprotect"]
    assert "Protected (not auto-deleted)" in li.tag.data["plot"]
    xbmcgui.yesno_answers.extend([True, True])
    run_plugin(_action("unprotect", rid))
    run_plugin(_action("delete", rid))
    kodi.reset()
    run_plugin("action=list")
    assert _list_item(kodi, rid) is None
    assert [w[0] for w in handler.writes] == ["PUT", "PUT", "DELETE"]


def test_recorded_action_marker_refreshes_movies_and_tvshows(kodi, fake_server, handler):
    """AC 5: the library cache is warm, a protect makes the next 映画 / TV番組 open refresh (one page request)."""
    import fake_epgstation
    import xbmcgui

    run_plugin("action=tvshows")
    kodi.reset()
    log = fake_epgstation.REQUESTS[fake_server]
    n = len(log)
    run_plugin("action=tvshows")
    assert not [q for p, q in log[n:] if p == "/api/recorded" and q.get("limit") == ["500"]]  # warm
    rid = _first_id(fake_server)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("protect", rid))
    kodi.reset()
    n = len(log)
    run_plugin("action=movies")
    assert len([q for p, q in log[n:] if p == "/api/recorded" and q.get("limit") == ["500"]]) == 1
    kodi.reset()
    n = len(log)
    run_plugin("action=tvshows")
    assert not [q for p, q in log[n:] if p == "/api/recorded" and q.get("limit") == ["500"]]  # warm again


def test_recorded_action_state_changed_before_the_dialog(kodi, fake_server, handler):
    import xbmc
    import xbmcgui

    rec = _target(fake_server, handler, protected=True)  # protected elsewhere since the list was made
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("delete", rec["id"]))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Protected: cannot delete"
    assert xbmc.builtins == ["Container.Refresh"]
    xbmc.builtins.clear()
    run_plugin(_action("protect", rec["id"]))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Recording has changed"
    assert xbmc.builtins == ["Container.Refresh"]


def test_refusal_marks_the_library_changed(kodi, fake_server, handler):
    """Codex P2b: protection changed elsewhere -> the warm library refreshes on its next open (marker, no op)."""
    import xbmcvfs

    rec = _target(fake_server, handler, protected=True)
    run_plugin(_action("delete", rec["id"]))
    marker = json.load(open(_changed_path(xbmcvfs), encoding="utf-8"))
    assert marker["server"] == fake_server and marker["deleted"] == [] and marker["protect"] == {}
    assert handler.writes == []


def test_gone_marks_the_recording_deleted(kodi, fake_server, handler):
    import xbmcvfs

    rec = _target(fake_server, handler)
    handler.recorded_deleted.add(rec["id"])  # deleted elsewhere
    run_plugin(_action("protect", rec["id"]))
    assert json.load(open(_changed_path(xbmcvfs), encoding="utf-8"))["deleted"] == [rec["id"]]
    assert handler.writes == []


@pytest.mark.parametrize("op,flip_to,want", [("delete", True, "Protected: cannot delete"),
                                             ("protect", True, "Recording has changed"),
                                             ("unprotect", False, "Recording has changed")])
def test_recorded_action_state_changes_while_the_dialog_is_open(kodi, fake_server, handler, op, flip_to, want):
    import xbmc
    import xbmcgui

    rec = _target(fake_server, handler, protected=op == "unprotect")
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: handler.recorded_protect.__setitem__(rec["id"], flip_to)
    run_plugin(_action(op, rec["id"]))
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert xbmcgui.notifications[-1][1] == want and xbmc.builtins == ["Container.Refresh"]


@pytest.mark.parametrize("patch", [{"isEncoding": True}, {"videoFiles": []}, "size"])
def test_recorded_action_different_recording_after_the_yes(kodi, fake_server, handler, patch):
    """Review M2: the re-read after the yes must be what the dialog showed (files, sizes, encoding)."""
    import xbmc
    import xbmcgui

    rec = _target(fake_server, handler)
    if patch == "size":
        patch = {"videoFiles": [dict(v, size=v["size"] + 1) for v in rec["videoFiles"]]}
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: handler.recorded_patch.__setitem__(rec["id"], patch)
    run_plugin(_action("delete", rec["id"]))
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Recording has changed" and xbmc.builtins == ["Container.Refresh"]


def test_recorded_action_protect_in_progress_while_the_file_grows(kodi, fake_server, handler):
    import xbmcgui

    rec = dict(_get(fake_server, "/api/recorded?limit=1")["records"][0], id=99901, isRecording=True)
    handler.recording_state.append(rec)
    grown = [dict(v, size=v["size"] + 1000) for v in rec["videoFiles"]]
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: handler.recorded_patch.__setitem__(99901, {"videoFiles": grown})
    run_plugin(_action("protect", 99901))
    assert handler.writes == [("PUT", "/api/recorded/99901/protect", "")]


def test_recorded_action_gone_while_the_dialog_is_open(kodi, fake_server, handler):
    import xbmcgui

    rec = _target(fake_server, handler)
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: handler.recorded_deleted.add(rec["id"])
    run_plugin(_action("delete", rec["id"]))
    assert handler.writes == [] and xbmcgui.notifications[-1][1] == "Recording is gone"


@pytest.mark.parametrize("op,second", [("delete", "Recording is gone"), ("protect", "Recording has changed")])
def test_recorded_action_double_confirm_writes_once(kodi, fake_server, handler, op, second):
    import xbmcgui

    rec = _target(fake_server, handler)
    xbmcgui.yesno_answers.extend([True, True])
    seen = []

    def second_click():
        xbmcgui.yesno_hook = None
        run_plugin(_action(op, rec["id"]))  # the second dialog, answered while the first is still open
        seen.append(list(handler.writes))

    xbmcgui.yesno_hook = second_click
    run_plugin(_action(op, rec["id"]))
    assert len(xbmcgui.yesnos) == 2 and len(handler.writes) == 1
    assert seen == [handler.writes]  # the inner (second) dialog wrote; the outer one saw it and wrote nothing
    assert xbmcgui.notifications[-1][1] == second


def test_recorded_action_missing_no_write(kodi, fake_server, handler):
    import xbmc
    import xbmcgui

    run_plugin(_action("delete", 999999))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Recording is gone" and xbmc.builtins == ["Container.Refresh"]


@pytest.mark.parametrize("query", ["action=recorded_action&op=delete", "action=recorded_action&op=x&id=1",
                                   "action=recorded_action&op=delete&id=x", "action=recorded_action&id=1"])
def test_recorded_action_bad_params(kodi, fake_server, handler, query):
    import xbmcgui

    run_plugin(query)
    assert handler.writes == [] and not xbmcgui.yesnos
    assert xbmcgui.notifications[-1][1] == "Invalid link" and kodi.ended is None


@pytest.mark.parametrize("errors,want,icon", [
    ("RecordedIsProtected", "Protected: cannot delete", "warning"),
    ("RecordedIdIsNotFound", "Recording is gone", "warning"),
    ("RecordedIsNull", "Recording is gone", "warning"),
    ("IPCTimeout", "Failed (connection error)", "error"),
    ("Other", "Failed: Other", "error"),
])
def test_recorded_action_server_error(kodi, fake_server, handler, errors, want, icon):
    import xbmc
    import xbmcgui
    import xbmcvfs

    rec = _target(fake_server, handler)
    handler.write_error = (500, {"code": 500, "message": "Internal Server Error", "errors": errors})
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("delete", rec["id"]))
    assert handler.writes == [("DELETE", f"/api/recorded/{rec['id']}", "")]
    assert xbmcgui.notifications[-1][1:] == (want, icon)
    assert xbmc.builtins == ["Container.Refresh"]
    failed = [m for _, m in xbmc.logs if "[epgstation] recorded:" in m and "failed" in m]
    assert len(failed) == 1 and errors in failed[0]
    assert ("may still have applied" in failed[0]) == (errors == "IPCTimeout")
    marker = json.load(open(_changed_path(xbmcvfs), encoding="utf-8"))
    # always a refresh; the deletion is recorded when it may have applied, or the server says it is gone (review L4)
    gone_or_maybe = errors in ("IPCTimeout", "RecordedIdIsNotFound", "RecordedIsNull")
    assert marker["deleted"] == ([rec["id"]] if gone_or_maybe else [])


def test_protect_answered_gone_marks_the_recording_deleted(kodi, fake_server, handler):
    import xbmcgui
    import xbmcvfs

    rec = _target(fake_server, handler)
    handler.write_error = (500, {"code": 500, "message": "Internal Server Error", "errors": "RecordedIsNull"})
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("protect", rec["id"]))
    marker = json.load(open(_changed_path(xbmcvfs), encoding="utf-8"))
    assert marker["deleted"] == [rec["id"]] and marker["protect"] == {}
    assert xbmcgui.notifications[-1][1] == "Recording is gone"


def test_recorded_action_write_timeout(kodi, fake_server, handler, monkeypatch):
    import xbmc
    import xbmcgui

    rec = _target(fake_server, handler)

    def timeout(self, recorded_id):
        raise api_mod.ApiError(f"/recorded/{recorded_id}/protect: timed out")

    monkeypatch.setattr(api_mod.EPGStation, "protect_recorded", timeout)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("protect", rec["id"]))
    assert xbmcgui.notifications[-1][1] == "Failed (connection error)"
    assert any("timed out" in m and "may still have applied" in m for _, m in xbmc.logs)
    assert xbmc.builtins == ["Container.Refresh"]


def test_recorded_action_unreachable_server(kodi, fake_server, handler):
    """AC 10: a dead server -> short error toast, no dialog, nothing written."""
    import xbmcaddon
    import xbmcgui

    xbmcaddon.settings["server_url"] = "http://127.0.0.1:1"
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("protect", 1))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1:] == ("Failed (connection error)", xbmcgui.NOTIFICATION_ERROR)


def test_recorded_action_read_fails_after_the_yes(kodi, fake_server, handler, monkeypatch):
    import xbmcgui

    rec = _target(fake_server, handler)
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: monkeypatch.setattr(api_mod.EPGStation, "recorded_item", _down)
    run_plugin(_action("delete", rec["id"]))
    assert handler.writes == [] and xbmcgui.notifications[-1][1] == "Failed (connection error)"


def _down(self, *a, **kw):
    raise api_mod.ApiError("/recorded: connection refused")


def test_recorded_action_encoding_line(kodi, fake_server, handler):
    import xbmcgui

    rec = _target(fake_server, handler)
    handler.recorded_patch[rec["id"]] = {"isEncoding": True}
    run_plugin(_action("delete", rec["id"]))
    assert xbmcgui.yesnos[-1]["message"].endswith(" (Encoding: will be cancelled)")


def test_recorded_action_in_progress(kodi, fake_server, handler):
    """録画中: protect works on an in-progress recording; delete is refused (no write)."""
    import xbmcgui

    rec = dict(_get(fake_server, "/api/recorded?limit=1")["records"][0], id=99901, isRecording=True)
    handler.recording_state.append(rec)
    run_plugin("action=recording")
    li = _list_item(kodi, 99901)
    assert [c[0] for c in li.context if "recorded_action" in c[1]] == ["Protect"]
    run_plugin(_action("delete", 99901))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Recording has changed"
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("protect", 99901))
    assert handler.writes == [("PUT", "/api/recorded/99901/protect", "")]


def test_delete_never_offered_for_protected_or_recording_items(kodi, fake_server, handler):
    for r in _get(fake_server, "/api/recorded?limit=10")["records"][::2]:
        handler.recorded_protect[r["id"]] = True
    run_plugin("action=list")
    for _, li, folder in kodi.items:
        if folder:
            continue
        ops = [c[1] for c in li.context if "recorded_action" in c[1]]
        prot = li.props["epgstation.isProtected"] == "true"
        live = "[COLOR red]" in li.label
        assert any("op=delete" in u for u in ops) == (not prot and not live), li.label
        assert any("op=unprotect" in u for u in ops) == prot
        assert ("[Protected]" in li.label) == prot


RECORDING_VIEWS = ["action=movies", "action=tvshows", "action=list", "action=recording", "action=channels",
                   "action=genres", "action=rules", "action=list&channelId=3211240960"]


@pytest.mark.parametrize("query", RECORDING_VIEWS)
def test_recording_views_are_not_disk_cached(kodi, query):
    run_plugin(query)
    assert kodi.ended is True and kodi.cache is False


def test_show_folder_not_disk_cached(kodi):
    run_plugin("action=tvshows")
    url = kodi.items[0][0]
    kodi.reset()
    run_plugin(url.split("?", 1)[1])
    assert kodi.ended is True and kodi.cache is False
    assert all(not li.label.startswith("[") or "●" in li.label for _, li, _ in kodi.items)


def test_tvshow_folders_have_no_entries_or_marker(kodi, fake_server, handler):
    for r in _get(fake_server, "/api/recorded?limit=10000")["records"]:
        handler.recorded_protect[r["id"]] = True
    run_plugin("action=tvshows")
    assert all(not li.context and "[Protected]" not in li.label for _, li, _ in kodi.items)


def _run(handle, query):
    from resources.lib.epgstation.plugin import Plugin

    Plugin(["plugin://plugin.video.epgstation/", str(handle), "?" + query, "resume:false"]).run()


def _results(kw):
    return items.plugin_url(action="search", keyword=kw)


def test_root_search_is_the_click_action():
    entry = items.root_menu({k: k for k in ("movies", "tvshows", "recent", "recording", "search", "browse")})[4]
    assert entry["path"] == items.plugin_url(action="search_ask") and entry["folder"] is False


@pytest.mark.parametrize("in_videos", [True, False])
def test_search_ask_click_opens_the_results_url(kodi, in_videos):
    """The root's 検索: Kodi runs a click with handle -1 and waits for no directory."""
    import xbmc
    import xbmcgui

    if in_videos:
        xbmc.visible.add("Window.IsActive(videos)")
    xbmcgui.input_answers.append("ニュース 7")
    _run(-1, "action=search_ask")
    assert kodi.ended is None and kodi.resolved is None
    want = f"Container.Update({_results('ニュース 7')})" if in_videos else \
        f"ActivateWindow(Videos,{_results('ニュース 7')},return)"
    assert xbmc.builtins == [want]


def test_search_ask_resolve_handle_resolves_false(kodi):
    """Review M3 / L5: a play or queue request (Kore, a favourite of the click item) has a resolve handle: never
    endOfDirectory, resolve False, then the results."""
    import xbmc
    import xbmcgui

    xbmcgui.input_answers.append("ニュース")
    _run(7, "action=search_ask")
    assert kodi.ended is None and kodi.resolved[0] is False
    assert xbmc.builtins == [f"ActivateWindow(Videos,{_results('ニュース')},return)"]


def test_search_ask_cancelled(kodi):
    import xbmc

    _run(-1, "action=search_ask")
    assert kodi.ended is None and xbmc.builtins == []
    _run(7, "action=search_ask")
    assert kodi.ended is None and kodi.resolved[0] is False and xbmc.builtins == []


def test_old_search_folder_link_lists_in_place(kodi, fake_server):
    """Review M3: an old 検索 link (favourite, skin shortcut, window start path: a directory handle) asks and lists
    in place, as before 006."""
    import xbmc
    import xbmcgui

    name = _get(fake_server, "/api/recorded?limit=1")["records"][0]["name"]
    xbmcgui.input_answers.append(items.to_half(name).split()[0][:4])
    _run(3, "action=search")
    assert kodi.ended is True and kodi.items and kodi.category == "Search" and kodi.cache is False
    assert xbmc.builtins == []


def test_old_search_folder_link_cancelled(kodi):
    import xbmc

    _run(3, "action=search")
    assert kodi.ended is False and xbmc.builtins == []


def test_old_search_link_run_as_a_script(kodi):
    import xbmc
    import xbmcgui

    xbmc.visible.add("Window.IsActive(videos)")
    xbmcgui.input_answers.append("a")
    _run(-1, "action=search")
    assert kodi.ended is None and xbmc.builtins == [f"Container.Update({_results('a')})"]


def test_search_with_keyword_lists_without_the_keyboard(kodi, fake_server):
    import xbmcgui

    name = _get(fake_server, "/api/recorded?limit=1")["records"][0]["name"]
    kw = items.to_half(name).split()[0][:4]
    run_plugin("action=search&keyword=" + urllib.parse.quote(kw))
    assert kodi.ended is True and kodi.items and kodi.cache is False
    assert xbmcgui.input_answers == [] and kodi.category == "Search"


def test_no_action_but_the_three_write_paths_writes(kodi, fake_server, handler):
    """Every listing (and its items' default action) with yes to anything: no write."""
    import xbmcgui
    from test_plugin import ALL_READ_ACTIONS

    for q in ALL_READ_ACTIONS + ["action=show&key=x", "action=search&keyword=a"]:
        kodi.reset()
        xbmcgui.yesno_answers[:] = [True] * 3
        run_plugin(q)
    run_plugin("action=list")
    rec_id = next(li for _, li, f in kodi.items if not f).props["epgstation.recordedId"]
    run_plugin(f"action=play&id={rec_id}")
    assert handler.writes == [] and not xbmcgui.yesnos


# --- grep rules (AC 1) --------------------------------------------------------------------------------------------

LIB = os.path.join(ROOT, "plugin.video.epgstation", "resources", "lib", "epgstation")


def test_recorded_module_has_no_kodi_imports_and_imports_items_only():
    src = _src("recorded.py")
    assert not re.search(r"^\s*(import|from)\s+xbmc", src, re.M)
    local_imports = re.findall(r"^\s*from\s+\.(\w*)\s+import\s+(.+)$", src, re.M)
    assert {m or i.strip() for m, i in local_imports} == {"items"}


def test_api_recorded_writes():
    src = _src("api.py")
    methods = _api_methods(src)
    assert src.count('"PUT"') == 1 and '"PUT"' in methods["put"]
    # transcoded playback (011): keep_stream is the only other PUT
    assert _callers(src, r"\bself\.put\(") == {"protect_recorded", "unprotect_recorded", "keep_stream"}
    assert src.count("self.put(") == 3
    assert "self.delete(" in methods["delete_recorded"]
    assert re.search(r'f"/recorded/\{int\(recorded_id\)\}/protect"', methods["protect_recorded"])
    assert re.search(r'f"/recorded/\{int\(recorded_id\)\}/unprotect"', methods["unprotect_recorded"])


def _addon_sources():
    return {name: _src(name) for name in os.listdir(LIB) if name.endswith(".py") and name != "api.py"}


def _method_body(src, name):
    return src.split(f"    def {name}(", 1)[1].split("\n    def ", 1)[0]


def test_recorded_writes_are_called_only_from_do_recorded_action():
    hits = {}
    for name, src in _addon_sources().items():
        for m in re.finditer(r"\.(protect_recorded|unprotect_recorded|delete_recorded)\(|\bapi\.(put)\(", src):
            hits.setdefault(name, []).append(m.group(1) or m.group(2))
    assert hits == {"plugin.py": ["protect_recorded", "unprotect_recorded", "delete_recorded"]}  # 018: below
    plugin = _src("plugin.py")
    body = _method_body(plugin, "do_recorded_action") + _method_body(plugin, "_recorded_write")
    assert len(re.findall(r"\.(protect_recorded|unprotect_recorded|delete_recorded)\(", body)) == 3
    write = _method_body(plugin, "_recorded_write")
    assert "_recorded_write(" in _method_body(plugin, "do_recorded_action")
    assert _callers(plugin, r"\b_recorded_write\(") == {"do_recorded_action"}
    assert re.search(r"\.(protect_recorded|unprotect_recorded|delete_recorded)\(", write)


def test_pure_modules_have_no_write_call():
    for name in ("items.py", "recorded.py", "library.py", "guide.py", "live.py", "reserves.py", "transcode.py"):
        src = _src(name)
        assert not re.search(r"\.(cancel_reserve|remove_skip|remove_overlap|delete|post|put|create_reserve|"
                             r"protect_recorded|unprotect_recorded|delete_recorded|delete_video_file|"
                             r"start_recorded_hls|keep_stream|stop_stream)\(", src), name
        assert "urlopen" not in src and '"POST"' not in src and '"PUT"' not in src, name


def test_strings_in_both_languages():
    for lang in ("en_gb", "ja_jp"):
        path = os.path.join(ROOT, "plugin.video.epgstation", "resources", "language", f"resource.language.{lang}",
                            "strings.po")
        ids = set(re.findall(r'msgctxt "#(\d+)"', open(path, encoding="utf-8").read()))
        assert {str(i) for i in range(30072, 30088)} <= ids, lang


def test_strings_po_match_the_defaults():
    """en_gb msgids = recorded.DEFAULT_STRINGS; ja msgstrs = the plan's words (JA above)."""
    from resources.lib.epgstation import plugin

    path = os.path.join(ROOT, "plugin.video.epgstation", "resources", "language", "resource.language.ja_jp",
                        "strings.po")
    found = {int(i): (en, ja) for i, en, ja in re.findall(r'msgctxt "#(\d+)"\nmsgid "([^"]*)"\nmsgstr "([^"]*)"',
                                                          open(path, encoding="utf-8").read())}
    for key, sid in plugin.RECORDED_STRINGS:
        assert found[sid][0] == recorded.DEFAULT_STRINGS[key], key
        assert found[sid][1] == JA[key], key


# --- smoke --------------------------------------------------------------------------------------------------------

def test_smoke_counts_recorded_lines():
    import kodi_smoke

    text = ("2026-10-12 10:00:01.000 T:1 info <general>: [epgstation] reserve: add program 5 (P)\n"
            "2026-10-12 10:00:02.000 T:1 info <general>: [epgstation] recorded: delete recorded 3 (x)\n"
            "2026-10-12 10:00:03.000 T:1 info <general>: [epgstation] live: channel 1\n")
    assert len(kodi_smoke.kodi_write_lines(text)) == 2


# --- tools/dev/test_reserve.py (006 DEV protocol) -----------------------------------------------------------------

NOW = 1_800_000_000_000
MINE = {"id": 5, "channelId": 3211240960, "programId": None, "startAt": NOW - 10 * MIN, "endAt": NOW - 8 * MIN}


class _RecDev(_FakeDev):
    """GET /recorded (all, or by channel), /recording, /recorded/{id}; PUT / DELETE recorded; every call recorded."""

    def __init__(self, recorded=None, recording=None):
        super().__init__({}, recorded or {})
        self.recording = recording or []

    def call(self, method, path, body=None, **params):
        if method == "GET" and path == "/recorded":
            self.sent.append((method, path))
            recs = [r for r in self.recorded.values() if "channelId" not in params
                    or r["channelId"] == params["channelId"]]
            return {"records": recs, "total": len(recs)}
        if method == "GET" and path == "/recording":
            self.sent.append((method, path))
            return {"records": self.recording, "total": len(self.recording)}
        if method == "PUT":
            self.sent.append((method, path))
            rid = int(path.split("/")[2])
            self.recorded[rid]["isProtected"] = path.endswith("/protect")
            return {"code": 200}
        return super().call(method, path, body, **params)


def _vf(i, size=1000):
    return {"id": i, "name": "TS", "type": "ts", "size": size}


def _demo(i, **kw):
    return dict({"id": i, "channelId": 3211240960, "startAt": NOW - 900 * MIN, "endAt": NOW - 870 * MIN,
                 "ruleId": 4, "programId": 1000 + i, "isRecording": False, "isProtected": False,
                 "videoFiles": [_vf(i * 10)]}, **kw)


def _own(rid=70, **kw):
    return dict({"id": rid, "channelId": MINE["channelId"], "startAt": MINE["startAt"], "endAt": MINE["endAt"],
                 "isRecording": False, "isProtected": False, "videoFiles": [_vf(700)]}, **kw)


def _tool(tmp_path, monkeypatch, entries=(MINE,)):
    tool = _test_reserve_tool()
    monkeypatch.setattr(tool, "STATE", str(tmp_path / "state.json"))
    tool.save_state({"reserves": [dict(e) for e in entries]})
    return tool


def _snap(tool, dev):
    tool.recorded_snapshot(dev)
    state = tool.load_state()
    state["recordedSnapshot"]["at"] = NOW - 20 * MIN
    tool.save_state(state)


def test_recorded_snapshot_stores_flags_files_and_recording(tmp_path, monkeypatch):
    tool = _tool(tmp_path, monkeypatch)
    dev = _RecDev({1: _demo(1), 2: _demo(2, isProtected=True, videoFiles=[_vf(20), _vf(21)])}, [{"id": 3}])
    tool.recorded_snapshot(dev)
    snap = tool.load_state()["recordedSnapshot"]
    assert snap["recorded"] == {"1": {"isProtected": False, "files": [10]}, "2": {"isProtected": True,
                                                                                 "files": [20, 21]}}
    assert snap["recording"] == [3] and snap["at"] > 0
    assert all(c[0] == "GET" for c in dev.sent)


def test_wait_recorded_polls_until_finished(tmp_path, monkeypatch):
    end = NOW + 2 * MIN
    tool = _tool(tmp_path, monkeypatch, [dict(MINE, endAt=end)])
    dev = _RecDev({70: _own(endAt=end, isRecording=True, videoFiles=[_vf(700, 0)])})
    clock = [NOW]

    def sleep(s):
        clock[0] += s * 1000
        if clock[0] >= NOW + 30 * 1000:
            dev.recorded[70] = _own(endAt=end)

    assert tool.wait_recorded(dev, 5, now=lambda: clock[0], sleep=sleep) == 70
    assert all(c[0] == "GET" for c in dev.sent)
    with pytest.raises(SystemExit):
        tool.wait_recorded(dev, 6, now=lambda: clock[0], sleep=sleep)  # not a reservation of the tool


def test_wait_recorded_gives_up_after_end_plus_3_min(tmp_path, monkeypatch):
    tool = _tool(tmp_path, monkeypatch)
    dev = _RecDev({})
    clock = [NOW]

    def sleep(s):
        clock[0] += s * 1000

    with pytest.raises(SystemExit):
        tool.wait_recorded(dev, 5, now=lambda: clock[0], sleep=sleep)
    assert clock[0] == NOW  # endAt + 3 min had passed already: one poll, no wait
    late = dict(MINE, id=6, endAt=NOW + 2 * MIN)
    tool.save_state({"reserves": [late]})
    with pytest.raises(SystemExit):
        tool.wait_recorded(dev, 6, now=lambda: clock[0], sleep=sleep)
    assert NOW + 5 * MIN <= clock[0] <= NOW + 5 * MIN + 15 * 1000


def test_adopt_recorded_refusals(tmp_path, monkeypatch):
    tool = _tool(tmp_path, monkeypatch)
    dev = _RecDev({1: _demo(1)})
    with pytest.raises(SystemExit):
        tool.adopt_recorded(dev, 70)  # no snapshot
    _snap(tool, dev)
    with pytest.raises(SystemExit):
        tool.adopt_recorded(dev, 1)  # in the snapshot
    dev.recorded[71] = _own(71, ruleId=4)  # a rule recording at the same time
    dev.recorded[72] = _own(72, isProtected=True)
    dev.recorded[73] = _own(73, isRecording=True)
    dev.recorded[74] = _own(74, startAt=MINE["startAt"] + MIN)
    for rid in (71, 72, 73, 74, 99):
        with pytest.raises(SystemExit):
            tool.adopt_recorded(dev, rid)
    assert "recorded" not in tool.load_state() or tool.load_state()["recorded"] == []
    dev.recorded[70] = _own()
    tool.adopt_recorded(dev, 70)
    assert tool.load_state()["recorded"] == [70]
    assert all(c[0] == "GET" for c in dev.sent)


def test_adopt_recorded_refuses_an_id_in_progress_at_the_snapshot(tmp_path, monkeypatch):
    tool = _tool(tmp_path, monkeypatch)
    dev = _RecDev({}, [{"id": 70}])
    _snap(tool, dev)
    dev.recorded[70] = _own()
    with pytest.raises(SystemExit):
        tool.adopt_recorded(dev, 70)


def _access(ts, method, path, ua="KODIstation/1 (plugin.video.epgstation)"):
    return f'[{ts}] [INFO] access - ::ffff:127.0.0.1 - - "{method} {path} HTTP/1.1" 200 12 "" "{ua}"'


def _kodi(ts, text):
    return f"{ts} T:1    info <general>: [epgstation] {text}"


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000)) + ".000"


def _kts(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) + ".000"


def _audit_setup(tmp_path, monkeypatch, access=(), kodi=()):
    tool = _tool(tmp_path, monkeypatch)
    dev = _RecDev({1: _demo(1), 2: _demo(2, isProtected=True)})
    _snap(tool, dev)
    dev.recorded[70] = _own()
    tool.adopt_recorded(dev, 70)
    access = list(access) + [_access(_iso(NOW - 4 * MIN), "GET", "/api/recorded?limit=1")]  # the self-check's GET
    monkeypatch.setattr(tool, "read_access_log", lambda dev, since_ms=0: "\n".join(access) + "\n")
    monkeypatch.setattr(tool, "read_kodi_log", lambda dev: "\n".join(kodi) + "\n")
    return tool, dev


def test_audit_passes_for_writes_on_the_adopted_recording(tmp_path, monkeypatch, capsys):
    t = NOW - 5 * MIN
    access = [_access(_iso(t), "PUT", "/api/recorded/70/protect"), _access(_iso(t), "DELETE", "/api/recorded/70"),
              _access(_iso(t), "GET", "/api/recorded/1"),
              _access(_iso(t), "POST", "/api/reserves", ua="Python-urllib/3.12"),  # the tool: reported only
              _access(_iso(NOW - 60 * MIN), "DELETE", "/api/recorded/1")]  # before the snapshot
    kodi = [_kodi(_kts(t), "recorded: protect recorded 70 (KODIstation test) done"),
            _kodi(_kts(NOW - 60 * MIN), "recorded: delete recorded 1 (x)")]
    tool, dev = _audit_setup(tmp_path, monkeypatch, access, kodi)
    del dev.recorded[70]
    assert tool.audit(dev) is True
    assert "PASS" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["flag", "files", "gone", "access", "kodi"])
def test_audit_fails(tmp_path, monkeypatch, capsys, change):
    t = NOW - 5 * MIN
    access = [_access(_iso(t), "PUT", "/api/recorded/1/protect")] if change == "access" else []
    kodi = [_kodi(_kts(t), "recorded: delete recorded 2 (x) done")] if change == "kodi" else []
    tool, dev = _audit_setup(tmp_path, monkeypatch, access, kodi)
    if change == "flag":
        dev.recorded[1]["isProtected"] = True
    elif change == "files":
        dev.recorded[2]["videoFiles"] = []
    elif change == "gone":
        del dev.recorded[1]
    assert tool.audit(dev) is False
    assert "FAIL" in capsys.readouterr().out


@pytest.mark.parametrize("broken", ["access", "kodi", "no_get"])
def test_audit_fails_without_evidence(tmp_path, monkeypatch, capsys, broken):
    """Review M4: an unreadable log, or no KODIstation/ GET after the snapshot (wrong log, broken regex), is a FAIL."""
    tool, dev = _audit_setup(tmp_path, monkeypatch)
    if broken == "access":
        monkeypatch.setattr(tool, "read_access_log", lambda dev, since_ms=0: None)
    elif broken == "kodi":
        monkeypatch.setattr(tool, "read_kodi_log", lambda dev: None)
    else:
        old = _access(_iso(NOW - 60 * MIN), "GET", "/api/recorded/1")  # before the snapshot only
        monkeypatch.setattr(tool, "read_access_log", lambda dev, since_ms=0: old + "\n")
    assert tool.audit(dev) is False
    assert "FAIL" in capsys.readouterr().out


def test_read_access_log_takes_rotated_files_newer_than_the_snapshot(tmp_path, monkeypatch):
    tool = _test_reserve_tool()
    logs = tmp_path / "Service"
    logs.mkdir()
    for name, text, age in (("access.log", "now\n", 0), ("access.log.-2026-10-12.1", "rotated\n", 60),
                            ("access.log.-2026-10-10.1", "old\n", 3 * 86400), ("encode.log", "enc\n", 0)):
        (logs / name).write_text(text, encoding="utf-8")
        t = time.time() - age
        os.utime(logs / name, (t, t))
    monkeypatch.setattr(tool, "ACCESS_LOG", str(logs / "access.log"))
    monkeypatch.setenv("EPGS_DEV_URL", "http://epgs-dev.test:8888")  # 019: dev tools need both URLs
    monkeypatch.setenv("EPGS_PROD_URL", "http://epgs-prod.test:9984")
    monkeypatch.setattr(tool, "_addresses", lambda host: set())  # no DNS in tests
    monkeypatch.setattr(tool, "_on_vm", lambda: True)  # these run on the VM (011 review nit 6)
    dev = tool.Dev("http://localhost:8888")
    text = tool.read_access_log(dev, (time.time() - 3600) * 1000)
    assert text.splitlines() == ["rotated", "now"]  # oldest first; the 2-day-old file and encode.log left out
    monkeypatch.setattr(tool, "ACCESS_LOG", str(tmp_path / "missing" / "access.log"))
    assert tool.read_access_log(dev, 0) is None


def test_read_kodi_log_reads_old_and_current_or_none(tmp_path, monkeypatch):
    tool = _test_reserve_tool()
    (tmp_path / "kodi.old.log").write_text("old\n", encoding="utf-8")
    (tmp_path / "kodi.log").write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(tool, "KODI_LOGS", (str(tmp_path / "kodi.old.log"), str(tmp_path / "kodi.log")))
    monkeypatch.setenv("EPGS_DEV_URL", "http://epgs-dev.test:8888")  # 019: dev tools need both URLs
    monkeypatch.setenv("EPGS_PROD_URL", "http://epgs-prod.test:9984")
    monkeypatch.setattr(tool, "_addresses", lambda host: set())  # no DNS in tests
    monkeypatch.setattr(tool, "_on_vm", lambda: True)  # these run on the VM (011 review nit 6)
    dev = tool.Dev("http://localhost:8888")
    assert tool.read_kodi_log(dev).split() == ["old", "new"]
    (tmp_path / "kodi.log").unlink()
    assert tool.read_kodi_log(dev) is None  # kodi.log itself must be readable


def test_audit_without_snapshot_fails(tmp_path, monkeypatch):
    tool = _tool(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        tool.audit(_RecDev({}))


def test_unprotect_recorded_only_adopted(tmp_path, monkeypatch):
    tool, dev = _audit_setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        tool.unprotect_recorded(dev, 2)  # a protected demo recording: never
    assert not [c for c in dev.sent if c[0] != "GET"]
    dev.recorded[70]["isProtected"] = True
    tool.unprotect_recorded(dev, 70)
    assert [c for c in dev.sent if c[0] != "GET"] == [("PUT", "/recorded/70/unprotect")]
    dev.recorded[70]["ruleId"] = 4  # the id is not our recording any more
    dev.recorded[70]["isProtected"] = True
    with pytest.raises(SystemExit):
        tool.unprotect_recorded(dev, 70)


def test_delete_recorded_accepts_adopted_but_never_a_snapshot_one(tmp_path, monkeypatch):
    tool, dev = _audit_setup(tmp_path, monkeypatch)
    dev.recorded[1].update(channelId=MINE["channelId"], startAt=MINE["startAt"], endAt=MINE["endAt"], ruleId=None,
                           programId=None)  # looks like ours, but it was there before the snapshot
    with pytest.raises(SystemExit):
        tool.delete_recorded(dev, 1)
    dev.recorded[70]["isProtected"] = True
    with pytest.raises(SystemExit):
        tool.delete_recorded(dev, 70)  # protected: refused
    dev.recorded[70]["isProtected"] = False
    tool.delete_recorded(dev, 70)
    assert [c for c in dev.sent if c[0] == "DELETE"] == [("DELETE", "/recorded/70")]
    assert tool.load_state()["recorded"] == []


def test_kodi_recorded_pattern_matches_the_plugin_log():
    tool = _test_reserve_tool()
    plugin = _src("plugin.py")
    assert 'f"[epgstation] recorded: {what}' in plugin
    assert "what = f\"{op} recorded {rec['id']} ({items.to_half(rec['name'])})\"" in plugin
    lines = tool.kodi_recorded(_kodi(_kts(NOW), "recorded: unprotect recorded 70 (KODIstation test) done") + "\n"
                               + _kodi(_kts(NOW), "reserve: cancel reserve 3 (x)"))
    assert lines == [(NOW, "unprotect", 70)]


def test_access_writes_pattern():
    tool = _test_reserve_tool()
    text = "\n".join([_access("2026-10-12T21:00:00.123", "PUT", "/api/recorded/70/protect"),
                      _access("2026-10-12T21:00:01.000", "GET", "/api/recorded/70"),
                      _access("2026-10-12T21:00:02.000", "DELETE", "/api/recorded/70", ua="Mozilla")])
    got = tool.access_writes(text)
    assert [(m, p, k) for _, m, p, k in got] == [("PUT", "/api/recorded/70/protect", True),
                                                 ("DELETE", "/api/recorded/70", False)]


# === delete a file (018) ==========================================================================================

def _two(**kw):
    """Dev recording 9 (TS #10, H.264 #13) at a fixed local time."""
    return rec_of(startAt=local(2026, 10, 10, 16, 0), endAt=local(2026, 10, 10, 17, 0), **kw)


def test_can_delete_file():
    files = rec_of()["videoFiles"]
    assert recorded.can("delete_file", rec_of())
    assert not recorded.can("delete_file", rec_of(videoFiles=files[:1]))
    assert not recorded.can("delete_file", rec_of(videoFiles=[]))
    assert not recorded.can("delete_file", rec_of(videoFiles=None))
    assert not recorded.can("delete_file", rec_of(isProtected=True))
    assert not recorded.can("delete_file", rec_of(isRecording=True))
    assert not recorded.can("delete_file", rec_of(isEncoding=True))  # decision 2a
    assert recorded.can("delete_file", rec_of(videoFiles=files + [dict(files[0], id=99)]))
    assert recorded.actions_for(rec_of(isProtected=True)) == ["unprotect"]
    assert recorded.actions_for(rec_of(isEncoding=True)) == ["protect", "delete"]
    assert recorded.refusal("delete_file", rec_of(isProtected=True), JA) == "保護されているため削除できません"
    assert recorded.refusal("delete_file", rec_of(isEncoding=True), JA) == "録画の状態が変わりました"


def test_delete_file_choices_in_server_order():
    rec = rec_of()
    assert [v["id"] for v in recorded.delete_file_choices(rec)] == [v["id"] for v in rec["videoFiles"]] == [10, 13]
    four = _fixture("dev", 4)
    assert [v["id"] for v in recorded.delete_file_choices(four)] == [4, 6]
    assert recorded.delete_file_choices(rec_of(isEncoding=True)) == []
    assert recorded.delete_file_choices(rec_of(videoFiles=rec["videoFiles"][:1])) == []


def test_confirm_file_message():
    rec = _two()
    name = recorded.one_line(items.to_half(rec["name"]))
    assert recorded.confirm_file_message(rec, 10, "MBS", JA).split("\n") == [
        "このファイルを削除しますか? 元に戻せません。", name, "10/10(土) 16:00-17:00 MBS",
        "削除: TS 136.8 MB / 残す: H.264 5.0 MB"]
    assert recorded.confirm_file_message(rec, 13, "MBS", JA).split("\n")[-1] == "削除: H.264 5.0 MB / 残す: TS 136.8 MB"
    en = recorded.confirm_file_message(rec, 10, "MBS", {}).split("\n")
    assert en[0] == "Delete this file? It cannot be recovered." and en[-1] == "Delete: TS 136.8 MB / Keep: H.264 5.0 MB"
    assert en[2] == "Sat 10/10 16:00-17:00 MBS"
    four = dict(_fixture("dev", 4), startAt=local(2026, 10, 10, 14, 50), endAt=local(2026, 10, 10, 15, 0))
    msg = recorded.confirm_file_message(four, 4, "NHK", JA).split("\n")
    assert len(msg) == 4 and msg[-1] == "削除: TS 512.0 MB / 残す: H.264 37.5 MB"
    assert recorded.confirm_file_message(four, 6, "NHK", JA).endswith("削除: H.264 37.5 MB / 残す: TS 512.0 MB")
    three = dict(rec, videoFiles=rec["videoFiles"] + [{"id": 99, "name": "H.265", "type": "encoded", "size": 1048576}])
    want = "削除: H.264 5.0 MB / 残す: TS 136.8 MB, H.265 1.0 MB"
    assert recorded.confirm_file_message(three, 13, "X", JA).endswith(want)


def test_error_message_and_refusal_for_a_missing_file():
    assert recorded.error_message(500, "VideoFileIsNotFound", JA) == "このファイルはもうありません"
    assert recorded.error_message(500, "VideoFileIsNotFound") == "File is gone"
    assert recorded.is_refusal("VideoFileIsNotFound") and recorded.is_refusal("RecordedIsProtected")
    assert not recorded.is_refusal("IPCTimeout") and not recorded.is_refusal(None)


def test_removal_checklist_names_every_018_hook():
    """recorded.py's docstring names every file with a "delete a file (018)" hook."""
    files = []
    for top in ("plugin.video.epgstation", "tools", "tests"):
        files += [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(ROOT, top)) for f in fs
                  if f.endswith((".py", ".xml", ".po")) and "fixtures" not in d and "__pycache__" not in d]
    me = os.path.abspath(recorded.__file__)
    hooks = {os.path.basename(f) for f in files
             if os.path.abspath(f) != me and "delete a file (018)" in open(f, encoding="utf-8").read()}
    assert {"plugin.py", "items.py", "api.py", "fake_epgstation.py", "test_reserve.py", "test_recorded.py"} <= hooks
    section = recorded.__doc__.split("Delete a file (018)", 1)[1]
    missing = sorted(h for h in hooks if h not in section)
    assert not missing, missing


def _dev_only(handler):
    if os.path.basename(handler.root) != "dev":
        pytest.skip("only the dev fixtures have two-file recordings")


def test_api_delete_video_file(fake_server, handler, monkeypatch):
    _dev_only(handler)
    sent = _spy(monkeypatch)
    client = api_mod.EPGStation(fake_server, "user", "pw")
    assert client.delete_video_file(13) == {"code": 200}
    assert sent[-1][0] == "DELETE" and sent[-1][1] == f"{fake_server}/api/videos/13"
    assert sent[-1][2].startswith("Basic ") and sent[-1][3] == api_mod.USER_AGENT
    with pytest.raises(api_mod.ApiError) as e:
        client.delete_video_file(13)
    assert e.value.status == 500 and e.value.detail == "VideoFileIsNotFound"
    assert [w[:2] for w in handler.writes] == [("DELETE", "/api/videos/13")] * 2


def test_fake_server_delete_video_file(fake_server, handler):
    _dev_only(handler)
    total = _get(fake_server, "/api/recorded?limit=1")["total"]
    handler.recorded_protect[9] = True
    assert _send(fake_server, "DELETE", "/api/videos/10")[1]["errors"] == "RecordedIsProtected"
    handler.recorded_protect[9] = False
    assert _send(fake_server, "DELETE", "/api/videos/10") == (200, {"code": 200})
    assert [v["id"] for v in _get(fake_server, "/api/recorded/9")["videoFiles"]] == [13]
    listed = next(r for r in _get(fake_server, "/api/recorded?limit=10000")["records"] if r["id"] == 9)
    assert [v["id"] for v in listed["videoFiles"]] == [13]
    assert _get(fake_server, "/api/recorded?limit=1")["total"] == total
    assert _send(fake_server, "DELETE", "/api/videos/10")[1]["errors"] == "VideoFileIsNotFound"
    assert _send(fake_server, "DELETE", "/api/videos/13") == (200, {"code": 200})  # the last file: the recording goes
    assert _send(fake_server, "GET", "/api/recorded/9")[0] == 404
    assert _get(fake_server, "/api/recorded?limit=1")["total"] == total - 1
    live = dict(_fixture("dev", 11), id=99901, isRecording=True)
    handler.recording_state.append(live)
    vid = live["videoFiles"][0]["id"]
    handler.video_deleted.discard(vid)
    assert _send(fake_server, "DELETE", f"/api/videos/{vid}")[0] == 200  # in progress: stopped and deleted
    assert all(r["id"] != 99901 for r in _get(fake_server, "/api/recording")["records"])
    handler.reset_state()
    assert [v["id"] for v in _get(fake_server, "/api/recorded/9")["videoFiles"]] == [10, 13]
    handler.multiply = 2  # copies share video file ids: refused, nothing changes
    assert _send(fake_server, "DELETE", "/api/videos/10")[0] == 405
    assert [v["id"] for v in _get(fake_server, f"/api/recorded/{9 + 100000}")["videoFiles"]] == [10, 13]
    assert [v["id"] for v in _get(fake_server, "/api/recorded/9")["videoFiles"]] == [10, 13]


def _file_action(rid=9):
    return _action("delete_file", rid)


def test_delete_file_select_back_does_nothing(kodi, fake_server, handler):
    import xbmc
    import xbmcgui

    _dev_only(handler)
    run_plugin(_file_action())
    heading, labels = xbmcgui.selects[-1]
    assert heading == "Choose the file to delete"
    assert [x.split("  ")[0] for x in labels] == ["TS (ts) 136.8 MB", "H.264 (encoded) 5.0 MB"]
    assert not xbmcgui.yesnos and handler.writes == [] and not xbmcgui.notifications and xbmc.builtins == []


def test_delete_file_no_writes_nothing(kodi, fake_server, handler):
    import xbmc
    import xbmcgui

    _dev_only(handler)
    xbmcgui.select_answers.append(0)
    xbmcgui.yesno_answers.append(False)
    run_plugin(_file_action())
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert not xbmcgui.notifications and xbmc.builtins == []


@pytest.mark.parametrize("idx,vid,keep", [(0, 10, 13), (1, 13, 10)])
def test_delete_file_yes_writes_once(kodi, fake_server, handler, idx, vid, keep):
    import xbmc
    import xbmcgui
    import xbmcvfs

    _dev_only(handler)
    rec = _get(fake_server, "/api/recorded/9")
    xbmcgui.select_answers.append(idx)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_file_action())
    y = xbmcgui.yesnos[-1]
    assert y["heading"] == "Delete a file" and y["yeslabel"] == "Delete" and y["nolabel"] == "Cancel"
    assert y["defaultbutton"] == xbmcgui.DLG_YESNO_NO_BTN
    lines = y["message"].split("\n")
    assert lines[0] == "Delete this file? It cannot be recovered."
    assert lines[1] == recorded.one_line(items.to_half(rec["name"])) and lines[2].startswith(recorded.time_line(rec))
    files = {v["id"]: v for v in rec["videoFiles"]}
    assert lines[3] == (f"Delete: {files[vid]['name']} {items.fmt_size(files[vid]['size'])} / "
                        f"Keep: {files[keep]['name']} {items.fmt_size(files[keep]['size'])}")
    assert handler.writes == [("DELETE", f"/api/videos/{vid}", "")]
    assert xbmcgui.notifications[-1][1:] == ("File deleted", xbmcgui.NOTIFICATION_INFO)
    assert xbmc.builtins == ["Container.Refresh"]
    what = f"delete_file recorded 9 ({items.to_half(rec['name'])}) file {vid} ({files[vid]['name']})"
    assert [m for _, m in xbmc.logs if "[epgstation] recorded:" in m] == [f"[epgstation] recorded: {what}",
                                                                          f"[epgstation] recorded: {what} done"]
    marker = json.load(open(_changed_path(xbmcvfs), encoding="utf-8"))
    assert marker["server"] == fake_server and marker["deleted"] == [] and marker["protect"] == {}  # refresh only
    assert [v["id"] for v in _get(fake_server, "/api/recorded/9")["videoFiles"]] == [keep]
    kodi.reset()
    run_plugin("action=list")
    li = _list_item(kodi, 9)
    # no 別のファイルを選ぶ, no ファイルを削除 (011: 変換して再生 first)
    assert [c[0] for c in li.context] == ["Stream transcoded", "Protect", "Delete"]
    gone, kept = files[vid], files[keep]
    files_lines = [ln for ln in li.tag.data["plot"].split("\n") if ln.startswith("Files: ")]  # 002: in the block
    assert files_lines == [f"Files: {kept['name']} {items.fmt_size(kept['size'])}"] and gone["id"] != kept["id"]


@pytest.mark.parametrize("view", ["list", "channel", "search"])
def test_delete_file_entry_only_on_unprotected_finished_two_file_items(kodi, fake_server, handler, view):
    """Prod has no two-file recording: no entry in any view. Dev: 9 (and 11, 4 where listed) has it in 最近の録画, its
    チャンネル list and 検索 results until it is protected / encoding."""
    dev = os.path.basename(handler.root) == "dev"
    target = _get(fake_server, "/api/recorded/9") if dev else _get(fake_server, "/api/recorded?limit=1")["records"][0]
    keyword = urllib.parse.quote(items.to_half(target["name"]).split()[0][:4])
    query = {"list": "action=list", "channel": f"action=list&channelId={target['channelId']}",
             "search": f"action=search&keyword={keyword}"}[view]

    def entries():
        kodi.reset()
        run_plugin(query)
        shown = {int(li.props["epgstation.recordedId"]) for _, li, f in kodi.items if not f}
        return shown, {int(li.props["epgstation.recordedId"]) for _, li, f in kodi.items
                       if not f and any("op=delete_file" in c[1] for c in li.context)}

    two = {r["id"] for r in _get(fake_server, "/api/recorded?limit=10000")["records"]
           if len(r.get("videoFiles") or []) >= 2}
    shown, listed = entries()
    assert target["id"] in shown, view
    assert listed <= two
    if not dev:
        assert listed == set()
        return
    assert {9, 11, 4} & listed == {9, 11, 4} & shown
    assert 9 in listed
    handler.recorded_protect[9] = True
    handler.recorded_patch[11] = {"isEncoding": True}
    handler.recorded_patch[4] = {"isEncoding": True}
    shown, now = entries()
    assert 9 in shown and not {9, 11, 4} & now
    handler.recorded_protect[9] = False
    handler.recorded_patch[9] = {"isEncoding": True}
    shown, now = entries()
    assert 9 in shown and 9 not in now


@pytest.mark.parametrize("change,want", [("protect", "Protected: cannot delete"), ("encoding", "Recording has changed"),
                                         ("file", "Recording has changed")])
def test_delete_file_state_changed_before_the_select(kodi, fake_server, handler, change, want):
    import xbmc
    import xbmcgui

    _dev_only(handler)
    if change == "protect":
        handler.recorded_protect[9] = True
    elif change == "encoding":
        handler.recorded_patch[9] = {"isEncoding": True}
    else:
        handler.video_deleted.add(13)  # one file left
    xbmcgui.select_answers.append(0)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_file_action())
    assert not xbmcgui.selects and not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == want and xbmc.builtins == ["Container.Refresh"]


@pytest.mark.parametrize("change,want", [("protect", "Protected: cannot delete"), ("encoding", "Recording has changed"),
                                         ("chosen", "Recording has changed"), ("other", "Recording has changed"),
                                         ("size", "Recording has changed"), ("gone", "Recording is gone")])
def test_delete_file_state_changes_while_the_dialog_is_open(kodi, fake_server, handler, change, want):
    import xbmc
    import xbmcgui

    _dev_only(handler)
    rec = _get(fake_server, "/api/recorded/9")
    act = {"protect": lambda: handler.recorded_protect.__setitem__(9, True),
           "encoding": lambda: handler.recorded_patch.__setitem__(9, {"isEncoding": True}),
           "chosen": lambda: handler.video_deleted.add(10),
           "other": lambda: handler.video_deleted.add(13),
           "size": lambda: handler.recorded_patch.__setitem__(9, {"videoFiles": [
               dict(v, size=v["size"] + 1) for v in rec["videoFiles"]]}),
           "gone": lambda: handler.recorded_deleted.add(9)}[change]
    xbmcgui.select_answers.append(0)  # TS #10
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = act
    run_plugin(_file_action())
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert xbmcgui.notifications[-1][1] == want and xbmc.builtins == ["Container.Refresh"]


def test_delete_file_chosen_file_replaced_while_the_dialog_is_open(kodi, fake_server, handler):
    """The fingerprint sees any file change; the explicit file check also holds if a file id were reused."""
    import xbmcgui

    _dev_only(handler)
    rec = _get(fake_server, "/api/recorded/9")
    files = [dict(v, id=77) if v["id"] == 10 else v for v in rec["videoFiles"]]
    xbmcgui.select_answers.append(0)
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: handler.recorded_patch.__setitem__(9, {"videoFiles": files})
    run_plugin(_file_action())
    assert handler.writes == [] and xbmcgui.notifications[-1][1] == "Recording has changed"


def test_delete_file_double_confirm_writes_once(kodi, fake_server, handler):
    import xbmcgui

    _dev_only(handler)
    xbmcgui.select_answers.extend([0, 1])
    xbmcgui.yesno_answers.extend([True, True])
    seen = []

    def second_click():
        xbmcgui.yesno_hook = None
        run_plugin(_file_action())  # the second dialog (the other file), answered while the first is still open
        seen.append(list(handler.writes))

    xbmcgui.yesno_hook = second_click
    run_plugin(_file_action())
    assert len(xbmcgui.yesnos) == 2 and handler.writes == [("DELETE", "/api/videos/13", "")]
    assert seen == [handler.writes]
    assert xbmcgui.notifications[-1][1] == "Recording has changed"
    assert [v["id"] for v in _get(fake_server, "/api/recorded/9")["videoFiles"]] == [10]


@pytest.mark.parametrize("errors,want,icon", [
    ("VideoFileIsNotFound", "File is gone", "warning"),
    ("RecordedIsProtected", "Protected: cannot delete", "warning"),
    ("RecordedIdIsNotFound", "Recording is gone", "warning"),
    ("IPCTimeout", "Failed (connection error)", "error"),
    ("Other", "Failed: Other", "error"),
])
def test_delete_file_server_error(kodi, fake_server, handler, errors, want, icon):
    import xbmc
    import xbmcgui
    import xbmcvfs

    _dev_only(handler)
    handler.write_error = (500, {"code": 500, "message": "Internal Server Error", "errors": errors})
    xbmcgui.select_answers.append(0)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_file_action())
    assert handler.writes == [("DELETE", "/api/videos/10", "")]
    assert xbmcgui.notifications[-1][1:] == (want, icon)
    assert xbmc.builtins == ["Container.Refresh"]
    failed = [m for _, m in xbmc.logs if "[epgstation] recorded:" in m and "failed" in m]
    assert len(failed) == 1 and errors in failed[0] and "delete_file recorded 9" in failed[0]
    marker = json.load(open(_changed_path(xbmcvfs), encoding="utf-8"))
    assert marker["deleted"] == ([9] if errors == "RecordedIdIsNotFound" else []) and marker["protect"] == {}


def test_delete_file_no_answer(kodi, fake_server, handler, monkeypatch):
    import xbmc
    import xbmcgui

    _dev_only(handler)

    def timeout(self, video_file_id):
        raise api_mod.ApiError(f"/videos/{video_file_id}: timed out")

    monkeypatch.setattr(api_mod.EPGStation, "delete_video_file", timeout)
    xbmcgui.select_answers.append(1)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_file_action())
    assert xbmcgui.notifications[-1][1:] == ("Failed (connection error)", xbmcgui.NOTIFICATION_ERROR)
    assert any("file 13" in m and "may still have applied" in m for _, m in xbmc.logs)
    assert xbmc.builtins == ["Container.Refresh"]


def test_delete_file_unreachable_server(kodi, fake_server, handler):
    """AC 7: a dead server -> short error toast, no select, nothing written."""
    import xbmcaddon
    import xbmcgui

    xbmcaddon.settings["server_url"] = "http://127.0.0.1:1"
    xbmcgui.select_answers.append(0)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_file_action())
    assert not xbmcgui.selects and not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1:] == ("Failed (connection error)", xbmcgui.NOTIFICATION_ERROR)


def test_delete_file_stale_entry_on_a_single_file_recording(kodi, fake_server, handler):
    """AC 4: the entry of a recording that has (now) one file: 録画の状態が変わりました, no select, no write."""
    import xbmcgui

    one = next(r for r in _get(fake_server, "/api/recorded?limit=10000")["records"]
               if len(r.get("videoFiles") or []) == 1 and not r.get("isProtected"))
    run_plugin(_file_action(one["id"]))
    assert not xbmcgui.selects and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Recording has changed"


def test_delete_video_file_is_called_only_from_recorded_write():
    hits = {name: len(re.findall(r"\.delete_video_file\(", src)) for name, src in _addon_sources().items()}
    assert {k: v for k, v in hits.items() if v} == {"plugin.py": 1}
    plugin = _src("plugin.py")
    assert re.search(r"\.delete_video_file\(", _method_body(plugin, "_recorded_write"))
    methods = _api_methods(_src("api.py"))
    assert re.search(r'self\.delete\(f"/videos/\{int\(video_file_id\)\}"\)', methods["delete_video_file"])


def test_018_strings_in_both_languages():
    for lang in ("en_gb", "ja_jp"):
        path = os.path.join(ROOT, "plugin.video.epgstation", "resources", "language", f"resource.language.{lang}",
                            "strings.po")
        ids = set(re.findall(r'msgctxt "#(\d+)"', open(path, encoding="utf-8").read()))
        assert {str(i) for i in range(30245, 30251)} <= ids, lang


# --- tools/dev/test_reserve.py (018 DEV protocol) -----------------------------------------------------------------

class _CreateDev:
    def __init__(self, modes):
        self.modes, self.sent = modes, []

    def call(self, method, path, body=None, **params):
        self.sent.append((method, path, body))
        if path == "/reserves" and method == "GET":
            return {"reserves": []}
        if path == "/config":
            return {"encode": self.modes}
        if method == "POST":
            return {"reserveId": 77}
        return {"name": "KODIstation test", "isConflict": False}


def test_create_with_encode(tmp_path, monkeypatch):
    import argparse

    tool = _tool(tmp_path, monkeypatch, [])
    args = argparse.Namespace(program=False, time=True, start_in=3, minutes=2, encode="H.264")
    dev = _CreateDev(["H.264"])
    tool.create(dev, args)
    post = next(c for c in dev.sent if c[0] == "POST")
    assert post[2]["encodeOption"] == {"mode1": "H.264", "isDeleteOriginalAfterEncode": False}
    assert post[2]["timeSpecifiedOption"]["channelId"] == tool.NHK
    dev = _CreateDev([])
    with pytest.raises(SystemExit):
        tool.create(dev, argparse.Namespace(program=False, time=True, start_in=3, minutes=2, encode="H.264"))
    assert all(c[0] == "GET" for c in dev.sent)  # an unknown mode: nothing posted
    dev = _CreateDev(["H.264"])
    tool.create(dev, argparse.Namespace(program=False, time=True, start_in=3, minutes=2, encode=None))
    assert "encodeOption" not in next(c for c in dev.sent if c[0] == "POST")[2]
    assert not [c for c in dev.sent if c[1] == "/config"]


def test_wait_recorded_files_waits_for_the_encode(tmp_path, monkeypatch):
    end = NOW + 2 * MIN
    tool = _tool(tmp_path, monkeypatch, [dict(MINE, endAt=end)])
    dev = _RecDev({70: _own(endAt=end, isEncoding=True)})
    clock = [NOW + 3 * MIN]

    def sleep(s):
        clock[0] += s * 1000
        if clock[0] >= NOW + 8 * MIN:
            dev.recorded[70] = _own(endAt=end, videoFiles=[_vf(700), _vf(701)])

    assert tool.wait_recorded(dev, 5, now=lambda: clock[0], sleep=sleep, files=2) == 70
    assert clock[0] >= NOW + 8 * MIN  # not at the first finished poll (one file, encoding)
    dev.recorded[70] = _own(endAt=end, isEncoding=True, videoFiles=[_vf(700), _vf(701)])
    clock[0] = NOW + 3 * MIN
    with pytest.raises(SystemExit):
        tool.wait_recorded(dev, 5, now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s * 1000),
                           files=2)
    assert end + 15 * MIN <= clock[0] <= end + 15 * MIN + 15 * 1000
    assert all(c[0] == "GET" for c in dev.sent)


def test_adopt_recorded_stores_file_ids(tmp_path, monkeypatch):
    tool = _tool(tmp_path, monkeypatch)
    dev = _RecDev({1: _demo(1)})
    _snap(tool, dev)
    dev.recorded[70] = _own(videoFiles=[_vf(701), _vf(700)])
    tool.adopt_recorded(dev, 70)
    assert tool.load_state()["recordedFiles"] == {"70": [700, 701]}


@pytest.mark.parametrize("vid,ok", [(700, True), (701, True), (10, False), (20, False)])
def test_audit_video_file_deletes(tmp_path, monkeypatch, capsys, vid, ok):
    t = NOW - 5 * MIN
    tool = _tool(tmp_path, monkeypatch)
    dev = _RecDev({1: _demo(1), 2: _demo(2, videoFiles=[_vf(20), _vf(21)])})
    _snap(tool, dev)
    dev.recorded[70] = _own(videoFiles=[_vf(700), _vf(701)])
    tool.adopt_recorded(dev, 70)
    access = [_access(_iso(t), "DELETE", f"/api/videos/{vid}"), _access(_iso(NOW - 4 * MIN), "GET", "/api/recorded")]
    kodi = [_kodi(_kts(t), f"recorded: delete_file recorded 70 (KODIstation test) file {vid} (TS) done")]
    monkeypatch.setattr(tool, "read_access_log", lambda dev, since_ms=0: "\n".join(access) + "\n")
    monkeypatch.setattr(tool, "read_kodi_log", lambda dev: "\n".join(kodi) + "\n")
    dev.recorded[70]["videoFiles"] = [v for v in dev.recorded[70]["videoFiles"] if v["id"] != vid]
    assert tool.audit(dev) is ok
    assert ("PASS" if ok else "FAIL") in capsys.readouterr().out
    assert tool.kodi_recorded(kodi[0]) == [(t, "delete_file", 70)]


def test_audit_refuses_a_video_get_or_put_path(tmp_path, monkeypatch):
    tool = _test_reserve_tool()
    assert tool._adopted_write("DELETE", "/api/videos/700", {70}, {700})
    assert not tool._adopted_write("PUT", "/api/videos/700", {70}, {700})
    assert not tool._adopted_write("DELETE", "/api/videos/700", {70}, set())
    assert tool._adopted_write("DELETE", "/api/recorded/70", {70}, set())


def test_stream_writes_are_called_only_from_the_stream_path():
    """Transcoded playback (011): the stream start / keep / stop are reached only from do_stream and its helpers."""
    hits = {}
    for name, src in _addon_sources().items():
        for m in re.finditer(r"\.(start_recorded_hls|keep_stream|stop_stream)\(", src):
            hits.setdefault(name, []).append(m.group(1))
    # tester F1: _stop_stale_streams stops what an earlier run left (after a GET /streams match)
    assert hits == {"plugin.py": ["start_recorded_hls", "stop_stream", "keep_stream", "stop_stream"]}
    plugin = _src("plugin.py")
    assert "start_recorded_hls(" in _method_body(plugin, "do_stream")
    assert "keep_stream(" in plugin.split("    def _keep(", 1)[1].split("\n    def ", 1)[0]
    assert _callers(plugin, r"\b_keep\(") == {"_wait_ready", "_play_stream"}
    assert "stop_stream(" in plugin.split("    def _stop_stream(", 1)[1].split("\n    def ", 1)[0]
    assert _callers(plugin, r"\.stop_stream\(") == {"_stop_stream", "_stop_stale_streams"}
    assert _callers(plugin, r"\b_stop_stale_streams\(") == {"do_stream"}
    assert _callers(plugin, r"\b_stop_stream\(") == {"do_stream"}
    # 011 review nit 5: the transcode start path and the stream write paths live in their api methods only
    api = _src("api.py")
    assert _callers(api, r"/streams/recorded/") == {"start_recorded_hls"}
    assert _callers(api, r'"/streams/\{int\(stream_id\)\}/keep"') == {"keep_stream"}
    assert _callers(api, r'f"/streams/\{int\(stream_id\)\}"') == {"stop_stream"}
