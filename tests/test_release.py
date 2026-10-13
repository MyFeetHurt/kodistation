"""tools/release.sh: bump, <news>, commit and annotated tag, in a throwaway git repo (backlog 010)."""
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "release.sh")
ADDON = "plugin.video.epgstation"

CHANGELOG = """v0.2.0 (2026-10-20)
- Movies & TV shows <library>
- Live TV

v0.1.0 (2026-10-10)
- Initial walking skeleton
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    (r / ADDON).mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, ADDON, "addon.xml"), r / ADDON / "addon.xml")
    xml = (r / ADDON / "addon.xml").read_text(encoding="utf-8")
    xml = re.sub(r'(<addon[^>]* version=")[^"]*"', r'\g<1>0.1.0"', xml, count=1)
    (r / ADDON / "addon.xml").write_text(xml, encoding="utf-8")
    (r / ADDON / "changelog.txt").write_text(CHANGELOG, encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    git(r, "config", "user.email", "t@example.org")
    git(r, "config", "user.name", "t")
    git(r, "config", "commit.gpgsign", "false")
    git(r, "config", "tag.gpgsign", "false")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "init")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(r, "remote", "add", "origin", str(origin))
    git(r, "push", "-q", "origin", "main")
    return r


def origin_refs(repo):
    return git(repo, "ls-remote", "origin")


def commit_and_push(repo, msg):
    git(repo, "commit", "-qam", msg)
    git(repo, "push", "-q", "origin", "main")


def run(repo, version, env=None):
    e = {k: v for k, v in os.environ.items() if k != "RELEASE_TRAILER"}
    e.update(env or {})
    return subprocess.run(["bash", SCRIPT, version], cwd=repo, capture_output=True, text=True, env=e)


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def test_happy_path(repo):
    before = head(repo)
    r = run(repo, "0.2.0", {"RELEASE_TRAILER": "Co-Authored-By: X <x@example.org>"})
    assert r.returncode == 0, r.stderr
    assert git(repo, "rev-list", "--count", f"{before}..HEAD").strip() == "1"
    msg = git(repo, "log", "-1", "--format=%B")
    assert msg.startswith("Release v0.2.0")
    assert msg.rstrip().endswith("Co-Authored-By: X <x@example.org>")
    assert git(repo, "cat-file", "-t", "v0.2.0").strip() == "tag"  # annotated
    assert git(repo, "rev-parse", "v0.2.0^{commit}").strip() == head(repo)
    assert git(repo, "status", "--porcelain") == ""
    root = ET.parse(repo / ADDON / "addon.xml").getroot()
    assert root.get("version") == "0.2.0"
    news = root.find("extension[@point='xbmc.addon.metadata']/news").text
    assert news == "v0.2.0 (2026-10-20)\n- Movies & TV shows <library>\n- Live TV"
    raw = (repo / ADDON / "addon.xml").read_text(encoding="utf-8")
    assert "&amp; TV shows &lt;library&gt;" in raw
    assert "git push --atomic origin main v0.2.0" in r.stdout  # prints the push command
    assert "v0.2.0" not in origin_refs(repo)  # nothing pushed
    assert git(repo, "rev-parse", "origin/main").strip() == before


def test_without_trailer(repo):
    r = run(repo, "0.2.0", {"RELEASE_TRAILER": ""})
    assert r.returncode == 0, r.stderr
    assert "Co-Authored-By" not in git(repo, "log", "-1", "--format=%B")


def refused(repo, version, env=None):
    before = head(repo)
    r = run(repo, version, env)
    assert r.returncode != 0
    assert head(repo) == before
    assert git(repo, "tag") == ""
    return r


def test_refuses_dirty_tree(repo):
    (repo / "x.txt").write_text("x")
    refused(repo, "0.2.0")
    (repo / "x.txt").unlink()
    (repo / ADDON / "changelog.txt").write_text(CHANGELOG + "\n", encoding="utf-8")
    refused(repo, "0.2.0")


def test_refuses_other_branch(repo):
    git(repo, "switch", "-q", "-c", "feat/x")
    refused(repo, "0.2.0")


@pytest.mark.parametrize("version", ["0.1.0", "0.0.9", "0.2", "v0.2.0", "0.2.0~beta1", ""])
def test_refuses_bad_or_lower_version(repo, version):
    refused(repo, version)


def test_refuses_missing_changelog_entry(repo):
    r = refused(repo, "0.3.0")
    assert "changelog" in r.stderr


def test_refuses_changelog_entry_not_on_top(repo):
    text = "v0.1.0 (2026-10-10)\n- Initial\n\nv0.2.0 (2026-10-20)\n- x\n"
    (repo / ADDON / "changelog.txt").write_text(text, encoding="utf-8")
    commit_and_push(repo, "reorder")
    refused(repo, "0.2.0")


def test_refuses_existing_tag(repo):
    git(repo, "tag", "-a", "v0.2.0", "-m", "x")
    before = head(repo)
    r = run(repo, "0.2.0")
    assert r.returncode != 0
    assert head(repo) == before


def test_news_is_capped_at_1500_chars(repo):
    long = "v0.2.0 (2026-10-20)\n" + "\n".join(f"- item {i} & more" for i in range(200)) + "\n"
    (repo / ADDON / "changelog.txt").write_text(long, encoding="utf-8")
    commit_and_push(repo, "long changelog")
    r = run(repo, "0.2.0")
    assert r.returncode == 0, r.stderr
    root = ET.parse(repo / ADDON / "addon.xml").getroot()
    news = root.find("extension[@point='xbmc.addon.metadata']/news").text
    assert len(news) <= 1500
    assert news.startswith("v0.2.0 (2026-10-20)\n- item 0 & more")


def test_refuses_head_ahead_of_origin_main(repo):
    (repo / "x.txt").write_text("x")
    git(repo, "add", "x.txt")
    git(repo, "commit", "-qm", "local only")
    r = refused(repo, "0.2.0")
    assert "origin/main" in r.stderr


def test_refuses_head_behind_origin_main(repo, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    git(other, "-c", "user.email=o@example.org", "-c", "user.name=o", "commit", "-q", "--allow-empty", "-m", "newer")
    git(other, "push", "-q", "origin", "main")
    r = refused(repo, "0.2.0")
    assert "origin/main" in r.stderr


def test_refuses_tag_already_on_origin(repo, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    git(other, "-c", "user.email=o@example.org", "-c", "user.name=o", "tag", "-a", "v0.2.0", "-m", "x")
    git(other, "push", "-q", "origin", "v0.2.0")
    r = refused(repo, "0.2.0")
    assert "origin" in r.stderr


def test_refuses_without_origin(repo):
    git(repo, "remote", "remove", "origin")
    refused(repo, "0.2.0")


def test_failed_commit_restores_addon_xml(repo):
    before_xml = (repo / ADDON / "addon.xml").read_bytes()
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    refused(repo, "0.2.0")
    assert (repo / ADDON / "addon.xml").read_bytes() == before_xml
    assert git(repo, "status", "--porcelain") == ""


def test_failed_tag_restores_addon_xml_and_drops_the_commit(repo):
    before_xml = (repo / ADDON / "addon.xml").read_bytes()
    git(repo, "config", "tag.gpgsign", "true")
    git(repo, "config", "gpg.program", "false")  # signing the tag fails
    refused(repo, "0.2.0")
    assert (repo / ADDON / "addon.xml").read_bytes() == before_xml
    assert git(repo, "status", "--porcelain") == ""
