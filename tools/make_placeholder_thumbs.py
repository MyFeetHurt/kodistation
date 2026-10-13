"""Replace fixture thumbnails with generated placeholders (backlog 019: no broadcast stills in the public tree).

    uv run --with pillow python tools/make_placeholder_thumbs.py [FILE.jpg | DIR ...]
        (default: every tests/fixtures/*/api/thumbnails/*.jpg)

Each JPEG becomes a deterministic 480x270 image carrying the JPEG comment `KODIstation placeholder`
(tools/public_audit.py accepts only such images in the fixtures). A source that thumbs.is_blank calls blank becomes
a solid black placeholder (still blank); every other one becomes a distinct pattern with its id as text, well above
the blank threshold, so the blank / content cases of the tests stay what they were. Running it twice changes nothing.
`make snapshot-dev` / `snapshot-dev2` run it after a capture.

It then rewrites tools/placeholder_thumbs.txt (`sha256  path`) for every fixture thumbnail that is a placeholder:
the public audit accepts a fixture JPEG only when it is listed there with the same bytes.
"""
import glob
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin.video.epgstation"))
from resources.lib.epgstation import thumbs  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import public_audit  # noqa: E402

MARK = public_audit.MARK
MANIFEST = os.path.join(ROOT, "tools", "placeholder_thumbs.txt")
W, H = 480, 270
MIN_CONTENT_BYTES = 6000


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def placeholder(name, blank):
    """JPEG bytes of the placeholder for the thumbnail file `name` (e.g. "11.jpg")."""
    import io
    if blank:
        img = Image.new("RGB", (W, H), (0, 0, 0))
        quality = 75
    else:
        rnd = random.Random(name)
        base = tuple(rnd.randrange(40, 200) for _ in range(3))
        img = Image.new("RGB", (W, H), base)
        d = ImageDraw.Draw(img)
        for _ in range(60):  # random tiles: enough detail to stay far above the blank threshold
            x, y = rnd.randrange(W), rnd.randrange(H)
            w, h = rnd.randrange(10, 120), rnd.randrange(10, 80)
            d.rectangle((x, y, x + w, y + h), fill=tuple(rnd.randrange(256) for _ in range(3)))
        for _ in range(4000):
            d.point((rnd.randrange(W), rnd.randrange(H)), fill=tuple(rnd.randrange(256) for _ in range(3)))
        d.rectangle((0, H - 70, W, H), fill=(0, 0, 0))
        d.text((16, H - 64), os.path.splitext(name)[0], fill=(255, 255, 255), font=_font(40))
        d.text((200, H - 44), "KODIstation placeholder", fill=(200, 200, 200), font=_font(20))
        quality = 85
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, comment=MARK, optimize=False, progressive=False)
    return out.getvalue()


def convert(path):
    """Rewrite one JPEG; returns 'blank' or 'content'."""
    with open(path, "rb") as f:
        data = f.read()
    nbytes, width, height = thumbs.summary(data)
    blank = thumbs.is_blank(nbytes, width, height)
    body = placeholder(os.path.basename(path), blank)
    b_bytes, b_w, b_h = thumbs.summary(body)
    if thumbs.is_blank(b_bytes, b_w, b_h) != blank or (not blank and b_bytes < MIN_CONTENT_BYTES):
        raise SystemExit(f"{path}: placeholder does not keep the blank/content class")
    if body != data:
        with open(path, "wb") as f:
            f.write(body)
    return "blank" if blank else "content"


def main(argv):
    paths = []
    for arg in argv or [os.path.join(ROOT, "tests", "fixtures")]:
        if os.path.isdir(arg):
            paths += sorted(glob.glob(os.path.join(arg, "**", "thumbnails", "*.jpg"), recursive=True))
        else:
            paths.append(arg)
    counts = {"blank": 0, "content": 0}
    for p in paths:
        counts[convert(p)] += 1
    print(f"{len(paths)} placeholders ({counts['content']} content, {counts['blank']} blank)")
    print(f"{write_manifest()} entries in {os.path.relpath(MANIFEST, ROOT)}")
    return 0


def regenerated(path):
    """True when the file is exactly the placeholder this script generates for it (verified by regenerating)."""
    with open(path, "rb") as f:
        data = f.read()
    nbytes, width, height = thumbs.summary(data)
    return data == placeholder(os.path.basename(path), thumbs.is_blank(nbytes, width, height))


def write_manifest(root=ROOT, manifest=MANIFEST):
    """List every fixture thumbnail that regenerates byte for byte (sha256  path, sorted). A marked JPEG that this
    script did not make (a copied still with the comment added) is left out, so the audit rejects it."""
    import hashlib
    lines = []
    for p in sorted(glob.glob(os.path.join(root, "tests", "fixtures", "**", "thumbnails", "*.jpg"), recursive=True)):
        if not regenerated(p):
            print(f"not a generated placeholder, left out of the list: {os.path.relpath(p, root)}")
            continue
        with open(p, "rb") as f:
            lines.append(f"{hashlib.sha256(f.read()).hexdigest()}  {os.path.relpath(p, root)}")
    with open(manifest, "w", encoding="utf-8") as f:
        f.write("# Generated fixture thumbnails (tools/make_placeholder_thumbs.py writes this list; do not edit).\n"
                "# tools/public_audit.py accepts a fixture JPEG only when it is listed here with these bytes.\n")
        f.write("".join(line + "\n" for line in lines))
    return len(lines)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
