#!/usr/bin/env python3
"""Demo video (backlog 021): build the README media from the raw clips and tools/demo/cuts.json (stdlib + ffmpeg).

    tools/demo/edit.py --raw DIR --out media [--work DIR] [--font FILE]

DIR holds the raw clips (CLIP.mkv) and their tour logs (CLIP.jsonl), both private. cuts.json names, per clip, the
kept ranges as tour-log steps plus offsets (seconds), a speed for each range and the section caption. Writes:
    kodistation-demo.mp4   1280x800, 30 fps, H.264 High yuv420p, no audio, faststart, no metadata, <= 10 MB
    demo.gif               the highlight, 800x500, 12 fps
    screenshot-*.png       1280x800 frames picked by step, only IHDR/PLTE/IDAT/IEND (+ sRGB/gAMA/pHYs) chunks
and prints each file's size, duration and sha256. The master (lossless, captions burned in) stays in --work.
"""
import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CUTS = os.path.join(HERE, "cuts.json")
FPS = 30
SIZE = (1280, 800)
MP4_TARGET = 10 * 1000 * 1000
MP4_CAP = 15 * 1000 * 1000
STRIP = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact"]
PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"pHYs"}
FONTS = ("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
         "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc")


def load_log(path):
    """{step: t} of a tour log: the last take only (the steps after the last "start" line), each step's first time."""
    steps = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["step"] == "start":
                steps = {}
            steps.setdefault(d["step"], d["t"])
    return steps


def resolve(point, steps, offset):
    """A range end: a number (log seconds) or [step, delta]; returns clip seconds (log t + clip offset)."""
    if isinstance(point, (int, float)):
        return float(point) + offset
    step, delta = point
    if step not in steps:
        raise SystemExit(f"cuts.json: step {step!r} is not in the tour log")
    return steps[step] + delta + offset


def segments(cuts, logs, key="video"):
    """[(clip, start, end, speed)] in cuts.json order; logs: {clip: {step: t}}."""
    out = []
    for part in cuts[key]:
        clip, off = part["clip"], cuts.get("offsets", {}).get(part["clip"], 0.0)
        for r in part["keep"]:
            start, end = resolve(r["from"], logs[clip], off), resolve(r["to"], logs[clip], off)
            if end <= start:
                raise SystemExit(f"cuts.json: empty range in {clip}: {r}")
            out.append((clip, round(start, 3), round(end, 3), float(r.get("speed", 1.0))))
    return out


def captions(cuts, segs):
    """[(text, t0, t1)] in output seconds: each part's caption over its first `caption_s` seconds."""
    out, t, i = [], 0.0, 0
    for part in cuts["video"]:
        n = len(part["keep"])
        if part.get("caption"):
            out.append((part["caption"], round(t + 0.2, 3), round(t + 0.2 + part.get("caption_s", 3.0), 3)))
        for _clip, s, e, speed in segs[i:i + n]:
            t += (e - s) / speed
        i += n
    return out


def duration(segs):
    return sum((e - s) / speed for _, s, e, speed in segs)


def esc(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def concat_argv(raw, segs, out, scale=None, fps=FPS, caps=(), font=None, extra=()):
    """ffmpeg argv: one input per segment (accurate seek), speed, concat, optional captions/scale, lossless out."""
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    chains = []
    for i, (clip, start, end, speed) in enumerate(segs):
        argv += ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", os.path.join(raw, f"{clip}.mkv")]
        chains.append(f"[{i}:v]setpts=(PTS-STARTPTS)/{speed},fps={fps},format=bgr0[s{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[s{i}]" for i in range(len(segs))) + \
        f"concat=n={len(segs)}:v=1:a=0[c]"
    last = "c"
    for j, (text, t0, t1) in enumerate(caps):
        graph += (f";[{last}]drawtext=fontfile='{font}':text='{esc(text)}':fontsize=34:fontcolor=white:"
                  f"box=1:boxcolor=black@0.62:boxborderw=14:x=(w-text_w)/2:y=h-text_h-56:"
                  f"enable='between(t,{t0},{t1})'[t{j}]")
        last = f"t{j}"
    if scale:
        graph += f";[{last}]scale={scale[0]}:{scale[1]}:flags=lanczos[z]"
        last = "z"
    argv += ["-filter_complex", graph, "-map", f"[{last}]", "-an", *extra, *STRIP, out]
    return argv


def mp4_argv(master, out, crf):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", master, "-an", "-c:v", "libx264",
            "-profile:v", "high", "-preset", "veryslow", "-tune", "animation", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-bsf:v", "filter_units=remove_types=6", "-movflags", "+faststart", *STRIP,
            "-metadata:s:v:0", "encoder=", out]


