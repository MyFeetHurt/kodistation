#!/usr/bin/env python3
"""Leak audit of the public export (backlog 019). Stdlib only.

    tools/public_audit.py [--strict] DIR            every file under DIR (the `git archive` tree)
    tools/public_audit.py [--strict] --worktree [REPO]   the export of REPO's working tree (tracked files plus
                                                    untracked, not ignored ones, as `git add -A` would commit
                                                    them), through `git archive` and .gitattributes export-ignore
    tools/public_audit.py [--strict] --zip Z...     the members of built zips
    tools/public_audit.py [--strict] --history REPO every commit / tag of a git repository (identity + messages)

The deny lists come from the environment or local.env (tools/local_env.py). Prints `path:line: rule` per hit
and a count summary; exit 1 on any hit, 2 on a usage or setup error.
A hit never prints the matched private value, only where it is and which rule found it.

Rules (text, file names and git metadata)
- generic (safe to publish, part of the tree): private IPv4 addresses (RFC 1918, also with `x` / `*` octets,
  100.64/10, 169.254/16), short host forms (a dot and 2-3 digits after a space), IPv6 ULA / link-local, ssh
  URLs, scp-style git remotes (github.com allowed), `/home/<name>`, the private Gitea credentials file, and
  outside tests/fixtures an e-mail address that is not `@users.noreply.github.com`, `@example.*` or a reserved
  `.test` / `.invalid` / `.localhost` domain; base64 data URIs (an embedded image is a binary in disguise); also
  over the NFKC (full-width) and percent-decoded forms of each line;
- private literals: $PUBLIC_AUDIT_DENY (comma-separated, case-insensitive, never written into the tree) plus the
  values of the Gitea credentials file ($PUBLIC_AUDIT_CREDENTIALS_FILE, default: the one in ~/.config/epgstation-kodi,
  read at run time so a rotated password stays covered), in every file including fixtures, and in file names.
  Literals shorter than 6 characters match only as a whole word;
- production fingerprints: $PUBLIC_AUDIT_DENY_FILE (one per line, `#` comments; several files separated by `:`),
  the private list `make snapshot-prod` writes (backlog 020). A fingerprint named by an allow list
  (tools/fingerprint_allow.txt and $PUBLIC_AUDIT_ALLOW_FILE) is not a hit. Raw and NFKC text are searched.
- binaries are denied by default: a file with a NUL byte or a non-text extension passes only when its sha256 is
  listed in tools/public_images.txt (README media under media/: `.png .gif .mp4`; the icons), or when it is a
  fixture thumbnail listed in tools/placeholder_thumbs.txt (written by tools/make_placeholder_thumbs.py) with only
  the plain JPEG segments the generator writes. Listed files must have their metadata stripped (no EXIF/XMP/IPTC,
  no PNG text or time chunks, no GIF comments, no MP4 title/comment/location tags), and their raw bytes pass the
  text rules. Archives, stream / other video files and other image formats always fail. UTF-16 text with a BOM is
  read as text. Symbolic links always fail (their names and targets are audited as well).
- history: every author, committer and tagger e-mail ends in `@users.noreply.github.com`; their names, every ref
  name and every commit and tag message pass the text rules.

Every printed location has the matched private or generic part masked as `***`.

--strict (export_public.py) fails when neither private literals nor a fingerprint file is configured.
"""
import argparse
import hashlib
import io
import os
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import urllib.parse
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_env  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = b"KODIstation placeholder"
MEDIA_LIST = os.path.join(ROOT, "tools", "public_images.txt")
PLACEHOLDER_LIST = os.path.join(ROOT, "tools", "placeholder_thumbs.txt")
ALLOW_LIST = os.path.join(ROOT, "tools", "fingerprint_allow.txt")
CREDENTIALS = os.path.join("~/.config/epgstation-kodi", "gitea")
MEDIA = (".png", ".gif", ".mp4")  # allowed under media/ when listed
LISTED_ELSEWHERE = (".png",)  # e.g. the add-on icons
VIDEOS = (".mp4", ".webm", ".mov", ".avi", ".m4v", ".mkv", ".ts", ".m2ts", ".mts", ".m2t", ".wmv", ".flv", ".mpg",
          ".mpeg", ".3gp")
OTHER_IMAGES = (".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".webp", ".jpeg", ".ico", ".psd", ".raw", ".cr2",
                ".nef", ".dng", ".svgz")
