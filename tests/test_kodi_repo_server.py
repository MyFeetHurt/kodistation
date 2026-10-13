"""tools/dev/kodi_repo_server.py: static file server with HTTP Range support (backlog 010).

Kodi opens zips over HTTP with Range requests ("Install from zip file" browses into the zip). Python's plain
http.server ignores Range, and Kodi 21.3 failed ("Requested range was not delivered") and then crashed on the dev VM.
"""
import http.client
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "dev"))
import kodi_repo_server  # noqa: E402

DATA = bytes(range(256)) * 4  # 1024 bytes


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("repo")
    (tmp_path / "a.zip").write_bytes(DATA)
    (tmp_path / "sub").mkdir()
    (tmp_path / "index.html").write_text('<a href="a.zip">a.zip</a>\n')
    (tmp_path / ".lock").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    listing = tmp_path / "listing"
    listing.mkdir()
    (listing / "a.txt").write_text("a")
    (listing / ".hidden").write_text("h")
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("outside")
    (tmp_path / "escape.txt").symlink_to(outside / "secret.txt")
    (tmp_path / "escdir").symlink_to(outside, target_is_directory=True)
    (listing / "esc.txt").symlink_to(outside / "secret.txt")
    srv = kodi_repo_server.make_server("127.0.0.1", 0, str(tmp_path), quiet=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_full_file_advertises_ranges(server):
    r, body = get(server, "/a.zip")
    assert r.status == 200 and body == DATA
    assert r.getheader("Accept-Ranges") == "bytes"
    assert r.getheader("Content-Length") == "1024"


@pytest.mark.parametrize("rng, start, end", [
    ("bytes=0-9", 0, 9),
    ("bytes=1000-", 1000, 1023),
    ("bytes=-22", 1002, 1023),
    ("bytes=1000-5000", 1000, 1023),
    ("bytes=5-5", 5, 5),
])
def test_range(server, rng, start, end):
    r, body = get(server, "/a.zip", headers={"Range": rng})
    assert r.status == 206
    assert body == DATA[start:end + 1]
    assert r.getheader("Content-Range") == f"bytes {start}-{end}/1024"
    assert r.getheader("Content-Length") == str(end - start + 1)


def test_head_range(server):
    r, body = get(server, "/a.zip", "HEAD", {"Range": "bytes=0-99"})
    assert r.status == 206 and body == b""
    assert r.getheader("Content-Length") == "100"


@pytest.mark.parametrize("rng", ["bytes=1024-", "bytes=2000-3000", "bytes=-0"])
def test_unsatisfiable_range(server, rng):
    r, _ = get(server, "/a.zip", headers={"Range": rng})
    assert r.status == 416
    assert r.getheader("Content-Range") == "bytes */1024"


@pytest.mark.parametrize("rng", ["bytes=0-1,5-6", "items=0-1", "bytes=9-2", "bytes=-"])
def test_unsupported_range_serves_the_whole_file(server, rng):
    r, body = get(server, "/a.zip", headers={"Range": rng})
    assert r.status == 200 and body == DATA


def test_directory_serves_index_html(server):
    r, body = get(server, "/")
    assert r.status == 200 and b'href="a.zip"' in body
    r, _ = get(server, "/a.zip", headers={"Range": "bytes=0-0"})
    assert r.status == 206


def test_missing_and_traversal(server):
    assert get(server, "/nope.zip")[0].status == 404
    assert get(server, "/../../etc/passwd")[0].status == 404


def test_main_parses_arguments(monkeypatch, tmp_path):
    seen = {}

    class Fake:
        def serve_forever(self):
            seen["served"] = True

    monkeypatch.setattr(kodi_repo_server, "make_server",
                        lambda bind, port, directory, quiet=False: seen.update(b=bind, p=port, d=directory) or Fake())
    kodi_repo_server.main(["--port", "8091", "--bind", "0.0.0.0", "--directory", str(tmp_path)])
    assert seen == {"b": "0.0.0.0", "p": 8091, "d": str(tmp_path), "served": True}


def test_listing_hides_dotfiles_and_escaping_links(server):
    r, body = get(server, "/listing/")
    assert r.status == 200
    assert b"a.txt" in body
    assert b".hidden" not in body and b"esc.txt" not in body


@pytest.mark.parametrize("path", ["/.lock", "/.git/config", "/listing/.hidden", "/%2Elock", "/escape.txt",
                                  "/escdir/secret.txt", "/escdir/", "/listing/esc.txt"])
def test_dotfiles_and_paths_outside_the_root_are_refused(server, path):
    assert get(server, path)[0].status == 404
    assert get(server, path, headers={"Range": "bytes=0-1"})[0].status == 404
    assert get(server, path, "HEAD")[0].status == 404


def test_shell_tools_load_local_env_with_environment_overrides(tmp_path):
    """Codex: deploy.sh run directly reads local.env; the environment still wins."""
    import subprocess
    env_file = tmp_path / "local.env"
    env_file.write_text("# c\nKODI_HOST=kodi-box.test  \r\n  export KODI_SSH_USER = kodi\nBAD-KEY=x\nEMPTY=\n")
    lib = os.path.join(os.path.dirname(os.path.dirname(kodi_repo_server.__file__)), "local_env.sh")
    script = f'set -euo pipefail; . "{lib}"; echo "$KODI_HOST|$KODI_SSH_USER|${{EMPTY-unset}}"'
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LOCAL_ENV": str(env_file)}
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "kodi-box.test|kodi|"
    out = subprocess.run(["bash", "-c", script], env=dict(env, KODI_HOST="other.test"), capture_output=True,
                         text=True, check=True).stdout
    assert out.strip() == "other.test|kodi|"
    # review 2 L4: the Python reader gives the same values for the same file
    import local_env
    assert {k: v for k, v in local_env.read(str(env_file)).items() if k in ("KODI_HOST", "KODI_SSH_USER")} == \
        {"KODI_HOST": "kodi-box.test", "KODI_SSH_USER": "kodi"}
    for name in ("deploy.sh",):
        text = open(os.path.join(os.path.dirname(lib), name)).read()
        assert "local_env.sh" in text and text.index("local_env.sh") < text.index("HOST=${1:-")
