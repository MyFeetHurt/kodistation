#!/usr/bin/env python3
"""DEV-ONLY test reservations for backlogs 007 (manage reservations) and 014 (record from the guide) and their testers.

Refuses every EPGStation but the dev one ($EPGS_DEV_URL from local.env, backlog 019; on the VM itself, i.e. on
$KODI_HOST, also http://localhost:8888), always refuses production ($EPGS_PROD_URL, also by host), and refuses
everything while EPGS_DEV_URL is unset. Prints every request it sends. It only ever changes what it created
itself: the ids of its reservations are kept in ~/.cache/kodistation-test-reserves.json.

    tools/dev/test_reserve.py [--base URL] create --program
        a free GR TV program starting >= 60 min from now and <= 10 min long, at a time with <= MAX_OTHERS other
        non-skip reservations; POST /reserves {programId, allowEndLack: true}; a conflict is deleted at once
    tools/dev/test_reserve.py create --time --start-in 4 --minutes 3
        a time-specified manual reservation on NHK総合1 (free GR), same concurrency check (stop-recording test)
    tools/dev/test_reserve.py delete-reserve ID      (only an id this tool created, and only while the server's
                                                      reservation still is that manual one: same channel, start, end)
    tools/dev/test_reserve.py delete-recorded ID     (only an unprotected, finished recording of a reservation this
                                                      tool created: same channel, start, end and programId, no rule;
                                                      a rule recording at the same minute is refused)
    tools/dev/test_reserve.py list                   (this tool's reservations; drops ids that are gone and left no
                                                      recording)

Backlog 014 (reservations made through Kodi's 録画 entry; these three commands only send GET):
    tools/dev/test_reserve.py pick [--airing]        print a free GR TV program of <= 10 min starting >= 60 min from
                                                      now (--airing: on air now with >= 3 min left) at a time with
                                                      <= MAX_OTHERS other non-skip reservations; nothing is stored
    tools/dev/test_reserve.py snapshot               store the ids of every reservation now (before the Kodi test)
    tools/dev/test_reserve.py adopt ID --program P   take reservation ID (made through Kodi after the snapshot) into
                                                      this tool's list: snapshot <= 2 h old, ID above every snapshot
                                                      id, no ruleId, programId P, a free GR program of <= 10 min, and
                                                      Kodi's own "[epgstation] reserve: add program P (…) done:
                                                      reserve ID" line in kodi.log after the snapshot (proves Kodi
                                                      made it); the snapshot is cleared afterwards. Then
                                                      delete-reserve / delete-recorded accept it as usual

Backlog 006 (protect / unprotect / delete through Kodi; code hooks marked "protect / delete (006)"). DEV protocol:
recorded-snapshot, create --time --start-in 3 --minutes 2, wait-recorded RID, adopt-recorded REC, the Kodi writes on
REC only, audit.
    tools/dev/test_reserve.py recorded-snapshot      (GET only) store every recording id with isProtected and its
                                                      video file ids, the /recording ids and the time
    tools/dev/test_reserve.py wait-recorded RID      (GET only) poll every 15 s, until RID's endAt + 3 min, for the
                                                      finished recording of this tool's reservation RID; print its id
    tools/dev/test_reserve.py adopt-recorded REC     (GET only) take recording REC into this tool's list: it belongs
                                                      to a reservation of this tool, was not there (nor in progress)
                                                      at the snapshot, is finished and unprotected
    tools/dev/test_reserve.py audit                  (GET only + access.log / kodi.log) every snapshot recording but
                                                      the adopted ones is still there with the same isProtected and
                                                      files; every non-GET from KODIstation/ in access.log and every
                                                      "[epgstation] recorded:" kodi.log line since the snapshot names an
                                                      adopted id; prints PASS / FAIL, exit 1 on FAIL
    tools/dev/test_reserve.py unprotect-recorded REC (cleanup only) PUT unprotect of an adopted recording
delete-recorded also accepts adopted recordings and never one that was in the recorded snapshot.

Backlog 018 (delete one video file through Kodi; code hooks marked "delete a file (018)"). DEV protocol:
recorded-snapshot, create --time --start-in 3 --minutes 2 --encode H.264, wait-recorded RID --files 2,
adopt-recorded REC, the Kodi writes on REC only, audit.
    create --time ... --encode MODE                  the reservation also encodes: encodeOption {mode1: MODE,
                                                      isDeleteOriginalAfterEncode: false}; MODE must be in GET /config
                                                      encode
    wait-recorded RID --files N                      also waits until the recording is not encoding and has >= N video
                                                      files, until RID's endAt + 15 min
adopt-recorded stores the adopted recording's video file ids; audit passes a KODIstation DELETE /api/videos/{id} only
for one of those ids.

Backlog 021 (demo video; code hooks marked "demo video (021)"). DEV protocol: snapshot, recorded-snapshot, demo-target
REC, pick, the filmed tour (one reservation from the guide, adopted and cancelled through Kodi; protect + unprotect of
REC; the delete prompt closed), demo-audit.
    tools/dev/test_reserve.py demo-target REC        (GET only, after recorded-snapshot) REC is the only recording the
                                                      tour may protect / unprotect: in the snapshot, finished, not
                                                      encoding, unprotected, on a free channel; stores REC's prior
                                                      isProtected and files, GET /rules and every reservation's endAt
    tools/dev/test_reserve.py demo-audit             (GET only + access.log / kodi.log) the only KODIstation writes
                                                      since the snapshot are one POST /api/reserves (Kodi's "reserve:
                                                      add program P (…) done: reserve RID", P a free GR program of <=
                                                      10 min starting >= 60 min later), DELETE /api/reserves/RID and one
                                                      PUT /api/recorded/REC/protect then /unprotect; RID is gone, left
                                                      no recording; REC is as before; every other snapshot recording,
                                                      every reservation (unless ended) and the rules are unchanged
    tools/dev/test_reserve.py demo-restore           (cleanup only) PUT unprotect of REC when it is the stored target,
                                                      was unprotected before and is protected now; a left-over adopted
                                                      reservation goes with delete-reserve RID
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ON_VM = ("http://localhost:8888", "http://127.0.0.1:8888")
STATE = os.path.expanduser("~/.cache/kodistation-test-reserves.json")
NHK = 3211240960  # NHK総合1・大阪 (free GR)
MAX_OTHERS = 6  # other non-skip reservations recording at the same time (dev limit: 8 at once)
MIN_AHEAD_MIN = 60
MAX_MINUTES = 10
MIN_LEFT_MIN = 3  # pick --airing: minutes left at least
SNAPSHOT_MAX_AGE_MS = 2 * 3600 * 1000  # adopt: a snapshot older than this is refused
KODI_LOGS = ("~/.kodi/temp/kodi.old.log", "~/.kodi/temp/kodi.log")  # Kodi moves kodi.log to kodi.old.log on start
# plugin.do_record: f"[epgstation] reserve: add program {program_id} ({name}) done: reserve {reserve_id}"
KODI_ADD = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{3}) .*"
                      r"\[epgstation\] reserve: add program (\d+) \(.*\) done: reserve (\d+)\s*$")
# protect / delete (006): plugin._recorded_write: f"[epgstation] recorded: {op} recorded {id} ({name})" (+ " done")
KODI_RECORDED = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{3}) .*"
                           r"\[epgstation\] recorded: (\w+) recorded (\d+) ")
ACCESS_LOG = "~/EPGStation/logs/Service/access.log"
ACCESS = re.compile(r'^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d{3})\] \[\w+\] access - '
                    r'.*?"(\w+) (\S+)[^"]*" .*"([^"]*)"\s*$')
WAIT_STEP = 15  # wait-recorded: seconds between polls
WAIT_AFTER_MS = 3 * 60 * 1000  # wait-recorded: give up this long after the reservation's end
WAIT_ENCODE_MS = 15 * 60 * 1000  # delete a file (018): wait-recorded --files: give up this long after the end
TV_TYPES = {0x01, 0xA1, 0xA5, 0xAD}
NAME = "KODIstation test"


def _url(value):
    return (value or "").strip().rstrip("/")


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _addresses(host):
    """Every address a host name resolves to (a DNS lookup only, no connection); empty when it does not resolve."""
    if not host:
        return set()
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return set()


def _same_server(a, b):
    ha, hb = _host(a), _host(b)
    return ha == hb or bool(_addresses(ha) & _addresses(hb))


def _on_vm():
    """True only on the dev VM itself: this machine owns an address of $KODI_HOST. A UDP socket "connected" to such
    an address (no packet is sent) takes it as its source only on that machine; elsewhere the source is this machine's
    own address. (011 review: localhost:8888 elsewhere could be a tunnel to production.)"""
    host = os.environ.get("KODI_HOST")
    for addr in _addresses(host) if host else ():
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((addr, 9))
                if s.getsockname()[0] == addr:
                    return True
        except OSError:
            continue
    return False


def check_base(base):
    """base when it is the dev EPGStation ($EPGS_DEV_URL, or localhost:8888 when running on the VM = $KODI_HOST);
    SystemExit otherwise."""
    base, dev, prod = _url(base), _url(os.environ.get("EPGS_DEV_URL")), _url(os.environ.get("EPGS_PROD_URL"))
    if not dev or not prod:
        sys.exit("refused: EPGS_DEV_URL and EPGS_PROD_URL must both be set (~/.config/epgstation-kodi/local.env)")
    if _same_server(base, prod) or _same_server(dev, prod):
        sys.exit(f"refused: {base} is the production EPGStation")
    allowed = (dev,) + (ON_VM if base in ON_VM and _on_vm() else ())
    if base not in allowed:
        sys.exit(f"refused: {base} is not the dev EPGStation ({', '.join(allowed)})")
    return base


def vm_ssh():
    """ssh target of the dev VM: $KODI_SSH_USER@$KODI_HOST (local.env)."""
    host = os.environ.get("KODI_HOST")
    if not host:
        sys.exit("KODI_HOST is not set (~/.config/epgstation-kodi/local.env)")
    user = os.environ.get("KODI_SSH_USER")
    return f"{user}@{host}" if user else host


class Dev:
    def __init__(self, base):
        self.base = check_base(base)

    def call(self, method, path, body=None, **params):
        url = f"{self.base}/api{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        print(f"> {method} {url}" + (f" {json.dumps(body, ensure_ascii=False)}" if body is not None else ""))
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                shown = f" {raw[:200].decode('utf-8', 'replace')}" if method != "GET" else ""
                print(f"< {resp.status}{shown}")
        except urllib.error.HTTPError as e:
            print(f"< {e.code} {e.read(500).decode('utf-8', 'replace')}")
            raise
        return json.loads(raw) if raw.strip() else None


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"reserves": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def others_at(reserves, start, end):
    """Most non-skip reservations recording at one instant within [start, end)."""
    live = [r for r in reserves if not r.get("isSkip") and r["startAt"] < end and r["endAt"] > start]
    return max([sum(1 for r in live if r["startAt"] <= t < r["endAt"]) for t in
                {max(start, r["startAt"]) for r in live} | {start}], default=0)


def all_reserves(dev):
    return dev.call("GET", "/reserves", type="all", isHalfWidth="true", offset=0, limit=5000)["reserves"]


def _free_gr(ch, p):  # record from the guide (014): pick / snapshot / adopt
    return (ch.get("channelType") == "GR" and (ch.get("type") is None or ch["type"] in TV_TYPES)
            and bool(p.get("isFree")) and p["endAt"] - p["startAt"] <= MAX_MINUTES * 60 * 1000)


def pick(dev, now, airing=False, reserves=None):
    """(program, channel): the first free GR TV program of <= 10 min starting >= 60 min from now (airing: on air
    now with >= MIN_LEFT_MIN min left), not reserved, at a time with <= MAX_OTHERS other non-skip reservations.
    GET only."""
    reserves = all_reserves(dev) if reserves is None else reserves
    channels = {c["id"]: c for c in dev.call("GET", "/channels")}
    start = now if airing else now + MIN_AHEAD_MIN * 60 * 1000
    end = now if airing else now + 24 * 3600 * 1000
    scheds = dev.call("GET", "/schedules", startAt=int(start), endAt=int(end), isHalfWidth="true",
                      GR="true", BS="false", CS="false", SKY="false", isFree="true")
    reserved = {r.get("programId") for r in reserves}
    found = []
    for s in scheds:
        ch = channels.get(s["channel"]["id"]) or s["channel"]
        for p in s["programs"]:
            if airing:
                when = p["startAt"] <= now and p["endAt"] - now >= MIN_LEFT_MIN * 60 * 1000
            else:
                when = p["startAt"] >= start
            if (when and _free_gr(ch, p) and p["id"] not in reserved
                    and others_at(reserves, p["startAt"], p["endAt"]) <= MAX_OTHERS):
                found.append((p["startAt"], p, ch))
    if not found:
        sys.exit("no free GR program of <= 10 min " + ("on air with >= 3 min left" if airing else
                                                      "starting >= 60 min from now") + " at a quiet time")
    found.sort(key=lambda x: x[0])
    return found[0][1], found[0][2]


def pick_program(dev, reserves, now):
    return pick(dev, now, reserves=reserves)


def _when(p):
    return (f"{time.strftime('%m/%d %H:%M', time.localtime(p['startAt'] / 1000))}-"
            f"{time.strftime('%H:%M', time.localtime(p['endAt'] / 1000))}")


def show_pick(dev, airing):
    p, ch = pick(dev, time.time() * 1000, airing)
    print(f"program {p['id']} {p['name']} on {ch.get('name')} ({ch['id']}) {_when(p)} "
          f"({(p['endAt'] - p['startAt']) // 60000} min)")


def snapshot(dev):
    """Store the ids of every reservation now (GET only)."""
    ids = sorted(r["id"] for r in all_reserves(dev))
    state = load_state()
    state.update(snapshot=ids, snapshotAt=int(time.time() * 1000))
    save_state(state)
    print(f"snapshot: {len(ids)} reservations stored in {STATE}")


def read_kodi_log(dev):
    """kodi.old.log + kodi.log of the dev Kodi: read locally on the VM (base localhost), else over ssh. None when
    kodi.log itself cannot be read (protect / delete (006) review M4: no evidence is never a pass)."""
    if dev.base.startswith(("http://localhost", "http://127.0.0.1")):
        out = []
        for i, path in enumerate(KODI_LOGS):
            try:
                with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as f:
                    out.append(f.read())
            except OSError:
                if i == len(KODI_LOGS) - 1:
                    return None
        return "\n".join(out)
    cmd = "; ".join(f"[ -r {p} ] && cat {p}" for p in KODI_LOGS) + f"; [ -r {KODI_LOGS[-1]} ]"
    done = subprocess.run(["ssh", vm_ssh(), cmd], check=False, capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def kodi_adds(text):
    """[(ms, programId, reserveId)] of do_record's success lines in kodi.log text."""
    out = []
    for line in text.splitlines():
        m = KODI_ADD.match(line)
        if m:
            ms = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) * 1000) + int(m.group(2))
            out.append((ms, int(m.group(3)), int(m.group(4))))
    return out