ARCHIVES = (".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".zst", ".7z", ".rar", ".jar", ".whl", ".apk", ".lz",
            ".lzma", ".cab", ".iso", ".dmg", ".deb", ".rpm")
TEXT = (".py", ".sh", ".md", ".txt", ".json", ".xml", ".po", ".toml", ".lock", ".yml", ".yaml", ".service", ".js",
        ".example", ".gitignore", ".gitattributes", ".cfg", ".ini", ".html", ".css", ".csv", ".svg")
TEXT_NAMES = ("Makefile", "LICENSE", "Dockerfile")
SKIP_TEXT = (".lock",)  # uv.lock: package hashes and PyPI URLs only (still checked for private literals)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d|x|\*)"

GENERIC = [
    ("private IPv4 address", re.compile(
        r"(?<![\w.])(?:10(?:\." + _OCTET + r"){3}|172\.(?:1[6-9]|2\d|3[01])(?:\." + _OCTET + r"){2}|"
        r"192\.168(?:\." + _OCTET + r"){2}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\." + _OCTET + r"){2}|"
        r"169\.254(?:\." + _OCTET + r"){2})(?![\w])")),
    ("short host address (.NNN)", re.compile(r"(?:^|(?<=[\s(]))\.\d{2,3}(?![\w.%])")),
    ("private IPv6 address", re.compile(r"(?<![\w:])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):[0-9a-f:]*", re.I)),
    ("ssh URL", re.compile(r"ssh:/{2}")),
    ("embedded data URI", re.compile(r"\bdata:[a-z]+/[\w.+-]+(?:;[\w=.-]+)*;base64,", re.I)),
    ("scp-style git remote", re.compile(r"\bgit@(?!github\.com:)[\w.-]+:")),
    ("/home/<name> path", re.compile(r"/home/[a-z_][\w.-]*")),
    ("private credentials file", re.compile(r"epgstation-kodi/gite[a]\b")),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z0-9-]*)\b")
EMAIL_OK = re.compile(r"(?:^|\.)(?:users\.noreply\.github\.com|example\.[a-z.]+|example|test|invalid|localhost)$", re.I)
NOT_A_DOMAIN = re.compile(r"\.(?:xml|json|py|txt|zip|png|jpg|md|html|sha256|po|sh)$", re.I)
NOREPLY = "@users.noreply.github.com"
# metadata that may carry names, paths, places or times
PNG_OK = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"pHYs", b"bKGD",
          b"sBIT", b"acTL", b"fcTL", b"fdAT"}
MP4_TAGS = (b"\xa9nam", b"\xa9ART", b"\xa9cmt", b"\xa9day", b"\xa9xyz", b"\xa9des", b"desc", b"\xa9alb", b"\xa9aut",
            b"\xa9wrt", b"XMP_", b"loci")


def _nfkc(text):
    return unicodedata.normalize("NFKC", text)


def _read_list(path):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def _read_sha_list(path):
    """{path: sha256} of a `sha256  path` list."""
    out = {}
    if path and os.path.isfile(path):
        for ln in _read_list(path):
            digest, _, rel = ln.partition(" ")
            out[rel.strip().lstrip("*")] = digest.strip().lower()
    return out


def credential_values(path):
    """Secret values of a credentials file (KEY=value, KEY: value or a bare value per line; URL user/password parts
    too). Read at run time only; the values are never printed, returned to output or written anywhere."""
    out = set()
    path = os.path.expanduser(path)
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in ("=", ": ", ":"):
                if sep in line and not line.startswith("http"):
                    line = line.split(sep, 1)[1]
                    break
            value = line.strip().strip("'\"")
            m = re.match(r"\w+://([^:/@]+)(?::([^@/]+))?@", value)
            if m:
                out |= {v for v in m.groups() if v}
            if len(value) >= 4:
                out.add(value)
    return {v for v in out if len(v) >= 4}


