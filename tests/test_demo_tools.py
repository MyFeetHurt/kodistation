"""Backlog 021 (demo video): tools/demo — free-channel guard, label navigation, EventServer packets, live cap,
refusals, edit.py's ffmpeg argv and check_media.py on generated samples. No VM, no ffmpeg needed."""
import json
import os
import struct
import subprocess
import sys
import zlib

import pytest
from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "tools", "demo"))
import check_media  # noqa: E402
import edit  # noqa: E402
import kodi_drive as kd  # noqa: E402
import snapshot_api  # noqa: E402
import tour  # noqa: E402

GR = {"id": 3211240960, "channelType": "GR", "name": "ＮＨＫ総合１・大阪"}
BS_FREE = {"id": 400101, "channelType": "BS", "serviceId": 101, "name": "ＮＨＫ　ＢＳ"}
BS_PAID = {"id": 400191, "channelType": "BS", "serviceId": 191, "name": "ＷＯＷＯＷプライム"}
BS10_PAID = {"id": 400201, "channelType": "BS", "serviceId": 201, "name": "ＢＳ１０プレミアム"}
CS = {"id": 500055, "channelType": "CS", "serviceId": 55, "name": "ショップチャンネル"}
CHANNELS = [GR, BS_FREE, BS_PAID, BS10_PAID, CS]
ROOT_URL = "plugin://plugin.video.epgstation/"


# --- free-channel guard -----------------------------------------------------------------------------------------

def test_free_bs_list_is_the_fixture_policy_list():
    assert kd.FREE_BS_SERVICES == snapshot_api.FREE_BS_SERVICES
    for ch in CHANNELS:
        assert kd.is_free_channel(ch) == snapshot_api.is_free_channel(ch)


@pytest.mark.parametrize("folder", [f"{ROOT_URL}?action=guide_now", f"{ROOT_URL}?action=guide_genres",
                                    f"{ROOT_URL}?action=reserves", f"{ROOT_URL}?action=live&type=BS",
                                    f"{ROOT_URL}?action=live&type=CS", f"{ROOT_URL}?action=live&type=SKY",
                                    f"{ROOT_URL}?action=search&keyword=x", f"{ROOT_URL}?type=bs&action=live",
                                    f"{ROOT_URL}?action=live&types=GR%2CBS%2Ctype%3DBS"])
def test_guard_refuses_folders_with_paid_channels(folder):
    assert kd.Guard(CHANNELS).folder_failures(folder)


@pytest.mark.parametrize("folder", [ROOT_URL, f"{ROOT_URL}?action=live&type=GR", f"{ROOT_URL}?action=tvshows",
                                    f"{ROOT_URL}?action=guide_channel&channelId=3211240960&day=1"])
def test_guard_passes_the_tour_folders(folder):
    assert kd.Guard(CHANNELS).folder_failures(folder) == []


def test_guard_items_channel_ids_and_paid_names():
    g = kd.Guard(CHANNELS)
    free = [{"label": "NHK総合1・大阪  うたコン", "file": f"{ROOT_URL}?action=live_play&channelId={GR['id']}"},
            {"label": "ニュース", "file": f"{ROOT_URL}?action=guide_info&programId=1&channelId={BS_FREE['id']}"},
            {"label": "TV番組", "file": f"{ROOT_URL}?action=tvshows"}]
    assert g.item_failures(free) == []
    assert g.item_failures([{"label": "x", "file": f"{ROOT_URL}?action=live_play&channelId={BS_PAID['id']}"}])
    assert g.item_failures([{"label": "x", "file": f"{ROOT_URL}?action=live_play&channelId=abc"}])
    # names: full-width in /channels, half-width (or full-width) in the label
    assert g.item_failures([{"label": "映画 WOWOWプライム 特集", "file": f"{ROOT_URL}?action=play&id=1"}])
    assert g.item_failures([{"label": "ＢＳ１０プレミアム", "file": f"{ROOT_URL}?action=play&id=1"}])
    assert g.item_failures([{"label": "bs10プレミアム", "file": ""}])


def test_guard_leaves_out_paid_names_inside_free_names():
    shadow = {"id": 9, "channelType": "CS", "name": "ＮＨＫ"}  # inside ＮＨＫ総合１: would trip every NHK label
    g = kd.Guard(CHANNELS + [shadow])
    assert g.shadowed == ["nhk"]
    assert g.item_failures([{"label": "NHK総合1・大阪", "file": ""}]) == []


def test_guard_records_and_preflight():
    g = kd.Guard(CHANNELS)
    assert g.records_failures("recording", [{"id": 1, "channelId": GR["id"]}]) == []
    assert g.records_failures("recording", [{"id": 2, "channelId": CS["id"], "name": "x"}])
    answers = {"/channels": CHANNELS, "/recorded": {"records": [{"id": 1, "channelId": GR["id"]}]},
               "/recording": {"records": []}, "/reserves": {"reserves": [{"id": 5, "channelId": BS_PAID["id"]}]},
               "/streams": {"items": []}}
    sent = []

    def get(url):
        sent.append(url)
        return answers[url.split("/api", 1)[1].split("?", 1)[0]]

    epgs = kd.DevEpgs("http://epgs-dev.test:8888", get)
    fails = epgs.preflight_failures(g)
    assert len(fails) == 1 and "reservation 5" in fails[0]
    answers["/streams"] = {"items": [{"streamId": 1}]}
    assert any("live stream" in f for f in epgs.preflight_failures(g))
    assert all(u.startswith("http://epgs-dev.test:8888/api/") for u in sent)


# --- driver -----------------------------------------------------------------------------------------------------

class FakeRpc:
    """JSON-RPC stand-in: a list of focused labels, Input.Down moves to the next one."""

    def __init__(self, labels, folder=ROOT_URL, items=()):
        self.labels, self.i, self.calls, self.folder, self.items = list(labels), 0, [], folder, list(items)

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "XBMC.GetInfoLabels":
            out = {}
            for name in params["labels"]:
                if name in ("Container.ListItem.Label", "System.CurrentControl"):
                    out[name] = self.labels[min(self.i, len(self.labels) - 1)]
                elif name == "Container.FolderPath":
                    out[name] = self.folder
                else:
                    out[name] = ""
            return out
        if method == "Input.Down":
            self.i += 1
        if method == "Files.GetDirectory":
            return {"files": self.items}
        return True


def kodi(rpc, guard=None):
    return kd.Kodi("kodi-dev.test", guard=guard, rpc=rpc, sleep=lambda s: None)


