"""tools/ci_release_guard.sh: the CI release job publishes only an annotated vX.Y.Z tag on main (backlog 010)."""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "ci_release_guard.sh")
ADDON = "plugin.video.epgstation"
ID = ["-c", "user.email=t@example.org", "-c", "user.name=t", "-c", "tag.gpgsign=false", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *ID, *args], check=True, capture_output=True, text=True).stdout


def set_version(repo, version):
    p = repo / ADDON / "addon.xml"
    p.write_text(re.sub(r'(<addon[^>]* version=")[^"]*"', rf'\g<1>{version}"', p.read_text(encoding="utf-8"),
                        count=1), encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    """origin.git with main at a release commit 0.2.0, and a CI-like clone 'ci'."""
    dev = tmp_path / "dev"
    (dev / ADDON).mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, ADDON, "addon.xml"), dev / ADDON / "addon.xml")
    set_version(dev, "0.2.0")
    git(tmp_path, "init", "-q", "-b", "main", str(dev))
    git(dev, "add", ".")
    git(dev, "commit", "-qm", "Release v0.2.0")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(dev, "remote", "add", "origin", str(origin))
    git(dev, "push", "-q", "origin", "main")
    return dev, origin


def ci_run(tmp_path, origin, tag):
    """Check out the tag the way CI does (detached) and run the guard."""
    ci = tmp_path / f"ci-{tag}"
    git(tmp_path, "clone", "-q", "--no-checkout", str(origin), str(ci))
    git(ci, "checkout", "-q", "--detach", f"refs/tags/{tag}")
    return subprocess.run(["bash", SCRIPT, tag], cwd=ci, capture_output=True, text=True)


def push_tag(dev, tag, annotated=True):
    if annotated:
        git(dev, "tag", "-a", tag, "-m", tag)
    else:
        git(dev, "tag", tag)
    git(dev, "push", "-q", "origin", tag)


def test_annotated_tag_on_main_passes(setup, tmp_path):
    dev, origin = setup
    push_tag(dev, "v0.2.0")
    r = ci_run(tmp_path, origin, "v0.2.0")
    assert r.returncode == 0, r.stderr


def test_tag_not_matching_addon_version_fails(setup, tmp_path):
    dev, origin = setup
    push_tag(dev, "v0.2.1")
    r = ci_run(tmp_path, origin, "v0.2.1")
    assert r.returncode != 0
    assert "addon.xml" in r.stderr


def test_tag_off_main_fails(setup, tmp_path):
    dev, origin = setup
    git(dev, "switch", "-qc", "side")
    set_version(dev, "0.3.0")
    git(dev, "commit", "-qam", "Release v0.3.0 (not on main)")
    push_tag(dev, "v0.3.0")
    r = ci_run(tmp_path, origin, "v0.3.0")
    assert r.returncode != 0
    assert "origin/main" in r.stderr


def test_lightweight_tag_fails(setup, tmp_path):
    dev, origin = setup
    push_tag(dev, "v0.2.0", annotated=False)
    r = ci_run(tmp_path, origin, "v0.2.0")
    assert r.returncode != 0
    assert "annotated" in r.stderr


def test_bad_tag_name_fails(setup, tmp_path):
    dev, origin = setup
    push_tag(dev, "v0.2.0")
    r = subprocess.run(["bash", SCRIPT, ""], cwd=dev, capture_output=True, text=True)
    assert r.returncode != 0


GITEA = os.path.join(ROOT, ".gitea", "workflows", "ci.yml")
GITHUB = os.path.join(ROOT, ".github", "workflows", "ci.yml")


def _release_job(path):
    text = open(path, encoding="utf-8").read()
    return text, text[text.index("\n  release:"):]


