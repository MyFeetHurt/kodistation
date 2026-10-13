#!/usr/bin/env python3
"""Demo video (backlog 021): drive the dev Kodi over JSON-RPC (and EventServer for builtins), by label, with a
free-channel guard after every navigation. stdlib only. No addresses in here: the Kodi host is KODI_HOST and the dev
EPGStation EPGS_DEV_URL (default http://$KODI_HOST:8888), both from local.env (tools/local_env.py, backlog 019) or
the environment; an unset or localhost KODI_HOST is refused. EPGStation is only ever sent GETs.

Every step appends one JSON line to the tour log: {"t": seconds since the clip start, "step", "label", "folder"}.
A guard hit or a label that cannot be focused aborts the take: Player.Stop, Back to the root, exit code 2.
"""
import importlib.util
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_env  # noqa: E402  (019: hosts from local.env)

ADDON = "plugin.video.epgstation"
ROOT_URL = f"plugin://{ADDON}/"
# folders that list paid channels (planner findings): never opened on camera
FORBIDDEN_PATHS = ("guide_now", "guide_genres", "action=reserves", "type=BS", "type=CS", "type=SKY", "search")
EVENT_PORT = 9777
# EventServer (xbmc/eventserver, xbmcclient.h): 32-byte header, then the payload
PT_HELO, PT_BYE, PT_ACTION = 0x01, 0x02, 0x0A
ACTION_EXECBUILTIN = 0x01
ICON_NONE = 0x00


class TakeError(Exception):
    """The take is not usable (guard hit, label not found): stop and go back to the root."""


