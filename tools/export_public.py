#!/usr/bin/env python3
"""Export one commit of this repository to the public clone (backlog 019). Stdlib only. Never pushes.

    tools/export_public.py --message MSG [--rev REV] [--tag] [--init] [--public-dir DIR]
    tools/export_public.py --init-pages [--public-dir DIR]
    make export-public MESSAGE="KODIstation x.y.z" [TAG=1] [INIT=1]
    make export-pages-init

The public repository is a one-way mirror of selected files: `git archive REV` (default HEAD; .gitattributes
export-ignore leaves out the private factory files) replaces the public clone's working tree and becomes one commit
on its `main`, authored and committed by the public identity ($PUBLIC_GIT_NAME / $PUBLIC_GIT_EMAIL).

Refuses unless
- the public clone ($PUBLIC_DIR) is a clean git repository on `main` whose `main` equals `origin/main` after a
  `git fetch` (nobody changed the public repository behind the export); with --init instead: no commit yet;
- the exported tree and MSG pass tools/public_audit.py --strict (generic rules, private literals, fingerprints,
  media);
- with --tag: REV carries an annotated tag vX.Y.Z equal to "v" + its addon.xml version. The public commit then gets
  the annotated tag vX.Y.Z "Release vX.Y.Z" (tagger = the public identity), and the public history is audited.
Nothing changed: "up to date", exit 0. At the end it prints the push command; pushing is the user's step.
--init also sets the clone's local user.name / user.email to the public identity, so a commit made there by hand
never carries a private address.

--init-pages (go-live step G4) creates the orphan branch `gh-pages` in the public clone: one commit holding an empty
index.html, made by the public identity without touching the working tree, audits the public history and prints
the push command. It refuses when gh-pages already exists.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import local_env  # noqa: E402
import public_audit  # noqa: E402

ADDON_XML = "plugin.video.epgstation/addon.xml"
NO_SIGN = ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "tag.forceSignAnnotated=false"]
NOREPLY = "@users.noreply.github.com"


class RefusedError(Exception):
    pass


def git(repo, *args, env=None, check=True):
    done = subprocess.run(["git", "-C", repo, *NO_SIGN, *args], capture_output=True, text=True, env=env)
    if check and done.returncode != 0:
        raise RefusedError(f"git {' '.join(args)} failed in {repo}: {done.stderr.strip()}")
    return done.stdout


def has_commits(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "-q", "--verify", "HEAD^{commit}"],
                          capture_output=True).returncode == 0


def check_public(public, init):
    if not os.path.isdir(os.path.join(public, ".git")):
        raise RefusedError(f"{public} is not a git repository (PUBLIC_DIR; create it with git init -b main)")
    branch = git(public, "symbolic-ref", "--short", "HEAD", check=False).strip()
    if branch != "main":
        raise RefusedError(f"{public} is on {branch or 'a detached HEAD'}, not main")
    if git(public, "status", "--porcelain", "--untracked-files=all").strip():
        raise RefusedError(f"{public} has uncommitted changes")
    if init:
        if has_commits(public):
            raise RefusedError(f"--init: {public} already has commits")
        return
    if not has_commits(public):
        raise RefusedError(f"{public} has no commit yet: the first export needs --init")
    if "origin" not in git(public, "remote").split():
        raise RefusedError(f"{public} has no remote 'origin' to compare main with")
    git(public, "fetch", "-q", "origin")
    origin = git(public, "rev-parse", "-q", "--verify", "refs/remotes/origin/main", check=False).strip()
    if origin != git(public, "rev-parse", "HEAD").strip():
        raise RefusedError(f"{public}: main differs from origin/main (the public repository changed behind the export)")


def release_tag(source, rev):
    """vX.Y.Z of REV: an annotated tag on it equal to "v" + its addon.xml version."""
    commit = git(source, "rev-parse", f"{rev}^{{commit}}").strip()
    xml = git(source, "show", f"{commit}:{ADDON_XML}")
    m = re.search(r'<addon[^>]* version="([^"]+)"', xml)
    want = f"v{m.group(1)}" if m else None
    tags = git(source, "tag", "--points-at", commit, "--list", "v*").split()
    if want not in tags:
        raise RefusedError(f"{rev} has no tag {want} (addon.xml version); tags there: {', '.join(tags) or 'none'}")
    if git(source, "cat-file", "-t", f"refs/tags/{want}").strip() != "tag":
        raise RefusedError(f"{want} is not an annotated tag (use make release)")
    return want


def sync_tree(src, dst):
    """Make dst (all but .git) an exact copy of src."""
    for name in os.listdir(dst):
        if name == ".git":
            continue
        path = os.path.join(dst, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s) and not os.path.islink(s):
            shutil.copytree(s, d, symlinks=True)
        else:
            shutil.copy2(s, d, follow_symlinks=False)


def undo(public, before, version_tag):
    """Take back this run's commit and tag: back to `before` (None: the repository had no commit yet)."""
    if version_tag:
        git(public, "tag", "-d", version_tag, check=False)
    if before:
        git(public, "reset", "-q", "--hard", before)
        return
    git(public, "update-ref", "-d", "HEAD", check=False)
    git(public, "read-tree", "--empty")
    for name in os.listdir(public):
        if name != ".git":
            path = os.path.join(public, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)


def audit(config, tree=None, message=None, history=None):
    a = public_audit.Audit(config)
    if tree:
        a.tree(tree)
    if message is not None:
        a.text("message", message)
    if history:
        a.history(history)
    return a.hits


