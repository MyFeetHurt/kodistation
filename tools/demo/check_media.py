#!/usr/bin/env python3
"""Demo video (backlog 021): check the README media before they are committed (stdlib + ffprobe).

    tools/demo/check_media.py FILE...

PNG: 1280x800, only IHDR/PLTE/IDAT/IEND/sRGB/gAMA/pHYs chunks.
GIF: no comment extension, no application extension but the NETSCAPE2.0 / ANIMEXTS1.0 loop block (no XMP), 12-18 s,
<= 8 MB.
MP4: exactly one stream (H.264 1280x800 video, no audio), top-level boxes only ftyp/moov/free/mdat with moov before
mdat (faststart), no tags but the container basics (no encoder/title/comment/creation_time/location), 90-120 s,
<= 15 MB.
Every file: no private literal of PUBLIC_AUDIT_DENY / PUBLIC_AUDIT_DENY_FILE (local.env) and none of the generic
rules (RFC 1918 addresses, /home/, /mnt/) in the raw bytes or the ffprobe dump. Exit 1 with the reasons.
"""
import json
import os
import re
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_env  # noqa: E402  (019: private literals from local.env)

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_OK = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"pHYs"}
GIF_APPS_OK = {b"NETSCAPE2.0", b"ANIMEXTS1.0"}
MP4_BOXES_OK = {"ftyp", "moov", "free", "mdat"}  # no udta/meta/uuid/XMP box at the top level
MP4_TAGS_OK = {"major_brand", "minor_version", "compatible_brands", "handler_name", "vendor_id"}
LIMITS = {".mp4": (90.0, 120.0, 15_000_000), ".gif": (12.0, 18.0, 8_000_000)}
WARN = {".mp4": 10_000_000, ".gif": 5_000_000}
PNG_SIZE = (1280, 800)
GENERIC = [re.compile(rb"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])"),
           re.compile(rb"/home/"), re.compile(rb"/mnt/")]


def deny_literals(env=None):
    """PUBLIC_AUDIT_DENY from local.env (comma-separated, backlog 019; | also splits) and the one-per-line
    PUBLIC_AUDIT_DENY_FILE when set."""
    if env is None:
        local_env.load()
        env = os.environ
    out = [x.strip() for x in re.split(r"[,|]", env.get("PUBLIC_AUDIT_DENY", "")) if x.strip()]
    deny_file = env.get("PUBLIC_AUDIT_DENY_FILE", "")
    if deny_file and os.path.isfile(os.path.expanduser(deny_file)):
        with open(os.path.expanduser(deny_file), encoding="utf-8") as f:
            out += [x.strip() for x in f if x.strip() and not x.startswith("#")]
    return out


def leak_failures(name, data, deny):
    out = [f"{name}: matches {p.pattern!r}" for p in GENERIC if p.search(data)]
    low = data.lower()
    out += [f"{name}: contains a private literal (PUBLIC_AUDIT_DENY #{i + 1})"
            for i, lit in enumerate(deny) if lit.lower().encode() in low]
    return out


def png_failures(name, data):
    if data[:8] != PNG_SIG:
        return [f"{name}: not a PNG"]
    out, pos, kinds = [], 8, []
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        kinds.append(kind)
        if kind == b"IHDR":
            w, h = struct.unpack(">II", data[pos + 8:pos + 16])
            if (w, h) != PNG_SIZE:
                out.append(f"{name}: {w}x{h} (expected {PNG_SIZE[0]}x{PNG_SIZE[1]})")
        pos += 12 + n
    out += [f"{name}: PNG chunk {k.decode('latin-1')}" for k in kinds if k not in PNG_OK]
    if not kinds or kinds[-1] != b"IEND":
        out.append(f"{name}: no IEND")
    return out


def _sub_blocks(data, pos):
    """Skip GIF data sub-blocks from pos; returns (end, first block bytes)."""
    first = None
    while True:
        n = data[pos]
        pos += 1
        if n == 0:
            return pos, first or b""
        if first is None:
            first = data[pos:pos + n]
        pos += n


