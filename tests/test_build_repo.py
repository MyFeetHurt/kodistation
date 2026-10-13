"""tools/build_repo.py: render the repository addon, publish zips into a Kodi repository, verify it (backlog 010)."""
import functools
import hashlib
import http.server
import os
import shutil
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile

import build_repo
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "dev"))
import kodi_repo_server  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_SRC = os.path.join(ROOT, "repository.kodistation")
PLUGIN_SRC = os.path.join(ROOT, "plugin.video.epgstation")
BASE = "http://kodi-repo.test:8091/"
REPO_VERSION = ET.parse(os.path.join(REPO_SRC, "addon.xml")).getroot().get("version")
REPO_ZIP = f"repository.kodistation-{REPO_VERSION}.zip"

ADDON_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="{id}" name="Test &amp; Co" version="{version}" provider-name="t">
    <extension point="xbmc.python.pluginsource" library="main.py"><provides>video</provides></extension>
    <extension point="xbmc.addon.metadata">
        <summary lang="en_GB">S</summary>
        <assets><icon>resources/icon.png</icon></assets>
        <news>{news}</news>
    </extension>
</addon>
"""


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def make_addon(tmp_path, addon_id="plugin.test", version="1.0.0", news="n", icon=b"PNG1", extra=None):
    """An addon source dir tmp/src-<id>-<ver>/<id>/ (zip root must be <id>/)."""
    d = tmp_path / f"src-{addon_id}-{version}-{abs(hash((news, icon)))}" / addon_id
    (d / "resources").mkdir(parents=True)
    (d / "addon.xml").write_text(ADDON_XML.format(id=addon_id, version=version, news=news), encoding="utf-8")
    (d / "resources" / "icon.png").write_bytes(icon)
    (d / "main.py").write_text("print('hi')\n")
    for name, data in (extra or {}).items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


def make_zip(tmp_path, addon_id="plugin.test", version="1.0.0", **kw):
    src = make_addon(tmp_path, addon_id, version, **kw)
    out = tmp_path / "zips" / f"{addon_id}-{version}-{abs(hash(str(src)))}" / f"{addon_id}-{version}.zip"
    out.parent.mkdir(parents=True)
    build_repo.make_zip(str(src), str(out))
    return out


def raw_zip(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def snapshot(d):
    """{relative path: bytes} of a directory tree."""
    out = {}
    for root, _dirs, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, d)] = fh.read()
    return out


# --- versions ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("low, high", [
    ("0.9.0", "0.10.0"),
    ("0.3.0~beta1", "0.3.0"),
    ("0.3.0~beta1", "0.3.0~beta2"),
    ("1.0.0", "1.0.1"),
    ("1.0", "1.0.1"),
    ("0.2.0", "1:0.1.0"),
])
def test_version_order(low, high):
    assert build_repo.compare_versions(low, high) < 0
    assert build_repo.compare_versions(high, low) > 0
    assert build_repo.compare_versions(high, high) == 0


# --- render -----------------------------------------------------------------------------------------------------

def test_render_substitutes_the_base_url(tmp_path):
    out = tmp_path / "repository.kodistation"
    build_repo.render(BASE, REPO_SRC, str(out))
    text = (out / "addon.xml").read_text(encoding="utf-8")
    assert "@BASE_URL@" not in text
    root = ET.fromstring(text.encode("utf-8"))
    assert root.get("id") == "repository.kodistation"
    assert root.get("version") == REPO_VERSION
    d = root.find("extension[@point='xbmc.addon.repository']/dir")
    assert d.find("info").text == BASE + "addons.xml"
    assert d.find("info").get("compressed") == "false"
    assert d.find("checksum").text == BASE + "addons.xml.sha256"
    assert d.find("checksum").get("verify") == "sha256"
    assert d.find("datadir").text == BASE
    assert d.find("datadir").get("zip") == "true"
    assert d.find("hashes").text == "sha256"
    assert (out / "resources" / "icon.png").is_file()
    assert (out / "LICENSE.txt").is_file()
    # the source keeps its placeholders
    assert "@BASE_URL@" in open(os.path.join(REPO_SRC, "addon.xml"), encoding="utf-8").read()


@pytest.mark.parametrize("url", ["http://host:8090", "ftp://host/", "host/", "http:///", "", "http://host/ x/"])
def test_render_rejects_bad_urls(tmp_path, url):
    with pytest.raises(build_repo.RepoError):
        build_repo.render(url, REPO_SRC, str(tmp_path / "out"))


def test_render_cli(tmp_path):
    out = tmp_path / "r"
    assert build_repo.main(["render", "--base-url", "https://example.org/kodi/", "--src", REPO_SRC,
                            "--out", str(out)]) == 0
    assert "https://example.org/kodi/addons.xml" in (out / "addon.xml").read_text(encoding="utf-8")
    assert build_repo.main(["render", "--base-url", "https://example.org/kodi", "--src", REPO_SRC,
                            "--out", str(tmp_path / "bad")]) != 0


# --- zips -------------------------------------------------------------------------------------------------------

def test_make_zip_is_reproducible_and_clean(tmp_path):
    src = make_addon(tmp_path, extra={"resources/lib/__pycache__/x.cpython-312.pyc": b"x", "resources/lib/a.pyc": b"y",
                                      "resources/lib/a.py": b"z"})
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    build_repo.make_zip(str(src), str(a))
    os.utime(src / "main.py", (1, 1))  # mtimes must not matter
    build_repo.make_zip(str(src), str(b))
    assert a.read_bytes() == b.read_bytes()
    names = zipfile.ZipFile(a).namelist()
    assert all(n.startswith("plugin.test/") for n in names)
    assert "plugin.test/resources/lib/a.py" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    for info in zipfile.ZipFile(a).infolist():
        mode = info.external_attr >> 16
        assert mode == (0o040755 if info.is_dir() else 0o100644), (info.filename, oct(mode))
        assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_make_zip_refuses_forbidden_content(tmp_path):
    src = make_addon(tmp_path, extra={"tests/test_x.py": b"x"})
    with pytest.raises(build_repo.RepoError):
        build_repo.make_zip(str(src), str(tmp_path / "x.zip"))


def test_check_zip_contents_flags_forbidden_paths(tmp_path):
    bad = raw_zip(tmp_path / "bad.zip", {"plugin.test/addon.xml": "x", "plugin.test/tools/a.py": "x"})
    assert build_repo.forbidden_entries(str(bad)) == ["plugin.test/tools/a.py"]
    good = make_zip(tmp_path)
    assert build_repo.forbidden_entries(str(good)) == []
    assert build_repo.main(["check-zip", str(bad)]) != 0
    assert build_repo.main(["check-zip", str(good)]) == 0


def test_read_zip(tmp_path):
    z = make_zip(tmp_path, "plugin.test", "1.2.3")
    info = build_repo.read_zip(str(z))
    assert info.addon_id == "plugin.test" and info.version == "1.2.3"
    assert info.assets == ["resources/icon.png"]


@pytest.mark.parametrize("entries, name", [
    ({"other/addon.xml": ADDON_XML.format(id="plugin.test", version="1.0.0", news="")}, "plugin.test-1.0.0.zip"),
    ({"plugin.test/addon.xml": ADDON_XML.format(id="plugin.test", version="1.0.0", news=""), "stray.txt": "x"},
     "plugin.test-1.0.0.zip"),
    ({"plugin.test/main.py": "x"}, "plugin.test-1.0.0.zip"),
    ({"plugin.test/addon.xml": ADDON_XML.format(id="plugin.test", version="1.0.0", news="")},
     "plugin.test-1.0.1.zip"),
    ({"plugin.test/addon.xml": ADDON_XML.format(id="plugin.other", version="1.0.0", news="")},
     "plugin.test-1.0.0.zip"),
    ({"plugin.test/addon.xml": "<addon"}, "plugin.test-1.0.0.zip"),
])
def test_read_zip_rejects_bad_zips(tmp_path, entries, name):
    z = raw_zip(tmp_path / name, entries)
    with pytest.raises(build_repo.RepoError):
        build_repo.read_zip(str(z))


# --- publish ----------------------------------------------------------------------------------------------------

def test_publish_layout(tmp_path):
    repo = tmp_path / "repo"
    plugin = make_zip(tmp_path, "plugin.test", "0.2.0", icon=b"PLUGIN-ICON")
    repozip = make_zip(tmp_path, "repository.test", "1.0.0", icon=b"REPO-ICON")
    build_repo.publish(str(repo), [str(plugin), str(repozip)])

    for addon_id, ver in (("plugin.test", "0.2.0"), ("repository.test", "1.0.0")):
        z = repo / addon_id / f"{addon_id}-{ver}.zip"
        assert z.is_file()
        side = (repo / addon_id / f"{addon_id}-{ver}.zip.sha256").read_text()
        assert side == f"{sha(z)}  {addon_id}-{ver}.zip\n"
    assert (repo / "plugin.test" / "resources" / "icon.png").read_bytes() == b"PLUGIN-ICON"
    assert (repo / "repository.test" / "resources" / "icon.png").read_bytes() == b"REPO-ICON"

    xml = (repo / "addons.xml").read_bytes()
    assert (repo / "addons.xml.sha256").read_text() == f"{hashlib.sha256(xml).hexdigest()}  addons.xml\n"
    assert xml.count(b"<?xml") == 1 and xml.startswith(b"<?xml")
    root = ET.fromstring(xml)
    assert root.tag == "addons"
    assert sorted((a.get("id"), a.get("version")) for a in root) == [("plugin.test", "0.2.0"),
                                                                      ("repository.test", "1.0.0")]
    assert root.find("addon[@id='plugin.test']").get("name") == "Test & Co"

    # the repository zip is also at the root. index.html links it and every file/dir Kodi reads from the root:
    # Kodi caches the root listing after "Install from zip file" and then refuses files that are not in it (F1).
    assert (repo / "repository.test-1.0.0.zip").read_bytes() == repozip.read_bytes()
    links = build_repo.parse_links((repo / "index.html").read_text(encoding="utf-8"))
    names = ["repository.test-1.0.0.zip", "addons.xml", "addons.xml.sha256", "plugin.test/", "repository.test/"]
    assert links == [(n, n) for n in names]
    assert not list(repo.rglob("*.tmp"))


def test_publish_same_bytes_is_a_noop(tmp_path):
    repo = tmp_path / "repo"
    z = make_zip(tmp_path, "plugin.test", "1.0.0")
    build_repo.publish(str(repo), [str(z)])
    before = snapshot(repo)
    mtimes = {p: os.stat(repo / p).st_mtime_ns for p in before}
    copy = tmp_path / "copy" / z.name
    copy.parent.mkdir()
    shutil.copy(z, copy)
    build_repo.publish(str(repo), [str(copy)])
    assert snapshot(repo) == before
    assert {p: os.stat(repo / p).st_mtime_ns for p in before} == mtimes


def test_publish_refuses_different_bytes_for_a_published_version(tmp_path):
    repo = tmp_path / "repo"
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "plugin.test", "1.0.0", news="first"))])
    before = snapshot(repo)
    changed = make_zip(tmp_path, "plugin.test", "1.0.0", news="second")
    newer_other = make_zip(tmp_path, "plugin.other", "2.0.0")
    with pytest.raises(build_repo.RepoError, match="plugin.test-1.0.0"):
        build_repo.publish(str(repo), [str(newer_other), str(changed)])
    assert snapshot(repo) == before  # nothing changed, not even the other addon
    assert build_repo.main(["publish", "--repo-dir", str(repo), str(changed)]) != 0
    assert snapshot(repo) == before


def test_publish_higher_version_keeps_the_old_zip(tmp_path):
    repo = tmp_path / "repo"
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "plugin.test", "0.9.0", icon=b"OLD"))])
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "plugin.test", "0.10.0", icon=b"NEW"))])
    # publishing an older zip later does not make it the listed one
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "plugin.test", "0.10.0~beta1"))])
    assert (repo / "plugin.test" / "plugin.test-0.9.0.zip").is_file()
    assert (repo / "plugin.test" / "plugin.test-0.10.0~beta1.zip").is_file()
    root = ET.fromstring((repo / "addons.xml").read_bytes())
    assert [(a.get("id"), a.get("version")) for a in root] == [("plugin.test", "0.10.0")]
    assert (repo / "plugin.test" / "resources" / "icon.png").read_bytes() == b"NEW"


def test_publish_newer_repository_zip_replaces_the_root_copy(tmp_path):
    repo = tmp_path / "repo"
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "repository.test", "1.0.0"))])
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "repository.test", "1.1.0"))])
    assert (repo / "repository.test-1.1.0.zip").is_file()
    assert not (repo / "repository.test-1.0.0.zip").exists()
    assert (repo / "repository.test" / "repository.test-1.0.0.zip").is_file()
    hrefs = [h for h, _t in build_repo.parse_links((repo / "index.html").read_text())]
    assert hrefs == ["repository.test-1.1.0.zip", "addons.xml", "addons.xml.sha256", "repository.test/"]


def test_publish_rejects_unsafe_asset_paths(tmp_path):
    xml = ADDON_XML.format(id="plugin.test", version="1.0.0", news="").replace("resources/icon.png", "../evil.png")
    z = raw_zip(tmp_path / "plugin.test-1.0.0.zip", {"plugin.test/addon.xml": xml, "plugin.test/../evil.png": "x"})
    with pytest.raises(build_repo.RepoError):
        build_repo.publish(str(tmp_path / "repo"), [str(z)])
    assert not (tmp_path / "evil.png").exists()


def test_publish_real_addons(tmp_path):
    """The real plugin and the rendered repository addon publish cleanly."""
    rendered = tmp_path / "dist" / "repository.kodistation"
    build_repo.render(BASE, REPO_SRC, str(rendered))
    plugin_zip, repo_zip = tmp_path / "p.zip", tmp_path / "r.zip"
    build_repo.make_zip(PLUGIN_SRC, str(plugin_zip))
    build_repo.make_zip(str(rendered), str(repo_zip))
    pv = ET.parse(os.path.join(PLUGIN_SRC, "addon.xml")).getroot().get("version")
    named = []
    for z, name in ((plugin_zip, f"plugin.video.epgstation-{pv}.zip"), (repo_zip, REPO_ZIP)):
        target = tmp_path / name
        z.rename(target)
        named.append(str(target))
    repo = tmp_path / "repo"
    build_repo.publish(str(repo), named)
    root = ET.fromstring((repo / "addons.xml").read_bytes())
    assert sorted((a.get("id"), a.get("version")) for a in root) == [("plugin.video.epgstation", pv),
                                                                      ("repository.kodistation", REPO_VERSION)]
    assert (repo / "plugin.video.epgstation" / "resources" / "icon.png").is_file()
    assert (repo / "repository.kodistation" / "resources" / "icon.png").is_file()
    names = zipfile.ZipFile(repo / "plugin.video.epgstation" / f"plugin.video.epgstation-{pv}.zip").namelist()
    assert "plugin.video.epgstation/LICENSE.txt" in names
    assert "plugin.video.epgstation/changelog.txt" in names


# --- verify -----------------------------------------------------------------------------------------------------

def _serve(directory, handler_cls):
    handler = functools.partial(handler_cls, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Plain http.server: no Range support."""

    def log_message(self, *args):
        pass


