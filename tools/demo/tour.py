#!/usr/bin/env python3
"""Demo video (backlog 021): the scripted, filmed tour of one section on the dev Kodi.

    tools/demo/tour.py SECTION --log FILE [--capture CLIP] [--pace F]
        [--program P --channel C] [--rec REC]

Sections (Japanese UI A-D, English E): A browse recordings, B live TV + a channel's guide, C record from the guide
(one reservation, adopted off camera with tools/dev/test_reserve.py, then cancelled), D protect / unprotect REC and
the delete prompt closed with Back, E English UI and genre names. --capture CLIP films it with tools/demo/capture.sh
on the VM (the log's t is then seconds since the capture started).

Hosts: KODI_HOST / KODI_SSH_USER / EPGS_DEV_URL from local.env (backlog 019). Refusals: KODI_HOST unset or
localhost; the addon's server_url (settings.xml, read over ssh) not a localhost URL; any recording, recording in
progress or reservation on a non-free channel; a running live stream. EPGStation
gets GETs only from here; the writes are Kodi's own (the addon), through the GUI.
"""
import argparse
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kodi_drive as kd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
LIVE_S = 15  # seconds of live playback (the cut keeps <= 5 s)
LIVE_MAX_S = 60  # CLAUDE.md: live-stream tests at most 1 minute
MAX_PACE = 2.0  # slower dwells would stretch the live part towards the cap
STREAM_LEFT = ("a live stream is still running after Player.Stop: stop it now (JSON-RPC Player.Stop, or "
               "`sudo systemctl restart kodi` on the VM; EPGStation frees the tuner when the client disconnects) and "
               "check GET /api/streams on the dev EPGStation is empty")
SETTINGS = "~/.kodi/userdata/addon_data/plugin.video.epgstation/settings.xml"
LOCAL_URL = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?/?$")


def ssh_target():
    """KODI_SSH_USER@KODI_HOST from local.env (no user part when unset: ssh's own default)."""
    return kd.local_env.ssh_target(kd.env_host())


