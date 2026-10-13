#!/usr/bin/env python3
"""Build and publish a Kodi add-on repository (backlog 010). Stdlib only; the same files work on any static host.

  build_repo.py render --base-url URL --src repository.kodistation --out dist/repository.kodistation
      Copy the repository addon and fill in @BASE_URL@ (http(s), ending in '/').
  build_repo.py zip --out dist/<id>-<ver>.zip dist/<id>
      Reproducible zip (sorted, fixed timestamps) with the single top-level folder <id>/; no caches.
  build_repo.py check-zip [--git-root .] ZIP...
      Fail if a zip contains tests/, tools/, dist/, __pycache__, .pyc or dotfiles, or (with --git-root) any file
      that git does not track.
  build_repo.py publish --repo-dir DIR ZIP...
      Add zips to DIR/<id>/<id>-<ver>.zip (+ .sha256), extract their assets, rebuild addons.xml(.sha256) from the
      newest version of each id, and write index.html linking the repository zip (copied to DIR/), addons.xml(.sha256)
      and each <id>/ (Kodi caches that listing and refuses unlisted files). A version that is
      already published with different bytes is refused and nothing changes: Kodi would never download it again.
      One publish at a time: an exclusive flock on DIR/.lock (the server hides dotfiles).
  build_repo.py check-repo-addon [--git-root .] [--src repository.kodistation]
      Fail if the repository addon source differs from the newest v* tag while its version is unchanged: the
      next publish would be refused (same id+version, different bytes), so bump it.
  build_repo.py verify --base-url URL [--expect-base-url URL]
      Fetch the repository the way Kodi does and check every digest, version and asset, Range support, the
      repository zip at the root (index.html) and, with --expect-base-url, the URLs inside the repository addon.
      Non-zero on any problem.

Layout and formats follow Kodi 21 (xbmc/addons/Repository.cpp): datadir/<id>/<id>-<ver>.zip, a "<hex>  <name>"
digest file (Kodi reads up to the first space), sidecar <zip>.sha256 for <hashes>sha256</hashes>, and assets fetched
from artdir/<id>/<asset path>.
"""
import argparse
import fcntl
import hashlib
import html
import html.parser
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

PLACEHOLDER = "@BASE_URL@"
FORBIDDEN_PARTS = {"tests", "tools", "dist", "__pycache__"}
SKIP_PARTS = {"__pycache__"}
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
ASSET_TAGS = ("icon", "fanart", "banner", "clearlogo", "screenshot")


class RepoError(Exception):
    pass


# --- Kodi version order (CAddonVersion: dpkg-style, '~' sorts before everything, optional "epoch:") -------------

def _order(c):
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == "~":
        return -1
    return ord(c) + 256


def _verrevcmp(a, b):
    i = j = 0
    while i < len(a) or j < len(b):
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        first_diff = 0
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _split_version(v):
    epoch, sep, rest = v.partition(":")
    if not sep or not epoch.isdigit():
        return 0, v
    return int(epoch), rest


def compare_versions(a, b):
    """<0, 0, >0 like Kodi's CAddonVersion: 0.9.0 < 0.10.0, 0.3.0~beta1 < 0.3.0, 1:0.1 > 0.2."""
    (ea, va), (eb, vb) = _split_version(a), _split_version(b)
    if ea != eb:
        return ea - eb
    return _verrevcmp(va, vb)


# --- small helpers ----------------------------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_line(data, name):
    return f"{sha256_bytes(data)}  {name}\n"


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def write_atomic(path, data):
    """Write through a temp file + rename; skip when the bytes are already there (a no-op keeps mtimes)."""
    if os.path.isfile(path) and _read(path) == data:
        return False
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def _safe_relpath(p):
    """A relative path inside the addon folder (no '..', not absolute)."""
    if not p or p.startswith(("/", "\\")) or "\\" in p or re.match(r"^[A-Za-z]:", p):
        return False
    return all(part not in ("", ".", "..") for part in p.split("/"))