def public_identity():
    """(name, email, audit config) of the public identity; RefusedError when it or the deny lists are missing."""
    name, email = local_env.get("PUBLIC_GIT_NAME"), local_env.get("PUBLIC_GIT_EMAIL")
    if not name or not email:
        raise RefusedError("PUBLIC_GIT_NAME and PUBLIC_GIT_EMAIL must be set (local.env)")
    if not email.lower().endswith(NOREPLY):
        raise RefusedError(f"PUBLIC_GIT_EMAIL must be a GitHub noreply address (*{NOREPLY})")
    config = public_audit.Config(public_email=email)
    if config.missing:
        raise RefusedError(f"PUBLIC_AUDIT_DENY_FILE names {len(config.missing)} missing file(s)")
    if not config.private():
        raise RefusedError("the export audit needs PUBLIC_AUDIT_DENY and/or PUBLIC_AUDIT_DENY_FILE (local.env)")
    for rule in public_audit.Audit(config).line_rules(name):
        raise RefusedError(f"PUBLIC_GIT_NAME breaks an audit rule ({rule})")
    return name, email, config


def identity_env(name, email):
    return dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name,
                GIT_COMMITTER_EMAIL=email)


def init_pages(public, out=print):
    """G4: the orphan gh-pages commit (empty index.html) by the public identity. Never pushes."""
    name, email, config = public_identity()
    if not os.path.isdir(os.path.join(public, ".git")):
        raise RefusedError(f"{public} is not a git repository (PUBLIC_DIR)")
    if not has_commits(public):
        raise RefusedError(f"{public} has no commit yet: export main first (make export-public INIT=1)")
    if git(public, "rev-parse", "-q", "--verify", "refs/heads/gh-pages", check=False).strip():
        raise RefusedError(f"{public} already has a gh-pages branch")
    env = identity_env(name, email)
    blob = subprocess.run(["git", "-C", public, "hash-object", "-w", "--stdin"], input=b"", capture_output=True,
                          check=True).stdout.decode().strip()
    tree = subprocess.run(["git", "-C", public, "mktree"], input=f"100644 blob {blob}\tindex.html\n".encode(),
                          capture_output=True, check=True).stdout.decode().strip()
    commit = git(public, "commit-tree", tree, "-m", "gh-pages: Kodi repository (filled by the release job)",
                 env=env).strip()
    git(public, "update-ref", "refs/heads/gh-pages", commit, "")
    hits = audit(config, history=public)
    if hits:
        for where, rule in hits:
            out(f"{where}: {rule}")
        git(public, "update-ref", "-d", "refs/heads/gh-pages", commit)
        raise RefusedError(f"public history audit: {len(hits)} hits; gh-pages was not created")
    out(f"created gh-pages {commit[:12]} (index.html) in {public}")
    out("next step (the user's, never automatic):")
    out(f"  git -C {public} push origin gh-pages")
    return 0


def export(source, public, message, rev="HEAD", tag=False, init=False, out=print):
    name, email, config = public_identity()
    if not message.strip():
        raise RefusedError("empty message")
    check_public(public, init)
    if init:  # a commit made in the public clone by hand uses the public identity, never the global one
        git(public, "config", "--local", "user.name", name)
        git(public, "config", "--local", "user.email", email)
    version_tag = release_tag(source, rev) if tag else None
    if version_tag and git(public, "tag", "--list", version_tag).strip():
        raise RefusedError(f"{public} already has the tag {version_tag}")
    with tempfile.TemporaryDirectory() as tmp:
        public_audit.export_tree(source, tmp, git(source, "rev-parse", f"{rev}^{{commit}}").strip())
        hits = audit(config, tree=tmp, message=message)
        if hits:
            for where, rule in hits:
                out(f"{where}: {rule}")
            raise RefusedError(f"public audit: {len(hits)} hits; nothing exported")
        sync_tree(tmp, public)
    env = identity_env(name, email)
    before = git(public, "rev-parse", "HEAD").strip() if has_commits(public) else None
    git(public, "add", "-A")
    changed = bool(git(public, "status", "--porcelain").strip())
    if changed:
        git(public, "commit", "-q", "-m", message, env=env)
        out(f"exported {rev} -> {public}: {git(public, 'rev-parse', '--short', 'HEAD').strip()} {message}")
    elif not version_tag:
        out(f"{public}: up to date with {rev}, nothing to commit")
        return 0
    if version_tag:
        git(public, "tag", "-a", version_tag, "-m", f"Release {version_tag}", env=env)
        out(f"tagged {version_tag}")
    hits = audit(config, history=public)
    if hits:
        for where, rule in hits:
            out(f"{where}: {rule}")
        undo(public, before, version_tag)
        raise RefusedError(f"public history audit: {len(hits)} hits; the export commit and tag were undone")
    refs = "main" + (f" {version_tag}" if version_tag else "")
    out("next step (the user's, never automatic):")
    out(f"  git -C {public} push {'--atomic ' if version_tag else ''}origin {refs}")
    return 0


def main(argv=None):
    local_env.load()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--message")
    ap.add_argument("--init-pages", action="store_true", help="create the orphan gh-pages branch (step G4)")
    ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--tag", action="store_true", help="REV is a release: also create the public tag vX.Y.Z")
    ap.add_argument("--init", action="store_true", help="first export into a repository without commits")
    ap.add_argument("--public-dir", default=None, help="the public clone (default $PUBLIC_DIR)")
    ap.add_argument("--source", default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    public = os.path.expanduser(args.public_dir or local_env.get("PUBLIC_DIR"))
    if not public:
        print("export refused: PUBLIC_DIR is not set (local.env)", file=sys.stderr)
        return 2
    if not args.init_pages and not args.message:
        ap.error("--message is required (or --init-pages)")
    try:
        if args.init_pages:
            return init_pages(public)
        return export(args.source, public, args.message, args.rev, args.tag, args.init)
    except RefusedError as e:
        print(f"export refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
