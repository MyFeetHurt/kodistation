"""tools/export_public.py: one public commit per export, never a push (backlog 019). Throwaway repositories only."""
import os
import subprocess

import export_public
import pytest

ADDON = "plugin.video.epgstation"
ID = ["-c", "user.email=dev@example.org", "-c", "user.name=dev", "-c", "commit.gpgsign=false",
      "-c", "tag.gpgsign=false"]
PUBLIC_EMAIL = "1+lee@users.noreply.github.com"
LAN_IP = "192." + "168.7.7"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *ID, *args], check=True, capture_output=True, text=True).stdout


def write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def set_version(src, version):
    write(src, f"{ADDON}/addon.xml", f'<?xml version="1.0"?>\n<addon id="{ADDON}" version="{version}"/>\n')


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """A Gitea-like source repo with a private file (export-ignore) and release tag v0.2.0, an empty public repo."""
    src = tmp_path / "src"
    git(tmp_path, "init", "-q", "-b", "main", str(src))
    write(src, ".gitattributes", "/CLAUDE.md export-ignore\n/.gitattributes export-ignore\n")
    write(src, "CLAUDE.md", f"dev VM {LAN_IP}\n")
    write(src, "README.md", "KODIstation\n")
    set_version(src, "0.2.0")
    git(src, "add", ".")
    git(src, "commit", "-qm", "Release v0.2.0 (private hash and message)")
    git(src, "tag", "-a", "v0.2.0", "-m", "Release v0.2.0")
    pub = tmp_path / "public"
    git(tmp_path, "init", "-q", "-b", "main", str(pub))
    monkeypatch.setenv("PUBLIC_GIT_NAME", "lee")
    monkeypatch.setenv("PUBLIC_GIT_EMAIL", PUBLIC_EMAIL)
    monkeypatch.setenv("PUBLIC_AUDIT_DENY", "private-literal")
    return src, pub


def export(src, pub, *args):
    return export_public.main(["--source", str(src), "--public-dir", str(pub), *args])


def test_init_creates_one_commit_and_tag_with_the_public_identity(repos, capsys):
    src, pub = repos
    assert export(src, pub, "--message", "KODIstation 0.2.0", "--init", "--tag") == 0
    assert git(pub, "log", "--format=%an <%ae>|%cn <%ce>|%s").splitlines() == \
        [f"lee <{PUBLIC_EMAIL}>|lee <{PUBLIC_EMAIL}>|KODIstation 0.2.0"]
    assert git(pub, "for-each-ref", "refs/tags", "--format=%(objecttype) %(refname:short) %(taggeremail) "
               "%(contents:subject)").split() == ["tag", "v0.2.0", f"<{PUBLIC_EMAIL}>", "Release", "v0.2.0"]
    assert sorted(git(pub, "ls-files").split()) == ["README.md", f"{ADDON}/addon.xml"]  # no CLAUDE.md
    out = capsys.readouterr().out
    assert f"git -C {pub} push --atomic origin main v0.2.0" in out
    assert "private hash" not in git(pub, "log", "--format=%B")


def test_second_run_on_the_same_rev_makes_no_commit(repos, capsys):
    src, pub = repos
    assert export(src, pub, "--message", "first", "--init") == 0
    origin = pub.parent / "origin.git"
    git(pub.parent, "clone", "-q", "--bare", str(pub), str(origin))
    git(pub, "remote", "add", "origin", str(origin))
    git(pub, "fetch", "-q", "origin")
    head = git(pub, "rev-parse", "HEAD")
    assert export(src, pub, "--message", "again") == 0
    assert git(pub, "rev-parse", "HEAD") == head
    assert "up to date" in capsys.readouterr().out
    write(src, "README.md", "KODIstation, now with more\n")
    git(src, "commit", "-qam", "readme")
    assert export(src, pub, "--message", "README update") == 0
    assert git(pub, "log", "--format=%s").splitlines() == ["README update", "first"]
    assert git(origin, "log", "--format=%s", "main").splitlines() == ["first"]  # never pushed


def test_refuses_a_dirty_or_diverged_public_clone(repos, capsys):
    src, pub = repos
    assert export(src, pub, "--message", "first", "--init") == 0
    assert export(src, pub, "--message", "x", "--init") == 2  # already has commits
    origin = pub.parent / "origin.git"
    git(pub.parent, "clone", "-q", "--bare", str(pub), str(origin))
    git(pub, "remote", "add", "origin", str(origin))
    write(pub, "stray.txt", "x")
    assert export(src, pub, "--message", "x") == 2
    assert "uncommitted" in capsys.readouterr().err
    os.remove(pub / "stray.txt")
    other = pub.parent / "other"
    git(pub.parent, "clone", "-q", str(origin), str(other))
    git(other, "commit", "-q", "--allow-empty", "-m", "made on GitHub by hand")
    git(other, "push", "-q", "origin", "main")
    assert export(src, pub, "--message", "x") == 2
    assert "differs from origin/main" in capsys.readouterr().err


def test_refuses_without_origin_or_identity_or_deny_list(repos, monkeypatch, capsys):
    src, pub = repos
    assert export(src, pub, "--message", "first", "--init") == 0
    assert export(src, pub, "--message", "x") == 2
    assert "no remote 'origin'" in capsys.readouterr().err
    monkeypatch.delenv("PUBLIC_AUDIT_DENY")
    assert export(src, pub, "--message", "x", "--init") == 2
    assert "PUBLIC_AUDIT_DENY" in capsys.readouterr().err
    monkeypatch.delenv("PUBLIC_GIT_EMAIL")
    assert export(src, pub, "--message", "x") == 2
    assert "PUBLIC_GIT_EMAIL" in capsys.readouterr().err


