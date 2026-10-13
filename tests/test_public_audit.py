"""tools/public_audit.py: leak audit of the public export (backlog 019).

Private-looking values are built by concatenation, so this file itself passes the audit."""
import glob
import hashlib
import os
import subprocess
import zipfile

import public_audit
import pytest
from resources.lib.epgstation import thumbs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAN_IP = "192." + "168.1.5"
SECRET = "sec" + "ret-host"
MAIL = "someone" + "@" + "mail.invalid-domain.org"
ID = ["-c", "user.email=t@example.org", "-c", "user.name=t", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]

JFIF = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
COM = b"\xff\xfe" + (2 + len(public_audit.MARK)).to_bytes(2, "big") + public_audit.MARK
JPEG_PLACEHOLDER = b"\xff\xd8" + JFIF + COM + b"\xff\xda\x00\x02" + b"\x00" * 16 + b"\xff\xd9"
JPEG_EXIF = b"\xff\xd8\xff\xe1\x00\x08Exif\x00\x00\xff\xda\x00\x02\x00\xff\xd9"
PNG_CLEAN = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 13 + b"CRC0" + b"\x00\x00\x00\x00IEND" + b"CRC0"
PNG_TEXT = PNG_CLEAN[:33] + b"\x00\x00\x00\x08tEXtAuthor\x00x" + b"CRC0" + PNG_CLEAN[33:]
GIF_COMMENT = b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00" + b"\x21\xfe\x03abc\x00" + b"\x3b"
GIF_CLEAN = b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00" + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00" + b"\x3b"
MP4_CLEAN = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16 + b"\x00\x00\x00\x10\xa9tooLavf60.3.100"
MP4_TITLE = MP4_CLEAN + b"\x00\x00\x00\x10\xa9namMy title"


def run(tmp_path, files, media=None, deny=None, deny_files=None, allow_files=(), placeholders=None, credentials=""):
    root = tmp_path / f"tree{len(list(tmp_path.glob('tree*')))}"
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    media_list = tmp_path / "public_images.txt"
    media_list.write_text("".join(f"{hashlib.sha256(d).hexdigest()}  {rel}\n" for rel, d in (media or {}).items()))
    ph_list = tmp_path / "placeholder_thumbs.txt"
    ph_list.write_text("".join(f"{hashlib.sha256(d).hexdigest()}  {rel}\n" for rel, d in (placeholders or {}).items()))
    config = public_audit.Config(deny=deny or [], deny_files=deny_files or [], allow_files=list(allow_files),
                                 media_list=str(media_list), public_email="", placeholder_list=str(ph_list),
                                 credentials=credentials)
    audit = public_audit.Audit(config)
    audit.tree(str(root))
    return audit.hits


def test_clean_tree_passes(tmp_path):
    assert run(tmp_path, {"README.md": "http://epgstation.local:8888\nkodi@192.0.2.10\nt@example.org\n",
                          "a/b.py": "x = 1\n"}) == []


@pytest.mark.parametrize("line, rule", [
    (f"url = 'http://{LAN_IP}:8888'", "private IPv4 address"),
    ("10." + "0.0.1", "private IPv4 address"),
    ("172." + "20.1.1", "private IPv4 address"),
    ("dev VM 192." + "168.0.x", "private IPv4 address"),
    ("100." + "101.102.103", "private IPv4 address"),
    ("169." + "254.1.1", "private IPv4 address"),
    ("the dev VM " + ".42 and", "short host address (.NNN)"),
    ("fd" + "12:3456:789a::1", "private IPv6 address"),
    ("fe80" + "::1%eth0", "private IPv6 address"),
    ("ssh:" + "//gitea.lan:2200/x.git", "ssh URL"),
    ("git" + "@gitea.lan:x/y.git", "scp-style git remote"),
    ("/ho" + "me/alice/x", "/home/<name> path"),
    ("~/.config/epgstation-kodi/" + "gitea", "private credentials file"),
    (MAIL, "e-mail address"),
])
def test_generic_rules(tmp_path, line, rule):
    hits = run(tmp_path, {"docs/x.md": f"ok\n{line}\n"})
    assert ("docs/x.md:2", rule) in hits and {w for w, _ in hits} == {"docs/x.md:2"}