class _RangeHandler(kodi_repo_server.RangeHandler):
    quiet = True


@pytest.fixture
def served_repo(tmp_path):
    """A published repository (plugin.test + the real repository addon rendered for the server URL), served with
    Range support (Kodi reads zips with Range requests)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    srv, url = _serve(repo, _RangeHandler)
    rendered = tmp_path / "dist" / "repository.kodistation"
    build_repo.render(url, REPO_SRC, str(rendered))
    repo_zip = tmp_path / "zips" / REPO_ZIP
    repo_zip.parent.mkdir()
    build_repo.make_zip(str(rendered), str(repo_zip))
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "plugin.test", "0.2.0")), str(repo_zip)])
    yield repo, url
    srv.shutdown()
    srv.server_close()


def test_verify_good_repo(served_repo):
    _repo, url = served_repo
    assert build_repo.verify(url) == []
    assert build_repo.verify(url, expect_base_url=url) == []
    assert build_repo.main(["verify", "--base-url", url]) == 0
    assert build_repo.main(["verify", "--base-url", url, "--expect-base-url", url]) == 0


def test_verify_require_versions(served_repo):
    """019: the Gitea release check requires that Pages already serves the tag's plugin version and repository 1.1.0."""
    _repo, url = served_repo
    ok = {"plugin.test": "0.2.0", "repository.kodistation": REPO_VERSION}
    assert build_repo.verify(url, require=ok) == []
    problems = build_repo.verify(url, require={"plugin.test": "0.2.1", "plugin.video.epgstation": "0.2.1"})
    assert problems == ["addons.xml lists plugin.test 0.2.0, required 0.2.1",
                        "addons.xml lists plugin.video.epgstation (missing), required 0.2.1"]
    assert build_repo.main(["verify", "--base-url", url, "--require", "plugin.test=0.2.0",
                            "--require", f"repository.kodistation={REPO_VERSION}"]) == 0
    assert build_repo.main(["verify", "--base-url", url, "--require", "plugin.test=9.9.9"]) != 0
    assert build_repo.main(["verify", "--base-url", url, "--require", "plugin.test"]) != 0