def test_focus_label_moves_until_found():
    rpc = FakeRpc(["..", "映画", "TV番組", "最近の録画"])
    k = kodi(rpc)
    assert k.focus_label("TV番組", exact=True) == "tv番組"
    assert k.sent == ["Down", "Down"]


def test_focus_label_gives_up_after_max_steps():
    rpc = FakeRpc(["..", "映画"])
    k = kodi(rpc)
    with pytest.raises(kd.TakeError):
        k.focus_label("ライブTV", max_steps=5)
    assert k.sent == ["Down"] * 5


def test_check_lists_the_folder_and_aborts_on_a_paid_item(tmp_path):
    items = [{"label": "x", "file": f"{ROOT_URL}?action=live_play&channelId={CS['id']}"}]
    rpc = FakeRpc(["x"], folder=f"{ROOT_URL}?action=live&type=GR", items=items)
    k = kodi(rpc, kd.Guard(CHANNELS))
    k.log_path = str(tmp_path / "tour.jsonl")
    with pytest.raises(kd.TakeError):
        k.check("live_gr")
    assert "Files.GetDirectory" in rpc.calls
    line = json.loads((tmp_path / "tour.jsonl").read_text(encoding="utf-8"))
    assert line["step"] == "live_gr" and line["guard"] == "FAIL" and "t" in line and "folder" in line
    rpc.items = [{"label": "NHK総合1", "file": f"{ROOT_URL}?action=live_play&channelId={GR['id']}"}]
    k.check("live_gr")  # free: passes


def test_select_is_refused_while_the_delete_prompt_is_open():
    k = kodi(FakeRpc(["x"]))
    k._no_select = True
    with pytest.raises(kd.TakeError):
        k.press("Select")
    k.press("Back")
    assert k.sent == ["Back"]


def test_delete_step_never_sends_select():
    """tour.section_d: from the 削除 context entry until the prompt is gone only Back is pressed."""
    with open(os.path.join(ROOT, "tools", "demo", "tour.py"), encoding="utf-8") as f:
        src = f.read()
    part = src[src.index('self.context("削除"'):src.index('k.log("delete_closed")')]
    assert 'press("Back")' in part and 'press("Select")' not in part and "confirm_yes" not in part
    assert "k._no_select = True" in part and "k._no_select = False" in part


# --- EventServer --------------------------------------------------------------------------------------------------

def test_event_packet_bytes_match_the_xbmcclient_layout():
    """Known-good bytes written out field by field from xbmcclient.h (not built with struct like the tool)."""
    payload = b"\x01" + b"Addon.OpenSettings(plugin.video.epgstation)" + b"\x00"
    fields = ["58424d43",  # "XBMC"
              "02", "00",  # protocol 2.0
              "000a",  # PT_ACTION
              "00000001",  # sequence 1
              "00000001",  # of 1 packet
              f"{len(payload):04x}",  # payload size
              "01020304",  # uid
              "00" * 10]  # reserved
    want = bytes.fromhex("".join(fields)) + payload
    got = kd.event_packet(kd.PT_ACTION, kd.builtin_payload("Addon.OpenSettings(plugin.video.epgstation)"),
                          seq=1, uid=0x01020304)
    assert got == want and len(got) == 32 + len(payload)
    assert kd.helo_payload("d") == b"d\x00" + b"\x00" + b"\x00\x00" + b"\x00" * 8


def test_event_client_helo_once_then_builtins_on_one_socket():
    class Sock:
        def __init__(self):
            self.sent, self.closed = [], False

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def close(self):
            self.closed = True

    sock = Sock()
    c = kd.EventClient("kodi-dev.test", sock=sock)
    c.helo()
    c.builtin("SetFocus(3)")
    c.builtin("SetFocus(28)")
    c.bye()
    types = [struct.unpack("!H", d[6:8])[0] for d, _ in sock.sent]
    assert types == [kd.PT_HELO, kd.PT_ACTION, kd.PT_ACTION, kd.PT_BYE]
    assert len({d[22:26] for d, _ in sock.sent}) == 1  # one uid: one client, one "new connection" toast
    assert {a for _, a in sock.sent} == {("kodi-dev.test", 9777)} and sock.closed


# --- refusals -----------------------------------------------------------------------------------------------------

def test_live_cap():
    assert tour.LIVE_S <= tour.LIVE_MAX_S == 60
    assert tour.check_live_seconds(15) == 15
    for bad in (61, 0, 600):
        with pytest.raises(SystemExit):
            tour.check_live_seconds(bad)


@pytest.mark.parametrize("url", ["http://epgs.example:8888", "http://192.0.2.10:8888", "http://localhost.evil:8888",
                                 "https://user@epgs.example/"])
def test_tour_refuses_a_non_localhost_server(url):
    with pytest.raises(SystemExit):
        tour.check_server_url(f'<settings><setting id="server_url">{url}</setting></settings>')


def test_tour_accepts_localhost_and_the_default():
    for url in ("http://localhost:8888", "http://127.0.0.1:8888/"):
        assert tour.check_server_url(f'<setting id="server_url" default="true">{url}</setting>') == url
    assert tour.check_server_url("<settings/>") == "http://localhost:8888"


def test_tools_take_hosts_from_local_env_and_refuse_none_or_localhost(monkeypatch, tmp_path):
    for key in ("KODI_HOST", "KODI_SSH_USER", "EPGS_DEV_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCAL_ENV", str(tmp_path / "missing.env"))
    with pytest.raises(SystemExit):
        kd.env_host()
    with pytest.raises(SystemExit):
        tour.ssh_target()
    monkeypatch.setenv("KODI_HOST", "localhost")  # the Makefile's default without local.env
    with pytest.raises(SystemExit):
        kd.env_host()
    monkeypatch.delenv("KODI_HOST")
    env = tmp_path / "local.env"
    env.write_text("KODI_HOST=kodi-dev.test\nKODI_SSH_USER=someone\nEPGS_DEV_URL=http://epgs-dev.test:8888\n",
                   encoding="utf-8")
    monkeypatch.setenv("LOCAL_ENV", str(env))
    assert tour.ssh_target() == "someone@kodi-dev.test"
    assert kd.dev_epgs_url("kodi-dev.test") == "http://epgs-dev.test:8888"
    monkeypatch.delenv("EPGS_DEV_URL")
    assert kd.dev_epgs_url("kodi-dev.test") == "http://kodi-dev.test:8888"
    monkeypatch.delenv("KODI_SSH_USER")
    monkeypatch.setenv("LOCAL_ENV", str(tmp_path / "missing.env"))
    assert tour.ssh_target() == "kodi-dev.test"  # no user: ssh's default


