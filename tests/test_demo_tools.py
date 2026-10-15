"""tools/demo (README demo video): shoot.py's pure helpers, edit.py's segments and ffmpeg argv, cuts.json, and
check_media.py on generated samples. No VM, no ffmpeg needed."""
import json
import os
import struct
import sys
import zlib

import pytest
from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "tools", "demo"))
import check_media  # noqa: E402
import edit  # noqa: E402

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
    # broadcast footage stays short: every playback range (recording, live, transcoded) at most 5 s
    playing = [r for p in cuts["video"] + cuts["gif"] for r in p["keep"] if r["to"][0] == "playing"]
    assert len(playing) >= 3
    for r in playing:
        assert r["from"][0] == "playing" or r.get("speed", 1.0) >= 3.0  # the wait before the picture, sped up
        if r["from"][0] == "playing":
            assert (r["to"][1] - r["from"][1]) / r.get("speed", 1.0) <= 5.0


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


def test_load_log_uses_the_last_take(tmp_path):
    p = tmp_path / "C1.jsonl"
    lines = [{"t": 1, "step": "start"}, {"t": 5, "step": "record_toast"}, {"t": 9, "step": "abort"},
             {"t": 1.5, "step": "start"}, {"t": 7, "step": "record_toast"}]
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    assert edit.load_log(str(p)) == {"start": 1.5, "record_toast": 7}


def test_check_media_top_level_mp4_boxes(tmp_path):
    box = lambda kind, body=b"": struct.pack(">I4s", 8 + len(body), kind) + body  # noqa: E731
    data = box(b"ftyp", b"isom\x00\x00\x02\x00") + box(b"moov", b"\x00" * 8) + box(b"udta", b"x") + box(b"mdat")
    path = write(tmp_path, "v.mp4", data)
    fails = check_media.check(path, {}, probe=lambda p: probe_json(100))
    assert any("udta" in f for f in fails)
    ok = box(b"ftyp", b"isom") + box(b"moov") + box(b"free") + box(b"mdat")
    assert check_media.check(write(tmp_path, "w.mp4", ok), {}, probe=lambda p: probe_json(100)) == []


# --- Codex round 3: live cleanup under hung / failing RPCs --------------------------------------------------------




# --- shoot.py -----------------------------------------------------------------------------------------------------

CHANNELS = [
    {"id": 1, "channelType": "GR", "serviceId": 1024, "name": "ＮＨＫ総合１・大阪"},
    {"id": 2, "channelType": "BS", "serviceId": 101, "name": "ＮＨＫ　ＢＳ"},
    {"id": 3, "channelType": "BS", "serviceId": 191, "name": "ＷＯＷＯＷプライム"},
    {"id": 4, "channelType": "BS", "serviceId": 766, "name": "ＮＨＫ　ＢＳ"},  # data service: a free name
    {"id": 5, "channelType": "CS", "serviceId": 55, "name": "ショップチャンネル"},
    {"id": 6, "channelType": "CS", "serviceId": 999, "name": " "},  # blank: would match every label
]


def test_shoot_paid_channels_are_half_width_and_never_a_free_name():
    import shoot

    assert shoot.paid_channels("", CHANNELS) == ["WOWOWプライム", "ショップチャンネル"]


def test_shoot_eventserver_packets_follow_the_xbmcclient_layout(monkeypatch):
    import socket

    import shoot

    sent = []

    class Sock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendto(self, data, addr):
            sent.append((data, addr))

    monkeypatch.setattr(socket, "socket", Sock)
    monkeypatch.setattr(shoot.time, "sleep", lambda s: None)
    shoot.Kodi("kodi").builtin("Container.Refresh")
    kinds = [struct.unpack("!H", d[6:8])[0] for d, _ in sent]
    assert kinds == [0x01, 0x0A, 0x02] and all(d[:4] == b"XBMC" and a == ("kodi", 9777) for d, a in sent)
    action = sent[1][0]
    assert struct.unpack("!H", action[16:18])[0] == len(action) - 32  # payload size after the 32-byte header
    assert action[32:] == b"\x01Container.Refresh\x00"


def test_shoot_guard_checks_only_the_rows_on_screen(monkeypatch):
    import shoot

    rows = [{"label": f"row {i}"} for i in range(shoot.VISIBLE_ROWS)] + [{"label": "WOWOWプライム  x"}]
    k = shoot.Kodi("kodi", ["WOWOWプライム"])
    monkeypatch.setattr(k, "folder", lambda: shoot.PLUGIN + "?action=list")
    monkeypatch.setattr(k, "rpc", lambda method, **kw: {"files": rows})
    k.check_listing()  # the paid row is below what the list shows
    rows.insert(3, {"label": "WOWOWプライム  y"})
    with pytest.raises(shoot.AbortError):
        k.check_listing()
