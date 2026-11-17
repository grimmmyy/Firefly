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
import urllib.request, urllib.parse
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
tune = {"t0": None, "drained": 0.0, "drain_left": 0.0, "last": None}

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
MAX_SLOTS = int(os.environ.get("SPOTCAST_MAX_SLOTS", "6"))
SLOT_ROOT = "/var/lib/spotcast-user/slots"


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
        self.playing = False
        os.makedirs(self.dir, exist_ok=True)

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
        return NAME if self.paired else SPARE_NAME

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
            self.proc = subprocess.Popen(
                ["soloist", "--device-name", self.name(),
                 "--api-key", os.environ["SOLOIST_API_KEY"], "--data-dir", self.dir,
                 "--ws", f"127.0.0.1:{self.port}", "--initial-volume", "100"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            threading.Thread(target=self.watch, args=(self.proc,), daemon=True).start()
            if not self.paired:
                threading.Thread(target=devices.set_spare, args=(self.proc.pid,),
                                 daemon=True).start()
            rc = self.proc.wait()
            self.playing = False
            if getattr(self, "retired", False):
                shutil.rmtree(self.dir, ignore_errors=True)
                return
            if rc == 10:
                log.error("soloist build expired; run soloist-update")
            if getattr(self, "_restart", False):
                self._restart = False
                continue
            log.info("slot %d exited rc=%s; restarting", self.n, rc)
            time.sleep(5)

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
                if t == "auth_state" and ev.get("logged_in") and not self.paired:
                    time.sleep(2)                       # let Soloist write its user dir
                    open(f"{self.dir}/paired", "w").close()
                    acct = self.account()
                    with open(f"{self.dir}/paired", "w") as f:
                        f.write(acct)
                    log.info("slot %d claimed by account %s", self.n, acct[:6] + "...")
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
                    # Every device on an account sees that account's playback;
                    # only count it when THIS device is the active output.
                    was = self.playing
                    self.playing = (ev.get("status") == "playing") and self.is_active()
                    if self.playing and not was:
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
        self.add_until = time.time() + ADD_WINDOW
        if not quiet:
            log.info("add window open for %ds", ADD_WINDOW)
        self.ensure_spare()

    def add_window_left(self):
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
                for sl in [x for x in self.slots if not x.paired]:
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
                self._spawn(n)
            else:
                shutil.rmtree(f"{SLOT_ROOT}/{n}", ignore_errors=True)
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
        threading.Thread(target=devices.watch_pollers, daemon=True).start()
        threading.Thread(target=devices.sniff, daemon=True).start()

    def _spawn(self, n):
        sl = Slot(self, n)
        self.slots.append(sl)
        threading.Thread(target=sl.run, daemon=True).start()
        return sl

    def ensure_spare(self):
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

    def took_over(self, slot):
        log.info("slot %d started playing; pausing others", slot.n)
        for s in self.slots:
            # Only pause a slot that is still the active output for its account;
            # pausing an inactive slot would pause that account on other devices.
            if s is not slot and s.playing and s.is_active():
                s.playing = False
                threading.Thread(target=s.ctl, args=("pause",), daemon=True).start()
        threading.Thread(target=ensure_playing, daemon=True).start()


slots = SlotManager()


# ---------------- receiver control ----------------
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
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def ync(xml):
    req = urllib.request.Request(f"http://{RECEIVER}/YamahaRemoteControl/ctrl",
                                 data=xml.encode(), method="POST",
                                 headers={"Content-Type": "text/xml"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def power_on():
    try:
        ync('<YAMAHA_AV cmd="PUT"><Main_Zone><Power_Control><Power>On</Power>'
            '</Power_Control></Main_Zone></YAMAHA_AV>')
    except Exception as e:
        log.info("power on (YNC) failed: %s", e)


def receiver_status():
    """(power, input) from the YNC API, or (None, None)."""
    try:
        r = ync('<YAMAHA_AV cmd="GET"><Main_Zone><Basic_Status>GetParam'
                '</Basic_Status></Main_Zone></YAMAHA_AV>')
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
        time.sleep(0.03)
    ests = ests[1:] or ests
    return sorted(ests)[len(ests) // 2] if ests else None


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
            if b > BUF_TARGET + 0.06:
                tune["drain_left"] = b - BUF_TARGET
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
        time.sleep(8)
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


def transport_state():
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
        for attempt in range(6):
            try:
                soap("AVTransport", "SetAVTransportURI",
                     [("InstanceID", 0), ("CurrentURI", html.escape(STREAM_URL)),
                      ("CurrentURIMetaData", "")])
                soap("AVTransport", "Play", [("InstanceID", 0), ("Speed", 1)])
                log.info("stream started on receiver")
                return
            except Exception as e:
                log.info("start attempt %d failed: %s", attempt + 1, e)
                time.sleep(2)   # receiver may still be booting from standby


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
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    FORMATS = {
        "/stream.mp3": ("audio/mpeg", "DLNA.ORG_PN=MP3"),
        "/stream.l16": ("audio/L16;rate=44100;channels=2", "DLNA.ORG_PN=LPCM"),
        "/stream.wav": ("audio/wav", ""),
    }

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
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=buffer_tuner, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