# --- edit.py ------------------------------------------------------------------------------------------------------

CUTS = {"offsets": {"A1": -0.5},
        "video": [{"clip": "A1", "caption": "録画を見る / Browse", "keep": [
            {"from": ["home", 0], "to": ["root", 1.0], "speed": 2.0}, {"from": 10, "to": 12}]},
            {"clip": "B1", "keep": [{"from": ["live", -1], "to": ["live", 3]}]}],
        "gif": [{"clip": "B1", "keep": [{"from": 0, "to": 5}]}]}
LOGS = {"A1": {"home": 1.0, "root": 5.0}, "B1": {"live": 20.0}}


def test_edit_segments_resolve_steps_offsets_and_keep_the_order():
    segs = edit.segments(CUTS, LOGS)
    assert segs == [("A1", 0.5, 5.5, 2.0), ("A1", 9.5, 11.5, 1.0), ("B1", 19.0, 23.0, 1.0)]
    assert edit.duration(segs) == pytest.approx(2.5 + 2 + 4)
    assert edit.captions(CUTS, segs) == [("録画を見る / Browse", 0.2, 3.2)]
    with pytest.raises(SystemExit):
        edit.segments({"video": [{"clip": "A1", "keep": [{"from": ["nope", 0], "to": 3}]}]}, LOGS)
    with pytest.raises(SystemExit):
        edit.segments({"video": [{"clip": "A1", "keep": [{"from": 5, "to": 5}]}]}, LOGS)


def test_edit_argv_strip_metadata_no_audio_faststart():
    segs = edit.segments(CUTS, LOGS)
    argv = edit.concat_argv("/raw", segs, "/work/master.mkv", caps=edit.captions(CUTS, segs), font="/f.ttc")
    inputs = [argv[i + 1] for i, a in enumerate(argv) if a == "-i"]
    assert inputs == ["/raw/A1.mkv", "/raw/A1.mkv", "/raw/B1.mkv"]  # the cut list order
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-ss"] == ["0.500", "9.500", "19.000"]
    graph = argv[argv.index("-filter_complex") + 1]
    assert "concat=n=3:v=1:a=0" in graph and "setpts=(PTS-STARTPTS)/2.0" in graph and "drawtext" in graph
    mp4 = edit.mp4_argv("/work/master.mkv", "media/kodistation-demo.mp4", 24)
    for argv in (mp4, edit.gif_argv("/work/g.mkv", "media/demo.gif")):
        assert "-an" in argv
        i = argv.index("-map_metadata")
        assert argv[i + 1] == "-1" and "-map_chapters" in argv
        assert argv[argv.index("-fflags") + 1] == "+bitexact" and argv[argv.index("-flags:v") + 1] == "+bitexact"
    assert mp4[mp4.index("-movflags") + 1] == "+faststart"
    assert mp4[mp4.index("-profile:v") + 1] == "high" and mp4[mp4.index("-pix_fmt") + 1] == "yuv420p"
    assert "encoder=" in mp4 and "filter_units=remove_types=6" in mp4  # no encoder tag, no x264 SEI
    assert "palettegen=stats_mode=diff" in edit.gif_argv("a", "b")[edit.gif_argv("a", "b").index("-filter_complex") + 1]


def test_edit_clean_png_drops_text_chunks(tmp_path):
    path = tmp_path / "s.png"
    path.write_bytes(png(text=b"Software\x00ffmpeg"))
    edit.clean_png(str(path))
    assert check_media.png_failures("s.png", path.read_bytes()) == []


def test_cuts_json_is_valid_and_within_limits():
    with open(edit.CUTS, encoding="utf-8") as f:
        cuts = json.load(f)
    assert {"video", "gif", "screenshots"} <= set(cuts)
    assert {s["name"] for s in cuts["screenshots"]} == {"screenshot-recordings.png", "screenshot-details.png",
                                                        "screenshot-guide.png", "screenshot-live.png"}
    for part in cuts["video"] + cuts["gif"]:
        for r in part["keep"]:
            assert float(r.get("speed", 1.0)) >= 1.0
    live = [r for p in cuts["video"] if p["clip"].startswith("B") for r in p["keep"]
            if "live_playing" in json.dumps(r["from"])]
    assert len(live) == 1  # one live range in the cut, at most 5 s of playback
    r = live[0]
    assert (r["to"][1] - r["from"][1]) / r.get("speed", 1.0) <= 5.0 and r["from"][0] == r["to"][0]


# --- check_media.py -----------------------------------------------------------------------------------------------

def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png(w=1280, h=800, text=None):
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    if text:
        out += chunk(b"tEXt", text)
    return out + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def gif(extensions=b"", image_data=b"\x02\x02\x4c\x01\x00"):
    """A 1x1 two-colour GIF89a; extensions go before the image; image_data = LZW size + sub-blocks."""
    head = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0) + b"\x00\x00\x00\xff\xff\xff"
    loop = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    img = b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + image_data
    return head + loop + extensions + img + b"\x3b"


def probe_json(duration, streams=None, tags=None):
    streams = streams if streams is not None else [{"codec_type": "video", "codec_name": "h264", "width": 1280,
                                                    "height": 800, "tags": {"language": "und",
                                                                            "handler_name": "VideoHandler"}}]
    return json.dumps({"streams": streams, "format": {"duration": str(duration), "filename": "x",
                                                     "tags": tags or {"major_brand": "isom"}}})


def mp4_bytes(faststart=True):
    box = lambda kind, body=b"": struct.pack(">I4s", 8 + len(body), kind) + body  # noqa: E731
    parts = [box(b"ftyp", b"isom\x00\x00\x02\x00"), box(b"moov", b"\x00" * 16), box(b"mdat", b"\x00" * 64)]
    if not faststart:
        parts[1], parts[2] = parts[2], parts[1]
    return b"".join(parts)


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_check_media_clean_samples_pass(tmp_path):
    env = {"PUBLIC_AUDIT_DENY": "secret-host,someone@example.org"}
    assert check_media.check(write(tmp_path, "a.png", png()), env) == []
    assert check_media.check(write(tmp_path, "demo.gif", gif()), env, probe=lambda p: probe_json(15, [
        {"codec_type": "video", "codec_name": "gif"}], {})) == []
    assert check_media.check(write(tmp_path, "v.mp4", mp4_bytes()), env, probe=lambda p: probe_json(117)) == []


def test_check_media_png_failures(tmp_path):
    assert check_media.check(write(tmp_path, "a.png", png(text=b"Comment\x00hi")), {})
    assert check_media.check(write(tmp_path, "b.png", png(w=640, h=400)), {})
    assert check_media.check(write(tmp_path, "c.png", b"not a png"), {})