class Config:
    """The private deny literals, fingerprints and allow lists (from the environment unless given)."""

    def __init__(self, deny=None, deny_files=None, allow_files=None, media_list=MEDIA_LIST, public_email=None,
                 placeholder_list=PLACEHOLDER_LIST, credentials=None):
        env = os.environ
        if deny is None:
            deny = [x.strip() for x in env.get("PUBLIC_AUDIT_DENY", "").split(",")]
        if credentials is None:
            credentials = env.get("PUBLIC_AUDIT_CREDENTIALS_FILE") or CREDENTIALS
        self.deny = [x for x in deny if x]
        self.secrets = sorted(credential_values(credentials) - set(self.deny)) if credentials else []
        self._deny_res = [self._literal_re(x) for x in self.deny + self.secrets]
        if deny_files is None:
            deny_files = [p for p in env.get("PUBLIC_AUDIT_DENY_FILE", "").split(":") if p]
        if allow_files is None:
            allow_files = [ALLOW_LIST] + [p for p in env.get("PUBLIC_AUDIT_ALLOW_FILE", "").split(":") if p]
        allow = set()
        for p in allow_files:
            if os.path.isfile(os.path.expanduser(p)):
                allow |= {_nfkc(ln.partition("\t")[0].strip()) for ln in _read_list(p)}
        self.fingerprints = []
        self.missing = []
        for p in deny_files:
            if not os.path.isfile(os.path.expanduser(p)):
                self.missing.append(p)
                continue
            self.fingerprints += [t for t in (_nfkc(x) for x in _read_list(p)) if t not in allow]
        self.media = _read_sha_list(media_list)
        self.placeholders = _read_sha_list(placeholder_list)
        self.public_email = (public_email if public_email is not None else env.get("PUBLIC_GIT_EMAIL", "")).lower()

    @staticmethod
    def _literal_re(lit):
        if len(lit) >= 6:  # long enough to be distinctive: also inside other words
            return re.compile(re.escape(lit), re.I)
        left = r"(?<![0-9A-Za-z])" if lit[0].isalnum() else ""
        right = r"(?![0-9A-Za-z])" if lit[-1].isalnum() else ""
        return re.compile(left + re.escape(lit) + right, re.I)

    def private(self):
        return bool(self.deny or self.fingerprints)

    def literal_count(self):
        return len(self.deny) + len(self.secrets)