def dev_tool():
    """tools/dev/test_reserve.py as a module: its check_base / _same_server / state are the one dev-only rule."""
    mod = sys.modules.get("test_reserve_tool")
    if mod is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dev", "test_reserve.py")
        spec = importlib.util.spec_from_file_location("test_reserve_tool", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["test_reserve_tool"] = mod
        spec.loader.exec_module(mod)
    return mod


def is_loopback(host, resolve=None):
    """True for localhost names and any loopback address, literal (127.0.0.0/8, ::1) or resolved by DNS."""
    host = (host or "").strip().lower().strip("[]")
    if host == "localhost" or host.startswith("localhost.") or host.endswith(".localhost"):
        return True
    addrs = {host}
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addrs = (resolve or dev_tool()._addresses)(host)
    for a in addrs:
        try:
            if ipaddress.ip_address(a.split("%", 1)[0]).is_loopback:
                return True
        except ValueError:
            continue
    return False


def env_host():
    """KODI_HOST from local.env / the environment (lowercased); never this machine: unset, localhost (the Makefile's
    default without local.env) or any loopback address is refused."""
    local_env.load()
    host = local_env.get("KODI_HOST").strip().lower()
    if not host or is_loopback(host):
        sys.exit("refused: set KODI_HOST to the dev Kodi (not this machine) in ~/.config/epgstation-kodi/local.env")
    return host


def dev_epgs_url(host):
    return local_env.get("EPGS_DEV_URL").strip().rstrip("/") or f"http://{host}:8888"


def check_hosts(host):
    """test_reserve's rules (019): EPGS_PROD_URL set, KODI_HOST not the production server (name or address), the dev
    EPGStation passes check_base and is the same server as KODI_HOST. Returns the dev EPGStation URL."""
    tool = dev_tool()
    local_env.load()
    prod = local_env.get("EPGS_PROD_URL").strip()
    if not prod:
        sys.exit("refused: EPGS_PROD_URL must be set (~/.config/epgstation-kodi/local.env)")
    if tool._same_server(f"http://{host}", prod):
        sys.exit(f"refused: KODI_HOST {host} is the production server")
    dev = tool.check_base(dev_epgs_url(host))
    if not tool._same_server(dev, f"http://{host}"):
        sys.exit(f"refused: the dev EPGStation {dev} is not on KODI_HOST {host}")
    return dev


def norm(text):
    """Half-width, case-folded (NFKC): full-width ＢＳ１０ matches BS10."""
    return unicodedata.normalize("NFKC", text or "").casefold()


# --- free-channel guard ------------------------------------------------------------------------------------------

# while the delete prompt is open (Kodi._no_select): any input that could confirm it
CONFIRM_ACTIONS = {"select", "accept", "enter", "click", "doubleclick", "leftclick", "play", "playpause"}
CONFIRM_BUILTIN = re.compile(r"(?i)\b(action\(\s*(select|accept|enter|click|play)|sendclick|setfocus)")


FREE_BS_SERVICES = frozenset([101, 102, 141, 142, 143, 151, 152, 153, 161, 162, 163, 171, 172, 173, 181, 182, 183,
                              200, 211, 222, 231, 232, 260, 265])  # = tools/snapshot_api.FREE_BS_SERVICES (tested)


def is_free_channel(channel):
    kind = channel.get("channelType")
    return kind == "GR" or (kind == "BS" and channel.get("serviceId") in FREE_BS_SERVICES)


class Guard:
    """Built from the dev GET /api/channels: free channel ids and the (normalised) names of every other channel."""

    def __init__(self, channels):
        self.free = {c["id"] for c in channels if is_free_channel(c)}
        free_names = [norm(c.get("name")) for c in channels if is_free_channel(c)]
        names = {norm(c.get("name")) for c in channels if not is_free_channel(c) and c.get("name")}
        # a paid name inside a free one (none today) would trip on every free label: left out, and said so
        self.shadowed = sorted(n for n in names if any(n in f for f in free_names))
        self.paid_names = sorted(n for n in names if n not in self.shadowed)

    def folder_failures(self, folder):
        raw = folder or ""
        both = (raw + " " + urllib.parse.unquote(raw)).casefold()
        return [f"folder {raw} contains {bad}" for bad in FORBIDDEN_PATHS if bad.casefold() in both]

    def item_failures(self, items):
        out = []
        for it in items:
            url = it.get("file") or ""
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            for cid in query.get("channelId", []):
                if not cid.isdigit() or int(cid) not in self.free:
                    out.append(f"item {it.get('label')!r}: channelId {cid} is not a free channel")
            label = norm(it.get("label"))
            for name in self.paid_names:
                if name in label:
                    out.append(f"item {it.get('label')!r}: names the non-free channel {name}")
        return out

    def records_failures(self, what, records):
        return [f"{what} {r.get('id')} ({r.get('name')}) is on channel {r.get('channelId')} (not free)"
                for r in records if r.get("channelId") not in self.free]


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class DevEpgs:
    """GET-only view of the dev EPGStation (the guard's data)."""

    def __init__(self, base, get=http_get_json):
        self.base, self.get = base.rstrip("/"), get

    def api(self, path, **params):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self.get(f"{self.base}/api{path}{query}")

    def channels(self):
        return self.api("/channels")

    def preflight_failures(self, guard):
        """(c): every recording, recording-in-progress and reservation is on a free channel; no live stream."""
        rec = self.api("/recorded", isHalfWidth="true", offset=0, limit=10000)["records"]
        now = self.api("/recording", isHalfWidth="true", offset=0, limit=100)["records"]
        res = self.api("/reserves", type="all", isHalfWidth="true", offset=0, limit=5000)["reserves"]
        out = guard.records_failures("recording", rec) + guard.records_failures("in progress", now)
        out += guard.records_failures("reservation", res)
        streams = self.streams()
        if streams:
            out.append(f"{len(streams)} live stream(s) running")
        return out

    def streams(self):
        got = self.api("/streams", isHalfWidth="true")
        return got.get("items", got) if isinstance(got, dict) else got


# --- EventServer --------------------------------------------------------------------------------------------------

def event_packet(ptype, payload, seq=1, uid=0):
    """One single-packet EventServer datagram (xbmcclient.h: "XBMC", v2.0, type, seq, maxseq, size, uid, 10 x 0)."""
    header = b"XBMC" + struct.pack("!BBHIIHI", 2, 0, ptype, seq, 1, len(payload), uid) + b"\0" * 10
    return header + payload


def helo_payload(name="KODIstation demo"):
    return name.encode() + b"\0" + struct.pack("!BHII", ICON_NONE, 0, 0, 0)


def builtin_payload(builtin):
    return struct.pack("!B", ACTION_EXECBUILTIN) + builtin.encode() + b"\0"


class EventClient:
    """One EventServer client for a whole take: Kodi toasts "Detected new connection" on every HELO, so the HELO is
    sent off camera (helo) and every builtin comes from the same socket (same client); bye at the end."""

    def __init__(self, host, port=EVENT_PORT, sock=None):
        self.addr = (host, port)
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.uid = int(time.time()) & 0xFFFFFFFF
        self.seq = 0

    def _send(self, ptype, payload):
        self.seq += 1
        self.sock.sendto(event_packet(ptype, payload, seq=self.seq, uid=self.uid), self.addr)

    def helo(self):
        self._send(PT_HELO, helo_payload())

    def builtin(self, builtin):
        self._send(PT_ACTION, builtin_payload(builtin))

    def bye(self):
        try:
            self._send(PT_BYE, b"")
        finally:
            self.sock.close()


def send_builtin(host, builtin, port=EVENT_PORT, sock_factory=None):
    """HELO, ACTION (EXECBUILTIN), BYE over UDP; Kodi answers nothing."""
    sock = (sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)))()
    uid = int(time.time()) & 0xFFFFFFFF
    try:
        for ptype, payload in ((PT_HELO, helo_payload()), (PT_ACTION, builtin_payload(builtin)), (PT_BYE, b"")):
            sock.sendto(event_packet(ptype, payload, uid=uid), (host, port))
            time.sleep(0.2)
    finally:
        sock.close()