def test_verify_tampered_zip(served_repo):
    repo, url = served_repo
    with open(repo / "plugin.test" / "plugin.test-0.2.0.zip", "ab") as f:
        f.write(b"x")
    problems = build_repo.verify(url)
    assert problems and "plugin.test-0.2.0.zip" in problems[0]
    assert build_repo.main(["verify", "--base-url", url]) != 0


def test_verify_tampered_sidecar(served_repo):
    repo, url = served_repo
    (repo / "repository.kodistation" / (REPO_ZIP + ".sha256")).write_text("0" * 64 + "  x\n")
    assert build_repo.verify(url)


def test_verify_tampered_addons_xml(served_repo):
    repo, url = served_repo
    p = repo / "addons.xml"
    p.write_bytes(p.read_bytes().replace(b"Test &amp; Co", b"Test &amp; Cx"))
    assert any("addons.xml" in m for m in build_repo.verify(url))


def test_verify_version_mismatch_and_missing_icon(served_repo):
    repo, url = served_repo
    p = repo / "addons.xml"
    p.write_bytes(p.read_bytes().replace(b'version="0.2.0"', b'version="0.2.1"'))
    (repo / "addons.xml.sha256").write_text(f"{sha(p)}  addons.xml\n")
    assert build_repo.verify(url)  # 0.2.1 zip is missing
    p.write_bytes(p.read_bytes().replace(b'version="0.2.1"', b'version="0.2.0"'))
    (repo / "addons.xml.sha256").write_text(f"{sha(p)}  addons.xml\n")
    assert build_repo.verify(url) == []
    (repo / "plugin.test" / "resources" / "icon.png").unlink()
    assert any("icon.png" in m for m in build_repo.verify(url))