class Audit:
    def __init__(self, config):
        self.c = config
        self.hits = []  # (where, rule), where already masked
        self.prefix = ""  # "<zip name>!" while auditing zip members

    def mask(self, text):
        """text with every private or generic match replaced by ***."""
        text = _nfkc(text)  # full-width forms are masked too
        for rx in self.c._deny_res:
            text = rx.sub("***", text)
        for p in self.c.fingerprints:
            if p in text:
                text = text.replace(p, "***")
        for _, rx in GENERIC:
            text = rx.sub("***", text)
        return EMAIL.sub(lambda m: m.group(0) if self._email_ok(m) else "***", text)

    def hit(self, where, rule):
        self.hits.append((self.mask(self.prefix + where), rule))

    @staticmethod
    def _email_ok(m):
        if m.group(0).lower() == "git@github.com":
            return True  # GitHub's ssh remote user, not a person
        domain = m.group(1)
        while NOT_A_DOMAIN.search(domain):  # "@BASE_URL@addons.xml" is a file name, an address plus ".md" is not
            domain = domain.rsplit(".", 1)[0]
            if "." not in domain:
                return True
        return bool(EMAIL_OK.search(domain))

    # text ------------------------------------------------------------------------------------------------------
    def line_rules(self, line, emails=True):
        """The rule names one line breaks. The generic rules also see the NFKC form (full-width digits) and the
        percent-decoded form of the line."""
        forms = [line, _nfkc(line)]
        if "%" in line:
            forms.append(urllib.parse.unquote(line))
        rules = [rule for rule, rx in GENERIC if any(rx.search(f) for f in forms)]
        if emails and any(not self._email_ok(m) for m in EMAIL.finditer(line)):
            rules.append("e-mail address")
        if any(rx.search(line) or rx.search(_nfkc(line)) for rx in self.c._deny_res):
            rules.append("private literal (PUBLIC_AUDIT_DENY)")
        if self.c.fingerprints:
            both = line + "\n" + _nfkc(line)
            n = sum(1 for p in self.c.fingerprints if p in both)
            if n:
                rules.append(f"production fingerprint x{n} (PUBLIC_AUDIT_DENY_FILE)")
        return rules

    def text(self, name, text, fixture=False, emails=True):
        """Every rule over one text; `name` is the location shown (masked)."""
        for no, line in enumerate(text.splitlines(), 1):
            for rule in self.line_rules(line, emails=emails and not fixture):
                self.hit(f"{name}:{no}", rule)

    def private_line(self, where, line):
        for rule in self.line_rules(line, emails=False):
            if rule.startswith(("private literal", "production fingerprint")):
                self.hit(where, rule)

    def name(self, rel):
        """Every text rule over a relative path (file names publish too)."""
        for rule in self.line_rules(rel):
            self.hit(f"{rel}:0", f"{rule} in the file name")

    # files -----------------------------------------------------------------------------------------------------
    def file(self, rel, data):
        rel = rel.replace(os.sep, "/")
        self.name(rel)
        low = rel.lower()
        base = os.path.basename(rel)
        ext = os.path.splitext(low)[1]
        fixture = rel.startswith("tests/fixtures/")
        if ext in ARCHIVES:
            self.hit(f"{rel}:0", "archive file")
            return
        if ext in VIDEOS and not (ext in MEDIA and rel.startswith("media/")):
            self.hit(f"{rel}:0", "video / stream file")
            return
        if ext in OTHER_IMAGES:
            self.hit(f"{rel}:0", "image format not allowed (png, gif, placeholder jpg only)")
            return
        if ext == ".jpg" and fixture and rel in self.c.placeholders:
            self.placeholder(rel, data)
            return
        text = decode_text(data)
        if text is None or not (ext in TEXT or base in TEXT_NAMES or base.startswith(".")):
            self.binary(rel, data, fixture)
            return
        if low.endswith(SKIP_TEXT):
            for no, line in enumerate(text.splitlines(), 1):
                self.private_line(f"{rel}:{no}", line)
            return
        self.text(rel, text, fixture=fixture)

    def raw(self, rel, data):
        """A binary file: the text rules over its printable runs (no e-mail rule: random image data looks like an
        address now and then; private literals and IP addresses still count)."""
        self.text(rel + " (bytes)", "\n".join(m.decode("latin-1") for m in re.findall(rb"[\x20-\x7e]{6,}", data)),
                  emails=False)

    def placeholder(self, rel, data):
        if self.c.placeholders[rel] != hashlib.sha256(data).hexdigest():
            self.hit(f"{rel}:0", "sha256 differs from tools/placeholder_thumbs.txt")
            return
        for problem in placeholder_problems(data):
            self.hit(f"{rel}:0", f"placeholder: {problem}")
        self.raw(rel, data)

    def binary(self, rel, data, fixture):
        low = rel.lower()
        listed = self.c.media.get(rel)
        allowed = (low.endswith(MEDIA) and rel.startswith("media/")) or low.endswith(LISTED_ELSEWHERE)
        if listed is None or not allowed:
            what = "fixture thumbnail not in tools/placeholder_thumbs.txt" if fixture and low.endswith(".jpg") \
                else "binary / image / video not in tools/public_images.txt"
            self.hit(f"{rel}:0", what)
            return
        if listed != hashlib.sha256(data).hexdigest():
            self.hit(f"{rel}:0", "sha256 differs from tools/public_images.txt")
            return
        for problem in metadata_problems(low, data):
            self.hit(f"{rel}:0", f"metadata: {problem}")
        self.raw(rel, data)

    def link(self, rel, path):
        """A symbolic link (to a file or a directory): never published; its name and target are audited too."""
        self.hit(f"{rel}:0", "symbolic link")
        self.name(rel)
        self.text(rel + " (link target)", os.readlink(path))

    def tree(self, d):
        for dirpath, dirs, files in os.walk(d):
            for x in sorted(dirs):
                if os.path.islink(os.path.join(dirpath, x)):  # os.walk lists but never enters them
                    self.link(os.path.relpath(os.path.join(dirpath, x), d), os.path.join(dirpath, x))
            dirs[:] = sorted(x for x in dirs if x != ".git" and not os.path.islink(os.path.join(dirpath, x)))
            for fn in sorted(files):
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, d)
                if os.path.islink(path):
                    self.link(rel, path)
                    continue
                with open(path, "rb") as f:
                    self.file(rel, f.read())

    def zips(self, paths):
        for z in paths:
            self.prefix = os.path.basename(z) + "!"
            with zipfile.ZipFile(z) as zf:
                for info in zf.infolist():
                    if not info.is_dir():
                        self.file(info.filename, zf.read(info))
            self.prefix = ""

    def history(self, repo):
        def git(*args):
            return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                                  text=True).stdout
        sep = "\x1e"
        out = git("log", "--all", f"--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B{sep}")
        for record in out.split(sep):
            record = record.strip("\n")
            if not record:
                continue
            sha, an, ae, cn, ce, body = record.split("\x1f", 5)
            where = f"commit {sha[:12]}"
            for role, name, email in (("author", an, ae), ("committer", cn, ce)):
                self.identity(where, role, name, email)
            self.text(f"{where} message", body)
        for ref in git("for-each-ref", "--format=%(refname)").splitlines():
            for rule in self.line_rules(ref):
                self.hit(f"ref {ref}:0", f"{rule} in a ref name")
        for line in git("for-each-ref", "refs/tags", "--format=%(objecttype) %(refname)").splitlines():
            kind, _, ref = line.partition(" ")
            if kind != "tag":
                continue
            name, email = git("for-each-ref", ref, "--format=%(taggername)%1f%(taggeremail)").strip().split("\x1f")
            self.identity(f"tag {ref}", "tagger", name, email.strip("<>"))
            self.text(f"tag {ref} message", git("for-each-ref", ref, "--format=%(contents)"))

    def identity(self, where, role, name, email):
        if not email.lower().endswith(NOREPLY):
            self.hit(f"{where}:0", f"{role} e-mail is not a GitHub noreply address")
        for rule in self.line_rules(name):
            self.hit(f"{where}:0", f"{role} name: {rule}")


