#!/usr/bin/env python3
"""spotcast: librespot (Spotify Connect) -> MP3 HTTP stream -> Yamaha RX-V671 via UPnP.

Pipeline:
  librespot --backend pipe (raw s16le 44.1k stereo on stdout)
    -> pcm pump (fills gaps with silence so the stream never stalls)
    -> ffmpeg (MP3 320k)
    -> HTTP /stream.mp3 fan-out to connected clients (the receiver)
librespot --onevent posts player events to /event; on "playing" we make sure
the receiver is powered on and playing our stream.
"""
import os, sys, re, time, struct, threading, subprocess, socket, html, logging
import urllib.request, urllib.parse, http.client
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

RECEIVER = os.environ.get("SPOTCAST_RECEIVER", "192.168.1.7")
RECEIVER_UPNP = os.environ.get("SPOTCAST_UPNP_PORT", "49154")
NAME = os.environ.get("SPOTCAST_NAME", "Living Room (RX-V671)")
PORT = int(os.environ.get("SPOTCAST_PORT", "8090"))
MAX_VOL = int(os.environ.get("SPOTCAST_MAX_VOLUME", "60"))  # cap, in receiver % units
BITRATE = os.environ.get("SPOTCAST_BITRATE", "320k")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
log = logging.getLogger("spotcast")

CHUNK_MS = int(os.environ.get("SPOTCAST_CHUNK_MS", "20"))
CHUNK = 44100 * 4 * CHUNK_MS // 1000   # s16le stereo @ 44.1k
# Output format the receiver pulls. Uncompressed LPCM moves ~4.4x more bytes per
# second than 320k MP3, so the receiver's fixed-size buffer holds far less time
# -> much lower latency. "wav" (RX-V671 accepts it), "l16" (RX-V671 rejects), "mp3".
FORMAT = os.environ.get("SPOTCAST_FORMAT", "wav")
SILENCE = b"\x00" * CHUNK


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((RECEIVER, 9))
        return s.getsockname()[0]
    finally:
        s.close()


STREAM_URL = None  # filled at start


# ---------------- audio pipeline ----------------
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []

    def add(self):
        q = []
        ev = threading.Event()
        c = (q, ev)
        with self.lock:
            self.clients.append(c)
        return c

    def remove(self, c):
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)

    def publish(self, data):
        with self.lock:
            for q, ev in self.clients:
                if len(q) < 2000:     # drop if a client is hopelessly behind
                    q.append(data)
                ev.set()

    def count(self):
        with self.lock:
            return len(self.clients)


# Receiver buffer tuning. The RX-V671 settles at a random 0.25-0.8 s buffer per
# connection, with a ~0.3 s floor it refills to. Withholding silent audio for d
# seconds shrinks its buffer by d, so we trim each connection down to TARGET.
BUF_TARGET = float(os.environ.get("SPOTCAST_BUFFER_TARGET", "0.30"))
BUF_TARGET_MAX = 0.60
IDLE_HANDBACK_MIN = float(os.environ.get("SPOTCAST_IDLE_MIN", "20"))
NTFY_TOPIC = os.environ.get("SPOTCAST_NTFY_TOPIC", "")          # e.g. spotcast-xyz
DISCORD_WEBHOOK = os.environ.get("SPOTCAST_DISCORD_WEBHOOK", "")
EVENT_LOG = "/var/lib/spotcast-user/events.jsonl"
UPDATE_STATE = "/var/lib/spotcast-user/update.json"
MAX_DB = float(os.environ.get("SPOTCAST_MAX_DB", "-10"))   # app volume ceiling
sleep_timer = {"until": None}
tune = {"t0": None, "drained": 0.0, "drain_left": 0.0, "last": None}
BUF = {"target": BUF_TARGET}

hub = Hub()        # mp3
pcm_hub = Hub()    # raw big-endian PCM (L16)
wav_hub = Hub()    # little-endian PCM (WAV body)


# Audio allowed to queue inside the Pi before the pump catches up (x CHUNK_MS).
PI_JITTER_CHUNKS = int(os.environ.get("SPOTCAST_JITTER_CHUNKS", "3"))
pi_buf_samples = []


def pi_buffer_ms():
    xs = pi_buf_samples[-500:]
    del pi_buf_samples[:-500]
    return round(sum(xs) / len(xs) / (44100 * 4) * 1000) if xs else 0


activity = {"last_sound": 0.0}


def is_silent(pcm, threshold=48):
    """True if every s16le sample in this chunk is near zero."""
    a = memoryview(pcm).cast("h")
    return max(a) < threshold and -min(a) < threshold


def start_pipeline():
    ff = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
         "-c:a", "libmp3lame", "-b:a", BITRATE, "-f", "mp3", "-flush_packets", "1", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)

    # Soloist (Spotify's official Connect player) plays into a PipeWire null
    # sink; we capture that sink's monitor as raw PCM.
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout
    for _ in range(30):
        if "Server Name" in sh("pactl info"):
            break
        time.sleep(1)
    if "spotcast_sink" not in sh("pactl list short sinks"):
        sh("pactl load-module module-null-sink sink_name=spotcast_sink "
           "rate=44100 channels=2 sink_properties=device.description=spotcast")
    sh("pactl set-default-sink spotcast_sink")
    sh("pactl set-sink-volume spotcast_sink 100%")

    slots.start()

    ls = subprocess.Popen(
        ["parec", "-d", "spotcast_sink.monitor", "--format=s16le", "--rate=44100",
         "--channels=2", "--raw", f"--latency-msec={CHUNK_MS}"],
        stdout=subprocess.PIPE, bufsize=0)

    buf = bytearray()
    buf_lock = threading.Lock()

    def read_librespot():
        while True:
            d = ls.stdout.read(CHUNK)
            if not d:
                log.error("parec exited")
                os._exit(1)
            with buf_lock:
                buf.extend(d)

    def pump():
        # Real-time clocked feeder: every 100 ms push audio, or silence if none.
        nxt = time.monotonic()
        while True:
            with buf_lock:
                if len(buf) >= CHUNK:
                    out = bytes(buf[:CHUNK]); del buf[:CHUNK]
                    if not is_silent(out):
                        activity["last_sound"] = time.time()
                    # if librespot got far ahead (it decodes faster than real time
                    # only briefly), drain faster to avoid growing latency
                    extra = len(buf) > CHUNK * PI_JITTER_CHUNKS
                    pi_buf_samples.append(len(buf))
                else:
                    out = SILENCE; extra = False
            if pcm_hub.count():
                be = bytearray(out)
                be[0::2], be[1::2] = out[1::2], out[0::2]   # s16le -> s16be (L16)
                pcm_hub.publish(bytes(be))
            if wav_hub.count():
                if tune["drain_left"] > 0 and is_silent(out):
                    step = CHUNK_MS / 1000            # withhold silence: buffer shrinks
                    tune["drain_left"] = max(0.0, tune["drain_left"] - step)
                    tune["drained"] += step
                else:
                    wav_hub.publish(out)
            if hub.count():
                try:
                    ff.stdin.write(out)
                except BrokenPipeError:
                    os._exit(1)
            if extra:
                continue
            nxt += CHUNK_MS / 1000
            delay = nxt - time.monotonic()
            if delay < -0.5:          # fell behind (e.g. CPU stall): resync clock
                nxt = time.monotonic()
            time.sleep(max(0, delay))

    def read_ffmpeg():
        while True:
            d = ff.stdout.read(4096)
            if not d:
                log.error("ffmpeg exited")
                os._exit(1)
            hub.publish(d)

    for f in (read_librespot, pump, read_ffmpeg):
        threading.Thread(target=f, daemon=True).start()


# ---------------- multi-account Soloist slots ----------------
import json, shutil

SPARE_NAME = os.environ.get("SPOTCAST_SPARE_NAME", "Living Room - Add me")
# MODE "shared" (default since 2026-11-18): one LAN-visible "Living Room
# (RX-V671)" that anyone on the network can pick; picking it hands it to that
# account (Spotify Connect hand-off, ~0.5 s). Plus one hidden slot for the
# account Alexa plays under, so "Alexa, play X" can still be followed.
# Other linked slots are kept on disk but not started. MODE "accounts" is the
# previous one-slot-per-account + "Add me" behaviour.
MODE = os.environ.get("SPOTCAST_MODE", "shared")
ALEXA_ACCOUNT = os.environ.get("SPOTCAST_ALEXA_ACCOUNT", "")
ALEXA_NAME = os.environ.get("SPOTCAST_ALEXA_NAME", "Living Room (Alexa)")
SHARED_N = 50          # shared slot: dir slots/shared, ws port 9150
MAX_SLOTS = int(os.environ.get("SPOTCAST_MAX_SLOTS", "6"))
SLOT_ROOT = "/var/lib/spotcast-user/slots"
HIDDEN_PROCS = "/sys/fs/cgroup/system.slice/spotcast.service/hidden/cgroup.procs"