def test_check_media_gif_failures(tmp_path):
    probe = lambda p: probe_json(15, [{"codec_type": "video", "codec_name": "gif"}], {})  # noqa: E731
    comment = b"\x21\xfe\x05hello\x00"
    xmp = b"\x21\xff\x0bXMP DataXMP\x03abc\x00"
    assert check_media.check(write(tmp_path, "c.gif", gif(comment)), {}, probe)
    assert check_media.check(write(tmp_path, "x.gif", gif(xmp)), {}, probe)
    # 0x21 0xFE inside the image data is no comment
    assert check_media.check(write(tmp_path, "d.gif", gif(image_data=b"\x02\x03\x21\xfe\x01\x00")), {}, probe) == []
    assert check_media.check(write(tmp_path, "s.gif", gif()), {}, lambda p: probe_json(30, [
        {"codec_type": "video"}], {}))  # too long


@pytest.mark.parametrize("bad", ["title", "encoder", "creation_time", "audio", "no_faststart", "short", "big",
                                 "size"])
def test_check_media_mp4_failures(tmp_path, monkeypatch, bad):
    tags, streams, data, dur = None, None, mp4_bytes(), 100
    video = {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 800}
    if bad in ("title", "encoder", "creation_time"):
        tags = {"major_brand": "isom", bad: "x"}
    elif bad == "audio":
        streams = [video, {"codec_type": "audio"}]
    elif bad == "no_faststart":
        data = mp4_bytes(faststart=False)
    elif bad == "short":
        dur = 60
    elif bad == "big":
        monkeypatch.setitem(check_media.LIMITS, ".mp4", (90.0, 120.0, 10))
    else:
        streams = [dict(video, width=1920, height=1080)]
    path = write(tmp_path, "v.mp4", data)
    assert check_media.check(path, {}, probe=lambda p: probe_json(dur, streams, tags))


def test_check_media_private_literals_and_generic_rules():
    deny = check_media.deny_literals({"PUBLIC_AUDIT_DENY": " Secret-Host , someone@example.org,|"})
    assert deny == ["Secret-Host", "someone@example.org"]
    assert check_media.leak_failures("f", b"..SECRET-HOST..", deny)  # case-insensitive
    p192, p10, p172 = b"192." + b"168", b"10." + b"0", b"172." + b"20"  # built here: no private address in the tree
    for data in (b"x " + p192 + b".1.20 x", b"x " + p10 + b".0.1 x", p172 + b".3.4", b"/ho" + b"me/x", b"/mnt/y"):
        assert check_media.leak_failures("f", data, []), data
    for data in (b"1." + p192 + b".1.2.3", b"version 172.15.0.1", b"localhost:8888"):
        assert check_media.leak_failures("f", data, []) == [], data


# --- review round 1: behaviour tests ------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class StateRpc:
    """A scripted Kodi for whole sections: focus cycles through `labels` on any move, dialogs open on ContextMenu /
    Select and close on Back, every call is recorded with the driver's _no_select flag at that moment."""

    def __init__(self, labels, folder, path, kodi_ref, pages=None, next_of=None):
        self.labels, self.folder, self.path, self.k = list(labels), folder, path, kodi_ref
        self.i, self.calls, self.yesno, self.ctx = 0, [], False, False
        self.booleans = {}
        self.pages, self.next_of = pages or {}, next_of or {}  # folder -> items; folder -> its 次のページ folder

    def __call__(self, method, params):
        self.calls.append((method, dict(params), bool(getattr(self.k[0], "_no_select", False))))
        if method in ("Input.Down", "Input.Up", "Input.Left", "Input.Right"):
            self.i = (self.i + 1) % len(self.labels)
        elif method == "Input.ContextMenu":
            self.ctx = True
        elif method == "Input.Select":
            if not self.ctx and not self.yesno and self.labels[self.i].startswith("次のページ"):
                self.folder = self.next_of[self.folder]
            self.yesno, self.ctx = self.ctx, False  # a context entry opens the yes/no; yes in it closes it
        elif method == "Input.Back":
            self.yesno = self.ctx = False
        if method == "XBMC.GetInfoLabels":
            out = {}
            for name in params["labels"]:
                if name in ("Container.ListItem.Label", "System.CurrentControl"):
                    out[name] = self.labels[self.i]
                elif name == "Container.FolderPath":
                    out[name] = self.folder
                elif name == "Container.ListItem.FileNameAndPath":
                    out[name] = self.path
                else:
                    out[name] = ""
            return out
        if method == "XBMC.GetInfoBooleans":
            cond = params["booleans"][0]
            neg = cond.startswith("!")
            base = cond.lstrip("!")
            val = {"Window.IsActive(yesnodialog)": self.yesno, "Window.IsActive(contextmenu)": True,
                   "Window.IsActive(notification)": True}.get(base, self.booleans.get(base, False))
            return {cond: (not val) if neg else val}
        if method == "Files.GetDirectory":
            return {"files": self.pages.get(params["directory"], [])}
        return True


def scripted(labels, folder, path, epgs=None, pace=1.0, pages=None, next_of=None, **kw):
    ref = [None]
    rpc = StateRpc(labels, folder, path, ref, pages, next_of)
    clock = Clock()
    k = kd.Kodi("kodi-dev.test", rpc=rpc, sleep=clock.sleep, clock=clock.now)
    ref[0] = k
    return tour.Tour(k, epgs, pace, **kw), rpc, clock


SELECT_LIKE = {"input.select", "input.sendtext"}


PAGE1, PAGE2 = f"{ROOT_URL}?action=list", f"{ROOT_URL}?action=list&offset=50"
D_PAGES = {PAGE1: [{"label": "a", "file": f"{ROOT_URL}?action=play&id=132"},
                   {"label": "次のページ (51-)", "file": PAGE2}],
           PAGE2: [{"label": "b", "file": f"{ROOT_URL}?action=play&id=169"},
                   {"label": "ダーウィン", "file": f"{ROOT_URL}?action=play&id=69"}]}


def demo_state(tmp_path, monkeypatch, rec=69, files=(690,)):
    tool = kd.dev_tool()
    monkeypatch.setattr(tool, "STATE", str(tmp_path / "state.json"))
    tool.save_state({"reserves": [], "demo": {"rec": rec, "prior": {"isProtected": False, "files": list(files)}}})
    return tool


def recorded(rid=69, protected=False, files=(690,)):
    return {"id": rid, "isProtected": protected, "videoFiles": [{"id": f} for f in files]}