def decode_text(data):
    """The text of a file, or None for a binary one (NUL bytes). UTF-16/32 with a BOM is text."""
    for bom, codec in ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
                       (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(codec, errors="replace")
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def export_tree(repo, dest, rev=None):
    """Extract the public export of `rev` (default: the working tree as `git add -A` sees it) into dest."""
    def git(*args, env=None):
        return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=env).stdout
    if rev is None:
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, "index")
            src = git("rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip()
            if os.path.isfile(src):
                with open(src, "rb") as a, open(index, "wb") as b:
                    b.write(a.read())
            env = dict(os.environ, GIT_INDEX_FILE=index)
            git("add", "-A", env=env)
            rev = git("write-tree", env=env).decode().strip()
    data = git("archive", "--format=tar", rev)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise ValueError(f"unsafe path in archive: {member.name}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # Python < 3.12: our own git archive, paths checked above
            tar.extractall(dest)
    return rev


# media metadata --------------------------------------------------------------------------------------------------
def jpeg_comments(data):
    """The COM segments of a JPEG (None when it is not a JPEG)."""
    return [body for marker, body in jpeg_segments(data) if marker == 0xFE] if data[:2] == b"\xff\xd8" else None


def jpeg_segments(data):
    out, i = [], 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        out.append((marker, data[i + 4:i + 2 + length]))
        if marker == 0xDA:  # start of scan: entropy-coded data follows
            break
        i += 2 + length
    return out


JPEG_PLAIN = {0xE0, 0xDB, 0xC0, 0xC1, 0xC2, 0xC4, 0xFE, 0xDA}  # APP0, DQT, SOF0-2, DHT, COM, SOS


def placeholder_problems(data):
    """What makes data more than a generated placeholder: other segments, other comments, bytes after EOI."""
    if data[:2] != b"\xff\xd8":
        return ["not a JPEG"]
    problems = []
    segments = jpeg_segments(data)
    for marker, body in segments:
        if marker not in JPEG_PLAIN:
            problems.append(f"JPEG segment 0x{marker:02X}")
        elif marker == 0xE0 and not body.startswith(b"JFIF\0"):
            problems.append("JPEG APP0 is not JFIF")
        elif marker == 0xFE and body != MARK:
            problems.append("JPEG comment")
    if [b for m, b in segments if m == 0xFE] != [MARK]:
        problems.append("no single placeholder comment")
    if not segments or segments[-1][0] != 0xDA:
        problems.append("no scan")
    else:
        sos = data.rfind(b"\xff\xda", 0, len(data))
        eoi = data.find(b"\xff\xd9", sos)
        if eoi != len(data) - 2:
            problems.append("data after the end of the image")
    return sorted(set(problems))


def metadata_problems(low, data):
    problems = []
    if low.endswith((".jpg", ".jpeg")):
        for marker, body in jpeg_segments(data):
            if marker == 0xE1:
                problems.append("JPEG APP1 (EXIF/XMP)")
            elif marker == 0xED:
                problems.append("JPEG APP13 (IPTC)")
            elif marker == 0xFE and body != MARK:
                problems.append("JPEG comment")
    elif low.endswith(".png"):
        i = 8
        while i + 8 <= len(data):
            length, kind = struct.unpack(">I4s", data[i:i + 8])
            if kind not in PNG_OK:
                problems.append(f"PNG chunk {kind.decode('latin-1')}")
            i += 12 + length
    elif low.endswith(".gif"):
        problems += gif_problems(data)
    elif low.endswith(".webp"):
        for kind in (b"EXIF", b"XMP "):
            if kind in data[:4096] or data.find(kind, 12) >= 0:
                problems.append(f"WebP {kind.decode().strip()} chunk")
    elif low.endswith(VIDEOS):
        problems += [f"MP4 tag {t.decode('latin-1')}" for t in MP4_TAGS if t in data]
    return sorted(set(problems))


def gif_problems(data):
    """Comment and non-loop application extensions of a GIF."""
    problems = []
    f = io.BytesIO(data)
    head = f.read(13)
    if len(head) < 13 or not head.startswith(b"GIF"):
        return ["not a GIF"]
    flags = head[10]
    if flags & 0x80:
        f.read(3 * (2 ** ((flags & 7) + 1)))

    def skip_blocks():
        while True:
            n = f.read(1)
            if not n or n[0] == 0:
                return
            f.read(n[0])

    while True:
        b = f.read(1)
        if not b or b == b"\x3b":
            break
        if b == b"\x21":
            label = f.read(1)
            if label == b"\xfe":
                problems.append("GIF comment")
                skip_blocks()
            elif label == b"\xff":
                n = f.read(1)
                ident = f.read(n[0]) if n else b""
                if ident[:8] not in (b"NETSCAPE", b"ANIMEXTS"):
                    problems.append(f"GIF application extension {ident[:8].decode('latin-1')}")
                skip_blocks()
            else:
                skip_blocks()
        elif b == b"\x2c":
            desc = f.read(9)
            if len(desc) < 9:
                break
            if desc[8] & 0x80:
                f.read(3 * (2 ** ((desc[8] & 7) + 1)))
            f.read(1)  # LZW minimum code size
            skip_blocks()
        else:
            problems.append("GIF structure")
            break
    return problems


def main(argv=None):
    local_env.load()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--zip", nargs="+", metavar="Z")
    ap.add_argument("--history", metavar="REPO")
    ap.add_argument("--worktree", nargs="?", const=".", metavar="REPO")
    ap.add_argument("--strict", action="store_true", help="fail unless private deny lists are configured")
    args = ap.parse_args(argv)
    if not (args.dir or args.zip or args.history or args.worktree):
        ap.error("give DIR, --worktree, --zip or --history")
    config = Config()
    if config.missing:
        print(f"public audit: PUBLIC_AUDIT_DENY_FILE names {len(config.missing)} missing file(s)", file=sys.stderr)
        return 2
    if args.strict and not config.private():
        print("public audit: --strict needs PUBLIC_AUDIT_DENY and/or PUBLIC_AUDIT_DENY_FILE", file=sys.stderr)
        return 2
    audit = Audit(config)
    if args.dir:
        audit.tree(args.dir)
    if args.worktree:
        with tempfile.TemporaryDirectory() as tmp:
            export_tree(args.worktree, tmp)
            audit.tree(tmp)
    if args.zip:
        audit.zips(args.zip)
    if args.history:
        audit.history(args.history)
    for where, rule in audit.hits:
        print(f"{where}: {rule}")
    rules = {}
    for _, rule in audit.hits:
        rules[rule] = rules.get(rule, 0) + 1
    scope = (f"{config.literal_count()} private literals, {len(config.fingerprints)} fingerprints"
             if config.private() else "generic rules only (no private deny list configured)")
    print(f"public audit: {len(audit.hits)} hits ({scope})" +
          ("".join(f"\n  {n} x {r}" for r, n in sorted(rules.items())) if rules else ""))
    return 1 if audit.hits else 0


if __name__ == "__main__":
    sys.exit(main())