def gif_argv(master, out, fps=12, size=(800, 500), colors=128):
    """128 colours: the UI is flat; 256 put the highlight over 5 MB."""
    graph = (f"fps={fps},scale={size[0]}:{size[1]}:flags=lanczos,split[a][b];"
             f"[a]palettegen=stats_mode=diff:max_colors={colors}[p];"
             "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", master, "-filter_complex", graph, "-an",
            *STRIP, out]


def frame_argv(raw, clip, t, out):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i",
            os.path.join(raw, f"{clip}.mkv"), "-frames:v", "1", "-an", *STRIP, "-c:v", "png", out]


def clean_png(path):
    """Rewrite a PNG keeping only the PNG_KEEP chunks (no tEXt/iTXt/zTXt/eXIf/tIME)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path}: not a PNG")
    out, pos = [data[:8]], 8
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos:pos + 12 + n]
        if kind in PNG_KEEP:
            out.append(chunk)
        pos += 12 + n
    with open(path, "wb") as f:
        f.write(b"".join(out))




def run(argv):
    print("$ " + " ".join(argv[:12]) + (" …" if len(argv) > 12 else ""))
    subprocess.run(argv, check=True)


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip() or 0)


def report(path):
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    dur = probe_duration(path) if not path.endswith(".png") else 0.0
    print(f"{os.path.basename(path)}  {os.path.getsize(path)} bytes  {dur:.2f} s  sha256 {digest}")


def find_font(font):
    for f in ([font] if font else []) + list(FONTS):
        if f and os.path.isfile(f):
            return f
    raise SystemExit("no Noto Sans CJK font found: pass --font")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", help="master files (default: --raw)")
    ap.add_argument("--font")
    ap.add_argument("--cuts", default=CUTS)
    ap.add_argument("--plan", action="store_true", help="print the segments and durations only")
    args = ap.parse_args()
    with open(args.cuts, encoding="utf-8") as f:
        cuts = json.load(f)
    clips = {p["clip"] for key in ("video", "gif") for p in cuts[key]} | {s["clip"] for s in cuts["screenshots"]}
    logs = {c: load_log(os.path.join(args.raw, f"{c}.jsonl")) for c in sorted(clips)}
    segs, gsegs = segments(cuts, logs), segments(cuts, logs, "gif")
    caps = captions(cuts, segs)
    for s in segs:
        print("video", *s)
    print(f"video: {duration(segs):.2f} s; gif: {duration(gsegs):.2f} s; captions {caps}")
    if args.plan:
        return
    work = args.work or args.raw
    os.makedirs(args.out, exist_ok=True)
    font = find_font(args.font)
    master = os.path.join(work, "master.mkv")
    run(concat_argv(args.raw, segs, master, caps=caps, font=font,
                    extra=["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0"]))
    gmaster = os.path.join(work, "gif-master.mkv")
    run(concat_argv(args.raw, gsegs, gmaster, extra=["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0"]))
    mp4 = os.path.join(args.out, "kodistation-demo.mp4")
    for crf in cuts.get("crf", [24, 26, 28, 30, 32]):
        run(mp4_argv(master, mp4, crf))
        if os.path.getsize(mp4) <= MP4_TARGET:
            break
    if os.path.getsize(mp4) > MP4_CAP:
        sys.exit(f"{mp4}: {os.path.getsize(mp4)} bytes, over the {MP4_CAP} cap")
    run(gif_argv(gmaster, os.path.join(args.out, "demo.gif")))
    for shot in cuts["screenshots"]:
        t = resolve(shot["at"], logs[shot["clip"]], cuts.get("offsets", {}).get(shot["clip"], 0.0))
        png = os.path.join(args.out, shot["name"])
        run(frame_argv(args.raw, shot["clip"], t, png))
        clean_png(png)
    for name in ["kodistation-demo.mp4", "demo.gif"] + [s["name"] for s in cuts["screenshots"]]:
        report(os.path.join(args.out, name))


if __name__ == "__main__":
    main()
