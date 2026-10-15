#!/usr/bin/env python3
"""Film the README demo: drive a Kodi over JSON-RPC, capture its screen on the Kodi machine with ffmpeg's kmsgrab,
and log when each step happened (edit.py cuts the clips by those step names). Stdlib only.

    tools/demo/shoot.py --kodi HOST --ssh USER@HOST --out DIR CLIP [--free-only URL] [--forbid TITLE ...]

The Kodi must run on GBM (kmsgrab reads its plane; the plane must be linear XR24) and have JSON-RPC on :8080 without
authentication and the EventServer on :9777. The clip writes CLIP.mkv (lossless) and its step log CLIP.jsonl (what
edit.py reads) into --out.
--forbid: label substrings that must never be on camera (paid channels); the rows a listing can show (VISIBLE_ROWS,
the clips never scroll further) are checked before it is filmed, and the clip is aborted when one shows up.
"""
import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from snapshot_api import is_free_channel  # noqa: E402  (the one free-channel rule of the fixtures)

PLUGIN = "plugin://plugin.video.epgstation/"
REMOTE_DIR = "demo-clips"  # on the Kodi machine, under the ssh user's home
VISIBLE_ROWS = 14  # Estuary's list view shows 13 rows below ".."


class AbortError(Exception):
    pass