def test_verify_unreachable():
    assert build_repo.verify("http://127.0.0.1:9/")


def test_verify_expect_base_url(served_repo):
    _repo, url = served_repo
    problems = build_repo.verify(url, expect_base_url="http://kodi-repo.test:8090/")
    assert problems and all("kodi-repo.test:8090" in m for m in problems)
    assert build_repo.main(["verify", "--base-url", url, "--expect-base-url", "http://kodi-repo.test:8090/"]) != 0


def test_verify_requires_range_support(served_repo):
    repo, _url = served_repo
    srv, plain = _serve(repo, _QuietHandler)
    try:
        problems = build_repo.verify(plain)
    finally:
        srv.shutdown()
        srv.server_close()
    assert any("Range" in m for m in problems)


def test_verify_root_repository_zip(served_repo):
    repo, url = served_repo
    root = repo / REPO_ZIP
    data = root.read_bytes()
    root.write_bytes(data + b"x")
    assert any(REPO_ZIP in m for m in build_repo.verify(url))
    root.unlink()
    assert any(REPO_ZIP in m for m in build_repo.verify(url))
    root.write_bytes(data)
    assert build_repo.verify(url) == []
    (repo / "index.html").write_text("<html><body>nothing</body></html>")
    assert any("index.html" in m for m in build_repo.verify(url))
    (repo / "index.html").write_text('<a href=REPO_ZIP>other text</a>')
    assert any("index.html" in m for m in build_repo.verify(url))