def gif_failures(name, data):
    """Walks the GIF block structure (a 0x21 0xFE inside image data is not a comment)."""
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        return [f"{name}: not a GIF"]
    out = []
    flags = data[10]
    pos = 13 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)
    try:
        while True:
            b = data[pos]
            if b == 0x3B:
                break
            if b == 0x2C:
                lflags = data[pos + 9]
                pos += 10 + (3 * 2 ** ((lflags & 7) + 1) if lflags & 0x80 else 0)
                pos, _ = _sub_blocks(data, pos + 1)  # + the LZW minimum code size
            elif b == 0x21:
                label = data[pos + 1]
                end, first = _sub_blocks(data, pos + 2)
                if label == 0xFE:
                    out.append(f"{name}: GIF comment extension")
                elif label == 0xFF and first[:11] not in GIF_APPS_OK:
                    out.append(f"{name}: GIF application extension {first[:11]!r} (XMP?)")
                pos = end
            else:
                return out + [f"{name}: bad GIF block 0x{b:02x} at {pos}"]
    except IndexError:
        out.append(f"{name}: truncated GIF")
    return out


def mp4_box_order(data):
    out, pos = [], 0
    while pos + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        elif size == 0:
            size = len(data) - pos
        out.append(kind.decode("latin-1"))
        if size < 8:
            break
        pos += size
    return out


def ffprobe(path):
    """ffprobe's JSON with format.filename cut to the base name (the local path is not in the file)."""
    done = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                          capture_output=True, text=True, check=True)
    info = json.loads(done.stdout)
    if "filename" in info.get("format", {}):
        info["format"]["filename"] = os.path.basename(info["format"]["filename"])
    return json.dumps(info, ensure_ascii=False)


def media_failures(name, ext, data, probe):
    """MP4/GIF checks on the ffprobe JSON (streams, tags, duration, size)."""
    info = json.loads(probe)
    out, fmt, streams = [], info.get("format", {}), info.get("streams", [])
    low, high, cap = LIMITS[ext]
    dur = float(fmt.get("duration") or 0)
    if not low <= dur <= high:
        out.append(f"{name}: duration {dur:.2f} s (expected {low:.0f}-{high:.0f} s)")
    if len(data) > cap:
        out.append(f"{name}: {len(data)} bytes (cap {cap})")
    if len(streams) != 1 or streams[0].get("codec_type") != "video":
        out.append(f"{name}: streams {[s.get('codec_type') for s in streams]} (expected one video stream)")
    if ext == ".mp4":
        s = streams[0] if streams else {}
        if (s.get("codec_name"), s.get("width"), s.get("height")) != ("h264", 1280, 800):
            out.append(f"{name}: video {s.get('codec_name')} {s.get('width')}x{s.get('height')}")
        tags = dict(fmt.get("tags") or {})
        for st in streams:
            tags.update(st.get("tags") or {})
        out += [f"{name}: tag {k}={v!r}" for k, v in tags.items()
                if k not in MP4_TAGS_OK and (k, v) != ("language", "und")]
        order = mp4_box_order(data)
        out += [f"{name}: top-level MP4 box {b!r}" for b in order if b not in MP4_BOXES_OK]
        if "moov" not in order or "mdat" not in order or order.index("moov") > order.index("mdat"):
            out.append(f"{name}: moov not before mdat (no faststart): {order}")
    return out


def check(path, env=None, probe=ffprobe):
    name, ext = os.path.basename(path), os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = f.read()
    deny = deny_literals(env)
    out = leak_failures(name, data, deny)
    if ext == ".png":
        return out + png_failures(name, data)
    if ext not in LIMITS:
        return out + [f"{name}: not a .png, .gif or .mp4"]
    dump = probe(path)
    out += leak_failures(name + " (ffprobe)", dump.encode(), deny)
    if ext == ".gif":
        out += gif_failures(name, data)
    out += media_failures(name, ext, data, dump)
    if not out and len(data) > WARN[ext]:
        print(f"note: {name}: {len(data)} bytes, over the {WARN[ext]} target (under the cap)")
    return out


def main(argv=None):
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        sys.exit("usage: check_media.py FILE...")
    fails = [f for p in paths for f in check(p)]
    for f in fails:
        print("FAIL:", f)
    print(f"check_media: {len(paths)} files: " + ("FAIL" if fails else "PASS"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