class Kodi:
    def __init__(self, host, forbid=()):
        self.host = host
        self.forbid = list(forbid)

    def rpc(self, method, **params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(f"http://{self.host}:8080/jsonrpc", body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            answer = json.load(resp)
        if "error" in answer:
            raise AbortError(f"{method}: {answer['error']}")
        return answer["result"]

    def builtin(self, command):
        """Run a Kodi builtin through the EventServer (JSON-RPC has no call for it)."""
        def packet(ptype, payload):
            return b"XBMC" + bytes([2, 0]) + struct.pack("!HIIHI", ptype, 1, 1, len(payload), 1) + b"\0" * 10 + payload
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(packet(0x01, b"demo\0\0" + b"\0" * 10), (self.host, 9777))  # HELO
            time.sleep(0.2)
            s.sendto(packet(0x0A, b"\x01" + command.encode() + b"\0"), (self.host, 9777))  # ACTION execbuiltin
            time.sleep(0.2)
            s.sendto(packet(0x02, b""), (self.host, 9777))  # BYE

    def labels(self, *names):
        return self.rpc("XBMC.GetInfoLabels", labels=list(names))

    def true(self, condition):
        return bool(self.rpc("XBMC.GetInfoBooleans", booleans=[condition]).get(condition))

    def label(self):
        return self.labels("ListItem.Label")["ListItem.Label"]

    def folder(self):
        return self.labels("Container.FolderPath")["Container.FolderPath"]

    def key(self, action, times=1, pause=0.35):
        for _ in range(times):
            self.rpc(f"Input.{action}")
            time.sleep(pause)

    def check_listing(self):
        """Abort when the rows of the current plugin folder that can be on screen list a forbidden title."""
        path = self.folder()
        if not self.forbid or not path.startswith(PLUGIN):
            return
        files = (self.rpc("Files.GetDirectory", directory=path, media="video").get("files") or [])[:VISIBLE_ROWS]
        bad = [f["label"] for f in files if any(t in f["label"] for t in self.forbid)]
        if bad:
            raise AbortError(f"forbidden item in {path}: {bad[0]}")

    def wait_folder(self, pattern, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if re.search(pattern, self.folder()) and not self.true("Window.IsActive(busydialog)"):
                time.sleep(0.6)  # let the list settle on screen
                return
            time.sleep(0.2)
        raise AbortError(f"folder {pattern!r} not reached (at {self.folder()})")

    def control(self):
        return self.labels("System.CurrentControl")["System.CurrentControl"]

    def focus(self, pattern, limit=60, pause=0.35, menu=False):
        """Move down to the first item whose label matches (from the current one). menu: in a context menu or select
        dialog, where the focused entry is the current control (ListItem.Label still names the list item below)."""
        for _ in range(limit):
            if re.search(pattern, self.control() if menu else self.label()):
                return
            self.key("Down", pause=pause)
        raise AbortError(f"no item matching {pattern!r} in {self.folder()}")

    def open(self, pattern, folder_pattern=None, pause=0.35):
        """Focus the item and select it; with folder_pattern wait for that folder (and check it)."""
        self.focus(pattern, pause=pause)
        time.sleep(0.5)
        self.key("Select")
        if folder_pattern:
            self.wait_folder(folder_pattern)
            self.check_listing()

    def wait_window(self, name, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.true(f"Window.IsActive({name})"):
                time.sleep(0.4)
                return
            time.sleep(0.2)
        raise AbortError(f"window {name} did not open")

    def wait_playing(self, seconds=1, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                t = self.rpc("Player.GetProperties", playerid=1, properties=["time"])["time"]
                if t["hours"] * 3600 + t["minutes"] * 60 + t["seconds"] >= seconds:
                    return
            except AbortError:
                pass
            time.sleep(0.3)
        raise AbortError("playback did not start")

    def stop(self):
        for p in self.rpc("Player.GetActivePlayers"):
            self.rpc("Player.Stop", playerid=p["playerid"])
        time.sleep(1.5)


class Capture:
    """kmsgrab on the Kodi machine (lossless x264), started and stopped over ssh."""

    def __init__(self, ssh, clip):
        self.ssh, self.clip = ssh, clip

    def run(self, cmd, check=True):
        return subprocess.run(["ssh", "-o", "LogLevel=ERROR", self.ssh, cmd], check=check, capture_output=True,
                              text=True, timeout=120).stdout

    def start(self):
        out = f"{REMOTE_DIR}/{self.clip}.mkv"
        self.run(f"mkdir -p {REMOTE_DIR}; rm -f {out}; nohup sudo ffmpeg -hide_banner -loglevel error -nostdin -y "
                 "-device /dev/dri/card0 -f kmsgrab -framerate 30 -i - -vf hwdownload,format=bgr0 -c:v libx264 "
                 f"-preset ultrafast -qp 0 -t 600 {out} > {REMOTE_DIR}/{self.clip}.log 2>&1 & echo $! > "
                 f"{REMOTE_DIR}/{self.clip}.pid")
        time.sleep(1.0)  # ffmpeg is grabbing from here on; steps are logged relative to this moment
        return time.monotonic()

    def stop(self, local_dir):
        pid = f"{REMOTE_DIR}/{self.clip}.pid"  # sudo's pid: it passes SIGINT on, ffmpeg then finishes the file
        self.run(f"sudo kill -INT $(cat {pid}); while sudo kill -0 $(cat {pid}) 2>/dev/null; do sleep 0.2; done",
                 check=False)
        subprocess.run(["scp", "-q", "-o", "LogLevel=ERROR", f"{self.ssh}:{REMOTE_DIR}/{self.clip}.mkv",
                        os.path.join(local_dir, f"{self.clip}.mkv")], check=True)


# --- the clips -----------------------------------------------------------------------------------------------------
# Each clip is a function of (kodi, mark): mark(name) logs a step for the edit. Labels are the Japanese UI's.

ROOT_RE = r"action=root|epgstation/$"


def go_root(k):
    """The addon's root menu, focus on "..". The video window reopens at the folder it showed last: open it, then
    Back up to the root."""
    if not k.true("Window.IsActive(videos)"):
        k.rpc("GUI.ActivateWindow", window="videos", parameters=[PLUGIN, "return"])
        k.wait_window("videos")
        k.wait_folder(r"^plugin://plugin\.video\.epgstation/")
    for _ in range(8):
        if k.folder() == PLUGIN:
            break
        k.key("Back", pause=0.8)
    k.wait_folder(r"epgstation/$")
    while k.label() != "..":
        k.key("Up", pause=0.1)


def clip_firstrun(k, mark, server):
    """A fresh install: the setup prompt, the server address, the menu. The server setting must be empty."""
    k.key("Home")
    time.sleep(2)
    mark("home")
    k.rpc("GUI.ActivateWindow", window="videos", parameters=[PLUGIN, "return"])
    k.wait_window("yesnodialog")
    mark("prompt")
    time.sleep(2.5)
    k.key("Select")  # はい (focused)
    k.wait_window("addonsettings")
    mark("settings")
    time.sleep(1.5)
    k.key("Select")  # EPGStation URL
    k.wait_window("virtualkeyboard")
    time.sleep(1)
    for i in range(1, len(server) + 1):  # typed, so it reads as typing
        k.rpc("Input.SendText", text=server[:i], done=False)
        time.sleep(0.08)
    time.sleep(0.8)
    k.rpc("Input.SendText", text=server, done=True)
    mark("typed")
    time.sleep(1.5)
    k.key("Right")
    time.sleep(0.6)
    k.key("Select")  # OK
    k.wait_folder(r"epgstation/$")
    mark("menu")
    time.sleep(3)


def clip_library(k, mark, episode, seek=25):
    go_root(k)
    mark("root")
    time.sleep(1.5)
    k.open(r"^TV番組$", r"action=tvshows")
    mark("tvshows")
    time.sleep(1.2)
    k.focus(r"^豊臣兄弟", pause=0.6)
    time.sleep(1.5)
    k.key("Select")
    k.wait_folder(r"action=show")
    k.check_listing()
    mark("episodes")
    while k.label() != "..":  # Kodi reopens a folder at the item it showed last
        k.key("Up", pause=0.1)
    time.sleep(1)
    k.focus(episode, pause=0.7)
    time.sleep(1)
    k.key("Info")
    k.wait_window("movieinformation")
    mark("info")
    time.sleep(5)  # cast, plot, credits, the stream flags
    k.key("Back")
    time.sleep(1)
    mark("play")
    k.key("Select")
    k.wait_playing(2)
    k.rpc("Player.Seek", playerid=1, value={"percentage": seek})  # past the lead-in (recordings start early)
    time.sleep(2.5)
    mark("playing")
    time.sleep(3)
    k.key("Info")  # the episode's OSD info
    time.sleep(3.5)
    k.stop()
    mark("browse")
    go_root(k)
    k.open(r"^分類から探す$", r"action=browse")
    time.sleep(1)
    k.open(r"^ジャンル別$", r"action=genres")
    mark("genres")
    time.sleep(1.5)
    k.open(r"^ドラマ", r"action=genre&genre=3")
    time.sleep(1.2)
    k.open(r"^国内ドラマ", r"subGenre=0")
    mark("subgenre")
    k.key("Down", times=4, pause=0.6)
    time.sleep(2)
    go_root(k)
    k.focus(r"^検索$")
    mark("search")
    k.key("Select")
    k.wait_window("virtualkeyboard")
    time.sleep(0.8)
    word = "ブラッサム"
    for i in range(1, len(word) + 1):
        k.rpc("Input.SendText", text=word[:i], done=False)
        time.sleep(0.25)
    time.sleep(0.8)
    k.rpc("Input.SendText", text=word, done=True)
    k.wait_folder(r"action=search&keyword=")
    k.check_listing()
    mark("results")
    k.key("Down", times=2, pause=0.6)
    time.sleep(2.5)


def clip_live(k, mark):
    go_root(k)
    mark("root")
    k.open(r"^ライブTV$", r"action=live$")
    mark("live")
    time.sleep(1.5)
    k.open(r"^地上波$", r"type=GR")
    mark("channels")
    k.key("Down", times=1, pause=0.6)
    k.focus(r"NHK総合")
    time.sleep(2.5)
    k.key("Down", times=3, pause=0.8)  # now / next of a few channels
    time.sleep(1)
    k.key("Up", times=3, pause=0.4)
    mark("tune")
    k.key("Select")
    k.wait_playing(1, timeout=90)
    mark("playing")
    time.sleep(4)
    k.key("Info")
    time.sleep(3)
    k.stop()
    mark("end")


def clip_record(k, mark, channel):
    """Record a program from a channel's schedule (番組表): the context entry, the confirmation, the ○ marker, the
    reservation list."""
    go_root(k)
    k.open(r"^ライブTV$", r"action=live$")
    k.open(r"^地上波$", r"type=GR")
    mark("channels")
    k.focus(channel)
    time.sleep(1.2)
    k.key("ContextMenu")
    k.wait_window("contextmenu")
    k.focus(r"^番組表$", menu=True)
    time.sleep(0.8)
    k.key("Select")
    k.wait_folder(r"action=guide_channel")
    k.check_listing()
    mark("days")
    time.sleep(1.5)
    k.open(r"^今日", r"action=guide_channel.*day=")
    mark("schedule")
    time.sleep(1)
    # "HH:MM  name" of a program not on air (▶) and not reserved (○ ● [競合] ...), an hour or more ahead
    for _ in range(60):
        lab = k.label()
        m = re.match(r"^(\d\d):(\d\d) ", lab)
        if m and not re.search(r"[○●▶]|\[COLOR", lab) and int(m.group(1)) * 60 + int(m.group(2)) >= \
                (time.localtime().tm_hour + 1) * 60:
            break
        k.key("Down", pause=0.3)
    else:
        raise AbortError("no unreserved future program in today's schedule")
    time.sleep(1.5)
    mark("context")
    k.key("ContextMenu")
    k.wait_window("contextmenu")
    time.sleep(1)
    k.focus(r"^録画$", menu=True)
    time.sleep(0.8)
    k.key("Select")
    k.wait_window("yesnodialog")
    mark("confirm")
    time.sleep(3)
    k.key("Left")  # 録画 (Cancel is focused)
    time.sleep(0.5)
    k.key("Select")
    mark("recorded")
    time.sleep(4)  # the toast, then the refreshed list with ○
    go_root(k)
    k.open(r"^録画予約$", r"action=reserves$")
    mark("reserves")
    time.sleep(3)


def clip_stream(k, mark, recording, start_at="00:05"):
    go_root(k)
    k.open(r"^最近の録画$", r"action=list")
    mark("recent")
    time.sleep(1.5)
    k.focus(recording, pause=0.6)
    time.sleep(1.5)
    k.key("ContextMenu")
    k.wait_window("contextmenu")
    time.sleep(1)
    k.focus(r"^変換して再生$", menu=True)
    mark("context")
    time.sleep(0.8)
    k.key("Select")
    k.wait_window("selectdialog")  # start: 最初から / 位置を指定… (a resume point too, when there is one)
    mark("start_choice")  # not "start": that word opens a take in the log
    time.sleep(1.5)
    k.focus(r"^位置を指定", menu=True, pause=0.6)  # recordings start early: begin a few minutes in
    time.sleep(0.8)
    k.key("Select")
    k.wait_window("numericinput")
    time.sleep(1)
    k.rpc("Input.SendText", text=start_at, done=False)
    time.sleep(1.5)
    k.rpc("Input.SendText", text=start_at, done=True)
    mark("starting")
    k.wait_playing(2, timeout=60)
    mark("playing")
    time.sleep(5)
    k.key("Info")
    time.sleep(3)
    k.stop()
    mark("end")


def paid_channels(base, channels=None):
    """Names (half-width, as the addon shows them) of the channels that are not free (snapshot_api's rule), less the
    ones a free channel also carries; channels: /api/channels (default: fetched from base)."""
    if channels is None:
        with urllib.request.urlopen(base.rstrip("/") + "/api/channels", timeout=20) as resp:
            channels = json.load(resp)
    half = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}

    def name(c):
        return (c.get("halfWidthName") or c["name"]).translate(half).replace("\u3000", " ")

    free_names = {name(c) for c in channels if is_free_channel(c)}  # a data service may carry a free channel's name
    paid = {name(c).strip() for c in channels if not is_free_channel(c)} - free_names
    return sorted(n for n in paid if len(n) >= 3)  # blank or very short names would match any label


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", choices=["firstrun", "library", "live", "record", "stream"])
    ap.add_argument("--kodi", required=True)
    ap.add_argument("--ssh", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--forbid", action="append", default=[])
    ap.add_argument("--free-only", metavar="EPGSTATION_URL",
                    help="also forbid every channel of this EPGStation that is not terrestrial or free BS")
    ap.add_argument("--server", help="firstrun: the address typed into the settings")
    ap.add_argument("--channel", default=r"NHK総合", help="record: the channel whose schedule to record from")
    ap.add_argument("--episode", default=r"^1x36 ", help="library: the episode label to open and play")
    ap.add_argument("--recording", default=r"^映像の世紀", help="stream: the recording to play transcoded")
    ap.add_argument("--name", help="clip file name (default: the clip)")
    a = ap.parse_args(argv)
    os.makedirs(os.path.expanduser(a.out), exist_ok=True)
    out = os.path.expanduser(a.out)
    name = a.name or a.clip
    k = Kodi(a.kodi, a.forbid + (paid_channels(a.free_only) if a.free_only else []))
    cap = Capture(a.ssh, name)
    k.stop()
    t0 = cap.start()
    steps = []

    def mark(step):
        steps.append({"t": round(time.monotonic() - t0, 2), "step": step})
        print(f"{steps[-1]['t']:7.2f} {step}", flush=True)

    mark("start")
    ok = True
    try:
        if a.clip == "firstrun":
            clip_firstrun(k, mark, a.server)
        elif a.clip == "library":
            clip_library(k, mark, a.episode)
        elif a.clip == "live":
            clip_live(k, mark)
        elif a.clip == "record":
            clip_record(k, mark, a.channel)
        else:
            clip_stream(k, mark, a.recording)
        mark("stop")
    except AbortError as e:
        ok = False
        print(f"ABORTED: {e}", file=sys.stderr)
        k.stop()
        k.key("Home")
    finally:
        cap.stop(out)
        with open(os.path.join(out, f"{name}.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(s, ensure_ascii=False) + "\n" for s in steps)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