def test_public_email_must_be_github_noreply(repos, monkeypatch, capsys):
    src, pub = repos
    monkeypatch.setenv("PUBLIC_GIT_EMAIL", "dev@example.org")
    assert export(src, pub, "--message", "x", "--init") == 2
    assert "noreply" in capsys.readouterr().err
    assert not export_public.has_commits(str(pub))


def test_private_public_name_is_refused(repos, monkeypatch, capsys):
    src, pub = repos
    monkeypatch.setenv("PUBLIC_GIT_NAME", "private-literal")
    assert export(src, pub, "--message", "x", "--init") == 2
    captured = capsys.readouterr()
    assert "PUBLIC_GIT_NAME" in captured.err and "private-literal" not in captured.err + captured.out


def test_history_audit_failure_undoes_the_commit_and_tag(repos, monkeypatch, capsys):
    """L7: a hit in the public history takes the export back (commit and tag; first export: empty again)."""
    src, pub = repos
    real = export_public.audit
    monkeypatch.setattr(export_public, "audit", lambda config, tree=None, message=None, history=None:
                        [("commit x:0", "synthetic history hit")] if history else real(config, tree, message))
    assert export(src, pub, "--message", "KODIstation 0.2.0", "--init", "--tag") == 2
    assert "were undone" in capsys.readouterr().err
    assert not export_public.has_commits(str(pub)) and os.listdir(pub) == [".git"]
    assert git(pub, "tag", "--list") == ""
    monkeypatch.setattr(export_public, "audit", real)
    assert export(src, pub, "--message", "first", "--init") == 0
    head = git(pub, "rev-parse", "HEAD")
    origin = pub.parent / "origin.git"
    git(pub.parent, "clone", "-q", "--bare", str(pub), str(origin))
    git(pub, "remote", "add", "origin", str(origin))
    write(src, "README.md", "changed\n")
    git(src, "commit", "-qam", "readme")
    monkeypatch.setattr(export_public, "audit", lambda config, tree=None, message=None, history=None:
                        [("commit x:0", "synthetic history hit")] if history else real(config, tree, message))
    assert export(src, pub, "--message", "second") == 2
    assert git(pub, "rev-parse", "HEAD") == head and not git(pub, "status", "--porcelain")


def test_init_sets_the_local_identity(repos):
    """Review 2 M1: a hand-made commit in the public clone never uses the global (private) identity."""
    src, pub = repos
    assert export(src, pub, "--message", "first", "--init") == 0
    assert git(pub, "config", "--local", "user.name").strip() == "lee"
    assert git(pub, "config", "--local", "user.email").strip() == PUBLIC_EMAIL


def test_init_pages_creates_the_orphan_branch_with_the_public_identity(repos, capsys):
    src, pub = repos
    assert export(src, pub, "--init-pages") == 2  # no main yet
    assert "export main first" in capsys.readouterr().err
    assert export(src, pub, "--message", "first", "--init") == 0
    head = git(pub, "rev-parse", "HEAD")
    assert export(src, pub, "--init-pages") == 0
    out = capsys.readouterr().out
    assert f"git -C {pub} push origin gh-pages" in out
    assert git(pub, "log", "gh-pages", "--format=%an <%ae>|%cn <%ce>|%P").splitlines() == \
        [f"lee <{PUBLIC_EMAIL}>|lee <{PUBLIC_EMAIL}>|"]  # one commit, no parent
    assert git(pub, "ls-tree", "--name-only", "gh-pages").split() == ["index.html"]
    assert git(pub, "show", "gh-pages:index.html") == ""
    assert git(pub, "rev-parse", "HEAD") == head and git(pub, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert not git(pub, "status", "--porcelain")
    assert export(src, pub, "--init-pages") == 2  # already there
    assert "already has a gh-pages branch" in capsys.readouterr().err


def test_tag_must_be_annotated_and_match_addon_xml(repos, capsys):
    src, pub = repos
    set_version(src, "0.2.1")
    git(src, "commit", "-qam", "bump")
    git(src, "tag", "v0.2.1")  # lightweight
    assert export(src, pub, "--message", "x", "--init", "--tag") == 2
    assert "not an annotated tag" in capsys.readouterr().err
    git(src, "tag", "-d", "v0.2.1")
    git(src, "tag", "-a", "v0.3.0", "-m", "wrong version")
    assert export(src, pub, "--message", "x", "--init", "--tag") == 2
    assert "no tag v0.2.1" in capsys.readouterr().err
    assert os.listdir(pub) == [".git"] and not export_public.has_commits(str(pub))


@pytest.mark.parametrize("change", ["file", "literal", "message"])
def test_refuses_an_audit_hit_and_exports_nothing(repos, capsys, change):
    src, pub = repos
    msg = "KODIstation"
    if change == "file":
        write(src, "tools/x.py", f"HOST = '{LAN_IP}'\n")
    elif change == "literal":
        write(src, "tests/fixtures/x.json", '{"a": "Private-Literal"}\n')
    else:
        msg = f"from {LAN_IP}"
    git(src, "add", ".")
    git(src, "commit", "-qm", "leak", "--allow-empty")
    assert export(src, pub, "--message", msg, "--init") == 2
    captured = capsys.readouterr()
    assert "public audit:" in captured.err and "private-literal" not in captured.out.lower()
    assert os.listdir(pub) == [".git"]


def test_never_runs_git_push():
    code = open(export_public.__file__, encoding="utf-8").read()
    assert '"push"' not in code and "'push'" not in code