# ---------------- show "Add me" only when an unregistered device needs it -----------
# A LAN-discovered Connect device can't be hidden per phone (its mDNS announce is
# multicast and Spotify lists it without contacting it). Instead we watch who is
# *looking*: every open Spotify app sends mDNS queries for _spotify-connect._tcp.
# If one comes from a device that isn't registered (or ignored), the spare runs
# for ADD_WINDOW seconds; otherwise no "Add me" exists at all.
REG_FILE = "/var/lib/spotcast-user/registered.json"
import struct


class DeviceWatch:
    def __init__(self):
        self.pollers = {}       # ip -> last time it hit the spare's zeroconf port
        self.unknown = {}       # mac -> {"ip", "at"} unregistered Spotify apps seen
        self.spare_port = None
        try:
            d = json.load(open(REG_FILE))
        except Exception:
            d = {}
        self.reg = d.get("registered", {})
        self.ignored = d.get("ignored", {})

    def save(self):
        with open(REG_FILE, "w") as f:
            json.dump({"registered": self.reg, "ignored": self.ignored}, f, indent=1)

    @staticmethod
    def mac_of(ip):
        out = subprocess.run(["ip", "neigh", "show", ip], capture_output=True,
                             text=True).stdout.split()
        return out[out.index("lladdr") + 1].lower() if "lladdr" in out else None

    def sniff(self):
        # raw UDP socket (CAP_NET_RAW) sees mDNS queries without binding :5353
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)
        my_ip = lan_ip()
        while True:
            pkt, (src, _) = sock.recvfrom(9000)
            ihl = (pkt[0] & 0x0F) * 4
            if len(pkt) < ihl + 8 or src == my_ip:
                continue
            dport = struct.unpack("!H", pkt[ihl + 2:ihl + 4])[0]
            if dport != 5353:
                continue
            dns = pkt[ihl + 8:]
            if len(dns) < 12 or dns[2] & 0x80:      # skip responses
                continue
            if b"_spotify-connect" not in dns:
                continue
            mac = self.mac_of(src)
            if not mac or mac in self.reg or mac in self.ignored:
                continue
            first = mac not in self.unknown
            self.unknown[mac] = {"ip": src, "at": int(time.time())}
            if first:
                log.info("unregistered Spotify device %s (%s) nearby; showing 'Add me'",
                         mac, src)
            slots.open_add_window(quiet=not first)

    def set_spare(self, pid):
        for _ in range(20):
            out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if f"pid={pid}," in line and "127.0.0.1:" not in line:
                    self.spare_port = int(line.split()[3].rsplit(":", 1)[1])
                    return
            time.sleep(1)

    def watch_pollers(self):
        while True:
            if self.spare_port:
                out = subprocess.run(["ss", "-tnH", "state", "all",
                                      f"( sport = :{self.spare_port} )"],
                                     capture_output=True, text=True).stdout
                for line in out.splitlines():
                    peer = line.split()[-1].rsplit(":", 1)[0].strip("[]")
                    if peer.startswith("::ffff:"):
                        peer = peer[7:]
                    self.pollers[peer] = time.time()
            time.sleep(0.3)

    def register_claim(self, account):
        # the claimer is the device that talked to the spare most recently
        now = time.time()
        recent = sorted(((t, ip) for ip, t in self.pollers.items() if now - t < 15),
                        reverse=True)
        self.pollers.clear()
        if not recent:
            log.info("claim by %s: couldn't tell which device", account[:6] + "...")
            return
        ip = recent[0][1]
        mac = self.mac_of(ip)
        if not mac:
            return
        self.reg[mac] = {"account": account, "ip": ip, "at": int(now)}
        self.unknown.pop(mac, None)
        self.save()
        log.info("registered device %s (%s) for account %s", mac, ip, account[:6] + "...")

    def ignore(self, mac):
        info = self.unknown.pop(mac, {})
        self.ignored[mac] = {"ip": info.get("ip", ""), "at": int(time.time())}
        self.save()

    def forget(self, mac):
        self.reg.pop(mac, None)
        self.ignored.pop(mac, None)
        self.save()


devices = DeviceWatch()