@pytest.mark.parametrize("path", [GITEA, GITHUB], ids=["gitea", "github"])
def test_ci_passes_the_tag_through_env_not_into_the_script(path):
    """${{ }} expanded inside run: is shell injection; the release job uses env TAG and "$TAG"."""
    if not os.path.isfile(path):
        pytest.skip(f"{os.path.relpath(path, ROOT)} is not in this tree (the public export has no .gitea, 019)")
    _, release = _release_job(path)
    run_lines = [ln for ln in release.splitlines() if ln.strip().startswith("run:")]
    assert run_lines and not [ln for ln in run_lines if "${{" in ln]
    assert not [ln for ln in release.splitlines() if "${{" in ln and "github.ref_name" not in ln
                and "steps.deployment" not in ln and "github.server_url" not in ln]
    assert "TAG: ${{ github.ref_name }}" in release
    assert 'tools/ci_release_guard.sh "$TAG"' in release
    assert "fetch-depth: 0" in release
    assert "build_repo.py check-repo-addon --src repository.kodistation" in release


def _jobs(text):
    """{job name: job text} of a workflow (two-space job keys under jobs:)."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    names = re.findall(r"^  ([\w-]+):\s*$", body, re.M)
    out = {}
    for i, name in enumerate(names):
        start = body.index(f"\n  {name}:") if i else 0
        end = body.index(f"\n  {names[i + 1]}:") if i + 1 < len(names) else len(body)
        out[name] = body[start:end]
    return out


def test_github_workflow_runs_only_on_github():
    """019: Gitea Actions also reads .github/workflows; every job is guarded so it never runs or queues there."""
    text = open(GITHUB, encoding="utf-8").read()
    jobs = _jobs(text)
    assert set(jobs) == {"check", "release"}
    for name, job in jobs.items():
        ifs = [ln.strip() for ln in job.splitlines() if ln.strip().startswith("if:")]
        assert ifs and "github.server_url == 'https://github.com'" in ifs[0], name
        assert "runs-on: ubuntu-24.04" in job
    assert "permissions:\n  contents: read" in text  # default: read only
    release = jobs["release"]
    assert "startsWith(github.ref, 'refs/tags/v')" in release and "needs: check" in release
    assert "concurrency: gh-pages" in release
    assert "--force" not in text and "push -f" not in text and "+HEAD" not in text
    assert "git push origin HEAD:gh-pages" in release
    assert "public_audit.py --zip dist/*.zip" in release
    assert "--expect-base-url \"$PUBLIC_REPO_BASE_URL\"" in release
    assert "PUBLIC_REPO_BASE_URL: https://myfeethurt.github.io/kodistation/" in release
    final = release[release.index("Verify GitHub Pages serves this release"):]
    assert '--require "plugin.video.epgstation=${TAG#v}"' in final  # C1: a cached old release never passes
    assert '--require "repository.kodistation=$repo_version"' in final
    check = jobs["check"]
    assert "make lint test check" in check
    assert "public_audit.py --worktree ." in check  # B1: tracked files only, never .venv / dist
    assert "public_audit.py --history ." in check
    hist = check[check.index("Public audit of the history"):]
    assert "if: github.event_name != 'pull_request'" in hist.split("run:")[0]
    uses = re.findall(r"uses: (\S+)", text)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses  # L4: SHA pins


def test_gitea_release_checks_pages_before_publishing():
    """019 B2: the LAN job moves the boxes to Pages (repository 1.1.0), so Pages must already serve the release."""
    if not os.path.isfile(GITEA):
        pytest.skip("no .gitea in this tree")
    _, release = _release_job(GITEA)
    check = release.index("verify --base-url https://myfeethurt.github.io/kodistation/")
    assert check < release.index("make publish")
    step = release[check:release.index("make publish")]
    assert '--require "plugin.video.epgstation=${TAG#v}"' in step
    assert '--require "repository.kodistation=$repo_version"' in step
    assert "--expect-base-url https://myfeethurt.github.io/kodistation/" in step


def test_public_base_url_is_the_same_everywhere():
    makefile = open(os.path.join(ROOT, "Makefile"), encoding="utf-8").read()
    assert "PUBLIC_REPO_BASE_URL := https://myfeethurt.github.io/kodistation/" in makefile
    assert "REPO_BASE_URL ?= $(PUBLIC_REPO_BASE_URL)" in makefile
    if os.path.isfile(GITEA):
        assert "--expect-base-url https://myfeethurt.github.io/kodistation/" in open(GITEA, encoding="utf-8").read()