def d_tour(epgs, pages=D_PAGES):
    labels = ["..", "最近の録画", "次のページ (51-)", "保護", "保護を解除", "削除", "キャンセル"]
    return scripted(labels, PAGE1, f"{ROOT_URL}?action=play&id=69", epgs=epgs, rec=69, pages=pages,
                    next_of={PAGE1: PAGE2})


def test_section_d_never_confirms_the_delete_prompt(tmp_path, monkeypatch):
    demo_state(tmp_path, monkeypatch)
    t, rpc, _ = d_tour(FakeEpgs(answers={"/recorded/69": recorded()}))
    t.section_d()
    assert rpc.folder == PAGE2  # found on page 2 by following 次のページ
    guarded = [(m, p) for m, p, no in rpc.calls if no]
    assert guarded, "the delete prompt phase ran"
    assert ("Input.Back", {}) in guarded
    for m, p in guarded:
        assert m.lower() not in SELECT_LIKE, m
        assert not (m == "Input.ExecuteAction" and str(p.get("action", "")).lower() in kd.CONFIRM_ACTIONS)
    selects = [i for i, (m, _, _) in enumerate(rpc.calls) if m == "Input.Select"]
    backs = [i for i, (m, _, no) in enumerate(rpc.calls) if m == "Input.Back" and no]
    assert selects and max(selects) < min(backs)  # the last Select chose 削除 in the context menu, before the prompt
    assert not t.k._no_select


def test_section_d_finds_the_target_on_the_first_page(tmp_path, monkeypatch):
    demo_state(tmp_path, monkeypatch)
    pages = {PAGE1: D_PAGES[PAGE2] + [{"label": "次のページ (51-)", "file": PAGE2}]}
    t, rpc, _ = d_tour(FakeEpgs(answers={"/recorded/69": recorded()}), pages)
    assert t.open_rec_page() == 1 and rpc.folder == PAGE1


def test_section_d_aborts_when_the_target_is_not_listed(tmp_path, monkeypatch):
    demo_state(tmp_path, monkeypatch)
    pages = {PAGE1: D_PAGES[PAGE1], PAGE2: [{"label": "b", "file": f"{ROOT_URL}?action=play&id=169"}]}
    t, rpc, _ = d_tour(FakeEpgs(answers={"/recorded/69": recorded()}), pages)
    with pytest.raises(kd.TakeError) as e:
        t.section_d()
    assert "not in 最近の録画" in str(e.value)
    assert "Input.ContextMenu" not in [m for m, _, _ in rpc.calls]  # no write was started


@pytest.mark.parametrize("bad", ["other_rec", "no_demo", "protected", "files"])
def test_section_d_refuses_anything_but_the_stored_demo_target(tmp_path, monkeypatch, bad):
    demo_state(tmp_path, monkeypatch, rec=70 if bad == "other_rec" else 69)
    if bad == "no_demo":
        kd.dev_tool().save_state({"reserves": []})
    rec = recorded(protected=bad == "protected", files=(691,) if bad == "files" else (690,))
    t, rpc, _ = d_tour(FakeEpgs(answers={"/recorded/69": rec}))
    with pytest.raises(kd.TakeError):
        t.precheck("D")
    assert rpc.calls == []  # refused before any Kodi input
    with pytest.raises(kd.TakeError):
        t.section_d()
    assert "Input.ContextMenu" not in [m for m, _, _ in rpc.calls]


@pytest.mark.parametrize("call", [("Input.Select", {}), ("Input.ExecuteAction", {"action": "SELECT"}),
                                  ("Input.ExecuteAction", {"action": "enter"}), ("Input.SendText", {"text": "x"}),
                                  ("Input.ExecuteAction", {"action": "Accept"})])
def test_confirm_input_is_refused_while_the_prompt_is_open(call):
    k = kodi(FakeRpc(["x"]))
    k._no_select = True
    with pytest.raises(kd.TakeError):
        k.rpc(call[0], **call[1])
    for action in ("Select", "select", "ENTER", "accept"):
        with pytest.raises(kd.TakeError):
            k.press(action)
    for text in ("Action(Select)", "action( enter )", "SendClick(11)", "SetFocus(11)"):
        with pytest.raises(kd.TakeError):
            k.builtin(None, text)
    k.press("Back")
    k._no_select = False
    k.rpc("Input.Select")  # outside the prompt: allowed


class FakeEpgs:
    def __init__(self, streams=(), reserves=None, answers=None):
        self._streams, self.reserves, self.calls = list(streams), reserves or {}, []
        self.answers = answers or {}

    def streams(self):
        return self._streams

    def api(self, path, **params):
        import urllib.error

        self.calls.append(path)
        if path in self.answers:
            return self.answers[path]
        if not path.startswith("/reserves/"):
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        rid = int(path.rsplit("/", 1)[1])
        if rid not in self.reserves:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return self.reserves[rid]


def live_tour(epgs, pace=1.0, video_at=2.0):
    t, rpc, clock = scripted(["NHK"], f"{ROOT_URL}?action=live&type=GR", "", epgs=epgs, pace=pace)
    start = clock.t
    orig = rpc.__call__

    def call(method, params):
        if method == "XBMC.GetInfoBooleans" and params["booleans"][0] == "Player.HasVideo":
            rpc.calls.append((method, dict(params), False))
            return {"Player.HasVideo": clock.t - start >= video_at}
        return orig(method, params)

    t.k._rpc = call
    return t, rpc, clock


def test_play_live_stops_on_an_exception():
    t, rpc, _ = live_tour(FakeEpgs())

    def boom(*a, **kw):
        raise RuntimeError("rpc gone")

    t.k.log = boom  # the first log after playback starts fails
    with pytest.raises(RuntimeError):
        t.play_live()
    assert "Player.Stop" in [m for m, _, _ in rpc.calls]


def test_play_live_aborts_when_the_stream_is_left_and_says_how_to_stop_it():
    t, rpc, _ = live_tour(FakeEpgs(streams=[{"streamId": 1}]))
    with pytest.raises(kd.TakeError) as e:
        t.play_live()
    assert "Player.Stop" in str(e.value) and "/api/streams" in str(e.value)


def test_play_live_cap_holds_under_pace(monkeypatch):
    monkeypatch.setattr(tour, "LIVE_S", tour.LIVE_MAX_S)
    t, rpc, clock = live_tour(FakeEpgs(), pace=tour.MAX_PACE, video_at=19.0)
    start = clock.t
    stopped = []
    orig = t.k._rpc

    def call(method, params):
        if method == "Player.Stop":
            stopped.append(clock.t - start)
        return orig(method, params)

    t.k._rpc = call
    t.play_live()
    assert stopped and stopped[0] <= tour.LIVE_MAX_S + 0.5  # + the Select press itself
    with pytest.raises(SystemExit):
        tour.check_pace(tour.MAX_PACE + 0.5)