def test_generic_rules_allow_public_values(tmp_path):
    ok = ["git" + "@github.com:MyFeetHurt/kodistation.git", "version 1.10.0.1", "172.32.0.1", "EPGStation 2.10.0",
          "LOCK_STALE = 10.0", 'x + ".000"', "1.0.0.100", "100.200.1.1", "ff02::1", "noreply " +
          "14802057+X" + "@users.noreply.github.com", "a" + "@example.com", "kodi" + "@kodi-box.test",
          "@BASE_URL@addons.xml"]
    assert run(tmp_path, {"x.txt": "\n".join(ok)}) == []


def test_email_in_fixtures_is_broadcast_data(tmp_path):
    assert run(tmp_path, {"tests/fixtures/dev/x.json": f'{{"extended": "{MAIL}"}}'}) == []


def test_private_literals_everywhere_without_printing_them(tmp_path, capsys):
    files = {"tests/fixtures/dev/x.json": f'{{"a": "{SECRET.upper()}"}}', f"tools/{SECRET}.txt": "x",
             "y.txt": "a:77770 b\n"}
    hits = run(tmp_path, files, deny=[SECRET, ":7777", "abc"])
    assert ("tests/fixtures/dev/x.json:1", "private literal (PUBLIC_AUDIT_DENY)") in hits  # case-insensitive
    assert ("tools/***.txt:0", "private literal (PUBLIC_AUDIT_DENY) in the file name") in hits  # masked
    assert not [h for h in hits if h[0].startswith("y.txt")]  # ":7777" is not part of ":77770"
    assert all(SECRET not in w + r for w, r in hits)
    # L5: a literal of 6+ characters matches inside other words too; a short one only as a word
    glued = run(tmp_path, {"z.md": f"x{SECRET}2\nabcd\n"}, deny=[SECRET, "abc"])
    assert glued == [("z.md:1", "private literal (PUBLIC_AUDIT_DENY)")]


def test_generic_rules_on_file_names_are_masked(tmp_path, capsys):
    hits = run(tmp_path, {f"tests/{LAN_IP}.json": "{}", f"docs/{MAIL}.md": "x"})
    assert ("tests/***.json:0", "private IPv4 address in the file name") in hits
    assert ("docs/***:0", "e-mail address in the file name") in hits
    assert all(LAN_IP not in w and MAIL not in w for w, _ in hits)


def test_credentials_file_values_are_deny_literals(tmp_path, capsys):
    """M4: the Gitea credentials file is read at run time; its values are never printed."""
    cred = tmp_path / "gitea"
    pw, user = "Pw" + "-synthetic-9f3k", "bot" + "user7"
    cred.write_text(f"user={user}\npassword: {pw}\nurl=http://{user}:{pw}@gitea.example/\n")
    assert sorted(public_audit.credential_values(str(cred))) == sorted(
        [user, pw, f"http://{user}:{pw}@gitea.example/"])
    hits = run(tmp_path, {"a.md": f"token {pw}\n", f"{user}.txt": "x"}, credentials=str(cred))
    assert sorted(hits) == [("***.txt:0", "private literal (PUBLIC_AUDIT_DENY) in the file name"),
                            ("a.md:1", "private literal (PUBLIC_AUDIT_DENY)")]
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "b.md").write_text(f"x {pw}\n")
    import os as _os
    _os.environ["PUBLIC_AUDIT_CREDENTIALS_FILE"] = str(cred)
    try:
        assert public_audit.main([str(tmp_path / "t")]) == 1
    finally:
        _os.environ["PUBLIC_AUDIT_CREDENTIALS_FILE"] = str(tmp_path / "none")
    out = capsys.readouterr().out
    assert pw not in out and user not in out and "b.md:1: private literal" in out
    assert run(tmp_path, {"a.md": "x"}, credentials=str(tmp_path / "missing")) == []