class Slot:
    """One Soloist instance = one Spotify account's view of the receiver."""

    def __init__(self, mgr, n):
        self.mgr, self.n = mgr, n
        self.dir = f"{SLOT_ROOT}/{n}"
        self.port = 9100 + n
        self.proc = None
        self.shared = n == SHARED_N
        if self.shared:
            self.dir = f"{SLOT_ROOT}/shared"
        self._playing = False      # from Soloist: playing AND the active device
        self.sounding = False      # from the mixer: its audio is reaching the receiver
        os.makedirs(self.dir, exist_ok=True)

    # Soloist's active-device flag can stay false while a slot plays (Alexa
    # follow hand-off), so the audio counts too. Writes set the Soloist side.
    @property
    def playing(self):
        return self._playing or self.sounding

    @playing.setter
    def playing(self, v):
        self._playing = bool(v)

    @property
    def paired(self):
        return os.path.exists(f"{self.dir}/paired")

    def account(self):
        """Spotify account id this slot is logged in as (stored at claim time,
        else the most recently used <id>-user dir Soloist keeps)."""
        try:
            a = open(f"{self.dir}/paired").read().strip()
            if a:
                return a
        except OSError:
            pass
        users = f"{self.dir}/settings/Users"
        try:
            ds = [d for d in os.listdir(users) if d.endswith("-user")]
            ds.sort(key=lambda d: os.path.getmtime(f"{users}/{d}"), reverse=True)
            return ds[0][:-5] if ds else ""
        except OSError:
            return ""

    def name(self):
        if self.shared:
            return NAME
        if not self.paired:
            return SPARE_NAME
        if MODE == "shared" and ALEXA_ACCOUNT and self.account() == ALEXA_ACCOUNT:
            return ALEXA_NAME
        return NAME

    def profile(self, refresh=False):
        """{'username', 'display_name', 'image'} for this slot's account.
        Read from Spotify's public profile page (no login needed), cached a day."""
        acct = self.account()
        if not acct:
            return {}
        path = f"{self.dir}/profile.json"
        try:
            prof = json.load(open(path))
            if prof.get("username") == acct and not refresh and \
                    time.time() - prof.get("fetched", 0) < 86400:
                return prof
        except Exception:
            prof = {}
        prof = {"username": acct, "display_name": prof.get("display_name"),
                "image": prof.get("image"), "fetched": time.time()}
        try:
            req = urllib.request.Request(f"https://open.spotify.com/user/{acct}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            page = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
            m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
            i = re.search(r'<meta property="og:image" content="([^"]*)"', page)
            if m:
                prof["display_name"] = html.unescape(m.group(1))
            if i:
                prof["image"] = html.unescape(i.group(1))
        except Exception as e:
            log.info("profile lookup for slot %d failed: %s", self.n, e)
        try:
            json.dump(prof, open(path, "w"))
        except OSError:
            pass
        return prof

    def is_active(self):
        try:
            out = subprocess.run(["soloist", "ctl", "--ws", f"127.0.0.1:{self.port}", "status"],
                                 capture_output=True, text=True, timeout=5).stdout
            self.active = "active device: yes" in out
        except Exception:
            pass
        return getattr(self, "active", False)

    def ctl(self, *args):
        subprocess.run(["soloist", "ctl", "--ws", f"127.0.0.1:{self.port}", *args],
                       capture_output=True, timeout=10)

    def run(self):
        while True:
            log.info("slot %d starting as '%s'", self.n, self.name())
            cmd = ["soloist", "--device-name", self.name(),
                 "--api-key", os.environ["SOLOIST_API_KEY"], "--data-dir", self.dir,
                 "--ws", f"127.0.0.1:{self.port}", "--initial-volume", "100",
                   "--cache-size", os.environ.get("SPOTCAST_CACHE_MB", "300")]
            if self.paired and not self.shared:
                # Linked account: join the "hidden" cgroup *before* exec, so the
                # mDNS socket Soloist opens is covered by spotcast-hide.sh's
                # firewall rule. Fails open (visible) if the cgroup is missing.
                if not os.access(HIDDEN_PROCS, os.W_OK):
                    log.info("slot %d: %s not writable; it will be visible on the LAN",
                             self.n, HIDDEN_PROCS)
                cmd = ["sh", "-c", 'echo $$ > "$0" 2>/dev/null; exec "$@"', HIDDEN_PROCS] + cmd
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
            threading.Thread(target=self.watch, args=(self.proc,), daemon=True).start()
            if not self.paired and not self.shared:
                threading.Thread(target=devices.set_spare, args=(self.proc.pid,),
                                 daemon=True).start()
            rc = self.proc.wait()
            self.playing = False
            if getattr(self, "retired", False):
                shutil.rmtree(self.dir, ignore_errors=True)
                return
            if rc == 10:
                log.error("soloist build expired; run soloist-update")
                alert("soloist-expired", "Spotify Soloist build expired - running update")
                subprocess.Popen(["sudo", "-n", "/usr/local/sbin/soloist-update"])
            if getattr(self, "_restart", False):
                self._restart = False
                continue
            log.info("slot %d exited rc=%s; restarting", self.n, rc)
            time.sleep(5)

    def read_output(self, proc):
        for line in proc.stdout:
            m = re.search(r"client expires in (\d+) days", line)
            if m:
                health["soloist_days_left"] = int(m.group(1))
            if "error" in line.lower() and "SOLOIST_API" not in line:
                log.info("slot %d soloist: %s", self.n, line.strip()[:200])

    def restart(self):
        self._restart = True
        if self.proc:
            self.proc.terminate()

    def watch(self, proc):
        time.sleep(3)
        while proc.poll() is None:
            tr = subprocess.Popen(["soloist", "ctl", "--ws", f"127.0.0.1:{self.port}", "trace"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in tr.stdout:
                try:
                    ev = json.loads(line.split(" ", 1)[1])
                except Exception:
                    continue
                t = ev.get("type")
                if t == "auth_state" and ev.get("logged_in") and self.shared:
                    log.info("shared slot: now %s", (self.account() or "?")[:6] + "...")
                    event("shared-handoff")
                elif t == "auth_state" and ev.get("logged_in") and not self.paired:
                    time.sleep(2)                       # let Soloist write its user dir
                    open(f"{self.dir}/paired", "w").close()
                    acct = self.account()
                    with open(f"{self.dir}/paired", "w") as f:
                        f.write(acct)
                    log.info("slot %d claimed by account %s", self.n, acct[:6] + "...")
                    threading.Thread(target=self.profile, kwargs={"refresh": True},
                                     daemon=True).start()
                    devices.register_claim(acct)
                    self.mgr.drop_duplicates(self, acct)
                    self.mgr.add_until = 0
                    self.mgr.ensure_spare()
                    self.pending_rename = True
                elif t == "auth_state":
                    self.active = bool(ev.get("is_active"))
                    if not self.active:
                        self.playing = False
                elif t == "playback_state":
                    uri = (ev.get("item") or {}).get("uri")
                    if ev.get("status") == "playing" and ev.get("is_active") is False:
                        follow.remote_play(self, uri)
                    # Every device on an account sees that account's playback;
                    # only count it when THIS device is the active output.
                    was = self._playing
                    self.playing = (ev.get("status") == "playing") and self.is_active()
                    if self._playing and not was:
                        self.mgr.took_over(self)
                    if not self.playing and getattr(self, "pending_rename", False):
                        self.pending_rename = False
                        self.restart()   # come back named NAME for its owner
            tr.wait()
            time.sleep(2)


ADD_WINDOW = int(os.environ.get("SPOTCAST_ADD_WINDOW", "600"))
# Default: always keep one unclaimed "Add me" device visible.
# Set SPOTCAST_PERMANENT_SPARE=0 to only show it after tapping "Add a new person".
PERMANENT_SPARE = os.environ.get("SPOTCAST_PERMANENT_SPARE", "0") == "1"


class SlotManager:
    def __init__(self):
        self.slots = []
        self.lock = threading.Lock()
        self.add_until = 0.0

    def open_add_window(self, quiet=False):
        if MODE == "shared":
            return                       # no "Add me" in shared mode
        self.add_until = time.time() + ADD_WINDOW
        if not quiet:
            log.info("add window open for %ds", ADD_WINDOW)
        self.ensure_spare()

    def add_window_left(self):
        if MODE == "shared":
            return 0
        if PERMANENT_SPARE:
            return 10 ** 9
        return max(0, int(self.add_until - time.time()))

    def reap(self):
        # Remove the unclaimed spare once the add window closes, so every
        # account sees exactly one "Living Room" entry in normal use.
        while True:
            time.sleep(10)
            if self.add_window_left() > 0:
                continue
            with self.lock:
                for sl in [x for x in self.slots if not x.paired and not x.shared]:
                    log.info("add window closed; removing spare slot %d", sl.n)
                    sl.retired = True
                    if sl.proc:
                        sl.proc.terminate()
                    self.slots.remove(sl)

    def start(self):
        os.makedirs(SLOT_ROOT, exist_ok=True)
        legacy = "/var/lib/spotcast-user/soloist"   # first, single-account setup
        if os.path.isdir(legacy) and not os.path.exists(f"{SLOT_ROOT}/0"):
            shutil.move(legacy, f"{SLOT_ROOT}/0")
            open(f"{SLOT_ROOT}/0/paired", "w").close()
        ns = sorted(int(d) for d in os.listdir(SLOT_ROOT) if d.isdigit())
        for n in ns:
            if os.path.exists(f"{SLOT_ROOT}/{n}/paired"):
                if MODE == "shared" and ALEXA_ACCOUNT:
                    try:
                        acct = open(f"{SLOT_ROOT}/{n}/paired").read().strip()
                    except OSError:
                        acct = ""
                    if acct != ALEXA_ACCOUNT:
                        log.info("shared mode: slot %d kept on disk, not started", n)
                        continue
                self._spawn(n)
            else:
                shutil.rmtree(f"{SLOT_ROOT}/{n}", ignore_errors=True)
        if MODE == "shared":
            os.makedirs(f"{SLOT_ROOT}/shared", exist_ok=True)
            self._spawn(SHARED_N)
            log.info("shared mode: '%s' open to everyone; Alexa slot: %s", NAME,
                     "yes" if any(x.paired for x in self.slots) else "none")
            threading.Thread(target=lambda: [x.profile() for x in list(self.slots) if x.paired],
                             daemon=True).start()
            return
        seen = {}
        for sl in sorted(self.slots, key=lambda x: os.path.getmtime(f"{x.dir}/paired"),
                         reverse=True):
            a = sl.account()
            if a and a in seen:
                log.info("startup: slot %d duplicates slot %d; removing", sl.n, seen[a])
                self.retire(sl)
            elif a:
                seen[a] = sl.n
        if not self.slots:          # brand-new install: let the first person in
            self.add_until = time.time() + ADD_WINDOW
        self.ensure_spare()
        threading.Thread(target=self.reap, daemon=True).start()
        threading.Thread(target=lambda: [x.profile() for x in list(self.slots) if x.paired],
                         daemon=True).start()
        threading.Thread(target=devices.watch_pollers, daemon=True).start()
        threading.Thread(target=devices.sniff, daemon=True).start()

    def _spawn(self, n):
        sl = Slot(self, n)
        self.slots.append(sl)
        threading.Thread(target=sl.run, daemon=True).start()
        return sl

    def ensure_spare(self):
        if MODE == "shared":
            return
        with self.lock:
            if self.add_window_left() <= 0:
                return
            if any(not s.paired for s in self.slots):
                return
            if len(self.slots) >= MAX_SLOTS:
                log.info("max slots reached; no spare")
                return
            n = max([s.n for s in self.slots], default=-1) + 1
            self._spawn(n)

    def retire(self, sl):
        sl.retired = True
        if sl in self.slots:
            self.slots.remove(sl)
        if sl.proc and sl.proc.poll() is None:
            sl.proc.terminate()
        else:
            shutil.rmtree(sl.dir, ignore_errors=True)

    def drop_duplicates(self, keep, acct):
        """One slot per Spotify account: a fresh login replaces the old slot."""
        if not acct:
            return
        with self.lock:
            for sl in [x for x in self.slots if x is not keep and x.paired]:
                if sl.account() == acct:
                    log.info("account %s re-linked; removing its old slot %d",
                             acct[:6] + "...", sl.n)
                    self.retire(sl)

    current = None   # the one slot whose audio may reach the receiver

    def took_over(self, slot):
        log.info("slot %d started playing; pausing others", slot.n)
        self.current = slot
        threading.Thread(target=enforce_single_output, daemon=True).start()
        for s in self.slots:
            # Only pause a slot that is still the active output for its account;
            # pausing an inactive slot would pause that account on other devices.
            if s is not slot and s.playing and s.is_active():
                s.playing = False
                threading.Thread(target=s.ctl, args=("pause",), daemon=True).start()
        threading.Thread(target=ensure_playing, daemon=True).start()


slots = SlotManager()


# ---------------- receiver control ----------------
# The RX-V671's embedded web server hangs if it gets parallel or rapid-fire
# requests. Every call to it goes through this gate: one at a time, with a
# minimum gap between requests.
RX_GATE = threading.Lock()
RX_MIN_GAP = float(os.environ.get("SPOTCAST_RX_GAP", "0.12"))
_rx_last = [0.0]


rx_stats = {"ok": 0, "fail": 0, "first_fail": None, "last_ok": None, "log": [],
            "streak": 0, "retry_at": 0.0}

# After consecutive failures, don't touch the receiver again until the back-off
# step has passed (2 s, 5 s, 15 s, then 60 s); a frozen network module gets
# quiet time to recover instead of a queue of retries.
RX_BACKOFF = (2, 5, 15, 60)


class ReceiverBackoff(OSError):
    pass


# One kept-open HTTP/1.1 connection per receiver port (YNC :80, UPnP :49154).
# All use is serialised by RX_GATE, so no locking of its own.
_rx_conns = {}


def _rx_send(port, path, data, headers, timeout, method="POST"):
    for attempt in (1, 2):
        conn = _rx_conns.get(port)
        fresh = conn is None
        if fresh:
            conn = _rx_conns[port] = http.client.HTTPConnection(RECEIVER, port, timeout=timeout)
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            body = r.read().decode("utf-8", "replace")
            if r.will_close:
                conn.close(); _rx_conns.pop(port, None)
            if r.status >= 400:
                raise urllib.error.HTTPError(f"http://{RECEIVER}:{port}{path}", r.status,
                                             r.reason, r.headers, None)
            return body, r.headers
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                http.client.CannotSendRequest, http.client.BadStatusLine):
            # the receiver closed an idle kept-open socket: reconnect once
            conn.close(); _rx_conns.pop(port, None)
            if fresh or attempt == 2:
                raise
        except Exception:
            conn.close(); _rx_conns.pop(port, None)
            raise


def rx_request(req, timeout=5, with_headers=False):
    with RX_GATE:
        if time.time() < rx_stats["retry_at"]:
            raise ReceiverBackoff(f"receiver backing off {rx_stats['retry_at'] - time.time():.0f}s")
        wait = RX_MIN_GAP - (time.monotonic() - _rx_last[0])
        if wait > 0:
            time.sleep(wait)
        what = (req.full_url.rsplit("/", 2)[-2] + ":" +
                (req.headers.get("Soapaction") or req.headers.get("SOAPACTION") or
                 (req.data or b"")[:60].decode("utf-8", "replace"))).replace(chr(10), " ")
        try:
            u = urllib.parse.urlsplit(req.full_url)
            body, hdrs = _rx_send(u.port or 80, u.path, req.data, dict(req.header_items()),
                                  timeout, req.get_method())
            rx_stats["streak"], rx_stats["retry_at"] = 0, 0.0
            if rx_stats["first_fail"]:
                log.info("receiver control recovered after %ds down",
                         time.time() - rx_stats["first_fail"])
                event("rx-recovered", down_s=int(time.time() - rx_stats["first_fail"]))
                rx_stats["first_fail"] = None
            rx_stats["ok"] += 1
            rx_stats["last_ok"] = time.time()
            rx_stats["log"] = (rx_stats["log"] + [(round(time.time(), 1), "ok", what[:70])])[-40:]
            return (body, hdrs) if with_headers else body
        except Exception as e:
            rx_stats["fail"] += 1
            if isinstance(e, urllib.error.HTTPError):
                raise                        # it answered: alive, no back-off
            rx_stats["streak"] += 1
            step = RX_BACKOFF[min(rx_stats["streak"], len(RX_BACKOFF)) - 1]
            rx_stats["retry_at"] = time.time() + step
            log.info("receiver request failed (%s); backing off %ds", e, step)
            rx_stats["log"] = (rx_stats["log"] + [(round(time.time(), 1), "FAIL", what[:70])])[-40:]
            if not rx_stats["first_fail"]:
                rx_stats["first_fail"] = time.time()
                # keep the requests that led up to the freeze for diagnosis
                event("rx-down", error=str(e)[:120], last_requests=rx_stats["log"][-15:])
                log.info("receiver control FAILED (%s); last requests logged", e)
            raise
        finally:
            _rx_last[0] = time.monotonic()


def soap(service, action, args):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">{body}</u:{action}>'
           '</s:Body></s:Envelope>').encode()
    req = urllib.request.Request(
        f"http://{RECEIVER}:{RECEIVER_UPNP}/{service}/ctrl", data=env, method="POST",
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"'})
    return rx_request(req)


def ync(xml):
    req = urllib.request.Request(f"http://{RECEIVER}/YamahaRemoteControl/ctrl",
                                 data=xml.encode(), method="POST",
                                 headers={"Content-Type": "text/xml"})
    return rx_request(req)


def power_on():
    invalidate_basic()
    try:
        ync('<YAMAHA_AV cmd="PUT"><Main_Zone><Power_Control><Power>On</Power>'
            '</Power_Control></Main_Zone></YAMAHA_AV>')
    except Exception as e:
        log.info("power on (YNC) failed: %s", e)


health = {
    "started": time.time(), "soloist_days_left": None, "receiver_ok": True,
    "receiver_fail_since": None, "stalls": 0, "reconnects": 0, "last_stall": None,
    "last_playing": time.time(), "handed_back": False,
}
_alert_last = {}


def event(kind, **kw):
    rec = {"t": int(time.time()), "kind": kind, **kw}
    try:
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        if os.path.getsize(EVENT_LOG) > 2_000_000:         # keep the SD card happy
            lines = open(EVENT_LOG).readlines()[-5000:]
            open(EVENT_LOG, "w").writelines(lines)
    except OSError:
        pass


def alert(key, text, every=6 * 3600):
    """Push a notification (ntfy and/or Discord), rate-limited per key."""
    event("alert", key=key, text=text)
    log.info("ALERT %s: %s", key, text)
    now = time.time()
    if now - _alert_last.get(key, 0) < every:
        return
    _alert_last[key] = now
    def send():
        try:
            if NTFY_TOPIC:
                urllib.request.urlopen(urllib.request.Request(
                    f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode(),
                    headers={"Title": "Living Room Speaker", "Tags": "speaker"}), timeout=10)
            if DISCORD_WEBHOOK:
                urllib.request.urlopen(urllib.request.Request(
                    DISCORD_WEBHOOK, data=json.dumps({"content": f"🔊 {text}"}).encode(),
                    headers={"Content-Type": "application/json",
                             "User-Agent": "spotcast"}), timeout=10)
        except Exception as e:
            log.info("alert send failed: %s", e)
    threading.Thread(target=send, daemon=True).start()


PREV_INPUT_FILE = "/var/lib/spotcast-user/previous_input"


def remember_input(inp):
    try:
        if open(PREV_INPUT_FILE).read().strip() == inp:
            return
    except OSError:
        pass
    try:
        open(PREV_INPUT_FILE, "w").write(inp)
    except OSError:
        pass


def previous_input():
    try:
        return open(PREV_INPUT_FILE).read().strip() or None
    except OSError:
        return None


def health_monitor():
    """Receiver reachability, Soloist build expiry, playback stalls."""
    last_pos, last_pos_t = None, time.time()
    while True:
        busy = any(x.playing for x in slots.slots) or time.time() - activity["last_sound"] < 60
        time.sleep(15 if busy else 60)
        # receiver reachable?
        pw, inp = receiver_status()
        if pw == "On" and inp and inp != "PC":
            remember_input(inp)
        if pw is None:
            if health["receiver_fail_since"] is None:
                health["receiver_fail_since"] = time.time()
            elif time.time() - health["receiver_fail_since"] > 300 and health["receiver_ok"]:
                health["receiver_ok"] = False
                alert("receiver-down", "Receiver (RX-V671) unreachable for 5 minutes - "
                      "check its power and network cable")
        else:
            if not health["receiver_ok"]:
                alert("receiver-up", "Receiver is reachable again", every=0)
            health["receiver_ok"], health["receiver_fail_since"] = True, None
        # soloist expiry
        d = health["soloist_days_left"]
        if d is not None and d <= 10:
            alert("soloist-expiring", f"Spotify Soloist build expires in {d} days and the "
                  "weekly auto-update hasn't replaced it yet", every=24 * 3600)
        # stall detection: Spotify playing on a slot, receiver position not advancing
        try:
            if any(x.playing for x in slots.slots) and wav_hub.count():
                pos = position_seconds()
                if pos is not None and pos == last_pos and time.time() - last_pos_t > 14:
                    health["stalls"] += 1
                    health["last_stall"] = int(time.time())
                    event("stall", buffer=tune["last"], target=BUF["target"])
                    log.info("playback stall detected (target %.2fs)", BUF["target"])
                    # back off the trim if stalls follow it
                    if BUF["target"] < BUF_TARGET_MAX:
                        BUF["target"] = round(BUF["target"] + 0.05, 2)
                        log.info("raising buffer target to %.2fs", BUF["target"])
                    if health["stalls"] in (3, 10):
                        alert("stalls", f"{health['stalls']} playback stalls today - "
                              "receiver may be dropping the stream")
                if pos != last_pos:
                    last_pos, last_pos_t = pos, time.time()
            else:
                last_pos, last_pos_t = None, time.time()
        except Exception:
            pass


_mix_lock = threading.Lock()
_uncorked = set()   # Spotify sink inputs that were producing audio last pass


def enforce_single_output():
    """Only the slot that most recently took over may be heard. A slot whose
    account disconnected is no longer 'active', so it isn't paused, but its
    Soloist can keep rendering into the shared sink; mute every Spotify stream
    that doesn't belong to the current slot (unmute it when it takes over).
    Soloist's "active device" flag can lag or stay false while a slot plays
    (seen with Alexa follow), so the audio decides too: the slot whose stream
    most recently started producing sound becomes current."""
    with _mix_lock:
        pids = {}
        for block in sh_out(["pactl", "list", "clients"]).split("Client #")[1:]:
            m = re.search(r'application.process.id = "(\d+)"', block)
            if m:
                pids[block.split(chr(10), 1)[0].strip()] = int(m.group(1))
        by_pid = {sl.proc.pid: sl for sl in list(slots.slots)
                  if sl.proc and sl.proc.poll() is None}
        streams = []
        for block in sh_out(["pactl", "list", "sink-inputs"]).split("Sink Input #")[1:]:
            if 'application.name = "Spotify"' not in block:
                continue
            idx = block.split(chr(10), 1)[0].strip()
            m = re.search(r"Client: (\d+)", block)
            sl = by_pid.get(pids.get(m.group(1) if m else ""))
            streams.append((idx, sl, "Corked: no" in block, block))
        now_uncorked = {idx for idx, _, live, _ in streams if live}
        for idx, sl, live, _ in streams:
            if live and idx not in _uncorked and sl is not None and sl is not slots.current:
                log.info("mixer: slot %d started sounding; it is now the current slot", sl.n)
                slots.current = sl
        _uncorked.clear(); _uncorked.update(now_uncorked)
        cur = slots.current
        for sl in by_pid.values():
            sl.sounding = sl is cur and any(x is sl and live for _, x, live, _ in streams)
        if cur is None or cur.proc is None or cur.proc.poll() is not None:
            return
        for idx, sl, live, block in streams:
            want = sl is not cur
            muted = "Mute: yes" in block
            if want != muted:
                sh_out(["pactl", "set-sink-input-mute", idx, "1" if want else "0"])
                log.info("mixer: %s stream %s (current slot %d)",
                         "muted" if want else "unmuted", idx, cur.n)
                if want:
                    event("muted-other-slot", sink_input=idx, current=cur.n)


def pin_stream_volumes():
    """Soloist ties its PipeWire stream volume to the Spotify volume slider, so a
    slider at 0 (e.g. after Alexa/phone changes) made the receiver silent. The
    receiver is the volume control: keep every Spotify stream at 100%."""
    n = 0
    while True:
        time.sleep(1)
        n += 1
        try:
            enforce_single_output()
            if n % 2:
                continue                     # volume pin every 2 s as before
            out = sh_out(["pactl", "list", "sink-inputs"])
            for block in out.split("Sink Input #")[1:]:
                idx = block.split(chr(10), 1)[0].strip()
                if 'application.name = "Spotify"' not in block:
                    continue
                m = re.search(r"Volume: \S+: (\d+) /", block)
                if m and int(m.group(1)) != 65536:
                    sh_out(["pactl", "set-sink-input-volume", idx, "100%"])
                    log.info("spotify stream volume was %d%%; pinned to 100%%",
                             round(int(m.group(1)) / 655.36))
        except Exception as e:
            log.info("pin_stream_volumes: %s", e)


STREAM_IDLE_STOP_S = int(os.environ.get("SPOTCAST_STREAM_IDLE_STOP", "120"))


def stream_rest():
    """Don't keep an endless (silent) stream open to the receiver: its 2011
    firmware has frozen its network module after long sessions. After
    STREAM_IDLE_STOP_S of silence, Stop the transport; any new sound restarts
    it (ensure_playing), costing ~0.5 s on the first song."""
    while True:
        time.sleep(15)
        try:
            if activity["last_sound"] == 0:
                activity["last_sound"] = time.time()   # just started: no history yet
            quiet = time.time() - activity["last_sound"]
            if any(x.playing for x in slots.slots) or quiet < STREAM_IDLE_STOP_S:
                continue
            if wav_hub.count() == 0:
                continue                         # receiver isn't pulling anyway
            soap("AVTransport", "Stop", [("InstanceID", 0)])
            _live_cache["t"] = 0.0
            log.info("stream rested after %ds of silence", int(quiet))
            event("stream-rest")
        except Exception as e:
            log.info("stream_rest: %s", e)


def idle_handback():
    """After IDLE_HANDBACK_MIN minutes with nothing playing, give the receiver back:
    restore the input it was on (or standby if we woke it)."""
    while True:
        time.sleep(30)
        if any(x.playing for x in slots.slots) or time.time() - activity["last_sound"] < 30:
            health["last_playing"], health["handed_back"] = time.time(), False
            continue
        if health["handed_back"] or not prev_receiver.get("saved"):
            continue
        if health["last_playing"] and \
                time.time() - health["last_playing"] < IDLE_HANDBACK_MIN * 60:
            continue
        pw, inp = receiver_status()
        if pw != "On" or inp != "PC":
            health["handed_back"] = True             # someone already changed it
            prev_receiver.clear()
            continue
        try:
            soap("AVTransport", "Stop", [("InstanceID", 0)])
            if prev_receiver.get("power") != "On":
                ync('<YAMAHA_AV cmd="PUT"><Main_Zone><Power_Control><Power>Standby'
                    '</Power></Power_Control></Main_Zone></YAMAHA_AV>')
                log.info("idle: receiver back to standby")
            elif prev_receiver.get("input") and prev_receiver["input"] != "PC":
                ync('<YAMAHA_AV cmd="PUT"><Main_Zone><Input><Input_Sel>'
                    f'{prev_receiver["input"]}</Input_Sel></Input></Main_Zone></YAMAHA_AV>')
                log.info("idle: receiver back to %s", prev_receiver["input"])
            event("handback", to=prev_receiver.get("input"), power=prev_receiver.get("power"))
        except Exception as e:
            log.info("handback failed: %s", e)
        health["handed_back"] = True
        prev_receiver.clear()
        follow.off("idle")


prev_receiver = {}


_basic = {"t": 0.0, "xml": None}


def basic_status(max_age=4.0):
    """Main_Zone Basic_Status, shared by everything for max_age seconds."""
    if time.time() - _basic["t"] < max_age and _basic["xml"] is not None:
        return _basic["xml"]
    try:
        _basic["xml"] = ync('<YAMAHA_AV cmd="GET"><Main_Zone><Basic_Status>GetParam'
                            '</Basic_Status></Main_Zone></YAMAHA_AV>')
    except Exception:
        _basic["xml"] = None
    _basic["t"] = time.time()
    return _basic["xml"]


def invalidate_basic():
    _basic["t"] = 0.0


def receiver_volume():
    try:
        r = basic_status()
        if r is None:
            return None, None
        m = re.search(r"<Volume><Lvl><Val>(-?\d+)</Val>", r)
        mute = re.search(r"<Mute>(\w+)</Mute>", r)
        return (int(m.group(1)) / 10 if m else None,
                (mute.group(1) == "On") if mute else None)
    except Exception:
        return None, None


def set_receiver_volume(db):
    invalidate_basic()
    db = max(-80.5, min(MAX_DB, round(float(db) * 2) / 2))   # 0.5 dB steps
    ync('<YAMAHA_AV cmd="PUT"><Main_Zone><Volume><Lvl><Val>%d</Val><Exp>1</Exp>'
        '<Unit>dB</Unit></Lvl></Volume></Main_Zone></YAMAHA_AV>' % int(db * 10))
    return db


def hand_back_now():
    health["last_playing"] = 0
    health["handed_back"] = False
    for x in slots.slots:
        if x.is_active():
            x.ctl("pause")
            x.playing = False
    if not prev_receiver.get("saved"):
        prev_receiver.update(power="On", input=None, saved=True)


def sleep_watch():
    while True:
        time.sleep(5)
        u = sleep_timer["until"]
        if u and time.time() >= u:
            sleep_timer["until"] = None
            log.info("sleep timer: pausing and handing receiver back")
            follow.off("sleep timer")
            hand_back_now()
            prev_receiver.update(power="Standby", saved=True)   # sleep => standby
            health["last_playing"] = 0


def update_info():
    try:
        d = json.load(open(UPDATE_STATE))
    except Exception:
        d = {}
    d["days_left"] = health["soloist_days_left"]
    return d


def queue(n=5):
    for x in slots.slots:
        if x.playing:
            try:
                out = subprocess.run(["soloist", "ctl", "--ws", f"127.0.0.1:{x.port}",
                                      "queue", str(n)], capture_output=True, text=True,
                                     timeout=4).stdout
                return [l.strip() for l in out.splitlines() if l.strip()][:n]
            except Exception:
                return []
    return []


def sh_out(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def sound_watch():
    """Any source producing sound -> make sure the receiver plays our stream."""
    while True:
        time.sleep(1)
        try:
            if time.time() - activity["last_sound"] < 1.0:
                health["last_playing"], health["handed_back"] = time.time(), False
                if not stream_live():
                    threading.Thread(target=ensure_playing, daemon=True).start()
                    time.sleep(3)
        except Exception as e:
            log.info("sound_watch error: %s", e)


_live_cache = {"t": 0.0, "v": False}


def stream_live():
    if time.time() - _live_cache["t"] < 5:
        return _live_cache["v"]
    st, uri = transport_state()
    _live_cache.update(t=time.time(), v=(st == "PLAYING" and uri == STREAM_URL))
    return _live_cache["v"]


class Follow:
    """'Alexa, play X' -> receiver, for any linked Spotify account.

    Alexa plays Spotify on the Echo under the speaker's own account. Every
    linked account has a Soloist slot here that sees that playback, and
    `soloist ctl activate` moves it to the receiver (a Spotify Connect hand-off).

      arm()   - 'Alexa, turn on the receiver': for ARM_S seconds, the first
                account that starts playing elsewhere becomes the owner.
      owner   - its *new* requests (track changes elsewhere) keep following;
                moving the same track to headphones is left alone.
      off()   - 'turn off the receiver', idle hand-back, or sleep timer.
    """
    ARM_S = 90

    def __init__(self):
        self.armed_until = 0.0
        self.owner = None          # account id
        self.last_grab = {}        # account -> uri we last pulled
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return self.owner is not None or time.time() < self.armed_until

    def arm(self):
        self.armed_until = time.time() + self.ARM_S
        log.info("follow: armed for %ds", self.ARM_S)
        event("follow-armed")
        # "Alexa, play X" said just *before* "turn on the receiver": take a track
        # that started on a linked account in the last RECENT_MS.
        threading.Thread(target=self._grab_recent, daemon=True).start()

    RECENT_MS = 45000

    def _grab_recent(self):
        best = None
        for sl in list(slots.slots):
            if not sl.paired:
                continue
            try:
                out = subprocess.run(["soloist", "ctl", "--ws", f"127.0.0.1:{sl.port}",
                                      "--json", "now"], capture_output=True, text=True,
                                     timeout=5).stdout
                ev = json.loads(out)
            except Exception:
                continue
            pos = (ev.get("position") or {}).get("position_ms", 10 ** 9)
            if ev.get("status") == "playing" and ev.get("is_active") is False                     and pos < self.RECENT_MS and (best is None or pos < best[0]):
                best = (pos, sl, (ev.get("item") or {}).get("uri"))
        if best:
            self.remote_play(best[1], best[2])

    def off(self, why=""):
        if self.enabled:
            log.info("follow: off %s", why)
            event("follow-off", why=why)
        self.armed_until, self.owner = 0.0, None

    def remote_play(self, slot, uri):
        acct = slot.account()
        with self.lock:
            if not acct or not uri:
                return
            if self.owner is None:
                if time.time() >= self.armed_until:
                    return
            elif acct != self.owner:
                return                            # someone else's own listening
            if self.last_grab.get(acct) == uri:
                return                            # same track moved away on purpose
            self.last_grab[acct] = uri
            self.owner, self.armed_until = acct, 0.0
        log.info("follow: pulling %s's playback to the receiver (slot %d)",
                 acct[:6] + "...", slot.n)
        event("follow-grab", slot=slot.n)
        threading.Thread(target=slot.ctl, args=("activate",), daemon=True).start()

    def status(self):
        return {"enabled": self.enabled,
                "armed_s": max(0, int(self.armed_until - time.time())),
                "owner": (self.owner or "")[:6] or None}


follow = Follow()


def receiver_status():
    """(power, input) from the YNC API, or (None, None)."""
    try:
        r = basic_status()
        if r is None:
            return None, None
        pw = r.split("<Power>")[1].split("<")[0] if "<Power>" in r else None
        inp = r.split("<Input_Sel>")[1].split("<")[0] if "<Input_Sel>" in r else None
        return pw, inp
    except Exception:
        return None, None


def position_seconds():
    r = soap("AVTransport", "GetPositionInfo", [("InstanceID", 0)])
    m = re.search(r"<RelTime>(\d+):(\d+):(\d+)", r)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def measure_buffer(seconds=7):
    """Receiver buffer = audio sent since connect - receiver's played position,
    sampled exactly at each RelTime tick (RelTime has 1 s resolution)."""
    t0 = tune["t0"]
    if t0 is None:
        return None
    ests, last = [], position_seconds()
    end = time.time() + seconds
    while time.time() < end:
        if tune["t0"] != t0:
            return None                    # reconnected mid-measure
        rt = position_seconds()
        if rt is not None and last is not None and rt > last:
            ests.append((time.time() - t0 - tune["drained"]) - rt)
        last = rt
        time.sleep(0.2)                  # gentle on the receiver (was 30 ms)
    ests = ests[1:] or ests
    # each tick is detected up to one poll interval late, which only ever
    # inflates the estimate, so the smallest sample is the most accurate
    return min(ests) if ests else None


def buffer_tuner():
    last_t0, next_check = None, 0
    while True:
        time.sleep(2)
        try:
            if tune["t0"] is None or wav_hub.count() == 0:
                continue
            if tune["t0"] != last_t0:              # new connection: let it settle
                last_t0, next_check = tune["t0"], time.time() + 3
            if time.time() < next_check or tune["drain_left"] > 0:
                continue
            b = measure_buffer()
            if b is None:
                continue
            tune["last"] = round(b, 2)
            if b > BUF["target"] + 0.06:
                tune["drain_left"] = b - BUF["target"]
                log.info("receiver buffer %.2fs -> trimming %.2fs of silence", b,
                         tune["drain_left"])
                next_check = time.time() + 20
            else:
                next_check = time.time() + 300
        except Exception as e:
            log.info("tuner error: %s", e)
            time.sleep(10)


def watchdog():
    """While Spotify is playing on any slot, keep the receiver on our stream.
    Leaves it alone if someone deliberately switched to another input."""
    while True:
        upnp["changed"].wait(8 if not upnp_subscribed() else 30)
        upnp["changed"].clear()
        try:
            if not any(x.playing for x in slots.slots):
                continue
            refresh_stream_url()
            state, uri = transport_state()
            if state == "PLAYING" and uri == STREAM_URL:
                continue
            pw, inp = receiver_status()
            if pw == "On" and inp not in (None, "PC"):
                continue                     # user picked another source
            log.info("watchdog: receiver %s/%s on %s; restoring stream", pw, inp, state)
            ensure_playing()
        except Exception as e:
            log.info("watchdog error: %s", e)


# ---------------- UPnP eventing (GENA) ----------------
# The receiver pushes AVTransport changes to /upnp/event, so transport_state()
# answers from this cache instead of two SOAP polls every few seconds. If the
# subscription lapses, it falls back to polling.
UPNP_SUB_S = int(os.environ.get("SPOTCAST_UPNP_SUB_S", "1800"))
upnp = {"sid": None, "until": 0.0, "callback": None, "state": None, "uri": None,
        "t": 0.0, "changed": threading.Event()}


def upnp_subscribed():
    return upnp["sid"] is not None and time.time() < upnp["until"]


def upnp_notify(body):
    x = html.unescape(body)                 # LastChange is XML escaped once
    st = re.search(r'<TransportState val="([^"]*)"', x)
    uri = re.search(r'<AVTransportURI val="([^"]*)"', x)
    if st:
        upnp["state"] = st.group(1)
    if uri:
        upnp["uri"] = html.unescape(uri.group(1))
    if st or uri:
        upnp["t"] = time.time()
        _live_cache["t"] = 0.0
        upnp["changed"].set()


def _upnp_subscribe():
    cb = f"<http://{lan_ip()}:{PORT}/upnp/event>"
    renew = upnp["sid"] is not None and cb == upnp["callback"]
    hdrs = ({"SID": upnp["sid"]} if renew else {"CALLBACK": cb, "NT": "upnp:event"})
    hdrs["TIMEOUT"] = f"Second-{UPNP_SUB_S}"
    req = urllib.request.Request(f"http://{RECEIVER}:{RECEIVER_UPNP}/AVTransport/event",
                                 method="SUBSCRIBE", headers=hdrs)
    _, h = rx_request(req, with_headers=True)
    sid = h.get("SID") or upnp["sid"]
    m = re.search(r"Second-(\d+)", h.get("TIMEOUT") or "")
    secs = int(m.group(1)) if m else UPNP_SUB_S
    if not renew:
        log.info("upnp: subscribed to AVTransport events (%ds)", secs)
        upnp["state"] = upnp["uri"] = None   # initial NOTIFY carries full state
    upnp.update(sid=sid, callback=cb, until=time.time() + secs)
    return secs


def upnp_events():
    while True:
        try:
            secs = _upnp_subscribe()
            time.sleep(max(30, secs * 0.8))
        except ReceiverBackoff:
            time.sleep(max(1, rx_stats["retry_at"] - time.time()))
        except Exception as e:
            if upnp["sid"]:
                log.info("upnp: subscription lost (%s); resubscribing", e)
            upnp.update(sid=None, until=0.0)
            time.sleep(RX_BACKOFF[min(rx_stats["streak"], len(RX_BACKOFF)) - 1]
                       if rx_stats["streak"] else 5)


def transport_state():
    if upnp_subscribed() and upnp["state"] is not None and upnp["uri"] is not None:
        return upnp["state"], upnp["uri"]
    try:
        r = soap("AVTransport", "GetTransportInfo", [("InstanceID", 0)])
        s = r.split("<CurrentTransportState>")[1].split("<")[0]
        m = soap("AVTransport", "GetMediaInfo", [("InstanceID", 0)])
        uri = html.unescape(m.split("<CurrentURI>")[1].split("<")[0]) if "<CurrentURI>" in m else ""
        return s, uri
    except Exception as e:
        log.info("transport query failed: %s", e)
        return None, ""


ensure_lock = threading.Lock()


def refresh_stream_url():
    """Re-derive our LAN address (it changes if the Pi moves Wi-Fi <-> Ethernet)."""
    global STREAM_URL
    try:
        url = f"http://{lan_ip()}:{PORT}/stream.{FORMAT}"
        if url != STREAM_URL:
            log.info("stream URL changed: %s -> %s", STREAM_URL, url)
            STREAM_URL = url
    except OSError:
        pass


def ensure_playing():
    refresh_stream_url()
    with ensure_lock:
        state, uri = transport_state()
        if state == "PLAYING" and uri == STREAM_URL:
            return
        log.info("receiver state=%s uri=%s -> starting stream", state, uri)
        pw, inp = receiver_status()
        if (pw != "On" or inp != "PC") and pw is not None:
            prev_receiver.update(power=pw, input=inp, saved=True)
            if inp and inp != "PC":
                remember_input(inp)
            log.info("taking over receiver (was power=%s input=%s)", pw, inp)
        try:  # the RX-V671 renderer only accepts Play while on its "PC" (DLNA) input
            if pw != "On":
                power_on()
                time.sleep(4)            # amp boot
            if inp != "PC":
                ync('<YAMAHA_AV cmd="PUT"><Main_Zone><Input><Input_Sel>PC</Input_Sel>'
                    '</Input></Main_Zone></YAMAHA_AV>')
                time.sleep(1.5)
        except Exception as e:
            log.info("power/input switch failed: %s", e)
        didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                '<item id="spotcast" parentID="0" restricted="1">'
                f'<dc:title>Spotify</dc:title><dc:creator>{html.escape(NAME)}</dc:creator>'
                '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
                f'<res protocolInfo="http-get:*:audio/mpeg:*">{STREAM_URL}</res>'
                '</item></DIDL-Lite>')
        for attempt in range(len(RX_BACKOFF) + 1):
            try:
                soap("AVTransport", "SetAVTransportURI",
                     [("InstanceID", 0), ("CurrentURI", html.escape(STREAM_URL)),
                      ("CurrentURIMetaData", "")])
                soap("AVTransport", "Play", [("InstanceID", 0), ("Speed", 1)])
                log.info("stream started on receiver")
                return
            except Exception as e:
                log.info("start attempt %d failed: %s", attempt + 1, e)
                if attempt < len(RX_BACKOFF):  # receiver may still be booting
                    time.sleep(max(RX_BACKOFF[attempt], rx_stats["retry_at"] - time.time()))


def set_volume(spotify_vol):
    # librespot VOLUME is 0..65535
    pct = round(int(spotify_vol) / 65535 * MAX_VOL)
    try:
        soap("RenderingControl", "SetVolume",
             [("InstanceID", 0), ("Channel", "Master"), ("DesiredVolume", pct)])
        log.info("volume -> %d", pct)
    except Exception as e:
        log.info("volume failed: %s", e)


# ---------------- HTTP ----------------
def now_playing():
    for x in slots.slots:
        if x.playing:
            try:
                out = subprocess.run(["soloist", "ctl", "--ws", f"127.0.0.1:{x.port}",
                                      "--json", "now"], capture_output=True, text=True,
                                     timeout=4).stdout
                ev = json.loads(out)
                item = ev.get("item", {})
                dec = item.get("decorations", {})
                name = dec.get("identity", {}).get("name")
                covers = dec.get("visual_identity", {}).get("cover", [])
                art = next((c["url"] for c in covers if c.get("size") == "default"),
                           covers[0]["url"] if covers else None)
                artists = [a.get("decorations", {}).get("identity", {}).get("name")
                           for a in item.get("artists", []) if isinstance(a, dict)]
                return {"title": name, "artists": [a for a in artists if a],
                        "art": art, "uri": item.get("uri")}
            except Exception:
                return {}
    return None


def accounts_list():
    out = []
    owner = follow.owner
    for sl in sorted(slots.slots, key=lambda x: x.n):
        if not sl.paired:
            continue
        prof = sl.profile()
        acct = sl.account()
        devs = [{"mac": m, "ip": v.get("ip")} for m, v in devices.reg.items()
                if v.get("account") == acct]
        out.append({
            "slot": sl.n,
            "username": acct,
            "display_name": prof.get("display_name") or acct,
            "image": prof.get("image"),
            "on_receiver": bool(sl.playing),
            "follow_owner": bool(owner and owner == acct),
            "devices": devs,
        })
    return out


def api_status():
    pw, inp = receiver_status()
    vol, mute = receiver_volume()
    sl = sleep_timer["until"]
    return {
        "receiver": {"power": pw, "input": inp, "reachable": health["receiver_ok"],
                     "volume_db": vol, "mute": mute, "max_db": MAX_DB},
        "queue": queue(),
        "follow": follow.status(),
        "source": ("spotify" if any(x.playing for x in slots.slots) else
                   "audio" if time.time() - activity["last_sound"] < 2 else None),
        "sleep_s": max(0, int(sl - time.time())) if sl else 0,
        "update": update_info(),
        "idle_handback_min": IDLE_HANDBACK_MIN,
        "previous_input": previous_input(),
        "playing": any(x.playing for x in slots.slots),
        "now_playing": now_playing(),
        "accounts": sum(1 for x in slots.slots if x.paired),
        "accounts_list": accounts_list(),
        "add_me_visible_s": slots.add_window_left(),
        "devices": {"registered": devices.reg, "ignored": devices.ignored,
                    "unknown": devices.unknown},
        "latency": {"receiver_buffer_s": tune["last"], "target_s": BUF["target"],
                    "pi_queue_ms": pi_buffer_ms()},
        "health": {k: health[k] for k in ("stalls", "reconnects", "last_stall",
                                          "soloist_days_left", "started")},
        "stream_url": STREAM_URL,
        "upnp_events": {"subscribed": upnp_subscribed(), "state": upnp["state"],
                        "age_s": int(time.time() - upnp["t"]) if upnp["t"] else None},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    FORMATS = {
        "/stream.mp3": ("audio/mpeg", "DLNA.ORG_PN=MP3"),
        "/stream.l16": ("audio/L16;rate=44100;channels=2", "DLNA.ORG_PN=LPCM"),
        "/stream.wav": ("audio/wav", ""),
    }

    def api(self, p):
        qs = urllib.parse.parse_qs(p.query)
        arg = lambda k: (qs.get(k) or [""])[0]
        code, out = 200, None
        if p.path.startswith("/api/receiver/"):
            invalidate_basic()               # state is about to change
        try:
            if p.path == "/api/status":
                out = api_status()
            elif p.path == "/api/add":
                slots.open_add_window(); out = {"ok": True}
            elif p.path == "/api/ignore":
                devices.ignore(arg("mac")); out = {"ok": True}
            elif p.path == "/api/forget":
                devices.forget(arg("mac")); out = {"ok": True}
            elif p.path == "/api/receiver/power":
                v = "On" if arg("on") in ("1", "true", "on") else "Standby"
                ync(f'<YAMAHA_AV cmd="PUT"><Main_Zone><Power_Control><Power>{v}'
                    '</Power></Power_Control></Main_Zone></YAMAHA_AV>'); out = {"ok": True}
            elif p.path == "/api/receiver/input":
                v = html.escape(arg("input"))
                ync(f'<YAMAHA_AV cmd="PUT"><Main_Zone><Input><Input_Sel>{v}</Input_Sel>'
                    '</Input></Main_Zone></YAMAHA_AV>'); out = {"ok": True}
            elif p.path == "/api/receiver/volume":
                out = {"ok": True, "db": set_receiver_volume(arg("db"))}
            elif p.path == "/api/receiver/mute":
                v = "On" if arg("on") in ("1", "true", "on") else "Off"
                ync(f'<YAMAHA_AV cmd="PUT"><Main_Zone><Volume><Mute>{v}</Mute>'
                    '</Volume></Main_Zone></YAMAHA_AV>'); out = {"ok": True}
            elif p.path == "/api/sleep":
                m = float(arg("min") or 0)
                sleep_timer["until"] = time.time() + m * 60 if m > 0 else None
                out = {"ok": True}
            elif p.path == "/api/account/remove":
                n = int(arg("slot"))
                sl = next((x for x in slots.slots if x.n == n and x.paired), None)
                if not sl:
                    raise ValueError("no such account slot")
                acct = sl.account()
                with slots.lock:
                    slots.retire(sl)
                for m in [m for m, v in devices.reg.items() if v.get("account") == acct]:
                    devices.forget(m)
                log.info("account %s unlinked (slot %d)", acct[:6] + "...", n)
                event("account-removed", slot=n)
                slots.ensure_spare()
                out = {"ok": True}
            elif p.path == "/api/follow":
                if arg("on") in ("1", "true", "on"):
                    follow.arm()
                else:
                    follow.off("api")
                out = follow.status()
            elif p.path == "/api/handback":
                hand_back_now(); out = {"ok": True}
            elif p.path == "/api/update":
                subprocess.Popen(["sudo", "-n", "/usr/local/sbin/soloist-update"])
                out = {"ok": True}
            elif p.path == "/api/control":
                cmd = arg("cmd")
                if cmd not in ("pause", "play", "next", "prev"):
                    raise ValueError("bad cmd")
                for x in slots.slots:
                    if x.is_active():
                        x.ctl(cmd)
                out = {"ok": True}
            else:
                code, out = 404, {"error": "not found"}
        except Exception as e:
            code, out = 500, {"error": str(e)}
        body = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_NOTIFY(self):
        if urllib.parse.urlparse(self.path).path == "/upnp/event" and                 self.client_address[0] == RECEIVER:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            # the initial NOTIFY can beat the SUBSCRIBE response, so don't
            # require the SID to be recorded yet; the source IP is checked above
            upnp_notify(body)
            self.send_response(200)
        else:
            self.send_response(412)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path in self.FORMATS:
            self._stream_headers(path)
        else:
            self.send_response(404); self.end_headers()

    def _stream_headers(self, path="/stream.mp3"):
        ctype, pn = self.FORMATS[path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("transferMode.dlna.org", "Streaming")
        feats = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
        self.send_header("contentFeatures.dlna.org", f"{pn};{feats}" if pn else feats)
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path in self.FORMATS:
            self._stream_headers(p.path)
            if p.path == "/stream.wav":
                # streaming WAV header with max sizes (length unknown)
                hdr = (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " +
                       struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16) +
                       b"data" + struct.pack("<I", 0xFFFFFFFF))
                self.wfile.write(hdr)
            src = hub if p.path == "/stream.mp3" else pcm_hub
            if p.path == "/stream.wav":
                src = wav_hub
                tune.update(t0=time.time(), drained=0.0, drain_left=0.0)
                health["reconnects"] += 1
                event("connect", ip=self.client_address[0])
            c = src.add()
            q, ev = c
            log.info("client connected %s (%d total)", self.client_address[0], hub.count())
            try:
                while True:
                    ev.wait(5); ev.clear()
                    while q:
                        self.wfile.write(q.pop(0))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                src.remove(c)
                log.info("client left %s", self.client_address[0])
        elif p.path == "/event":
            qs = urllib.parse.parse_qs(p.query)
            e = qs.get("e", [""])[0]
            if e in ("playing", "started", "track_changed", "session_connected"):
                if e == "playing":
                    threading.Thread(target=ensure_playing, daemon=True).start()
            elif e == "volume_changed" and qs.get("v"):
                threading.Thread(target=set_volume, args=(qs["v"][0],), daemon=True).start()
            self.send_response(204); self.end_headers()
        elif p.path in ("/", "/add", "/ignore", "/forget"):
            qs = urllib.parse.parse_qs(p.query)
            if p.path == "/add":
                slots.open_add_window()
            elif p.path == "/ignore" and qs.get("mac"):
                devices.ignore(qs["mac"][0])
            elif p.path == "/forget" and qs.get("mac"):
                devices.forget(qs["mac"][0])
            if p.path != "/":
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
                return
            left = slots.add_window_left()
            people = sum(1 for x in slots.slots if x.paired)
            playing = any(x.playing for x in slots.slots)

            def rows(d, action, label):
                return "".join(
                    f"<li><code>{m}</code> {v.get('ip', '')} "
                    f"<a href='/{action}?mac={m}'>{label}</a></li>"
                    for m, v in d.items()) or "<li>none</li>"
            state = (f"<p class=on>'Add me' is visible for {left // 60}:{left % 60:02d}.</p>"
                     if left else
                     "<p>'Add me' is hidden (no unregistered devices nearby).</p>")
            plural = "s" if people != 1 else ""
            now_txt = "playing now" if playing else "idle"
            body = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Living Room Speaker</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;
padding:24px 16px;max-width:520px;margin:auto}}button{{width:100%;padding:16px;font-size:17px;
font-weight:600;border:0;border-radius:12px;background:#1db954;color:#000}}
p,li{{color:#aaa;line-height:1.5}}.on{{color:#1db954}}a{{color:#1db954}}
code{{color:#ddd}}</style>
<h1>Living Room Speaker</h1>
<p>{people} Spotify account{plural} linked &middot; {now_txt}</p>{state}
<form action=/add><button>Show 'Add me' for 10 minutes</button></form>
<p>For linking another account from a device that's already registered.</p>
<h3>Unregistered Spotify devices nearby</h3>
<p>These make 'Add me' appear. Ignore TVs and speakers that aren't people.</p>
<ul>{rows(devices.unknown, 'ignore', 'ignore')}</ul>
<h3>Registered devices</h3><ul>{rows(devices.reg, 'forget', 'forget')}</ul>
<h3>Ignored</h3><ul>{rows(devices.ignored, 'forget', 'un-ignore')}</ul>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif p.path.startswith("/api/"):
            self.api(p)
        elif p.path == "/status":
            body = (f"clients mp3={hub.count()} l16={pcm_hub.count()} "
                    f"wav={wav_hub.count()} url={STREAM_URL} "
                    f"pi_queue={pi_buffer_ms()}ms buffer={tune['last']}s drained={tune['drained']:.2f}s "
                    f"pending={tune['drain_left']:.2f}s\n").encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


def main():
    global STREAM_URL
    while True:
        try:
            STREAM_URL = f"http://{lan_ip()}:{PORT}/stream.{FORMAT}"
            break
        except OSError:
            time.sleep(3)
    log.info("spotcast '%s' -> %s, stream %s", NAME, RECEIVER, STREAM_URL)
    os.makedirs("/var/lib/spotcast", exist_ok=True)
    start_pipeline()
    threading.Thread(target=upnp_events, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=buffer_tuner, daemon=True).start()
    threading.Thread(target=health_monitor, daemon=True).start()
    threading.Thread(target=idle_handback, daemon=True).start()
    threading.Thread(target=stream_rest, daemon=True).start()
    threading.Thread(target=pin_stream_volumes, daemon=True).start()
    threading.Thread(target=sleep_watch, daemon=True).start()
    threading.Thread(target=sound_watch, daemon=True).start()
    try:
        up = float(open("/proc/uptime").read().split()[0])
        if up < 600:
            alert("pi-reboot", f"Pi rebooted {int(up // 60)} min ago; speaker is back online",
                  every=0)
    except OSError:
        pass
    event("start")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
