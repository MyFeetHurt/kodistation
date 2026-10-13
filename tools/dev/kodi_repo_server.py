#!/usr/bin/env python3
"""Static file server for the Kodi repository, with HTTP Range support (backlog 010). Stdlib only.

  kodi_repo_server.py --port 8090 --bind 0.0.0.0 --directory /srv/kodi-repo

Like `python3 -m http.server` (index.html, directory listings), plus single byte ranges (206 / 416). Dotfiles
(e.g. publish's .lock) are neither listed nor served, and nothing outside the root is served, even via symlinks.
Kodi reads zips over HTTP with Range requests, e.g. "Install from zip file" browses into the zip; with plain
http.server Kodi 21.3 logged "Requested range was not delivered by the server" and crashed.
Run by kodi-repo.service.
"""
import argparse
import functools
import html
import http.server
import io
import os
import re
import shutil
import sys
import urllib.parse

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    quiet = False

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def _parse_range(self, size):
        """(start, end) inclusive, 'unsatisfiable', or None to serve the whole file."""
        m = RANGE_RE.match(self.headers.get("Range", "").strip())
        if not m or (not m.group(1) and not m.group(2)):
            return None
        first, last = m.group(1), m.group(2)
        if not first:  # suffix: the last N bytes
            n = int(last)
            return "unsatisfiable" if n == 0 or size == 0 else (max(size - n, 0), size - 1)
        start = int(first)
        end = int(last) if last else size - 1
        if last and end < start:
            return None
        if start >= size:
            return "unsatisfiable"
        return start, min(end, size - 1)

    def _inside_root(self, path):
        root = os.path.realpath(self.directory)
        return os.path.commonpath([root, os.path.realpath(path)]) == root

    def _hidden(self):
        url_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        return any(part.startswith(".") for part in url_path.split("/") if part)

    def list_directory(self, path):
        """A plain listing without dotfiles or entries that point outside the root."""
        try:
            names = sorted(n for n in os.listdir(path) if not n.startswith("."))
        except OSError:
            self.send_error(404, "No permission to list directory")
            return None
        links = []
        for name in names:
            full = os.path.join(path, name)
            if not self._inside_root(full):
                continue
            shown = name + "/" if os.path.isdir(full) else name
            links.append(f'<a href="{urllib.parse.quote(shown)}">{html.escape(shown)}</a><br>')
        body = ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"></head><body>\n"
                + "\n".join(links) + "\n</body></html>\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def send_head(self):
        self._range = None
        path = self.translate_path(self.path)
        if self._hidden() or not self._inside_root(path):
            self.send_error(404, "File not found")
            return None
        if "Range" not in self.headers or not os.path.isfile(path) or self.path.endswith("/"):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            size = os.fstat(f.fileno()).st_size
            rng = self._parse_range(size)
            if rng is None:
                f.close()
                return super().send_head()
            if rng == "unsatisfiable":
                f.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            start, end = rng
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Last-Modified", self.date_time_string(os.fstat(f.fileno()).st_mtime))
            self.end_headers()
            f.seek(start)
            self._range = end - start + 1
            return f
        except BaseException:
            f.close()
            raise

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_range", None)
        if remaining is None:
            return shutil.copyfileobj(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)


def make_server(bind, port, directory, quiet=False):
    handler = functools.partial(type("Handler", (RangeHandler,), {"quiet": quiet}), directory=directory)
    return http.server.ThreadingHTTPServer((bind, port), handler)


def main(argv=None):
    p = argparse.ArgumentParser(description="Static file server with HTTP Range support")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--directory", default=".")
    args = p.parse_args(argv)
    srv = make_server(args.bind, args.port, args.directory)
    print(f"serving {args.directory} on {args.bind}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