@pytest.mark.parametrize("missing", ["addons.xml", "addons.xml.sha256", "plugin.test/", "repository.kodistation/"])
def test_verify_index_links_everything_kodi_reads(served_repo, missing):
    """F1: without these links Kodi's cached root listing blocks addons.xml(.sha256) until a restart."""
    repo, url = served_repo
    links = [n for n in [REPO_ZIP, "addons.xml", "addons.xml.sha256", "plugin.test/",
                         "repository.kodistation/"] if n != missing]
    (repo / "index.html").write_text("".join(f'<a href="{n}">{n}</a><br>\n' for n in links))
    problems = build_repo.verify(url)
    assert any("index.html" in m and missing in m for m in problems), problems


def test_verify_addon_without_id(served_repo):
    repo, url = served_repo
    p = repo / "addons.xml"
    p.write_bytes(p.read_bytes().replace(b"</addons>", b'<addon version="1.0.0"/>\n</addons>'))
    (repo / "addons.xml.sha256").write_text(f"{sha(p)}  addons.xml\n")
    problems = build_repo.verify(url)
    assert any("without id" in m for m in problems)


def test_read_zip_rejects_a_missing_asset(tmp_path):
    xml = ADDON_XML.format(id="plugin.test", version="1.0.0", news="")
    z = raw_zip(tmp_path / "plugin.test-1.0.0.zip", {"plugin.test/addon.xml": xml})  # icon listed, not in zip
    with pytest.raises(build_repo.RepoError, match="icon.png"):
        build_repo.read_zip(str(z))