def test_main_refuses_before_any_kodi_input(monkeypatch, tmp_path):
    def no(*a, **kw):
        raise AssertionError("no request before the refusals")

    monkeypatch.setattr(tour, "ssh", no)
    monkeypatch.setattr(kd.Kodi, "_http_rpc", no)
    monkeypatch.setattr(kd, "http_get_json", no)
    monkeypatch.setattr(kd, "send_builtin", no)
    for key in ("KODI_HOST", "KODI_SSH_USER", "EPGS_DEV_URL", "EPGS_PROD_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCAL_ENV", str(tmp_path / "missing.env"))
    log = str(tmp_path / "A1.jsonl")
    monkeypatch.setenv("KODI_HOST", "kodi-dev.test")
    for argv in (["A", "--log", log],  # no EPGS_PROD_URL
                 ["A", "--log", log, "--pace", "3"], ["B", "--log", log], ["D", "--log", log]):
        with pytest.raises(SystemExit):
            tour.main(argv)
    monkeypatch.setenv("EPGS_PROD_URL", "http://epgs-prod.test:8888")
    monkeypatch.setenv("EPGS_DEV_URL", "http://epgs-dev.test:8888")
    with pytest.raises(SystemExit):
        tour.main(["A", "--log", log])  # the dev EPGStation is not on KODI_HOST
    (tmp_path / "A1.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        tour.main(["A", "--log", log])  # an existing log without --force
    for host in ("127.0.0.1", "::1", "127.0.1.1", "localhost.localdomain", "LOCALHOST"):
        monkeypatch.setenv("KODI_HOST", host)
        with pytest.raises(SystemExit):
            tour.main(["A", "--log", str(tmp_path / "new.jsonl"), "--force"])


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "127.0.1.1", "localhost", "localhost.localdomain",
                                  "LocalHost"])