def test_fingerprints_with_allow_lists(tmp_path):
    fp = tmp_path / "fp.txt"
    fp.write_text("# private\nＴｏｋｅｎＡ\n4242424242\nAllowedOne\n", encoding="utf-8")
    allow = tmp_path / "allow.txt"
    allow.write_text("AllowedOne\t# reviewed\n", encoding="utf-8")
    hits = run(tmp_path, {"t.json": '"TokenA" and 4242424242\nAllowedOne\n'}, deny_files=[str(fp)],
               allow_files=[str(allow)])
    assert hits == [("t.json:1", "production fingerprint x2 (PUBLIC_AUDIT_DENY_FILE)")]  # NFKC match


def test_fingerprint_hits_print_only_place_and_count(tmp_path, monkeypatch, capsys):
    fp = tmp_path / "fp.txt"
    fp.write_text("VerySecretTitle\n", encoding="utf-8")
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "a.txt").write_text("x VerySecretTitle\n")
    monkeypatch.setenv("PUBLIC_AUDIT_DENY_FILE", str(fp))
    assert public_audit.main([str(tmp_path / "tree")]) == 1
    out = capsys.readouterr().out
    assert "a.txt:1: production fingerprint x1" in out and "VerySecretTitle" not in out
    assert "1 hits (0 private literals, 1 fingerprints)" in out
    monkeypatch.setenv("PUBLIC_AUDIT_DENY_FILE", str(tmp_path / "missing.txt"))
    assert public_audit.main([str(tmp_path / "tree")]) == 2


def test_strict_needs_a_private_deny_list(tmp_path, monkeypatch):
    (tmp_path / "t").mkdir()
    assert public_audit.main(["--strict", str(tmp_path / "t")]) == 2
    monkeypatch.setenv("PUBLIC_AUDIT_DENY", SECRET)
    assert public_audit.main(["--strict", str(tmp_path / "t")]) == 0


def test_images_need_the_placeholder_list_or_the_media_list(tmp_path):
    fix = "tests/fixtures/dev/api/thumbnails/"
    assert run(tmp_path, {fix + "1.jpg": JPEG_PLACEHOLDER}, placeholders={fix + "1.jpg": JPEG_PLACEHOLDER}) == []
    hits = run(tmp_path, {fix + "1.jpg": JPEG_PLACEHOLDER, fix + "2.jpg": JPEG_EXIF, "docs/a.png": PNG_CLEAN})
    assert (fix + "1.jpg:0", "fixture thumbnail not in tools/placeholder_thumbs.txt") in hits  # marker alone: no
    assert (fix + "2.jpg:0", "fixture thumbnail not in tools/placeholder_thumbs.txt") in hits
    assert ("docs/a.png:0", "binary / image / video not in tools/public_images.txt") in hits
    assert run(tmp_path, {"docs/a.png": PNG_CLEAN}, media={"docs/a.png": PNG_CLEAN}) == []
    assert run(tmp_path, {"docs/a.png": PNG_CLEAN}, media={"docs/a.png": PNG_TEXT}) == \
        [("docs/a.png:0", "sha256 differs from tools/public_images.txt")]


def test_placeholder_structure_is_checked_even_when_listed(tmp_path):
    """M2: a listed placeholder still may hold nothing but the generator's plain JPEG segments."""
    fix = "tests/fixtures/dev/api/thumbnails/9.jpg"
    exif = b"\xff\xe1\x00\x10Exif\x00\x00" + b"10." + b"1.2.3."
    bad = {
        "JPEG segment 0xE1": b"\xff\xd8" + JFIF + exif + COM + b"\xff\xda\x00\x02\x00\xff\xd9",
        "data after the end of the image": JPEG_PLACEHOLDER + b"tail",
        "JPEG comment": b"\xff\xd8" + JFIF + COM + b"\xff\xfe\x00\x05abc" + b"\xff\xda\x00\x02\x00\xff\xd9",
    }
    for problem, data in bad.items():
        hits = run(tmp_path, {fix: data}, placeholders={fix: data})
        assert (fix + ":0", f"placeholder: {problem}") in hits, problem
    tail = JPEG_PLACEHOLDER + b" /ho" + b"me/alice "
    assert (fix + " (bytes):2", "/home/<name> path") in run(tmp_path, {fix: tail}, placeholders={fix: tail})
    changed = JPEG_PLACEHOLDER[:-3] + b"\x01\xff\xd9"
    assert run(tmp_path, {fix: changed}, placeholders={fix: JPEG_PLACEHOLDER}) == \
        [(fix + ":0", "sha256 differs from tools/placeholder_thumbs.txt")]