def check_base_url(url):
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname or not url.endswith("/") or re.search(r"\s", url):
        raise RepoError(f"base URL must be http(s)://host/... ending in '/': {url!r}")
    return url


# --- render -----------------------------------------------------------------------------------------------------

def render(base_url, src, out):
    """Copy the repository addon source to out/ with @BASE_URL@ replaced in addon.xml."""
    check_base_url(base_url)
    xml_path = os.path.join(src, "addon.xml")
    text = open(xml_path, encoding="utf-8").read()
    if PLACEHOLDER not in text:
        raise RepoError(f"{xml_path} has no {PLACEHOLDER}")
    rendered = text.replace(PLACEHOLDER, base_url)
    ET.fromstring(rendered.encode("utf-8"))
    if os.path.exists(out):
        shutil.rmtree(out)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with open(os.path.join(out, "addon.xml"), "w", encoding="utf-8") as f:
        f.write(rendered)


# --- zips -------------------------------------------------------------------------------------------------------

def _forbidden(name):
    parts = name.rstrip("/").split("/")
    dotfile = any(p.startswith(".") for p in parts)
    return dotfile or bool(FORBIDDEN_PARTS.intersection(parts)) or name.endswith(".pyc")


def forbidden_entries(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return [n for n in z.namelist() if _forbidden(n)]


def untracked_entries(zip_path, git_root):
    """Files in the zip that git does not track under git_root/<top folder> (e.g. scratch files in the addon)."""
    with zipfile.ZipFile(zip_path) as z:
        files = [n for n in z.namelist() if not n.endswith("/")]
    tops = {n.split("/", 1)[0] for n in files}
    tracked = set()
    for top in tops:
        tracked.update(_git(git_root, "ls-files", "--", top).splitlines())
    return [n for n in files if n not in tracked]


def make_zip(src_dir, out):
    """Reproducible installable zip of src_dir with the top-level folder basename(src_dir)."""
    src_dir = os.path.abspath(src_dir)
    top = os.path.basename(src_dir)
    entries = []  # (arcname, abs path or None for a directory)
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS)
        rel = os.path.relpath(root, src_dir)
        base = top if rel == "." else f"{top}/{rel.replace(os.sep, '/')}"
        entries.append((base + "/", None))
        for f in sorted(files):
            if f.endswith(".pyc"):
                continue
            entries.append((f"{base}/{f}", os.path.join(root, f)))
    bad = [a for a, _ in entries if _forbidden(a)]
    if bad:
        raise RepoError(f"refusing to zip development files: {', '.join(bad)}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, path in sorted(entries):
            info = zipfile.ZipInfo(arcname, ZIP_DATE)
            info.create_system = 3
            if path is None:
                info.external_attr = (0o40755 << 16) | 0x10
                z.writestr(info, b"")
            else:
                info.external_attr = 0o100644 << 16  # regular file, rw-r--r--
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, _read(path))
    write_atomic(out, buf.getvalue())


class AddonZip:
    def __init__(self, path, addon_id, version, addon_xml, assets):
        self.path, self.addon_id, self.version, self.addon_xml, self.assets = path, addon_id, version, addon_xml, assets


def _assets(root):
    out = []
    for assets in root.findall("extension[@point='xbmc.addon.metadata']/assets"):
        for el in assets:
            if el.tag in ASSET_TAGS and (el.text or "").strip():
                out.append(el.text.strip())
    return out