def ssh(cmd, stdin=None, check=True, timeout=60):
    """One ssh command; a hung connection becomes a TakeError after `timeout` seconds."""
    try:
        done = subprocess.run(["ssh", "-o", "ConnectTimeout=10", ssh_target(), cmd], input=stdin, capture_output=True,
                              text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise kd.TakeError(f"ssh {cmd!r}: no answer in {timeout} s") from e
    if check and done.returncode != 0:
        raise kd.TakeError(f"ssh {cmd!r}: {done.stderr.strip()}")
    return done.stdout


def server_url(settings_xml):
    m = re.search(r'<setting id="server_url"[^>]*>([^<]*)</setting>', settings_xml)
    return m.group(1).strip() if m else "http://localhost:8888"  # absent = the default


def check_server_url(settings_xml):
    url = server_url(settings_xml)
    if not LOCAL_URL.match(url):
        sys.exit(f"refused: the addon's server_url {url!r} is not a localhost URL (the dev EPGStation on the VM)")
    return url


def check_live_seconds(seconds):
    if not 0 < seconds <= LIVE_MAX_S:
        sys.exit(f"refused: live playback of {seconds} s (at most {LIVE_MAX_S} s)")
    return seconds


def check_pace(pace):
    if not 0 < pace <= MAX_PACE:
        sys.exit(f"refused: --pace {pace} (0 < pace <= {MAX_PACE})")
    return pace


class Capture:
    def __init__(self, clip, force=False):
        self.clip, self.force = clip, force
        with open(os.path.join(HERE, "capture.sh"), encoding="utf-8") as f:
            self.script = f.read()

    def start(self):
        force = " force" if self.force else ""
        print(ssh(f"sh -s start {self.clip}{force}", stdin=self.script).strip())

    def stop(self):
        print(ssh(f"sh -s stop {self.clip}", stdin=self.script).strip())


class Tour:
    def __init__(self, kodi, epgs, pace=1.0, program=None, channel=None, rec=None, rehearse=False):
        self.k, self.epgs, self.pace, self.rehearse = kodi, epgs, check_pace(pace), rehearse
        self.start_ms = int(time.time() * 1000)  # the take's start (wall clock, as kodi.log stamps)
        self.cancel_only = False
        self.events = None
        self.pending = []  # what a write or stream start left to clean up if the take dies now (printed by main)
        self.program, self.channel, self.rec = program, channel, rec

    # helpers
    def dwell(self, seconds, deadline=None):
        """seconds x pace; with a deadline (k.clock() time) never past it."""
        t = seconds * self.pace
        if deadline is not None:
            t = max(0.0, min(t, deadline - self.k.clock()))
        self.k.sleep(t)

    def open_item(self, label, folder, step, exact=True, max_steps=40):
        self.k.focus_label(label, max_steps=max_steps, exact=exact)
        self.k.press("Select")
        self.k.wait_folder(folder)
        self.k.check(step)

    def back_to(self, folder, step):
        self.k.press("Back")
        self.k.wait_folder(folder)
        self.k.check(step)

    def focus_param(self, key, value, max_steps=120, direction="Down"):
        """Focus the list item whose URL has key=value (query parameter, exact)."""
        for step in range(max_steps + 1):
            path = self.k.label("Container.ListItem.FileNameAndPath")
            if urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get(key) == [str(value)]:
                return path
            if step < max_steps:
                self.k.press(direction, wait=0.12)
        raise kd.TakeError(f"no item with {key}={value} within {max_steps} steps")

    def context(self, entry, step):
        self.k.press("ContextMenu", wait=0.6)
        self.k.wait_for("Window.IsActive(contextmenu)", timeout=10)
        self.k.focus_label(entry, where="control", max_steps=12, exact=True)
        self.k.log(step)
        self.dwell(0.8)
        self.k.press("Select")

    def confirm_yes(self, yes_label, step, dwell=1.8):
        """The addon's yes/no (No focused): move to the yes button by its label, then Select."""
        self.k.wait_for("Window.IsActive(yesnodialog)", timeout=15)
        self.k.log(step + "_question")
        self.dwell(dwell)
        if self.rehearse:  # no write: the yes button is only found, the dialog closed with Back
            self.k.focus_label(yes_label, where="control", max_steps=3, direction="Left", exact=True)
            self.k.press("Back")
            self.k.wait_for("!Window.IsActive(yesnodialog)", timeout=10)
            self.k.log(step + "_rehearsed")
            return False
        self.k.focus_label(yes_label, where="control", max_steps=3, direction="Left", exact=True)
        self.dwell(0.5)
        self.k.press("Select")
        return True

    def toast(self, step, seconds=3.0):
        try:
            self.k.wait_for("Window.IsActive(notification)", timeout=8)
            text = self.k.labels("Control.GetLabel(401)", "Control.GetLabel(402)")
        except kd.TakeError:
            text = {}
        self.k.log(step, toast=text)
        self.dwell(seconds)

    def to_root(self):
        self.k.rpc("GUI.ActivateWindow", window="videos", parameters=[kd.ROOT_URL])
        self.k.wait_folder(kd.ROOT_URL, exact=True)

    # sections
    def precheck(self, section):
        """Before any Kodi input (main): C's reservation target (or, --cancel-only, the adopted reservation) and D's
        demo target must pass test_reserve's rules, else TakeError."""
        if section == "C":
            if self.cancel_only:
                self.check_cancel_target()
            elif not self.rehearse:
                self.check_record_target()
        elif section == "D" and not self.rehearse:
            self.check_demo_target()

    def prep(self, section):
        """Off camera: where the section starts."""
        if section == "A":
            self.to_root()  # the videos window remembers its folder: the add-on tile then opens the root
            self.k.rpc("GUI.ActivateWindow", window="home")
            self.k.sleep(1.5)
            for _ in range(4):  # the home menu entry, not a tile of the panel
                if kd.norm(self.k.focused("control")) in ("add-ons", "アドオン"):
                    break
                self.k.press("Left", wait=0.4)
            else:
                raise kd.TakeError("the home menu's add-ons entry is not focused")
        elif section == "C":
            self.k.rpc("GUI.ActivateWindow", window="videos", parameters=[self.guide_day_url()])
            self.k.wait_folder("action=guide_channel")
        else:
            self.to_root()
        if section == "E":  # Kodi toasts every new EventServer client: connect off camera, wait the toast out
            self.events = kd.EventClient(self.k.host)
            self.events.helo()
            self.k.sleep(7.0)
        self.k.sleep(1.0)

    def guide_day_url(self):
        lives = self.k.listdir(f"{kd.ROOT_URL}?action=live&type=GR")
        item = next((f for f in lives if f"channelId={self.channel}" in f["file"]), None)
        if item is None:
            raise kd.TakeError(f"channel {self.channel} is not in the 地上波 list")
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(item["file"]).query)["name"][0]
        days = self.k.listdir(f"{kd.ROOT_URL}?action=guide_channel&channelId={self.channel}&"
                              f"name={urllib.parse.quote(name)}&logo=1")
        want = f"day={self.day_ms()}"
        day = next((f for f in days if want in f["file"]), None)
        if day is None:
            raise kd.TakeError(f"no guide day {want} for channel {self.channel}")
        return day["file"]

    def day_ms(self):
        p = self.epgs.api(f"/schedules/detail/{self.program}", isHalfWidth="true")
        lt = time.localtime(p["startAt"] / 1000)
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)

    def section_a(self):
        k = self.k
        k.log("home")
        self.dwell(1.5)
        k.press("Right")
        k.focus_label("KODIstation", where="control", max_steps=3)
        self.dwell(1.0)
        k.press("Select")
        k.wait_folder(kd.ROOT_URL, exact=True)
        k.check("root")
        self.dwell(1.5)
        self.open_item("TV番組", "action=tvshows", "tvshows")
        k.focus_label("おじゃる丸", max_steps=40)
        k.log("show_focused")
        self.dwell(2.0)
        k.press("Select")
        k.wait_folder("action=show")
        k.check("episodes")
        k.press("Down")
        k.log("episode1")
        self.dwell(2.5)
        k.press("Down")
        k.log("episode2")
        self.dwell(1.5)
        k.press("Up")
        self.dwell(0.5)
        k.press("Info", wait=0.8)
        k.wait_for("Window.IsActive(movieinformation)", timeout=10)
        k.log("details")
        self.dwell(4.5)
        k.press("Back")
        self.back_to("action=tvshows", "tvshows_back")
        self.back_to(kd.ROOT_URL, "root_back")
        self.open_item("映画", "action=movies", "movies")
        k.press("Down")
        k.press("Down")
        k.press("Down")
        k.log("movie")
        self.dwell(2.5)
        self.back_to(kd.ROOT_URL, "root_back2")
        self.open_item("分類から探す", "action=browse", "browse")
        self.dwell(0.8)
        self.open_item("ジャンル別", "action=genres", "genres")
        self.dwell(1.2)
        self.open_item("アニメ・特撮", "action=genre&", "genre", exact=False)
        self.dwell(1.2)
        self.focus_param("subGenre", 0, max_steps=6)
        k.press("Select")
        k.wait_folder("subGenre")
        k.check("subgenre")
        k.press("Down")
        k.press("Down")
        k.log("subgenre_item")
        self.dwell(2.5)

    def section_b(self):
        k = self.k
        k.check("root")
        self.dwell(1.0)
        self.open_item("ライブTV", "action=live", "live")
        self.dwell(1.0)
        self.open_item("地上波", "type=GR", "live_gr")
        self.dwell(1.5)
        k.focus_label("..", max_steps=20, direction="Up", exact=True)
        k.press("Down")  # the first channel (NHK総合 on the dev list: no commercials) is the one played
        k.log("live_channel_focused")
        self.dwell(2.0)
        self.play_live()
        self.focus_param("channelId", self.channel)
        k.check("live_gr_back")
        self.context("番組表", "ctx_guide")
        k.wait_folder("action=guide_channel")
        k.check("guide_days")
        self.dwell(1.5)
        self.focus_param("day", self.day_ms(), max_steps=10)
        k.log("day_focused")
        self.dwell(0.8)
        k.press("Select")
        k.wait_folder("day=")
        k.check("guide_day")
        self.focus_param("programId", self.program)
        k.log("program_focused")
        self.dwell(2.0)

    def play_live(self):
        """One live channel for LIVE_S seconds. From the request that starts it, everything runs under a deadline of
        start + LIVE_MAX_S: every RPC waits at most the time left, the OSD and log lines are dropped when little is
        left, and Stop (short timeout, EventServer fallback) is sent in `finally` whatever happened."""
        k, seconds = self.k, check_live_seconds(LIVE_S)
        if self.rehearse:
            k.log("live_rehearsed")
            return
        start = k.clock()
        deadline = start + LIVE_MAX_S
        end = min(start + seconds, deadline)
        k.deadline = deadline
        self.pending.append("a live stream may be running: Player.Stop (or `sudo systemctl restart kodi` on the VM), "
                            "then check GET /api/streams on the dev EPGStation is empty")
        try:
            k.press("Select")  # starts the stream: inside the protected region
            k.wait_for("Player.HasVideo", timeout=max(0.0, min(20.0, deadline - k.clock())))
            if k.time_left() > 5:
                k.log("live_playing")
            self.dwell(3.0, deadline)
            if k.time_left() > 5:
                k.press("Info")
                k.log("live_osd")
            while k.clock() < end:
                k.sleep(max(0.0, min(0.5, end - k.clock())))
        finally:
            k.deadline = None
            k.stop_player()
            played = round(k.clock() - start, 1)
        k.log("live_stopped", played=played)
        for _ in range(30):
            if not k.boolean("Player.HasMedia") and not self.epgs.streams():
                break
            k.sleep(0.5)
        else:
            raise kd.TakeError(STREAM_LEFT)
        self.pending.pop()
        k.wait_folder("type=GR")
        k.log("streams_empty")

    def section_c(self):
        k = self.k
        k.check("guide_day")
        self.focus_param("programId", self.program)
        k.log("program_focused")
        self.dwell(1.5)
        if self.cancel_only:  # a retake of the second half: the reservation exists and is adopted already
            rid = self.check_cancel_target()
            self.pending.append(f"reservation {rid} (adopted) may still exist: tools/dev/test_reserve.py "
                                f"delete-reserve {rid}")
        else:
            if not self.rehearse:
                self.check_record_target()  # again, right before the write
            self.context("録画", "ctx_record")
            self.pending.append(f"a reservation of program {self.program} may exist (made through Kodi): find it "
                                f"with GET /api/reserves; adopt it (tools/dev/test_reserve.py adopt RID --program "
                                f"{self.program}) and delete-reserve RID, or remove it in Kodi (番組表 → 予約を削除)")
            if not self.confirm_yes("録画", "record"):
                self.pending.pop()
                return
            self.toast("record_toast")
            rid = self.adopt()
            self.pending[-1] = (f"reservation {rid} (adopted) may still exist: tools/dev/test_reserve.py "
                                f"delete-reserve {rid}")
            k.sleep(1.0)  # the addon's Container.Refresh
        k.wait_folder("day=")
        self.focus_param("programId", self.program)
        k.log("record_mark", reserve=rid)
        self.dwell(2.5)
        if self.cancel_only:
            self.check_cancel_target()  # again, right before the write
        self.context("予約を削除", "ctx_cancel")
        self.confirm_yes("予約を削除", "cancel")
        self.toast("cancel_toast")
        k.wait_folder("day=")
        self.focus_param("programId", self.program)
        if self.reserve_or_none(rid) is not None:
            raise kd.TakeError(f"reservation {rid} still exists after 予約を削除")
        self.pending.pop()
        k.log("cancelled")
        self.dwell(1.5)

    def kodi_adds(self):
        """[(ms, programId, reserveId)] of do_record's "add program P (…) done: reserve RID" lines in kodi.log."""
        out = ssh(f"grep -aF 'reserve: add program {self.program} (' ~/.kodi/temp/kodi.log", check=False)
        return kd.dev_tool().kodi_adds(out)

    def run_adopt(self, rid):
        done = subprocess.run([sys.executable, os.path.join(REPO, "tools", "dev", "test_reserve.py"), "adopt",
                               str(rid), "--program", str(self.program)], capture_output=True, text=True,
                              timeout=120)
        print(done.stdout + done.stderr)
        return done.returncode == 0

    def reserve_or_none(self, rid):
        import urllib.error

        try:
            return self.epgs.api(f"/reserves/{rid}", isHalfWidth="true")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def adopt(self):
        """Off camera (the clip keeps running, the cut drops it): the last kodi.log add line of program P stamped
        after the take's start gives RID; it must exist on the dev EPGStation; then test_reserve adopts it."""
        for _ in range(20):
            adds = [x for x in self.kodi_adds() if x[1] == self.program and x[0] >= self.start_ms]
            if adds:
                break
            self.k.sleep(0.5)
        else:
            raise kd.TakeError(f"no 'reserve: add program {self.program} … done' line in kodi.log after the take "
                               "started")
        rid = adds[-1][2]
        r = self.reserve_or_none(rid)
        if r is None or r.get("programId") != self.program:
            raise kd.TakeError(f"reservation {rid} (kodi.log) is not program {self.program} on the dev EPGStation")
        if not self.run_adopt(rid):
            raise kd.TakeError(f"adopt {rid} failed: reservation {rid} (program {self.program}) was made through Kodi "
                               f"but is not managed: remove it in Kodi (番組表 → 予約を削除), or fix the cause and run "
                               f"tools/dev/test_reserve.py adopt {rid} --program {self.program} then delete-reserve "
                               f"{rid}")
        return rid

    def check_record_target(self):
        """Section C before the GUI write: test_reserve pick/adopt's rules for --program on --channel: a fresh
        reservation snapshot, a free GR TV program of <= MAX_MINUTES starting >= MIN_AHEAD_MIN from now, not reserved,
        <= MAX_OTHERS other reservations at that time."""
        tool = kd.dev_tool()
        state = tool.load_state()
        now = time.time() * 1000
        if "snapshot" not in state or "snapshotAt" not in state:
            raise kd.TakeError("no reservation snapshot: run tools/dev/test_reserve.py snapshot before section C")
        if now - state["snapshotAt"] > tool.SNAPSHOT_MAX_AGE_MS - 10 * 60 * 1000:
            raise kd.TakeError("the reservation snapshot is (nearly) 2 h old: take a new one (test_reserve snapshot)")
        p = self.epgs.api(f"/schedules/detail/{self.program}", isHalfWidth="true")
        channels = {c["id"]: c for c in self.epgs.api("/channels")}
        ch = channels.get(p.get("channelId")) or {}
        if p.get("channelId") != self.channel or not tool._free_gr(ch, p):
            raise kd.TakeError(f"program {self.program} is not a free GR TV program of <= {tool.MAX_MINUTES} min "
                               f"on channel {self.channel}")
        if p["startAt"] < now + tool.MIN_AHEAD_MIN * 60 * 1000:
            raise kd.TakeError(f"program {self.program} starts less than {tool.MIN_AHEAD_MIN} min from now")
        reserves = self.epgs.api("/reserves", type="all", isHalfWidth="true", offset=0, limit=5000)["reserves"]
        if any(r.get("programId") == self.program for r in reserves):
            raise kd.TakeError(f"program {self.program} is already reserved")
        if tool.others_at(reserves, p["startAt"], p["endAt"]) > tool.MAX_OTHERS:
            raise kd.TakeError(f"more than {tool.MAX_OTHERS} other reservations record at that time (conflict)")
        return p

    def check_demo_target(self):
        """Section D before the first write: --rec is test_reserve's stored demo target, unprotected, with the files
        it had at demo-target (GET)."""
        import urllib.error

        tool = kd.dev_tool()
        demo = tool.load_state().get("demo")
        if not demo or demo.get("rec") != self.rec:
            raise kd.TakeError(f"recording {self.rec} is not the demo target stored by test_reserve demo-target")
        try:
            rec = self.epgs.api(f"/recorded/{self.rec}", isHalfWidth="true")
        except urllib.error.HTTPError as e:
            raise kd.TakeError(f"recording {self.rec}: GET failed ({e.code})") from e
        if rec.get("isProtected") or tool._files(rec) != demo["prior"]["files"]:
            raise kd.TakeError(f"recording {self.rec} is protected or has other files than at demo-target")
        return rec

    def open_rec_page(self, max_pages=20):
        """最近の録画: the page listing --rec, following the 次のページ entries from the current page; TakeError when
        the listing ends without it."""
        k = self.k
        for page in range(1, max_pages + 1):
            items = k.listdir(k.label("Container.FolderPath"))
            queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(f.get("file") or "").query) for f in items]
            if any(q.get("action") == ["play"] and q.get("id") == [str(self.rec)] for q in queries):
                return page
            nxt = next((q for q in queries if q.get("action") == ["list"] and q.get("offset")), None)
            if nxt is None:
                raise kd.TakeError(f"recording {self.rec} is not in 最近の録画 (searched {page} pages)")
            k.focus_label("次のページ", max_steps=120, direction="Up")
            k.press("Select")
            k.wait_folder(f"offset={nxt['offset'][0]}")
            k.check(f"recent_page{page + 1}")
        raise kd.TakeError(f"recording {self.rec} not found within {max_pages} pages of 最近の録画")

    def check_cancel_target(self):
        """--cancel-only: the reservation of --program is the one test_reserve adopted (state + GET), else refuse."""
        tool = kd.dev_tool()
        mine = [x for x in tool.load_state().get("reserves", [])
                if x.get("adopted") and x.get("programId") == self.program]
        if not mine:
            raise kd.TakeError(f"--cancel-only: no adopted reservation of program {self.program} in {tool.STATE}")
        r = self.reserve_or_none(mine[-1]["id"])
        if r is None or r.get("programId") != self.program or not tool.same_reserve(mine[-1], r):
            raise kd.TakeError(f"--cancel-only: reservation {mine[-1]['id']} on the server is not the adopted one "
                               f"of program {self.program}")
        return mine[-1]["id"]

    def section_d(self):
        k = self.k
        k.check("root")
        self.dwell(1.0)
        self.open_item("最近の録画", "action=list", "recent")
        self.dwell(1.5)
        self.open_rec_page()
        self.focus_param("id", self.rec)
        k.log("rec_focused")
        if not self.rehearse:
            self.check_demo_target()  # again, right before the first write
        self.dwell(2.0)
        self.context("保護", "ctx_protect")
        self.pending.append(f"recording {self.rec} may be left protected: tools/dev/test_reserve.py demo-restore, "
                            "then demo-audit")
        if self.confirm_yes("保護", "protect"):
            self.toast("protect_toast")
            k.sleep(1.0)  # the addon's Container.Refresh
            k.wait_folder("offset=")
            self.focus_param("id", self.rec)
            k.log("protected")
            self.dwell(1.5)
            self.context("保護を解除", "ctx_unprotect")
            self.confirm_yes("保護を解除", "unprotect")
            self.toast("unprotect_toast")
            k.sleep(1.0)
        k.wait_folder("offset=")
        if not self.rehearse:
            rec = self.epgs.api(f"/recorded/{self.rec}", isHalfWidth="true")
            if rec.get("isProtected"):
                raise kd.TakeError(f"recording {self.rec} is still protected after 保護を解除")
        self.pending.pop()
        self.focus_param("id", self.rec)
        self.context("削除", "ctx_delete")
        k.wait_for("Window.IsActive(yesnodialog)", timeout=15)
        k._no_select = True  # from here until the prompt is gone: never Select
        try:
            k.log("delete_question")
            self.dwell(2.5)
            k.press("Back")
            k.wait_for("!Window.IsActive(yesnodialog)", timeout=10)
        finally:
            k._no_select = False
        k.log("delete_closed")
        self.dwell(1.5)

    def section_e(self):
        k = self.k
        k.log("ja_root")
        self.dwell(0.5)
        k.rpc("Settings.SetSettingValue", setting="locale.language", value="resource.language.en_gb")
        k.log("language_set")
        k.sleep(6.0)
        k.wait_for("Window.IsActive(videos)", timeout=30)
        k.builtin(self.events, f"Addon.OpenSettings({kd.ADDON})")
        k.wait_for("Window.IsActive(addonsettings)", timeout=15)
        k.log("settings")
        self.dwell(1.5)
        k.builtin(self.events, "SetFocus(3)")  # the category list
        k.sleep(0.6)
        k.focus_label("Display", where="control", max_steps=4, exact=True)
        self.dwell(0.8)
        k.press("Right")
        k.focus_label("Genre names", where="control", max_steps=6)
        k.log("genre_setting")
        self.dwell(1.0)
        k.press("Select")
        k.wait_for("Window.IsActive(selectdialog)", timeout=10)
        k.focus_label("English", where="control", max_steps=3, exact=True)
        self.dwell(0.8)
        k.press("Back" if self.rehearse else "Select")
        k.wait_for("!Window.IsActive(selectdialog)", timeout=10)
        k.log("genre_english")
        self.dwell(1.2)
        button = "Cancel" if self.rehearse else "OK"
        k.builtin(self.events, "SetFocus(29)" if self.rehearse else "SetFocus(28)")  # Kodi's settings buttons
        k.sleep(0.6)
        k.focus_label(button, where="control", max_steps=0, exact=True)
        self.dwell(0.6)
        k.press("Select")
        k.wait_for("!Window.IsActive(addonsettings)", timeout=10)
        self.to_root()
        k.check("root_en")
        self.dwell(1.5)
        self.open_item("Browse", "action=browse", "browse_en")
        self.dwell(0.8)
        self.open_item("By genre", "action=genres", "genres_en")
        self.dwell(2.0)
        self.focus_param("genre", 7, max_steps=20)  # アニメ・特撮 / Anime
        k.press("Select")
        k.wait_folder("action=genre&")
        k.check("genre_en")
        self.dwell(1.5)
        self.focus_param("subGenre", 0, max_steps=6)
        self.dwell(1.0)
        k.press("Select")
        k.wait_folder("subGenre")
        k.check("subgenre_en")
        k.press("Down")
        self.dwell(1.0)
        k.press("Info", wait=0.8)
        k.wait_for("Window.IsActive(movieinformation)", timeout=10)
        k.log("details_en")
        self.dwell(4.0)
        k.press("Back")
        self.dwell(0.5)
        if self.rehearse:
            k.rpc("Settings.SetSettingValue", setting="locale.language", value="resource.language.ja_jp")
            k.sleep(6.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("section", choices="ABCDE")
    ap.add_argument("--log", required=True)
    ap.add_argument("--capture", help="clip name: film the section with capture.sh")
    ap.add_argument("--force", action="store_true", help="overwrite an existing tour log / clip of that name")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--program", type=int)
    ap.add_argument("--channel", type=int)
    ap.add_argument("--rec", type=int)
    ap.add_argument("--rehearse", action="store_true", help="no live stream; every yes/no closed with Back")
    ap.add_argument("--cancel-only", action="store_true",
                    help="section C: only cancel the (adopted) reservation made by an interrupted take")
    args = ap.parse_args(argv)
    # every refusal before any ssh, JSON-RPC or EPGStation request
    if args.section in "BC" and not (args.program and args.channel):
        sys.exit("sections B and C need --program and --channel (tools/dev/test_reserve.py pick)")
    if args.section == "D" and not args.rec:
        sys.exit("section D needs --rec (the demo target)")
    check_pace(args.pace)
    if os.path.exists(args.log) and not args.force:
        sys.exit(f"refused: {args.log} exists (a retake: pass --force, or use a new clip name)")
    kd.check_hosts(kd.env_host())
    check_server_url(ssh(f"cat {SETTINGS}"))
    kodi, epgs, guard = kd.build(args.log)
    fails = epgs.preflight_failures(guard)
    if fails:
        sys.exit("refused: " + "; ".join(fails))
    open(args.log, "w").close()  # a retake starts a fresh log
    tour = Tour(kodi, epgs, args.pace, args.program, args.channel, args.rec, args.rehearse)
    tour.cancel_only = args.cancel_only
    try:
        tour.precheck(args.section)
    except kd.TakeError as e:
        sys.exit(f"refused: {e}")
    capture = Capture(args.capture, args.force) if args.capture else None
    started = False
    try:
        tour.prep(args.section)
        if capture:
            kodi.reset_clock()
            capture.start()
            started = True
        kodi.log("start", section=args.section)
        getattr(tour, "section_" + args.section.lower())()
        kodi.log("end")
    except BaseException as e:  # any error or Ctrl-C: Stop, Back, Home, then say what may be left
        kodi.abort_cleanup()
        try:
            kodi.log("abort", reason=repr(e))
        except Exception:  # noqa: BLE001 - the reason is printed below anyway
            pass
        print(f"ABORT: {e!r}", file=sys.stderr)
        for left in tour.pending:
            print(f"cleanup: {left}", file=sys.stderr)
        if args.section == "D":
            print("cleanup: check with tools/dev/test_reserve.py demo-audit (demo-restore if the target is left "
                  "protected)", file=sys.stderr)
        if not isinstance(e, kd.TakeError):
            raise
        sys.exit(2)
    finally:
        if started:
            try:
                capture.stop()
            except Exception as e:  # noqa: BLE001 - never hide the abort reason
                print(f"capture stop failed: {e!r}", file=sys.stderr)
        if tour.events:
            try:
                tour.events.bye()
            except OSError as e:
                print(f"EventServer bye failed: {e!r}", file=sys.stderr)
    if epgs.streams():
        sys.exit(STREAM_LEFT)


if __name__ == "__main__":
    main()