@pytest.mark.parametrize("rel, data, rule", [
    ("docs/x.bmp", b"BM" + b"\0" * 20, "image format not allowed (png, gif, placeholder jpg only)"),
    ("docs/x.heic", b"\0" * 20, "image format not allowed (png, gif, placeholder jpg only)"),
    ("media/x.mov", b"\0\0\0\x14ftypqt  ", "video / stream file"),
    ("docs/x.avi", b"RIFF" + b"\0" * 20, "video / stream file"),
    ("docs/x.json.gz", b"\x1f\x8b\x08\x00", "archive file"),
    ("media/x.zip", b"PK\x03\x04", "archive file"),
    ("tools/blob", b"\x00\x01binary", "binary / image / video not in tools/public_images.txt"),
    ("tools/data.bin", b"just text", "binary / image / video not in tools/public_images.txt"),
])
def test_binaries_are_denied_by_default(tmp_path, rel, data, rule):
    """M3: a NUL byte or a non-text extension fails unless listed; archives and other formats always fail."""
    assert run(tmp_path, {rel: data}, media={rel: data}) == [(f"{rel}:0", rule)]


def test_utf16_with_bom_is_read_as_text(tmp_path):
    data = f"x {LAN_IP}\n".encode("utf-16")
    assert run(tmp_path, {"docs/x.txt": data}) == [("docs/x.txt:1", "private IPv4 address")]