def read_zip(path):
    """Validate an addon zip named <id>-<version>.zip and return its AddonZip."""
    name = os.path.basename(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise RepoError(f"{name}: not a zip ({e})") from e
    with z:
        names = z.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1 or any("/" not in n for n in names):
            raise RepoError(f"{name}: needs exactly one top-level folder, found {sorted(tops)}")
        top = tops.pop()
        if any(not _safe_relpath(n.rstrip("/")) for n in names):
            raise RepoError(f"{name}: unsafe path in zip")
        try:
            xml = z.read(f"{top}/addon.xml")
        except KeyError:
            raise RepoError(f"{name}: no {top}/addon.xml") from None
        members = set(names)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise RepoError(f"{name}: addon.xml does not parse ({e})") from e
    addon_id, version = root.get("id"), root.get("version")
    if root.tag != "addon" or not addon_id or not version:
        raise RepoError(f"{name}: addon.xml has no <addon id version>")
    if addon_id != top:
        raise RepoError(f"{name}: top-level folder {top}/ is not the addon id {addon_id}")
    if name != f"{addon_id}-{version}.zip":
        raise RepoError(f"{name}: file name does not match {addon_id}-{version}.zip")
    assets = _assets(root)
    for a in assets:
        if not _safe_relpath(a):
            raise RepoError(f"{name}: unsafe asset path {a!r}")
        if f"{top}/{a}" not in members:
            raise RepoError(f"{name}: asset {a} is listed in addon.xml but not in the zip")
    return AddonZip(path, addon_id, version, xml, assets)


# --- publish ----------------------------------------------------------------------------------------------------

def _zip_path(repo_dir, addon_id, version):
    return os.path.join(repo_dir, addon_id, f"{addon_id}-{version}.zip")


def _published_versions(repo_dir):
    """{id: [versions]} of the zips under repo_dir/<id>/."""
    out = {}
    if not os.path.isdir(repo_dir):
        return out
    for addon_id in sorted(os.listdir(repo_dir)):
        d = os.path.join(repo_dir, addon_id)
        if not os.path.isdir(d) or addon_id.startswith("."):
            continue
        prefix = addon_id + "-"
        for f in os.listdir(d):
            if f.startswith(prefix) and f.endswith(".zip"):
                out.setdefault(addon_id, []).append(f[len(prefix):-4])
    return out


def _newest(versions):
    best = versions[0]
    for v in versions[1:]:
        if compare_versions(v, best) > 0:
            best = v
    return best


def _addon_element(xml_bytes):
    text = xml_bytes.decode("utf-8").lstrip("﻿")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    return text.strip()


def build_addons_xml(addon_xmls):
    body = "\n".join(_addon_element(x) for x in addon_xmls)
    data = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n{body}\n</addons>\n'.encode()
    ET.fromstring(data)
    return data


def index_names(root_zips, addon_ids):
    """Everything index.html links: Kodi caches the root listing after "Install from zip file" and then refuses to
    open files of that directory that are not in it ("Could not connect to repository" until a restart)."""
    return list(root_zips) + ["addons.xml", "addons.xml.sha256"] + [f"{i}/" for i in addon_ids]


def build_index(names):
    links = "\n".join(f'<a href="{html.escape(n)}">{html.escape(n)}</a><br>' for n in names)
    return ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><title>Kodi add-on repository</title></head>\n"
            f"<body>\n{links}\n</body></html>\n").encode()


class _LinkParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text)))
            self._href = None


def parse_links(text):
    """[(href, link text)] of an HTML page."""
    p = _LinkParser()
    p.feed(text)
    return p.links


