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
import os, sys, time, threading, subprocess, socket, html, logging
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

CHUNK = 4410 * 4            # 100 ms of s16le stereo @ 44.1k
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
                if len(q) < 400:      # drop if a client is hopelessly behind
                    q.append(data)
                ev.set()

    def count(self):
        with self.lock:
            return len(self.clients)


hub = Hub()


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
         "--channels=2", "--raw", "--latency-msec=50"],
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
                    extra = len(buf) > CHUNK * 20
                else:
                    out = SILENCE; extra = False
            try:
                ff.stdin.write(out)
            except BrokenPipeError:
                os._exit(1)
            if extra:
                continue
            nxt += 0.1
            time.sleep(max(0, nxt - time.monotonic()))

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

    def name(self):
        return NAME if self.paired else SPARE_NAME

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
                    open(f"{self.dir}/paired", "w").close()
                    log.info("slot %d claimed by a new account", self.n)
                    self.mgr.add_until = 0
                    self.mgr.ensure_spare()
                    self.pending_rename = True
                elif t == "playback_state":
                    was = self.playing
                    self.playing = ev.get("status") == "playing"
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
PERMANENT_SPARE = os.environ.get("SPOTCAST_PERMANENT_SPARE", "1") != "0"


class SlotManager:
    def __init__(self):
        self.slots = []
        self.lock = threading.Lock()
        self.add_until = 0.0

    def open_add_window(self):
        self.add_until = time.time() + ADD_WINDOW
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
        if not self.slots:          # brand-new install: let the first person in
            self.add_until = time.time() + ADD_WINDOW
        self.ensure_spare()
        threading.Thread(target=self.reap, daemon=True).start()

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

    def took_over(self, slot):
        log.info("slot %d started playing; pausing others", slot.n)
        for s in self.slots:
            if s is not slot and s.playing:
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


def ensure_playing():
    with ensure_lock:
        state, uri = transport_state()
        if state == "PLAYING" and uri == STREAM_URL:
            return
        log.info("receiver state=%s uri=%s -> starting stream", state, uri)
        power_on()
        try:  # the RX-V671 renderer only accepts Play while on its "PC" (DLNA) input
            ync('<YAMAHA_AV cmd="PUT"><Main_Zone><Input><Input_Sel>PC</Input_Sel>'
                '</Input></Main_Zone></YAMAHA_AV>')
            time.sleep(3)
        except Exception as e:
            log.info("input switch failed: %s", e)
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

    def do_HEAD(self):
        if self.path.startswith("/stream.mp3"):
            self._stream_headers()
        else:
            self.send_response(404); self.end_headers()

    def _stream_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org",
                         "DLNA.ORG_PN=MP3;DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/stream.mp3":
            self._stream_headers()
            c = hub.add()
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
                hub.remove(c)
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
        elif p.path in ("/", "/add"):
            if p.path == "/add":
                slots.open_add_window()
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
                return
            left = slots.add_window_left()
            people = sum(1 for x in slots.slots if x.paired)
            playing = any(x.playing for x in slots.slots)
            msg = ("<p class=on>New people: pick 'Living Room - Add me' in Spotify once.</p>"
                   if PERMANENT_SPARE else
                   f"<p class=on>'Living Room - Add me' is visible in Spotify for "
                   f"{left // 60}:{left % 60:02d} more. The new person should pick it now.</p>"
                   if left else
                   "<form action=/add><button>Add a new person</button></form>"
                   "<p>Shows 'Living Room - Add me' in Spotify for 10 minutes. "
                   "They pick it once and it's theirs from then on.</p>")
            body = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Living Room Speaker</title>{'<meta http-equiv=refresh content=5>' if left and not PERMANENT_SPARE else ''}
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;
padding:24px 16px;max-width:480px;margin:auto}}button{{width:100%;padding:16px;font-size:17px;
font-weight:600;border:0;border-radius:12px;background:#1db954;color:#000}}
p{{color:#aaa;line-height:1.45}}.on{{color:#1db954}}</style>
<h1>Living Room Speaker</h1>
<p>{people} Spotify account{'s' if people != 1 else ''} linked &middot;
{'playing now' if playing else 'idle'}</p>{msg}""".encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers(); self.wfile.write(body)
        elif p.path == "/status":
            body = f"clients={hub.count()} url={STREAM_URL}\n".encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


def main():
    global STREAM_URL
    while True:
        try:
            STREAM_URL = f"http://{lan_ip()}:{PORT}/stream.mp3"
            break
        except OSError:
            time.sleep(3)
    log.info("spotcast '%s' -> %s, stream %s", NAME, RECEIVER, STREAM_URL)
    os.makedirs("/var/lib/spotcast", exist_ok=True)
    start_pipeline()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