def test_ignored_venv_and_dist_do_not_count_in_worktree_mode(tmp_path, capsys):
    """B1: GitHub CI audits the tracked tree (--worktree), not .venv / dist on the runner."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / ".gitignore").write_text(".venv/\ndist/\n")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "c")
    for rel in (".venv/lib/site.py", "dist/x.zip"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(b"PK\x03\x04 " + f"{LAN_IP} /ho".encode() + b"me/runner")
    assert public_audit.main(["--worktree", str(repo)]) == 0
    assert public_audit.main([str(repo)]) == 1  # the plain directory mode would have seen them
    assert "0 hits" in capsys.readouterr().out.split("public audit:")[1]


@pytest.mark.parametrize("rel, data, problem", [
    ("media/a.png", PNG_TEXT, "metadata: PNG chunk tEXt"),
    ("media/demo.gif", GIF_COMMENT, "metadata: GIF comment"),
    ("media/demo.mp4", MP4_TITLE, "metadata: MP4 tag \xa9nam"),
    ("icon.png", PNG_TEXT, "metadata: PNG chunk tEXt"),
])
def test_listed_media_must_have_no_metadata(tmp_path, rel, data, problem):
    assert (f"{rel}:0", problem) in run(tmp_path, {rel: data}, media={rel: data})


def test_clean_media_pass_and_their_bytes_are_scanned(tmp_path):
    media = {"media/demo.gif": GIF_CLEAN, "media/demo.mp4": MP4_CLEAN, "media/s.png": PNG_CLEAN}
    assert run(tmp_path, media, media=media) == []
    leaky = MP4_CLEAN + b"\x00\x00/ho" + b"me/alice/clip.ts\x00"
    hits = run(tmp_path, {"media/demo.mp4": leaky}, media={"media/demo.mp4": leaky})
    assert [(w.split(":")[0], r) for w, r in hits] == [("media/demo.mp4 (bytes)", "/home/<name> path")]


@pytest.mark.parametrize("rel", ["tests/fixtures/dev/a.m2ts", "x.ts", "docs/clip.mp4", "a.mkv", "media/a.m4v"])
def test_stream_and_stray_video_files_fail(tmp_path, rel):
    assert run(tmp_path, {rel: MP4_CLEAN}, media={rel: MP4_CLEAN}) == [(f"{rel}:0", "video / stream file")]


def test_zip_members(tmp_path, monkeypatch, capsys):
    z = tmp_path / "plugin.test-1.0.0.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("plugin.test/addon.xml", f"<source>http://{LAN_IP}:3000/x</source>\n")
        zf.writestr("plugin.test/icon.png", PNG_CLEAN)
    assert public_audit.main(["--zip", str(z)]) == 1
    out = capsys.readouterr().out
    assert "plugin.test-1.0.0.zip!plugin.test/addon.xml:1: private IPv4 address" in out
    assert "plugin.test-1.0.0.zip!plugin.test/icon.png:0: binary / image / video not in tools/public_images.txt" in out


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *ID, *args], check=True, capture_output=True, text=True,
                          env=env).stdout


def test_history_identities_names_refs_and_messages(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    noreply = "1+lee" + "@users.noreply.github.com"
    env = dict(os.environ, GIT_AUTHOR_EMAIL=noreply, GIT_COMMITTER_EMAIL=noreply, GIT_AUTHOR_NAME="lee",
               GIT_COMMITTER_NAME="lee")
    git(repo, "commit", "-q", "--allow-empty", "-m", "first", env=env)
    config = public_audit.Config(deny=[SECRET], deny_files=[], public_email="", credentials="")
    a = public_audit.Audit(config)
    a.history(str(repo))
    assert a.hits == []
    git(repo, "commit", "-q", "--allow-empty", "-m", f"see {LAN_IP}")  # t@example.org identity
    git(repo, "tag", "-a", "v1.0.0", "-m", "Release v1.0.0")
    env2 = dict(env, GIT_AUTHOR_NAME=SECRET, GIT_COMMITTER_NAME=SECRET)
    git(repo, "commit", "-q", "--allow-empty", "-m", "named", env=env2)
    git(repo, "tag", f"{SECRET}-tag")  # lightweight tag: only its name
    git(repo, "branch", f"feat/{SECRET}")
    a = public_audit.Audit(config)
    a.history(str(repo))
    rules = sorted(r for _, r in a.hits)
    assert rules == ["author e-mail is not a GitHub noreply address",
                     "author name: private literal (PUBLIC_AUDIT_DENY)",
                     "committer e-mail is not a GitHub noreply address",
                     "committer name: private literal (PUBLIC_AUDIT_DENY)",
                     "private IPv4 address",
                     "private literal (PUBLIC_AUDIT_DENY) in a ref name",
                     "private literal (PUBLIC_AUDIT_DENY) in a ref name",
                     "tagger e-mail is not a GitHub noreply address"]
    assert all(SECRET not in w for w, _ in a.hits)
    # M1: an identity listed as PUBLIC_GIT_EMAIL is not enough, only noreply passes
    a = public_audit.Audit(public_audit.Config(deny=[], deny_files=[], public_email="t@example.org", credentials=""))
    a.history(str(repo))
    assert "author e-mail is not a GitHub noreply address" in [r for _, r in a.hits]


def test_worktree_export_uses_export_ignore_and_untracked_files(tmp_path, capsys):
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / ".gitattributes").write_text("/private export-ignore\n/.gitattributes export-ignore\n")
    (repo / "private").mkdir()
    (repo / "private" / "env.md").write_text(f"http://{LAN_IP}:8888\n")
    (repo / "pub.txt").write_text("ok\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "c")
    assert public_audit.main(["--worktree", str(repo)]) == 0
    (repo / "new.txt").write_text(f"{LAN_IP}\n")  # untracked, not ignored: it would be committed next
    assert public_audit.main(["--worktree", str(repo)]) == 1
    assert "new.txt:1: private IPv4 address" in capsys.readouterr().out
    out = tmp_path / "out"
    out.mkdir()
    public_audit.export_tree(str(repo), str(out), "HEAD")
    assert sorted(os.listdir(out)) == ["pub.txt"]


def test_repository_fixture_thumbnails_are_placeholders():
    """AC 4: every fixture JPEG is a 480x270 placeholder; dev 22 stays blank and dev 11 has content."""
    paths = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", "*", "api", "thumbnails", "*.jpg")))
    assert len(paths) >= 24
    listed = public_audit.Config(deny=[], deny_files=[], credentials="").placeholders
    for p in paths:
        data = open(p, "rb").read()
        assert public_audit.jpeg_comments(data) == [public_audit.MARK], p
        assert public_audit.placeholder_problems(data) == [], p
        assert listed[os.path.relpath(p, ROOT)] == hashlib.sha256(data).hexdigest(), p
        assert thumbs.summary(data)[1:] == (480, 270), p

    def blank(name):
        return thumbs.is_blank(*thumbs.summary(open(os.path.join(ROOT, "tests", "fixtures", "dev", "api",
                                                                "thumbnails", name), "rb").read()))
    assert blank("22.jpg") and not blank("11.jpg")


def test_public_media_list_covers_the_icons():
    config = public_audit.Config(deny=[], deny_files=[], credentials="")
    for icon in ("plugin.video.epgstation/resources/icon.png", "repository.kodistation/resources/icon.png"):
        data = open(os.path.join(ROOT, icon), "rb").read()
        assert config.media.get(icon) == hashlib.sha256(data).hexdigest()
        assert public_audit.metadata_problems(icon, data) == []


def test_symlinks_fail_with_name_and_target_audited(tmp_path):
    """Review 2 C2: os.walk does not enter directory links; every link fails and its name / target are checked."""
    root = tmp_path / "tree"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("ok\n")
    os.symlink("/ho" + "me/alice/private", root / "docs" / "dirlink")  # a (dangling) directory-style link
    os.symlink(str(root / "docs"), root / f"to-{LAN_IP}")  # a real directory link, address in its name
    os.symlink("a.md", root / "docs" / "filelink.md")
    audit = public_audit.Audit(public_audit.Config(deny=[], deny_files=[], credentials=""))
    audit.tree(str(root))
    hits = sorted(audit.hits)
    assert ("docs/dirlink:0", "symbolic link") in hits
    assert ("docs/dirlink (link target):1", "/home/<name> path") in hits
    assert ("docs/filelink.md:0", "symbolic link") in hits
    assert ("to-***:0", "symbolic link") in hits and ("to-***:0", "private IPv4 address in the file name") in hits
    assert all(LAN_IP not in w for w, _ in hits)


@pytest.mark.parametrize("line, rule", [
    ("<img src=\"data:" + "image/png;base64,iVBORw0KGgo=\">", "embedded data URI"),
    ("url(data:" + "font/woff2;charset=utf-8;base64,d09GMgAB)", "embedded data URI"),
    ("dev box １９２．" + "１６８．１．５", "private IPv4 address"),  # full-width digits (NFKC)
    ("http://h/?ip=192%2E" + "168%2E1%2E5", "private IPv4 address"),  # percent-encoded
])
def test_encoded_content_in_text(tmp_path, line, rule):
    hits = run(tmp_path, {"docs/x.md": line + "\n"})
    assert ("docs/x.md:1", rule) in hits


def test_placeholder_list_needs_regenerated_files():
    """Review 2 L2: make_placeholder_thumbs.py lists only files that regenerate byte for byte (needs Pillow)."""
    pytest.importorskip("PIL")
    import make_placeholder_thumbs as m
    for rel in public_audit.Config(deny=[], deny_files=[], credentials="").placeholders:
        assert m.regenerated(os.path.join(ROOT, rel)), rel