def publish(repo_dir, zips):
    """Validate the zips, then publish them under an exclusive flock on repo_dir/.lock (one publish at a time)."""
    infos = [read_zip(z) for z in zips]
    os.makedirs(repo_dir, exist_ok=True)
    with open(os.path.join(repo_dir, ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _publish_locked(repo_dir, infos)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _publish_locked(repo_dir, infos):

    # 1. Plan; refuse before touching anything.
    conflicts, todo, seen = [], [], {}
    for info in infos:
        key = (info.addon_id, info.version)
        digest = sha256_file(info.path)
        if key in seen:
            if seen[key] != digest:
                conflicts.append(f"{info.addon_id}-{info.version}.zip given twice with different contents")
            continue
        seen[key] = digest
        dest = _zip_path(repo_dir, *key)
        if os.path.exists(dest):
            if sha256_file(dest) != digest:
                conflicts.append(f"{info.addon_id}-{info.version}.zip is already published with different contents; "
                                 "bump the version (Kodi never re-downloads a published version)")
            continue
        todo.append(info)
    if conflicts:
        raise RepoError("; ".join(conflicts))

    # 2. New zips and their sidecars.
    for info in todo:
        dest = _zip_path(repo_dir, info.addon_id, info.version)
        data = _read(info.path)
        write_atomic(dest, data)
        write_atomic(dest + ".sha256", digest_line(data, os.path.basename(dest)).encode())

    # 3. Newest version of every id: assets, addons.xml, the repository zip at the root, index.html.
    newest = []
    for addon_id, versions in sorted(_published_versions(repo_dir).items()):
        info = read_zip(_zip_path(repo_dir, addon_id, _newest(versions)))
        newest.append(info)
        side = info.path + ".sha256"
        data = _read(info.path)
        write_atomic(side, digest_line(data, os.path.basename(info.path)).encode())
        with zipfile.ZipFile(info.path) as z:
            for asset in info.assets:
                blob = z.read(f"{addon_id}/{asset}")  # read_zip checked that it exists
                write_atomic(os.path.join(repo_dir, addon_id, *asset.split("/")), blob)

    root_zips = []
    for info in newest:
        if not info.addon_id.startswith("repository."):
            continue
        name = os.path.basename(info.path)
        write_atomic(os.path.join(repo_dir, name), _read(info.path))
        root_zips.append(name)
        for f in os.listdir(repo_dir):
            if f != name and f.startswith(info.addon_id + "-") and f.endswith(".zip"):
                os.unlink(os.path.join(repo_dir, f))
    names = index_names(root_zips, [i.addon_id for i in newest])
    write_atomic(os.path.join(repo_dir, "index.html"), build_index(names))

    addons_xml = build_addons_xml([i.addon_xml for i in newest])
    write_atomic(os.path.join(repo_dir, "addons.xml"), addons_xml)
    write_atomic(os.path.join(repo_dir, "addons.xml.sha256"), digest_line(addons_xml, "addons.xml").encode())
    return newest


# --- repository addon changed without a version bump ------------------------------------------------------------

def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True).stdout


def _addon_version(xml_text):
    return ET.fromstring(xml_text.encode("utf-8")).get("version")


def repo_addon_problems(git_root, src):
    """[] unless src (working tree) differs from a released v* tag while the addon version is the same.

    Compared with the newest v* tag before HEAD, and with a v* tag on HEAD itself (uncommitted changes). In the CI
    release job HEAD is the tag being released, so the comparison is with the previous release.
    """
    def newest(*args):
        tags = _git(git_root, "tag", "--list", "v*", "--sort=-v:refname", *args).split()
        return tags[:1]

    with open(os.path.join(git_root, src, "addon.xml"), encoding="utf-8") as f:
        new = _addon_version(f.read())
    problems = []
    for tag in newest("--points-at", "HEAD") + newest("--merged", "HEAD", "--no-contains", "HEAD"):
        try:
            old = _addon_version(_git(git_root, "show", f"{tag}:{src}/addon.xml"))
        except subprocess.CalledProcessError:
            continue  # the addon did not exist at that tag
        changed = subprocess.run(["git", "-C", git_root, "diff", "--quiet", tag, "--", src]).returncode != 0
        if changed and old == new:
            problems.append(f"{src} differs from {tag} but its version is still {new}: bump it in {src}/addon.xml")
    return problems


# --- verify -----------------------------------------------------------------------------------------------------

def _request(url, headers=None):
    """(status, headers, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": "build_repo.py verify", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.headers, r.read()


def _fetch(url):
    return _request(url)[2]


def _first_token(data):
    text = data.decode("ascii", "replace")
    return re.split(r"[ \n]", text, maxsplit=1)[0].strip().lower()


def _repo_dir_problems(addon, expect):
    """The rendered <dir> URLs of a repository addon must use the expected base URL."""
    want = {"info": expect + "addons.xml", "checksum": expect + "addons.xml.sha256", "datadir": expect}
    problems = []
    dirs = addon.findall("extension[@point='xbmc.addon.repository']/dir")
    if not dirs:
        return [f"{addon.get('id')}: no <dir> in addons.xml"]
    for d in dirs:
        for tag, url in want.items():
            got = (d.findtext(tag) or "").strip()
            if got != url:
                problems.append(f"{addon.get('id')}: <{tag}> is {got!r}, expected {url!r}")
    return problems


def verify(base_url, expect_base_url=None, require=None):
    """Problems found fetching the repository at base_url like Kodi does ([] = good).

    Also: Range support (Kodi reads zips with Range requests), the repository zip at the root linked from
    index.html and equal to <id>/<zip>, with expect_base_url the URLs baked into the repository addon, and with
    require ({addon id: version}) that addons.xml lists each of those add-ons at exactly that version (019: the LAN
    release job checks that GitHub Pages already serves the release before it moves the boxes there).
    """
    try:
        check_base_url(base_url)
        if expect_base_url is not None:
            check_base_url(expect_base_url)
    except RepoError as e:
        return [str(e)]
    problems = []

    def get(rel):
        url = base_url + rel
        try:
            return _fetch(url)
        except (urllib.error.URLError, OSError, ValueError) as e:
            problems.append(f"cannot fetch {url}: {e}")
            return None

    xml = get("addons.xml")
    digest = get("addons.xml.sha256")
    if xml is None or digest is None:
        return problems
    if _first_token(digest) != sha256_bytes(xml):
        problems.append("addons.xml does not match addons.xml.sha256")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return problems + [f"addons.xml does not parse: {e}"]
    addons = root.findall("addon")
    if not addons:
        problems.append("addons.xml lists no add-ons")
    listed = {a.get("id"): a.get("version") for a in addons}
    for addon_id, version in sorted((require or {}).items()):
        if listed.get(addon_id) != version:
            problems.append(f"addons.xml lists {addon_id} {listed.get(addon_id) or '(missing)'}, required {version}")
    zips = {}  # rel path -> bytes
    repo_addons = []
    for addon in addons:
        addon_id, version = addon.get("id"), addon.get("version")
        if not addon_id or not version:
            problems.append(f"addons.xml has an <addon> without id or version (id={addon_id!r})")
            continue
        name = f"{addon_id}-{version}.zip"
        rel = f"{urllib.parse.quote(addon_id)}/{urllib.parse.quote(name)}"
        data, side = get(rel), get(rel + ".sha256")
        if data is not None and side is not None:
            if _first_token(side) != sha256_bytes(data):
                problems.append(f"{name} does not match its .sha256 sidecar")
            else:
                zips[rel] = data
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        inner = ET.fromstring(z.read(f"{addon_id}/addon.xml")).get("version")
                    if inner != version:
                        problems.append(f"{name}: addon.xml inside says version {inner}")
                except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
                    problems.append(f"{name}: unreadable ({e})")
        for asset in _assets(addon):
            get(f"{urllib.parse.quote(addon_id)}/{urllib.parse.quote(asset)}")
        if addon_id.startswith("repository."):
            repo_addons.append((addon, name, zips.get(rel)))

    # Range: Kodi opens zips over HTTP with Range requests (plain http.server ignores them and Kodi fails).
    if zips:
        rel, data = next(iter(zips.items()))
        try:
            status, headers, body = _request(base_url + rel, {"Range": "bytes=0-9"})
            want = f"bytes 0-9/{len(data)}"
            if status != 206 or headers.get("Content-Range") != want or body != data[:10]:
                problems.append(f"Range request on {rel}: got {status} {headers.get('Content-Range')!r}, "
                                f"expected 206 {want!r}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            problems.append(f"Range request on {rel} failed: {e}")

    # The repository zip at the root, linked from index.html ("Add source" + "Install from zip file").
    if repo_addons:
        index = get("")
        links = parse_links(index.decode("utf-8", "replace")) if index is not None else []
        ids = [a.get("id") for a in addons if a.get("id")]
        for n in index_names([], ids):
            if (n, n) not in links:
                problems.append(f"index.html does not link {n} with link text == href")
        for addon, name, data in repo_addons:
            if (name, name) not in links:
                problems.append(f"index.html does not link {name} with link text == href")
            root_zip = get(urllib.parse.quote(name))
            if root_zip is not None and data is not None and root_zip != data:
                problems.append(f"{name} at the root differs from {addon.get('id')}/{name}")
            if expect_base_url is not None:
                problems += _repo_dir_problems(addon, expect_base_url)
    elif expect_base_url is not None:
        problems.append("addons.xml lists no repository.* addon to check --expect-base-url against")
    return problems


# --- CLI --------------------------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="fill in the base URL of the repository addon")
    r.add_argument("--base-url", required=True)
    r.add_argument("--src", required=True)
    r.add_argument("--out", required=True)
    z = sub.add_parser("zip", help="reproducible addon zip")
    z.add_argument("--out", required=True)
    z.add_argument("src")
    c = sub.add_parser("check-zip", help="fail on development files in zips")
    c.add_argument("--git-root", help="also fail on files git does not track under GIT_ROOT/<addon id>/")
    c.add_argument("zips", nargs="+")
    pub = sub.add_parser("publish", help="add zips to a repository directory")
    pub.add_argument("--repo-dir", required=True)
    pub.add_argument("zips", nargs="+")
    cr = sub.add_parser("check-repo-addon", help="repository addon changed without a version bump?")
    cr.add_argument("--git-root", default=".")
    cr.add_argument("--src", default="repository.kodistation")
    v = sub.add_parser("verify", help="check a served repository")
    v.add_argument("--base-url", required=True)
    v.add_argument("--expect-base-url", help="URL the repository addon must point at (default: not checked)")
    v.add_argument("--require", action="append", default=[], metavar="ID=VERSION",
                   help="addons.xml must list this add-on at exactly this version (repeatable)")
    args = p.parse_args(argv)
    try:
        if args.cmd == "render":
            render(args.base_url, args.src, args.out)
        elif args.cmd == "zip":
            make_zip(args.src, args.out)
            print(args.out)
        elif args.cmd == "check-zip":
            bad = [f"{zp}: {n}" for zp in args.zips for n in forbidden_entries(zp)]
            if args.git_root:
                bad += [f"{zp}: {n} (not tracked by git)" for zp in args.zips
                        for n in untracked_entries(zp, args.git_root)]
            if bad:
                print("development files in zip:\n  " + "\n  ".join(bad), file=sys.stderr)
                return 1
        elif args.cmd == "publish":
            for info in publish(args.repo_dir, args.zips):
                print(f"{info.addon_id} {info.version}")
        elif args.cmd == "check-repo-addon":
            problems = repo_addon_problems(args.git_root, args.src)
            for m in problems:
                print(f"FAIL {m}", file=sys.stderr)
            if problems:
                return 1
        elif args.cmd == "verify":
            require = {}
            for item in args.require:
                addon_id, sep, version = item.partition("=")
                if not sep or not addon_id or not version:
                    raise RepoError(f"--require needs ID=VERSION, got {item!r}")
                require[addon_id] = version
            problems = verify(args.base_url, args.expect_base_url, require)
            for m in problems:
                print(f"FAIL {m}", file=sys.stderr)
            if problems:
                return 1
            print(f"OK {args.base_url}")
    except (RepoError, OSError, subprocess.CalledProcessError) as e:
        print(f"build_repo: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
