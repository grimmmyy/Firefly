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

    ls = subprocess.Popen(
        ["librespot", "--name", NAME, "--backend", "pipe", "--bitrate", "320",
         "--device-type", "avr", "--initial-volume", "40", "--volume-ctrl", "fixed",
         "--onevent", "/opt/spotcast/onevent.sh", "--disable-audio-cache",
         "--system-cache", "/var/lib/spotcast"],
        stdout=subprocess.PIPE, bufsize=0)

    buf = bytearray()
    buf_lock = threading.Lock()

    def read_librespot():
        while True:
            d = ls.stdout.read(CHUNK)
            if not d:
                log.error("librespot exited")
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