def test_publish_with_a_missing_asset_changes_nothing(tmp_path):
    repo = tmp_path / "repo"
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "plugin.test", "1.0.0"))])
    before = snapshot(repo)
    xml = ADDON_XML.format(id="plugin.test", version="1.1.0", news="")
    z = raw_zip(tmp_path / "x" / "plugin.test-1.1.0.zip", {"plugin.test/addon.xml": xml})
    with pytest.raises(build_repo.RepoError):
        build_repo.publish(str(repo), [str(z)])
    assert snapshot(repo) == before


# --- repository addon changed without a version bump ------------------------------------------------------------

def _git(repo, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "-c",
                           "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def tagged_repo(tmp_path):
    r = tmp_path / "g"
    shutil.copytree(REPO_SRC, r / "repository.kodistation")
    _git(tmp_path, "init", "-q", "-b", "main", str(r))
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "init")
    return r


def test_repo_addon_check_without_tags_passes(tagged_repo):
    assert build_repo.repo_addon_problems(str(tagged_repo), "repository.kodistation") == []


def test_repo_addon_check(tagged_repo):
    r = tagged_repo
    _git(r, "tag", "-a", "v0.2.0", "-m", "x")
    assert build_repo.repo_addon_problems(str(r), "repository.kodistation") == []
    lic = r / "repository.kodistation" / "LICENSE.txt"
    lic.write_text(lic.read_text() + "changed\n")
    problems = build_repo.repo_addon_problems(str(r), "repository.kodistation")
    assert problems and "v0.2.0" in problems[0]
    _git(r, "commit", "-qam", "change without bump")
    assert build_repo.repo_addon_problems(str(r), "repository.kodistation")
    assert build_repo.main(["check-repo-addon", "--git-root", str(r), "--src", "repository.kodistation"]) != 0
    xml = r / "repository.kodistation" / "addon.xml"
    xml.write_text(xml.read_text(encoding="utf-8").replace(f'version="{REPO_VERSION}"', 'version="98.0.1"'),
                   encoding="utf-8")
    assert build_repo.repo_addon_problems(str(r), "repository.kodistation") == []
    assert build_repo.main(["check-repo-addon", "--git-root", str(r), "--src", "repository.kodistation"]) == 0


def test_repo_addon_check_uses_the_newest_tag(tagged_repo):
    r = tagged_repo
    _git(r, "tag", "-a", "v0.9.0", "-m", "x")
    xml = r / "repository.kodistation" / "addon.xml"
    xml.write_text(xml.read_text(encoding="utf-8").replace(f'version="{REPO_VERSION}"', 'version="99.1.0"'),
                   encoding="utf-8")
    _git(r, "commit", "-qam", "bump")
    _git(r, "tag", "-a", "v0.10.0", "-m", "x")
    assert build_repo.repo_addon_problems(str(r), "repository.kodistation") == []


def test_repo_addon_check_ignores_a_tag_on_head(tagged_repo):
    """The CI release job runs at the tagged commit: compare with the previous tag, not with itself."""
    r = tagged_repo
    _git(r, "tag", "-a", "v0.2.0", "-m", "x")
    lic = r / "repository.kodistation" / "LICENSE.txt"
    lic.write_text(lic.read_text() + "changed\n")
    _git(r, "commit", "-qam", "change without bump")
    _git(r, "tag", "-a", "v0.3.0", "-m", "x")
    assert build_repo.repo_addon_problems(str(r), "repository.kodistation")