def test_env_host_refuses_loopback(monkeypatch, tmp_path, host):
    monkeypatch.setenv("LOCAL_ENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("KODI_HOST", host)
    with pytest.raises(SystemExit):
        kd.env_host()


def test_is_loopback_by_resolution():
    assert kd.is_loopback("kodi.example", resolve=lambda h: {"127.0.1.1"})
    assert kd.is_loopback("kodi.example", resolve=lambda h: {"::1"})
    assert not kd.is_loopback("kodi.example", resolve=lambda h: {"192.0.2.10"})
    assert not kd.is_loopback("192.0.2.10")


def test_check_hosts_reuses_test_reserve_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_ENV", str(tmp_path / "missing.env"))
    monkeypatch.delenv("EPGS_PROD_URL", raising=False)
    monkeypatch.setenv("EPGS_DEV_URL", "http://epgs-dev.test:8888")
    with pytest.raises(SystemExit):
        kd.check_hosts("epgs-dev.test")  # EPGS_PROD_URL required
    monkeypatch.setenv("EPGS_PROD_URL", "http://epgs-prod.test:8888")
    assert kd.check_hosts("epgs-dev.test") == "http://epgs-dev.test:8888"
    with pytest.raises(SystemExit):
        kd.check_hosts("epgs-prod.test")  # KODI_HOST is the production server
    with pytest.raises(SystemExit):
        kd.check_hosts("other-kodi.test")  # the dev EPGStation is not on KODI_HOST
    monkeypatch.setenv("EPGS_DEV_URL", "http://epgs-prod.test:8888")
    with pytest.raises(SystemExit):
        kd.check_hosts("epgs-prod.test")



def test_check_host_cli_applies_the_tour_host_rules(tmp_path):
    """make demo-probe/capture/edit/check get the host from `kodi_drive.py --check-host`: it refuses the production
    server like the tour does, not just unset and loopback hosts."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPGS_", "KODI_"))}
    env.update(LOCAL_ENV=str(tmp_path / "missing.env"), EPGS_DEV_URL="http://epgs-dev.test:8888",
               EPGS_PROD_URL="http://epgs-prod.test:8888")
    cli = [sys.executable, os.path.join(ROOT, "tools", "demo", "kodi_drive.py"), "--check-host"]
    ok = subprocess.run(cli, env=dict(env, KODI_HOST="epgs-dev.test"), capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == "epgs-dev.test"
    bad = subprocess.run(cli, env=dict(env, KODI_HOST="epgs-prod.test"), capture_output=True, text=True)
    assert bad.returncode != 0 and bad.stdout == ""

P = 327230207212427


def adopt_tour(adds, reserves):
    t, _, _ = scripted(["x"], ROOT_URL, "", epgs=FakeEpgs(reserves=reserves), program=P)
    t.start_ms = 5_000_000
    t.kodi_adds = lambda: adds
    adopted = []
    t.run_adopt = lambda rid: adopted.append(rid) or True
    return t, adopted


def test_adopt_takes_the_last_line_after_the_take_start_and_checks_the_server():
    stale = (4_999_000, P, 578)
    t, adopted = adopt_tour([stale], {578: {"programId": P}})
    with pytest.raises(kd.TakeError):
        t.adopt()  # only a line from before the take
    assert adopted == []
    lines = [stale, (5_001_000, P, 579), (5_001_000, P, 579), (5_002_000, P + 1, 581), (5_003_000, P, 580)]
    t, adopted = adopt_tour(lines, {579: {"programId": P}, 580: {"programId": P}})
    assert t.adopt() == 580 and adopted == [580]
    t, adopted = adopt_tour(lines, {579: {"programId": P}})
    with pytest.raises(kd.TakeError) as e:
        t.adopt()  # 580 is not on the server
    assert "580" in str(e.value) and adopted == []


def test_cancel_only_checks_the_adopted_reservation(tmp_path, monkeypatch):
    tool = kd.dev_tool()
    monkeypatch.setattr(tool, "STATE", str(tmp_path / "state.json"))
    mine = {"id": 579, "channelId": 3272302072, "programId": P, "startAt": 10, "endAt": 20, "adopted": True}
    server = {"id": 579, "channelId": 3272302072, "programId": P, "startAt": 10, "endAt": 20}
    t, _, _ = scripted(["x"], ROOT_URL, "", epgs=FakeEpgs(reserves={579: server}), program=P)
    tool.save_state({"reserves": []})
    with pytest.raises(kd.TakeError):
        t.check_cancel_target()  # nothing adopted
    tool.save_state({"reserves": [dict(mine, adopted=False)]})
    with pytest.raises(kd.TakeError):
        t.check_cancel_target()  # made by the tool, not adopted from Kodi
    tool.save_state({"reserves": [mine]})
    assert t.check_cancel_target() == 579
    t.epgs.reserves[579] = dict(server, startAt=11)  # another reservation under that id
    with pytest.raises(kd.TakeError):
        t.check_cancel_target()
    del t.epgs.reserves[579]
    with pytest.raises(kd.TakeError):
        t.check_cancel_target()


def test_load_log_uses_the_last_take(tmp_path):
    p = tmp_path / "C1.jsonl"
    lines = [{"t": 1, "step": "start"}, {"t": 5, "step": "record_toast"}, {"t": 9, "step": "abort"},
             {"t": 1.5, "step": "start"}, {"t": 7, "step": "record_toast"}]
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    assert edit.load_log(str(p)) == {"start": 1.5, "record_toast": 7}


def test_capture_refuses_to_overwrite_a_clip_without_force():
    with open(os.path.join(ROOT, "tools", "demo", "capture.sh"), encoding="utf-8") as f:
        src = f.read()
    assert '[ ! -e "$DIR/$clip.mkv" ] || [ "${3:-}" = force ] || die' in src
    assert tour.Capture("A1", force=True).force and not tour.Capture("A1").force


def test_check_media_top_level_mp4_boxes(tmp_path):
    box = lambda kind, body=b"": struct.pack(">I4s", 8 + len(body), kind) + body  # noqa: E731
    data = box(b"ftyp", b"isom\x00\x00\x02\x00") + box(b"moov", b"\x00" * 8) + box(b"udta", b"x") + box(b"mdat")
    path = write(tmp_path, "v.mp4", data)
    fails = check_media.check(path, {}, probe=lambda p: probe_json(100))
    assert any("udta" in f for f in fails)
    ok = box(b"ftyp", b"isom") + box(b"moov") + box(b"free") + box(b"mdat")
    assert check_media.check(write(tmp_path, "w.mp4", ok), {}, probe=lambda p: probe_json(100)) == []


MIN_MS = 60 * 1000
GR_CH = {"id": 3272302072, "channelType": "GR", "type": 1, "name": "ABC"}
BS_CH = {"id": 400101, "channelType": "BS", "serviceId": 101, "type": 1, "name": "NHK BS"}


def c_tour(tmp_path, monkeypatch, bad=None):
    import time

    tool = kd.dev_tool()
    monkeypatch.setattr(tool, "STATE", str(tmp_path / "state.json"))
    now = time.time() * 1000
    state = {"reserves": [], "snapshot": [1, 2], "snapshotAt": int(now - 5 * MIN_MS)}
    if bad == "no_snapshot":
        state.pop("snapshot")
    elif bad == "stale_snapshot":
        state["snapshotAt"] = int(now - 115 * MIN_MS)
    tool.save_state(state)
    start = now + (30 if bad == "too_soon" else 70) * MIN_MS
    prog = {"id": P, "channelId": GR_CH["id"], "startAt": start, "endAt": start + (30 if bad == "too_long" else 4)
            * MIN_MS, "isFree": bad != "paid", "name": "P"}
    if bad == "bs":
        prog["channelId"] = BS_CH["id"]
    reserves = []
    if bad == "reserved":
        reserves = [{"id": 9, "programId": P, "startAt": start, "endAt": start + 4 * MIN_MS}]
    elif bad == "conflict":
        reserves = [{"id": 10 + i, "startAt": start, "endAt": start + 4 * MIN_MS} for i in range(7)]
    answers = {f"/schedules/detail/{P}": prog, "/channels": [GR_CH, BS_CH], "/reserves": {"reserves": reserves}}
    epgs = FakeEpgs(answers=answers)
    labels = ["..", "21:50 P", "録画", "予約を削除", "キャンセル"]
    t, rpc, _ = scripted(labels, f"{ROOT_URL}?action=guide_channel&day=1", f"{ROOT_URL}?programId={P}", epgs=epgs,
                         program=P, channel=99 if bad == "channel" else GR_CH["id"])
    return t, rpc


def test_section_c_target_passes_the_pick_rules(tmp_path, monkeypatch):
    t, rpc = c_tour(tmp_path, monkeypatch)
    assert t.check_record_target()["id"] == P
    t.precheck("C")
    assert rpc.calls == []


@pytest.mark.parametrize("bad", ["no_snapshot", "stale_snapshot", "paid", "bs", "channel", "too_long", "too_soon",
                                 "reserved", "conflict"])
def test_section_c_refuses_before_the_gui_write(tmp_path, monkeypatch, bad):
    t, rpc = c_tour(tmp_path, monkeypatch, bad)
    with pytest.raises(kd.TakeError):
        t.precheck("C")
    assert rpc.calls == []  # before any Kodi input
    with pytest.raises(kd.TakeError):
        t.section_c()
    assert "Input.ContextMenu" not in [m for m, _, _ in rpc.calls]  # no 録画 opened, no reservation


def test_adopt_failure_names_the_reservation_and_how_to_remove_it():
    t, _ = adopt_tour([(5_001_000, P, 579)], {579: {"programId": P}})
    t.run_adopt = lambda rid: False
    with pytest.raises(kd.TakeError) as e:
        t.adopt()
    msg = str(e.value)
    assert "579" in msg and "予約を削除" in msg and "delete-reserve 579" in msg


# --- Codex round 3: live cleanup under hung / failing RPCs --------------------------------------------------------

def hang_tour(epgs=None, hang=(), fail=(), stop_hangs=False):
    """A live tour whose RPCs in `hang` block for the whole timeout they are given (then time out) and in `fail`
    raise at once; Player.Stop hangs too when stop_hangs."""
    t, rpc, clock = scripted(["NHK"], f"{ROOT_URL}?action=live&type=GR", "", epgs=epgs or FakeEpgs())
    orig = rpc.__call__
    seen = {"stop_at": None, "timeouts": [], "events": []}
    start = clock.t

    def call(method, params):
        key = params.get("booleans", [method])[0] if method == "XBMC.GetInfoBooleans" else method
        seen["timeouts"].append((key, t.k.current_timeout(), t.k.time_left()))
        if method == "Player.Stop":
            seen["stop_at"] = clock.t - start
            if stop_hangs:
                clock.sleep(t.k.current_timeout())
                raise TimeoutError("stop hung")
        if key in hang:
            clock.sleep(t.k.current_timeout())
            raise TimeoutError(f"{key} hung")
        if key in fail:
            raise ConnectionError(f"{key} failed")
        if key == "Player.HasVideo":
            return {key: True}
        return orig(method, params)

    t.k._rpc = call
    t.k.event_sender = lambda host, text: seen["events"].append(text)
    return t, rpc, clock, seen


def test_stop_is_sent_when_the_start_request_fails():
    t, _, _, seen = hang_tour(fail=("Input.Select",))
    with pytest.raises(ConnectionError):
        t.play_live()
    assert seen["stop_at"] is not None and t.k.deadline is None


def test_stop_is_sent_within_the_cap_when_rpcs_hang():
    t, _, _, seen = hang_tour(hang=("Input.Select", "Player.HasVideo"))
    with pytest.raises(TimeoutError):
        t.play_live()
    assert seen["stop_at"] is not None and seen["stop_at"] <= tour.LIVE_MAX_S + 1.0
    for key, timeout, left in seen["timeouts"]:
        if key != "Player.Stop" and left != float("inf"):
            assert timeout <= max(1.0, left) + 1e-6, key  # no RPC may wait past the deadline (+1 s minimum)
    assert [x for x in seen["timeouts"] if x[0] == "Player.Stop"][0][1] == kd.Kodi.STOP_TIMEOUT


def test_stop_falls_back_to_the_eventserver_when_stop_hangs():
    t, _, _, seen = hang_tour(hang=("Player.HasVideo",), stop_hangs=True)
    with pytest.raises(TimeoutError):
        t.play_live()
    assert seen["events"] == ["PlayerControl(Stop)"]


def test_play_live_drops_the_osd_when_little_time_is_left(monkeypatch):
    t, rpc, clock, seen = hang_tour()
    start = clock.t
    orig = t.k._rpc

    def slow(method, params):
        if method == "XBMC.GetInfoBooleans" and params["booleans"][0] == "Player.HasVideo":
            clock.t = start + tour.LIVE_MAX_S - 2  # the video came very late
            return {"Player.HasVideo": True}
        return orig(method, params)

    t.k._rpc = slow
    monkeypatch.setattr(tour, "LIVE_S", tour.LIVE_MAX_S)
    t.play_live()
    names = [m for m, _, _ in rpc.calls]
    assert "Input.Info" not in names
    assert seen["stop_at"] is not None and seen["stop_at"] <= tour.LIVE_MAX_S + 0.5


def test_abort_cleanup_never_raises_and_stops_first():
    calls = []

    def dead(method, params):
        calls.append(method)
        raise ConnectionError("down")

    events = []
    k = kd.Kodi("kodi-dev.test", rpc=dead, sleep=lambda s: None)
    k.event_sender = lambda host, text: events.append(text)
    k._no_select = True
    k.abort_cleanup()
    assert calls[0] == "Player.Stop" and events == ["PlayerControl(Stop)"] and not k._no_select


class MainKodi:
    def __init__(self):
        self.cleaned, self.logged = 0, []

    def reset_clock(self):
        pass

    def log(self, step, **kw):
        self.logged.append(step)

    def abort_cleanup(self):
        self.cleaned += 1


def run_main(monkeypatch, tmp_path, section_error, section="A", argv=()):
    k = MainKodi()
    epgs = FakeEpgs()
    epgs.preflight_failures = lambda guard: []
    monkeypatch.setattr(kd, "env_host", lambda: "kodi-dev.test")
    monkeypatch.setattr(kd, "check_hosts", lambda host: "http://kodi-dev.test:8888")
    monkeypatch.setattr(tour, "ssh", lambda cmd, **kw: '<setting id="server_url">http://localhost:8888</setting>')
    monkeypatch.setattr(kd, "build", lambda log: (k, epgs, None))
    monkeypatch.setattr(tour.Tour, "precheck", lambda self, s: None)
    monkeypatch.setattr(tour.Tour, "prep", lambda self, s: None)

    def boom(self):
        self.pending.append("something to clean up")
        raise section_error

    monkeypatch.setattr(tour.Tour, f"section_{section.lower()}", boom)
    return k, lambda: tour.main([section, "--log", str(tmp_path / "x.jsonl"), *argv])


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("rpc"), TimeoutError("hung")])
def test_main_cleans_up_on_any_exception_and_reraises(monkeypatch, tmp_path, capsys, error):
    k, run = run_main(monkeypatch, tmp_path, error)
    with pytest.raises(type(error)):
        run()
    assert k.cleaned == 1
    assert "cleanup: something to clean up" in capsys.readouterr().err


def test_main_take_error_exits_2_with_the_cleanup_hints(monkeypatch, tmp_path, capsys):
    k, run = run_main(monkeypatch, tmp_path, kd.TakeError("guard"), section="D", argv=["--rec", "69"])
    with pytest.raises(SystemExit) as e:
        run()
    err = capsys.readouterr().err
    assert e.value.code == 2 and k.cleaned == 1 and "something to clean up" in err and "demo-audit" in err


def test_section_c_leaves_a_hint_when_adopt_fails_after_the_write(tmp_path, monkeypatch):
    t, rpc = c_tour(tmp_path, monkeypatch)

    def no_adopt():
        raise kd.TakeError("adopt failed")

    t.adopt = no_adopt
    with pytest.raises(kd.TakeError):
        t.section_c()
    assert t.pending and str(P) in t.pending[-1] and "予約を削除" in t.pending[-1]


def test_section_c_checks_the_reservation_is_gone_after_the_cancel(tmp_path, monkeypatch):
    t, rpc = c_tour(tmp_path, monkeypatch)
    t.adopt = lambda: 579
    t.epgs.reserves[579] = {"id": 579, "programId": P}  # 予約を削除 did not remove it
    with pytest.raises(kd.TakeError) as e:
        t.section_c()
    assert "579" in str(e.value) and "delete-reserve 579" in t.pending[-1]
    del t.epgs.reserves[579]
    t2, _ = c_tour(tmp_path, monkeypatch)
    t2.adopt = lambda: 579
    t2.section_c()
    assert t2.pending == []


def test_ssh_has_a_timeout(monkeypatch):
    import subprocess

    def hung(*a, **kw):
        assert kw.get("timeout")
        raise subprocess.TimeoutExpired(a[0], kw["timeout"])

    monkeypatch.setattr(tour, "ssh_target", lambda: "kodi-dev.test")
    monkeypatch.setattr(tour.subprocess, "run", hung)
    with pytest.raises(kd.TakeError):
        tour.ssh("true")