def adopt(dev, rid, program_id):
    """Take reservation rid, made through Kodi after the snapshot, into this tool's list (GET only)."""
    state = load_state()
    if "snapshot" not in state or "snapshotAt" not in state:
        sys.exit("refused: run snapshot before the Kodi test")
    if time.time() * 1000 - state["snapshotAt"] > SNAPSHOT_MAX_AGE_MS:
        sys.exit("refused: the snapshot is older than 2 h; take a new one before the Kodi test")
    if rid in state["snapshot"]:
        sys.exit(f"refused: reservation {rid} existed before the snapshot")
    if rid <= max(state["snapshot"], default=0):
        sys.exit(f"refused: reservation {rid} is not newer than every reservation of the snapshot")
    r = _get_or_none(dev, f"/reserves/{rid}", isHalfWidth="true")
    if r is None:
        sys.exit(f"refused: reservation {rid} does not exist")
    if r.get("ruleId") is not None or r.get("programId") != program_id:
        sys.exit(f"refused: reservation {rid} is not a manual reservation of program {program_id}")
    p = _get_or_none(dev, f"/schedules/detail/{program_id}", isHalfWidth="true")
    channels = {c["id"]: c for c in dev.call("GET", "/channels")}
    if p is None or not _free_gr(channels.get(r["channelId"]) or {}, p):
        sys.exit(f"refused: program {program_id} is not a free GR program of <= {MAX_MINUTES} min")
    if any(x["id"] == rid for x in state["reserves"]):
        sys.exit(f"reservation {rid} is already in {STATE}")
    adds = kodi_adds(read_kodi_log(dev) or "")
    if not any(ms > state["snapshotAt"] and pid == program_id and n == rid for ms, pid, n in adds):
        sys.exit(f"refused: kodi.log has no \"reserve: add program {program_id} (…) done: reserve {rid}\" line after "
                 "the snapshot: not made through Kodi in this session")
    state.pop("snapshot", None)
    state.pop("snapshotAt", None)
    state["reserves"].append({"id": rid, "channelId": r["channelId"], "programId": program_id, "startAt": r["startAt"],
                              "endAt": r["endAt"], "created": int(time.time() * 1000), "adopted": True})
    save_state(state)
    print(f"adopted reservation {rid}: {r.get('name', p.get('name'))} {_when(r)}")