# --- zips contain only tracked files, no dotfiles ---------------------------------------------------------------

def test_dotfiles_are_forbidden(tmp_path):
    bad = raw_zip(tmp_path / "bad.zip", {"plugin.test/addon.xml": "x", "plugin.test/.env": "SECRET=1",
                                         "plugin.test/.git/config": "x"})
    assert build_repo.forbidden_entries(str(bad)) == ["plugin.test/.env", "plugin.test/.git/config"]
    src = make_addon(tmp_path, extra={".DS_Store": b"x"})
    with pytest.raises(build_repo.RepoError):
        build_repo.make_zip(str(src), str(tmp_path / "x.zip"))


def test_untracked_files_are_rejected(tmp_path):
    g = tmp_path / "g"
    g.mkdir()
    src = make_addon(g, "plugin.test", "1.0.0")
    rel = src.relative_to(g)
    _git(tmp_path, "init", "-q", "-b", "main", str(g))
    _git(g, "add", ".")
    _git(g, "commit", "-qm", "init")
    dist = tmp_path / "dist" / "plugin.test"
    shutil.copytree(src, dist)
    good = tmp_path / "plugin.test-1.0.0.zip"
    build_repo.make_zip(str(dist), str(good))
    tracked_root = str(g / rel.parent)
    assert build_repo.untracked_entries(str(good), tracked_root) == []
    (dist / "resources" / "notes.txt").write_text("scratch")
    bad = tmp_path / "bad" / "plugin.test-1.0.0.zip"
    bad.parent.mkdir()
    build_repo.make_zip(str(dist), str(bad))
    assert build_repo.untracked_entries(str(bad), tracked_root) == ["plugin.test/resources/notes.txt"]
    assert build_repo.main(["check-zip", "--git-root", tracked_root, str(bad)]) != 0
    assert build_repo.main(["check-zip", "--git-root", tracked_root, str(good)]) == 0


# --- concurrent publishes are serialized ----------------------------------------------------------------------