# --- JSON-RPC driver ----------------------------------------------------------------------------------------------

class Kodi:
    def __init__(self, host, log_path=None, guard=None, rpc=None, sleep=time.sleep, clock=time.monotonic):
        self.host = host
        self.url = f"http://{host}:{os.environ.get('KODI_RPC_PORT', '8080')}/jsonrpc"
        self.guard, self.sleep, self.clock = guard, sleep, clock
        self.t0 = clock()
        self.log_path = log_path
        self._rpc = rpc or self._http_rpc
        self.sent = []  # every input action, for the tests
        self.deadline = None  # while live plays (k.clock() time): every RPC waits at most until then (>= 1 s)
        self._timeout = None  # a one-call override (stop_player, abort_cleanup)
        self.event_sender = send_builtin  # the Stop fallback (EventServer), injectable for the tests

    # transport
    RPC_TIMEOUT = 60.0
    STOP_TIMEOUT = 3.0

    def current_timeout(self):
        """Seconds the next RPC may wait: the override, else the time left before the deadline (>= 1 s), else 60."""
        if self._timeout is not None:
            return self._timeout
        if self.deadline is not None:
            return max(1.0, min(self.RPC_TIMEOUT, self.deadline - self.clock()))
        return self.RPC_TIMEOUT

    def time_left(self):
        return float("inf") if self.deadline is None else self.deadline - self.clock()

    def _http_rpc(self, method, params):
        """One request on its own connection (urllib opens one per call), so a hung call never blocks Stop."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.current_timeout()) as resp:
            got = json.loads(resp.read())
        if "error" in got:
            raise RuntimeError(f"{method}: {got['error']}")
        return got.get("result")

    def rpc(self, method, **params):
        if getattr(self, "_no_select", False) and self.confirm_input(method, params):
            raise TakeError(f"{method} {params} is refused while the delete prompt is open")
        return self._rpc(method, params)

    @staticmethod
    def confirm_input(method, params):
        """A JSON-RPC call that could answer a yes/no dialog with yes (select, enter, click, text input)."""
        m = method.lower()
        if m in ("input.select", "input.sendtext", "input.buttonevent"):
            return True
        if m == "input.executeaction":
            return str(params.get("action", "")).lower() in CONFIRM_ACTIONS
        return m.startswith("player.open")

    def builtin(self, events, text):
        """An EventServer builtin through events (an EventClient), with the same delete-prompt rule."""
        if getattr(self, "_no_select", False) and CONFIRM_BUILTIN.search(text):
            raise TakeError(f"builtin {text} is refused while the delete prompt is open")
        events.builtin(text)

    # log
    def reset_clock(self, t0=None):
        self.t0 = self.clock() if t0 is None else t0

    def log(self, step, **extra):
        labels = self.labels("Container.ListItem.Label", "Container.FolderPath", "System.CurrentWindow",
                             "System.CurrentControl")
        line = {"t": round(self.clock() - self.t0, 2), "step": step, "label": labels.get("Container.ListItem.Label"),
                "control": labels.get("System.CurrentControl"), "window": labels.get("System.CurrentWindow"),
                "folder": labels.get("Container.FolderPath")}
        line.update(extra)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(json.dumps(line, ensure_ascii=False))
        return line

    # queries
    def labels(self, *names):
        return self.rpc("XBMC.GetInfoLabels", labels=list(names)) or {}

    def label(self, name):
        return self.labels(name).get(name, "")

    def boolean(self, cond):
        return bool((self.rpc("XBMC.GetInfoBooleans", booleans=[cond]) or {}).get(cond))

    def listdir(self, url):
        got = self.rpc("Files.GetDirectory", directory=url, media="video", properties=["title"])
        return (got or {}).get("files") or []

    # input
    def press(self, action, wait=0.35):
        """action: Up/Down/Left/Right/Select/Back/ContextMenu/Info/Home, or an Input.ExecuteAction name."""
        if getattr(self, "_no_select", False) and action.lower() in CONFIRM_ACTIONS:
            raise TakeError(f"{action} is not allowed while the delete prompt is open")
        self.sent.append(action)
        if action in ("Up", "Down", "Left", "Right", "Select", "Back", "ContextMenu", "Info", "Home"):
            self.rpc(f"Input.{action}")
        else:
            self.rpc("Input.ExecuteAction", action=action)
        self.sleep(wait)

    def focused(self, where="list"):
        if where == "list":
            return self.label("Container.ListItem.Label")
        return self.label("System.CurrentControl")

    def focus_label(self, text, max_steps=40, where="list", direction="Down", exact=False):
        """Press direction until the focused label contains (exact: equals, after norm) text, else TakeError."""
        want = norm(text)
        for step in range(max_steps + 1):
            got = norm(self.focused(where))
            if (got == want) if exact else (want in got):
                return got
            if step < max_steps:
                self.press(direction, wait=0.25)
        raise TakeError(f"label {text!r} not found within {max_steps} steps (focused: {self.focused(where)!r})")

    def wait_for(self, cond=None, label=None, value=None, timeout=20, step=0.3):
        """Until the boolean cond is true, or the info label `label` contains value."""
        end = self.clock() + timeout
        while self.clock() < end:
            if cond and self.boolean(cond):
                return True
            if label and value is not None and norm(value) in norm(self.label(label)):
                return True
            self.sleep(step)
        raise TakeError(f"timeout waiting for {cond or (label, value)}")

    def wait_folder(self, contains, timeout=30, exact=False):
        """Until the window shows a folder whose path contains (exact: is) `contains` and the listing has loaded."""
        end = self.clock() + timeout
        while self.clock() < end:
            path = self.label("Container.FolderPath")
            if (path == contains if exact else contains in path) and not self.boolean("Container.IsUpdating") and \
                    not self.boolean("Window.IsActive(busydialog)") and \
                    not self.boolean("Window.IsActive(busydialognocancel)"):
                return path
            self.sleep(0.3)
        raise TakeError(f"folder with {contains!r} not shown in {timeout} s")

    # guard
    def check(self, step):
        """The guard after a navigation: the folder path, then every item of that folder (Files.GetDirectory)."""
        folder = self.label("Container.FolderPath")
        fails = self.guard.folder_failures(folder) if self.guard else []
        if self.guard and not fails and folder.startswith(ROOT_URL):
            fails = self.guard.item_failures(self.listdir(folder))
        self.log(step, guard="FAIL" if fails else "ok")
        if fails:
            raise TakeError("free-channel guard: " + "; ".join(fails))

    def stop_player(self):
        """Player.Stop with a short timeout on its own connection; on any failure the EventServer builtin
        PlayerControl(Stop) as well (UDP, never waits). True when the JSON-RPC Stop was answered."""
        self.deadline, self._timeout = None, self.STOP_TIMEOUT
        try:
            self._rpc("Player.Stop", {"playerid": 1})
            return True
        except Exception as e:  # noqa: BLE001 - a hung or failed transport: the fallback below
            print(f"Player.Stop failed ({e!r}): EventServer PlayerControl(Stop)", file=sys.stderr)
            try:
                self.event_sender(self.host, "PlayerControl(Stop)")
            except Exception as e2:  # noqa: BLE001
                print(f"EventServer Stop failed: {e2!r}", file=sys.stderr)
            return False
        finally:
            self._timeout = None

    def abort_cleanup(self):
        """After any abort: Stop first (always, short timeouts, EventServer fallback), then Back x8 and Home, each
        step on its own with a short timeout; never raises."""
        self._no_select = False
        self.stop_player()
        self._timeout = 5.0
        try:
            for _ in range(8):
                try:
                    self._rpc("Input.Back", {})
                except Exception as e:  # noqa: BLE001 - best effort after an abort
                    print(f"cleanup Back: {e!r}", file=sys.stderr)
                    break
                self.sleep(0.3)
            try:
                self._rpc("GUI.ActivateWindow", {"window": "home"})
            except Exception as e:  # noqa: BLE001
                print(f"cleanup Home: {e!r}", file=sys.stderr)
        finally:
            self._timeout = None


def build(log_path=None, host=None):
    host = host or env_host()
    epgs = DevEpgs(check_hosts(host))
    guard = Guard(epgs.channels())
    if guard.shadowed:
        print(f"guard: paid names inside free names, not matched in labels: {guard.shadowed}")
    return Kodi(host, log_path, guard), epgs, guard


if __name__ == "__main__":  # quick look: python3 tools/demo/kodi_drive.py LABEL... | --check-host (make demo-*)
    if sys.argv[1:] == ["--check-host"]:  # every make demo-* target, the probe too: same host rules as the tour
        host = env_host()
        check_hosts(host)
        print(host)
        sys.exit(0)
    k = Kodi(env_host())
    print(json.dumps(k.labels(*(sys.argv[1:] or ["Container.FolderPath", "Container.ListItem.Label",
                                                  "System.CurrentWindow", "System.CurrentControl"])),
                     ensure_ascii=False, indent=1))