def create(dev, args):
    now = time.time() * 1000
    reserves = all_reserves(dev)
    if args.program:
        p, ch = pick_program(dev, reserves, now)
        print(f"program {p['id']} {p['name']} on {ch.get('name')} "
              f"{time.strftime('%m/%d %H:%M', time.localtime(p['startAt'] / 1000))} "
              f"({(p['endAt'] - p['startAt']) // 60000} min)")
        body = {"programId": p["id"], "allowEndLack": True}
        start, end, channel, program_id = p["startAt"], p["endAt"], ch["id"], p["id"]
    else:
        if not 1 <= args.minutes <= MAX_MINUTES or args.start_in < 1:
            sys.exit(f"--minutes 1..{MAX_MINUTES}, --start-in >= 1")
        start = (int(now // 60000) + args.start_in) * 60000
        end = start + args.minutes * 60000
        channel, program_id = NHK, None
        if others_at(reserves, start, end) > MAX_OTHERS:
            sys.exit(f"more than {MAX_OTHERS} other reservations record then; try later")
        body = {"allowEndLack": True,
                "timeSpecifiedOption": {"name": NAME, "channelId": NHK, "startAt": start, "endAt": end}}
    if getattr(args, "encode", None):  # delete a file (018): a second (encoded) file
        modes = dev.call("GET", "/config").get("encode") or []
        if args.encode not in modes:
            sys.exit(f"refused: encode mode {args.encode!r} is not one of the server's {modes}")
        body["encodeOption"] = {"mode1": args.encode, "isDeleteOriginalAfterEncode": False}
    added = dev.call("POST", "/reserves", body)
    rid = added["reserveId"]
    state = load_state()
    state["reserves"].append({"id": rid, "channelId": channel, "programId": program_id, "startAt": start,
                              "endAt": end, "created": int(now)})
    save_state(state)
    r = dev.call("GET", f"/reserves/{rid}", isHalfWidth="true")
    if r.get("isConflict"):
        print(f"reservation {rid} is a conflict: deleting it")
        dev.call("DELETE", f"/reserves/{rid}")
        sys.exit(1)
    print(f"created reservation {rid}: {r['name']} "
          f"{time.strftime('%m/%d %H:%M', time.localtime(start / 1000))}-"
          f"{time.strftime('%H:%M', time.localtime(end / 1000))}")


def own(rid):
    mine = next((x for x in load_state()["reserves"] if x["id"] == rid), None)
    if mine is None:
        sys.exit(f"refused: reservation {rid} was not created by this tool")
    return mine


def forget(rid):
    state = load_state()
    state["reserves"] = [x for x in state["reserves"] if x["id"] != rid]
    save_state(state)


def same_reserve(mine, r):
    """The server's reservation is still the manual one this tool created (ids are reused by nobody, but check)."""
    return (r.get("ruleId") is None and r["channelId"] == mine["channelId"] and r["startAt"] == mine["startAt"]
            and r["endAt"] == mine["endAt"])


def owns_recording(mine, rec):
    """rec was recorded from this tool's reservation: same channel, start and end, no rule, and the same programId
    (none for a time-specified one). A rule recording of the same channel at the same minute is not ours."""
    return (rec.get("ruleId") is None and rec["channelId"] == mine["channelId"] and rec["startAt"] == mine["startAt"]
            and rec["endAt"] == mine["endAt"] and rec.get("programId") == mine.get("programId"))


def _get_or_none(dev, path, **params):
    try:
        return dev.call("GET", path, **params)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def own_recordings(dev, mine):
    recs = dev.call("GET", "/recorded", isHalfWidth="true", offset=0, limit=100, channelId=mine["channelId"])
    return [r for r in recs["records"] if owns_recording(mine, r)]


def prune(dev):
    """Drop ids whose reservation is gone and that left no recording (e.g. cancelled before they started)."""
    for mine in list(load_state()["reserves"]):
        if _get_or_none(dev, f"/reserves/{mine['id']}", isHalfWidth="true") is None and not own_recordings(dev, mine):
            print(f"{mine['id']}: gone, no recording: removed from {STATE}")
            forget(mine["id"])


def delete_reserve(dev, rid):
    mine = own(rid)
    r = _get_or_none(dev, f"/reserves/{rid}", isHalfWidth="true")
    if r is None:
        prune(dev)
        sys.exit(f"reservation {rid} is already gone")
    if not same_reserve(mine, r):
        sys.exit(f"refused: reservation {rid} on the server is not the one this tool created")
    dev.call("DELETE", f"/reserves/{rid}")
    print(f"deleted reservation {rid}")
    prune(dev)


def delete_recorded(dev, rec_id):
    state = load_state()
    snap = state.get("recordedSnapshot") or {}
    if str(rec_id) in (snap.get("recorded") or {}) and rec_id not in state.get("recorded", []):
        sys.exit(f"refused: recording {rec_id} existed at the recorded snapshot")  # protect / delete (006)
    rec = dev.call("GET", f"/recorded/{rec_id}", isHalfWidth="true")
    mine = next((x for x in state["reserves"] if owns_recording(x, rec)), None)
    if mine is None:
        sys.exit(f"refused: recording {rec_id} does not belong to a reservation of this tool")
    if rec.get("isProtected") or rec.get("isRecording"):
        sys.exit(f"refused: recording {rec_id} is protected or still recording")
    dev.call("DELETE", f"/recorded/{rec_id}")
    print(f"deleted recording {rec_id} (reservation {mine['id']})")
    state = load_state()
    if rec_id in state.get("recorded", []):
        state["recorded"] = [x for x in state["recorded"] if x != rec_id]
        save_state(state)
    prune(dev)


# --- protect / delete (006) --------------------------------------------------------------------------------------

def _files(rec):
    return sorted(v["id"] for v in rec.get("videoFiles") or [])


def all_recorded(dev):
    return dev.call("GET", "/recorded", isHalfWidth="true", offset=0, limit=10000)["records"]


def recorded_snapshot(dev):
    """Store every recording (isProtected, video file ids), the /recording ids and the time (GET only)."""
    recs = {str(r["id"]): {"isProtected": bool(r.get("isProtected")), "files": _files(r)} for r in all_recorded(dev)}
    live = sorted(r["id"] for r in dev.call("GET", "/recording", isHalfWidth="true", offset=0, limit=100)["records"])
    state = load_state()
    state["recordedSnapshot"] = {"at": int(time.time() * 1000), "recorded": recs, "recording": live}
    state["recorded"] = []
    state["recordedFiles"] = {}  # delete a file (018)
    save_state(state)
    print(f"recorded snapshot: {len(recs)} recordings ({sum(v['isProtected'] for v in recs.values())} protected), "
          f"{len(live)} in progress, stored in {STATE}")


def _finished(rec):
    return not rec.get("isRecording") and any(v.get("size", 0) > 0 for v in rec.get("videoFiles") or [])


def _ready(rec, files):
    """Finished, and (delete a file (018), files > 0) not encoding with at least `files` video files."""
    return _finished(rec) and (not files or (not rec.get("isEncoding")
                                             and len(rec.get("videoFiles") or []) >= files))


def wait_recorded(dev, rid, now=lambda: time.time() * 1000, sleep=time.sleep, files=0):
    """The id of the finished recording of this tool's reservation rid (GET only); polls every WAIT_STEP s until the
    reservation's end + 3 min (files > 0: also until it has that many files and no encode runs; end + 15 min)."""
    mine = own(rid)
    deadline = mine["endAt"] + (WAIT_ENCODE_MS if files else WAIT_AFTER_MS)
    while True:
        done = [r for r in own_recordings(dev, mine) if _ready(r, files)]
        if done:
            print(f"recording {done[0]['id']} of reservation {rid} is finished"
                  + (f" with {len(done[0]['videoFiles'])} files" if files else ""))
            return done[0]["id"]
        if now() >= deadline:
            late = (deadline - mine["endAt"]) // 60000
            sys.exit(f"no finished recording of reservation {rid} by its end + {late} min"
                     + (f" with {files} files" if files else ""))
        sleep(WAIT_STEP)


def _snapshot_or_exit(state):
    snap = state.get("recordedSnapshot")
    if not snap:
        sys.exit("refused: run recorded-snapshot first")
    return snap


def adopt_recorded(dev, rec_id):
    """Take recording rec_id into this tool's list (the only recording Kodi may write to; GET only)."""
    state = load_state()
    snap = _snapshot_or_exit(state)
    if str(rec_id) in snap["recorded"] or rec_id in snap["recording"]:
        sys.exit(f"refused: recording {rec_id} existed at the recorded snapshot")
    rec = _get_or_none(dev, f"/recorded/{rec_id}", isHalfWidth="true")
    if rec is None:
        sys.exit(f"refused: recording {rec_id} does not exist")
    mine = next((x for x in state["reserves"] if owns_recording(x, rec)), None)
    if mine is None:
        sys.exit(f"refused: recording {rec_id} does not belong to a reservation of this tool")
    if rec.get("isProtected") or not _finished(rec):
        sys.exit(f"refused: recording {rec_id} is protected or not finished")
    state.setdefault("recorded", [])
    if rec_id not in state["recorded"]:
        state["recorded"].append(rec_id)
    files = state.setdefault("recordedFiles", {}).setdefault(str(rec_id), [])  # delete a file (018)
    files[:] = sorted(set(files) | set(_files(rec)))
    save_state(state)
    print(f"adopted recording {rec_id} (reservation {mine['id']}): {rec.get('name')}")


def _ms(stamp, millis):
    return int(time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) * 1000) + int(millis)


def access_writes(text):
    """[(ms, method, path, by KODIstation)] of EPGStation access.log lines with any method but GET/HEAD/OPTIONS."""
    out = []
    for line in text.splitlines():
        m = ACCESS.match(line)
        if m and m.group(3).upper() not in ("GET", "HEAD", "OPTIONS"):
            out.append((_ms(m.group(1).replace("T", " "), m.group(2)), m.group(3).upper(), m.group(4),
                        m.group(5).startswith("KODIstation/")))
    return out


def kodi_recorded(text):
    """[(ms, op, recorded id)] of plugin._recorded_write's kodi.log lines."""
    out = []
    for line in text.splitlines():
        m = KODI_RECORDED.match(line)
        if m:
            out.append((_ms(m.group(1), m.group(2)), m.group(3), int(m.group(4))))
    return out


def read_access_log(dev, since_ms=0):
    """EPGStation's access.log plus its rotated files (access.log.-DATE.N) written since since_ms, oldest first; read
    locally (base localhost), else over ssh. None when access.log cannot be read (review M4)."""
    if dev.base.startswith(("http://localhost", "http://127.0.0.1")):
        path = os.path.expanduser(ACCESS_LOG)
        folder, base = os.path.split(path)
        try:
            names = [n for n in os.listdir(folder) if n.startswith(os.path.basename(base) + ".")]
            rotated = sorted((os.path.getmtime(os.path.join(folder, n)), n) for n in names)
            out = []
            for mtime, name in rotated:
                if mtime * 1000 >= since_ms:
                    with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                        out.append(f.read())
            with open(path, encoding="utf-8", errors="replace") as f:
                out.append(f.read())
            return "".join(out)
        except OSError:
            return None
    secs = int(since_ms // 1000)
    cmd = (f"cd $(dirname {ACCESS_LOG}) && for f in $(ls -tr access.log.* 2>/dev/null); do "
           f"[ $(stat -c %Y $f) -ge {secs} ] && cat $f; done; cat access.log")
    done = subprocess.run(["ssh", vm_ssh(), cmd], check=False, capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def access_reads(text, since_ms):
    """GETs by KODIstation/ at or after since_ms: the audit's self-check (none: wrong log or a broken pattern)."""
    n = 0
    for line in (text or "").splitlines():
        m = ACCESS.match(line)
        if m and m.group(3).upper() == "GET" and m.group(5).startswith("KODIstation/") \
                and _ms(m.group(1).replace("T", " "), m.group(2)) >= since_ms:
            n += 1
    return n


_RECORDED_PATH = re.compile(r"^/api/recorded/(\d+)(?:/protect|/unprotect)?(?:\?.*)?$")
_VIDEO_PATH = re.compile(r"^/api/videos/(\d+)(?:\?.*)?$")  # delete a file (018)


def _adopted_write(method, path, adopted, adopted_files):
    """A KODIstation write on an adopted recording: /recorded/{id}[/protect|/unprotect], or (018) a DELETE of one of
    the adopted recordings' video files."""
    m = _RECORDED_PATH.match(path)
    if m:
        return int(m.group(1)) in adopted
    m = _VIDEO_PATH.match(path)
    return bool(m) and method == "DELETE" and int(m.group(1)) in adopted_files


def audit(dev):
    """True (PASS) when nothing but the adopted recordings changed since the recorded snapshot (see the docstring)."""
    state = load_state()
    snap = _snapshot_or_exit(state)
    adopted = set(state.get("recorded", []))
    adopted_files = {f for key, ids in (state.get("recordedFiles") or {}).items() if int(key) in adopted for f in ids}
    failures = []
    now = {r["id"]: r for r in all_recorded(dev)}
    for key, was in snap["recorded"].items():
        rid = int(key)
        if rid in adopted:
            continue
        rec = now.get(rid)
        if rec is None:
            failures.append(f"recording {rid} is gone")
        elif bool(rec.get("isProtected")) != was["isProtected"]:
            failures.append(f"recording {rid}: isProtected {rec.get('isProtected')} (was {was['isProtected']})")
        elif _files(rec) != was["files"]:
            failures.append(f"recording {rid}: video files {_files(rec)} (were {was['files']})")
    access = read_access_log(dev, snap["at"])
    kodi_log = read_kodi_log(dev)
    if access is None:
        failures.append(f"{ACCESS_LOG} not readable: no evidence")
    elif not access_reads(access, snap["at"]):
        failures.append(f"no KODIstation/ GET in {ACCESS_LOG} since the snapshot (wrong log or pattern?)")
    if kodi_log is None:
        failures.append("kodi.log not readable: no evidence")
    for ms, method, path, kodi in access_writes(access or ""):
        if ms < snap["at"]:
            continue
        if not kodi:
            print(f"note: write by another client: {method} {path}")
        elif not _adopted_write(method, path, adopted, adopted_files):
            failures.append(f"KODIstation wrote {method} {path} (not an adopted recording)")
        else:
            print(f"ok: KODIstation {method} {path}")
    for ms, op, rid in kodi_recorded(kodi_log or ""):
        if ms >= snap["at"]:
            if rid in adopted:
                print(f"ok: kodi.log recorded: {op} recorded {rid}")
            else:
                failures.append(f"kodi.log: recorded: {op} recorded {rid} (not an adopted recording)")
    for f in failures:
        print("FAIL:", f)
    print(f"audit: {len(snap['recorded'])} snapshot recordings, adopted {sorted(adopted)}: "
          + ("PASS" if not failures else "FAIL"))
    return not failures


def unprotect_recorded(dev, rec_id):
    """Cleanup only: PUT unprotect of an adopted recording that is still this tool's."""
    state = load_state()
    if rec_id not in state.get("recorded", []):
        sys.exit(f"refused: recording {rec_id} is not an adopted recording")
    rec = dev.call("GET", f"/recorded/{rec_id}", isHalfWidth="true")
    if not any(owns_recording(x, rec) for x in state["reserves"]):
        sys.exit(f"refused: recording {rec_id} does not belong to a reservation of this tool")
    if not rec.get("isProtected"):
        print(f"recording {rec_id} is not protected")
        return
    dev.call("PUT", f"/recorded/{rec_id}/unprotect")
    print(f"unprotected recording {rec_id}")


# --- demo video (021) --------------------------------------------------------------------------------------------

DEMO_MIN_AHEAD_MS = MIN_AHEAD_MIN * 60 * 1000
KODI_RESERVE_OP = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{3}) .*"
                             r"\[epgstation\] reserve: (\w+) (?:reserve|program) (\d+)")


def _free_channel_ids(channels):
    """tools/snapshot_api.is_free_channel (GR, or a free BS service): the one free-channel rule of the repo."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "snapshot_api.py")
    spec = importlib.util.spec_from_file_location("snapshot_api_free", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.free_channel_ids(channels)


def all_rules(dev):
    return dev.call("GET", "/rules", offset=0, limit=1000)["rules"]


def demo_target(dev, rec_id):
    """Store recording rec_id as the tour's protect target with its prior state, the rules and the reservations."""
    state = load_state()
    snap = _snapshot_or_exit(state)
    if str(rec_id) not in snap["recorded"]:
        sys.exit(f"refused: recording {rec_id} is not in the recorded snapshot")
    rec = _get_or_none(dev, f"/recorded/{rec_id}", isHalfWidth="true")
    if rec is None:
        sys.exit(f"refused: recording {rec_id} does not exist")
    if not _finished(rec) or rec.get("isEncoding"):
        sys.exit(f"refused: recording {rec_id} is recording or encoding")
    if rec.get("isProtected"):
        sys.exit(f"refused: recording {rec_id} is protected")
    if rec.get("channelId") not in _free_channel_ids(dev.call("GET", "/channels")):
        sys.exit(f"refused: recording {rec_id} is not on a free channel")
    state["demo"] = {"rec": rec_id, "prior": {"isProtected": False, "files": _files(rec)}, "at": snap["at"],
                     "rules": all_rules(dev), "reserves": {str(r["id"]): r["endAt"] for r in all_reserves(dev)}}
    save_state(state)
    print(f"demo target: recording {rec_id} {rec.get('name')} (unprotected, files {_files(rec)}); "
          f"{len(state['demo']['rules'])} rules, {len(state['demo']['reserves'])} reservations stored")


def kodi_reserve_ops(text):
    """[(ms, op, id)] of every "[epgstation] reserve: OP reserve|program ID" kodi.log line (attempts and done)."""
    out = []
    for line in text.splitlines():
        m = KODI_RESERVE_OP.match(line)
        if m:
            out.append((_ms(m.group(1), m.group(2)), m.group(3), int(m.group(4))))
    return out


def _demo_writes(access, since, rec_id):
    """(failures, rid or None) from the KODIstation writes in access.log since `since`: exactly one POST /api/reserves,
    at most one DELETE /api/reserves/{id}, exactly PUT protect then unprotect of REC."""
    fails, posts, deletes, puts = [], 0, [], []
    for ms, method, path, kodi in access_writes(access or ""):
        if ms < since:
            continue
        if not kodi:
            print(f"note: write by another client: {method} {path}")
            continue
        bare = path.split("?", 1)[0]
        m = re.match(r"^/api/reserves/(\d+)$", bare)
        if method == "POST" and bare == "/api/reserves":
            posts += 1
        elif method == "DELETE" and m:
            deletes.append(int(m.group(1)))
        elif method == "PUT" and bare in (f"/api/recorded/{rec_id}/protect", f"/api/recorded/{rec_id}/unprotect"):
            puts.append(bare.rsplit("/", 1)[1])
        else:
            fails.append(f"KODIstation wrote {method} {path} (not an allowed demo write)")
            continue
        print(f"ok: KODIstation {method} {path}")
    if posts != 1:
        fails.append(f"{posts} KODIstation POST /api/reserves (expected exactly 1)")
    if len(deletes) != 1:
        fails.append(f"{len(deletes)} KODIstation DELETE /api/reserves/... (expected exactly 1)")
    if puts != ["protect", "unprotect"]:
        fails.append(f"KODIstation PUTs on recording {rec_id}: {puts} (expected protect, unprotect)")
    return fails, (deletes[0] if len(deletes) == 1 else None)


def demo_audit(dev, now=None):
    """True (PASS) when the tour wrote exactly what the demo allows (see the docstring)."""
    state = load_state()
    snap = _snapshot_or_exit(state)
    demo = state.get("demo")
    if not demo:
        sys.exit("refused: run demo-target first")
    now = time.time() * 1000 if now is None else now
    rec_id, since = demo["rec"], snap["at"]
    failures = []
    access = read_access_log(dev, since)
    kodi_log = read_kodi_log(dev)
    if access is None:
        failures.append(f"{ACCESS_LOG} not readable: no evidence")
    elif not access_reads(access, since):
        failures.append(f"no KODIstation/ GET in {ACCESS_LOG} since the snapshot (wrong log or pattern?)")
    if kodi_log is None:
        failures.append("kodi.log not readable: no evidence")
    write_fails, deleted = _demo_writes(access, since, rec_id)
    failures += write_fails
    adds, seen = [], set()
    for ms, pid, rid in kodi_adds(kodi_log or ""):  # Kodi may write the same line twice: one per reservation
        if ms >= since and (pid, rid) not in seen:
            seen.add((pid, rid))
            adds.append((ms, pid, rid))
    if len(adds) != 1:
        failures.append(f"{len(adds)} kodi.log \"reserve: add program P (…) done: reserve RID\" lines (expected 1)")
    rid = adds[0][2] if len(adds) == 1 else None
    if rid is not None:
        add_ms, pid = adds[0][0], adds[0][1]
        if deleted != rid:
            failures.append(f"the deleted reservation {deleted} is not the one Kodi made ({rid})")
        if rid in {int(k) for k in demo["reserves"]}:
            failures.append(f"reservation {rid} existed before the tour")
        p = _get_or_none(dev, f"/schedules/detail/{pid}", isHalfWidth="true")
        channels = {c["id"]: c for c in dev.call("GET", "/channels")}
        if p is None or not _free_gr(channels.get(p.get("channelId")) or {}, p):
            failures.append(f"program {pid} is not a free GR program of <= {MAX_MINUTES} min")
        elif p["startAt"] - add_ms < DEMO_MIN_AHEAD_MS:
            failures.append(f"program {pid} starts less than {MIN_AHEAD_MIN} min after the reservation was made")
        if _get_or_none(dev, f"/reserves/{rid}", isHalfWidth="true") is not None:
            failures.append(f"reservation {rid} still exists")
        if p is not None:
            mine = {"channelId": p.get("channelId"), "startAt": p["startAt"], "endAt": p["endAt"], "programId": pid}
            if own_recordings(dev, mine):
                failures.append(f"reservation {rid} left a recording")
    for ms, op, ident in kodi_reserve_ops(kodi_log or ""):
        if ms < since or op == "add" or (op == "cancel" and ident == rid):
            continue
        failures.append(f"kodi.log: reserve: {op} {ident} (not an allowed demo write)")
    for ms, op, ident in kodi_recorded(kodi_log or ""):
        if ms >= since and not (ident == rec_id and op in ("protect", "unprotect")):
            failures.append(f"kodi.log: recorded: {op} recorded {ident} (not an allowed demo write)")
    recs = {r["id"]: r for r in all_recorded(dev)}
    for key, was in snap["recorded"].items():
        rec = recs.get(int(key))
        want = demo["prior"] if int(key) == rec_id else was
        if rec is None:
            failures.append(f"recording {key} is gone")
        elif bool(rec.get("isProtected")) != want["isProtected"]:
            failures.append(f"recording {key}: isProtected {rec.get('isProtected')} (was {want['isProtected']})")
        elif _files(rec) != want["files"]:
            failures.append(f"recording {key}: video files {_files(rec)} (were {want['files']})")
    present = {r["id"] for r in all_reserves(dev)}
    for key, end in demo["reserves"].items():
        if int(key) not in present and end > now:
            failures.append(f"reservation {key} is gone before its end")
    if all_rules(dev) != demo["rules"]:
        failures.append("GET /rules differs from the snapshot")
    for f in failures:
        print("FAIL:", f)
    print(f"demo-audit: target {rec_id}, reservation {rid}: " + ("PASS" if not failures else "FAIL"))
    return not failures


def demo_restore(dev):
    """Cleanup only: unprotect the stored demo target when it was unprotected before and is protected now."""
    demo = load_state().get("demo")
    if not demo:
        sys.exit("refused: no demo target stored")
    rec_id = demo["rec"]
    if demo["prior"]["isProtected"]:
        sys.exit(f"refused: recording {rec_id} was protected before the tour; nothing to restore")
    rec = dev.call("GET", f"/recorded/{rec_id}", isHalfWidth="true")
    if _files(rec) != demo["prior"]["files"]:
        sys.exit(f"refused: recording {rec_id} has other files than at demo-target; not the same recording?")
    if not rec.get("isProtected"):
        print(f"recording {rec_id} is not protected")
        return
    dev.call("PUT", f"/recorded/{rec_id}/unprotect")
    print(f"unprotected recording {rec_id}")


def list_own(dev):
    prune(dev)
    for x in load_state()["reserves"]:
        r = _get_or_none(dev, f"/reserves/{x['id']}", isHalfWidth="true")
        print(f"{x['id']}: " + (f"exists ({r['name']})" if r else "gone, its recording is still there"))


def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import local_env
    local_env.load()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("EPGS_DEV_URL", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    kind = c.add_mutually_exclusive_group(required=True)
    kind.add_argument("--program", action="store_true")
    kind.add_argument("--time", action="store_true")
    c.add_argument("--start-in", type=int, default=4, help="minutes from now (--time)")
    c.add_argument("--minutes", type=int, default=3, help="length (--time)")
    c.add_argument("--encode", help="delete a file (018): encode mode from GET /config encode, e.g. H.264")
    sub.add_parser("delete-reserve").add_argument("id", type=int)
    sub.add_parser("delete-recorded").add_argument("id", type=int)
    sub.add_parser("list")
    sub.add_parser("pick").add_argument("--airing", action="store_true")
    sub.add_parser("snapshot")
    a = sub.add_parser("adopt")
    a.add_argument("id", type=int)
    a.add_argument("--program", type=int, required=True)
    sub.add_parser("recorded-snapshot")  # protect / delete (006)
    w = sub.add_parser("wait-recorded")
    w.add_argument("id", type=int)
    w.add_argument("--files", type=int, default=0, help="delete a file (018): wait for this many files, no encode")
    sub.add_parser("adopt-recorded").add_argument("id", type=int)
    sub.add_parser("audit")
    sub.add_parser("unprotect-recorded").add_argument("id", type=int)
    sub.add_parser("demo-target").add_argument("id", type=int)  # demo video (021)
    sub.add_parser("demo-audit")
    sub.add_parser("demo-restore")
    args = ap.parse_args()
    dev = Dev(args.base)
    if args.cmd == "create":
        create(dev, args)
    elif args.cmd == "delete-reserve":
        delete_reserve(dev, args.id)
    elif args.cmd == "delete-recorded":
        delete_recorded(dev, args.id)
    elif args.cmd == "pick":
        show_pick(dev, args.airing)
    elif args.cmd == "snapshot":
        snapshot(dev)
    elif args.cmd == "adopt":
        adopt(dev, args.id, args.program)
    elif args.cmd == "recorded-snapshot":
        recorded_snapshot(dev)
    elif args.cmd == "wait-recorded":
        wait_recorded(dev, args.id, files=args.files)
    elif args.cmd == "adopt-recorded":
        adopt_recorded(dev, args.id)
    elif args.cmd == "audit":
        sys.exit(0 if audit(dev) else 1)
    elif args.cmd == "unprotect-recorded":
        unprotect_recorded(dev, args.id)
    elif args.cmd == "demo-target":  # demo video (021)
        demo_target(dev, args.id)
    elif args.cmd == "demo-audit":
        sys.exit(0 if demo_audit(dev) else 1)
    elif args.cmd == "demo-restore":
        demo_restore(dev)
    else:
        list_own(dev)


if __name__ == "__main__":
    main()