def test_publish_waits_for_the_repo_lock(tmp_path):
    """The flock is on the repository directory itself (backlog 023: no lock file in the published tree)."""
    import fcntl
    import time
    repo = tmp_path / "repo"
    repo.mkdir()
    z = make_zip(tmp_path, "plugin.test", "1.0.0")
    held = os.open(repo, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        t = threading.Thread(target=build_repo.publish, args=(str(repo), [str(z)]))
        t.start()
        time.sleep(0.5)
        assert t.is_alive()
        assert not (repo / "addons.xml").exists()
        fcntl.flock(held, fcntl.LOCK_UN)
    finally:
        os.close(held)
    t.join(10)
    assert not t.is_alive()
    assert (repo / "addons.xml").is_file()
    assert ".lock" not in (repo / "index.html").read_text()


def _dotfiles(root):
    return [os.path.relpath(os.path.join(d, n), root) for d, dirs, files in os.walk(root)
            for n in dirs + files if n.startswith(".")]


def test_publish_leaves_no_lock_file_and_removes_an_old_one(tmp_path):
    """023: the empty .lock of older publishes was committed to gh-pages; publish creates none and deletes it."""
    repo = tmp_path / "repo"
    build_repo.publish(str(repo), [str(make_zip(tmp_path, "plugin.test", "1.0.0"))])
    assert _dotfiles(repo) == []
    (repo / ".lock").write_text("")
    (tmp_path / "z2").mkdir()
    z2 = str(make_zip(tmp_path / "z2", "plugin.test", "1.0.1"))
    build_repo.publish(str(repo), [z2])
    assert _dotfiles(repo) == []
    (repo / ".lock").write_text("")
    build_repo.publish(str(repo), [z2])  # same bytes: nothing new, the old lock still goes
    assert _dotfiles(repo) == []


def test_publish_removes_the_old_lock_from_a_git_checkout_as_a_normal_change(tmp_path):
    """023: on gh-pages `git add -A` stages the deletion; a plain commit on top (fast-forward), no rewrite."""
    site = tmp_path / "site"
    site.mkdir()
    _git(tmp_path, "init", "-q", "-b", "gh-pages", str(site))
    build_repo.publish(str(site), [str(make_zip(tmp_path, "plugin.test", "1.0.0"))])
    (site / ".lock").write_text("")
    _git(site, "add", "-A")
    _git(site, "commit", "-qm", "Publish v0.3.0")
    before = _git(site, "rev-parse", "HEAD").strip()
    (tmp_path / "z2").mkdir()
    build_repo.publish(str(site), [str(make_zip(tmp_path / "z2", "plugin.test", "1.0.1"))])
    _git(site, "add", "-A")
    status = _git(site, "diff", "--cached", "--name-status")
    assert "D\t.lock" in status
    _git(site, "commit", "-qm", "Publish v0.3.1")
    assert _git(site, "rev-parse", "HEAD~1").strip() == before
    assert ".lock" not in _git(site, "ls-files").split()


def test_check_zip_without_git_skips_only_the_tracked_check(tmp_path, capsys):
    """023: `make zip` in a GitHub "Download ZIP" tree (no .git) works; the development-file check still runs."""
    src = make_addon(tmp_path, "plugin.test", "1.0.0")
    good = tmp_path / "plugin.test-1.0.0.zip"
    build_repo.make_zip(str(src), str(good))
    root = tmp_path / "nogit"
    root.mkdir()
    assert build_repo.main(["check-zip", "--git-root", str(root), str(good)]) == 0
    err = capsys.readouterr().err
    assert "not a git work tree" in err and "skipped" in err and err.count("\n") == 1
    bad = raw_zip(tmp_path / "bad.zip", {"plugin.test/addon.xml": "x", "plugin.test/.env": "SECRET=1"})
    assert build_repo.main(["check-zip", "--git-root", str(root), str(bad)]) == 1


def test_check_zip_without_a_git_binary(tmp_path, monkeypatch, capsys):
    src = make_addon(tmp_path, "plugin.test", "1.0.0")
    good = tmp_path / "plugin.test-1.0.0.zip"
    build_repo.make_zip(str(src), str(good))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    assert build_repo.main(["check-zip", "--git-root", str(tmp_path), str(good)]) == 0
    err = capsys.readouterr().err
    assert "git not installed" in err and "skipped" in err


def test_check_zip_other_git_failures_stay_errors(tmp_path, monkeypatch, capsys):
    """023 review: only "no repository" / "no git" skip the check; a bad GIT_DIR (or dubious ownership, a corrupt
    repo) is a hard error with git's stderr, never a silent skip."""
    src = make_addon(tmp_path, "plugin.test", "1.0.0")
    good = tmp_path / "plugin.test-1.0.0.zip"
    build_repo.make_zip(str(src), str(good))
    root = tmp_path / "nogit"
    root.mkdir()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git-dir"))
    rc = build_repo.main(["check-zip", "--git-root", str(root), str(good)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing-git-dir" in err and "skipped" not in err


def test_git_work_tree_states(tmp_path, monkeypatch):
    def fake(stdout="", stderr="", rc=0):
        monkeypatch.setattr(build_repo.subprocess, "run",
                            lambda *a, **k: build_repo.subprocess.CompletedProcess(a, rc, stdout, stderr))
    fake("true\n")
    assert build_repo.git_work_tree(".") == "yes"
    fake("false\n")
    assert build_repo.git_work_tree(".") == "no-repo"
    fake(stderr="fatal: not a git repository (or any of the parent directories): .git\n", rc=128)
    assert build_repo.git_work_tree(".") == "no-repo"
    fake(stderr="fatal: detected dubious ownership in repository at '/x'\n", rc=128)
    with pytest.raises(build_repo.GitError, match="dubious ownership"):
        build_repo.git_work_tree(".")


def test_publish_removes_temp_files_of_a_crashed_write(tmp_path):
    """023 review: write_atomic's .*.tmp files left by a crash are deleted under the lock, so no dotfile stays."""
    repo = tmp_path / "repo"
    z = str(make_zip(tmp_path, "plugin.test", "1.0.0"))
    build_repo.publish(str(repo), [z])
    (repo / ".abc123.tmp").write_bytes(b"half")
    (repo / "plugin.test" / ".def456.tmp").write_bytes(b"half")
    (repo / ".git").mkdir()
    (repo / ".git" / ".keep.tmp").write_bytes(b"git's own")
    build_repo.publish(str(repo), [z])
    assert sorted(_dotfiles(repo)) == [".git", os.path.join(".git", ".keep.tmp")]
