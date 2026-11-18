#!/usr/bin/env python3
"""alexa_local: local Alexa smart-home control for spotcast (no skill, no cloud).

Emulates a Philips Hue bridge (v1 API) that Echo devices discover on the LAN.
Each "light" maps to a receiver action through spotcast's /api:

  Receiver  on/off  -> receiver power
            dim %   -> receiver volume (0-100% across -80.5 dB .. SPOTCAST_MAX_DB)
  TV Mode   on      -> switch receiver to the TV input
            off     -> back to the Spotify / Bluetooth input (PC)

"Alexa, discover devices" once, then: "Alexa, turn on the receiver",
"Alexa, set the receiver to 40 percent", "Alexa, turn on TV mode".
"""
import json, os, socket, struct, threading, time, urllib.request, urllib.parse, uuid, logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

API = os.environ.get("SPOTCAST_API", "http://127.0.0.1:8090")
TV_INPUT = os.environ.get("SPOTCAST_TV_INPUT", "")          # e.g. HDMI1; empty = remembered input
STATE_FILE = "/var/lib/spotcast/alexa_local.json"
PORT = 80

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("alexa-local")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 9))
        return s.getsockname()[0]
    finally:
        s.close()


IP = lan_ip()
MAC = "%012x" % uuid.getnode()
BRIDGE_ID = (MAC[:6] + "fffe" + MAC[6:]).upper()
SERIAL = MAC.lower()
UUID = f"2f402f80-da50-11e1-9b23-{SERIAL}"


def api(path, **q):
    url = f"{API}/api/{path}" + (("?" + urllib.parse.urlencode(q)) if q else "")
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read() or b"{}")


def status():
    try:
        return api("status")
    except Exception:
        return {}


# ---------------------------------------------------------------- devices
def receiver_get():
    s = status().get("receiver", {})
    db, mx = s.get("volume_db"), s.get("max_db", -10)
    bri = 1
    if db is not None:
        bri = max(1, min(254, round((db + 80.5) / (mx + 80.5) * 254)))
    return {"on": s.get("power") == "On", "bri": bri}


def receiver_set(on=None, bri=None):
    if on is not None:
        api("receiver/power", on="1" if on else "0")
    if bri is not None:
        mx = status().get("receiver", {}).get("max_db", -10)
        db = -80.5 + (bri / 254) * (mx + 80.5)
        api("receiver/volume", db=f"{db:.1f}")


def tv_get():
    s = status().get("receiver", {})
    return {"on": s.get("power") == "On" and s.get("input") not in (None, "PC"), "bri": 254}


def tv_set(on=None, bri=None):
    if on is None:
        return
    if on:
        target = TV_INPUT or status().get("previous_input") or "HDMI1"
        api("receiver/power", on="1")
        api("receiver/input", input=target)
    else:
        api("receiver/input", input="PC")


DEVICES = {
    "1": {"name": "Receiver", "get": receiver_get, "set": receiver_set, "dim": True},
    "2": {"name": "TV Mode", "get": tv_get, "set": tv_set, "dim": False},
}


def light_json(i, d):
    st = d["get"]()
    j = {
        "state": {"on": st["on"], "reachable": True, "alert": "none", "mode": "homeautomation"},
        "type": "Dimmable light" if d["dim"] else "On/Off plug-in unit",
        "name": d["name"],
        "modelid": "LWB010" if d["dim"] else "LOM001",
        "manufacturername": "Signify Netherlands B.V.",
        "productname": "Hue white lamp" if d["dim"] else "Hue Smart plug",
        "uniqueid": f"00:17:88:01:00:{int(i):02x}:{int(i):02x}:{int(i):02x}-0b",
        "swversion": "1.104.2",
    }
    if d["dim"]:
        j["state"]["bri"] = st["bri"]
    return j


# ---------------------------------------------------------------- HTTP (Hue v1 API)
DESCRIPTION = f"""<?xml version="1.0" encoding="UTF-8" ?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<URLBase>http://{IP}:{PORT}/</URLBase>
<device>
<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
<friendlyName>Firefly Living Room ({IP})</friendlyName>
<manufacturer>Signify</manufacturer>
<manufacturerURL>http://www.philips-hue.com</manufacturerURL>
<modelDescription>Philips hue Personal Wireless Lighting</modelDescription>
<modelName>Philips hue bridge 2015</modelName>
<modelNumber>BSB002</modelNumber>
<modelURL>http://www.philips-hue.com</modelURL>
<serialNumber>{SERIAL}</serialNumber>
<UDN>uuid:{UUID}</UDN>
<presentationURL>index.html</presentationURL>
</device>
</root>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, obj, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        p = self.path.rstrip("/")
        if p == "/description.xml":
            return self.send(DESCRIPTION, "text/xml")
        parts = p.split("/")       # ['', 'api', user, 'lights', id]
        if len(parts) >= 3 and parts[1] == "api":
            if len(parts) == 3:
                return self.send({"lights": {i: light_json(i, d) for i, d in DEVICES.items()}})
            if parts[3] == "lights":
                if len(parts) == 4:
                    return self.send({i: light_json(i, d) for i, d in DEVICES.items()})
                if parts[4] in DEVICES:
                    return self.send(light_json(parts[4], DEVICES[parts[4]]))
            if parts[3] == "config":
                return self.send({"name": "Firefly", "bridgeid": BRIDGE_ID, "mac": ":".join(
                    MAC[i:i + 2] for i in range(0, 12, 2)), "modelid": "BSB002",
                    "apiversion": "1.56.0", "swversion": "1956000000"})
            return self.send({})
        self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    def do_POST(self):
        # "link button" pairing: Echo creates a user; always accept
        self.body()
        self.send([{"success": {"username": "firefly" + SERIAL}}])

    def do_PUT(self):
        parts = self.path.rstrip("/").split("/")
        data = self.body()
        if len(parts) >= 6 and parts[3] == "lights" and parts[4] in DEVICES and parts[5] == "state":
            i, d = parts[4], DEVICES[parts[4]]
            on = data.get("on")
            bri = data.get("bri") if d["dim"] else None
            log.info("alexa -> %s on=%s bri=%s", d["name"], on, bri)
            try:
                d["set"](on=on, bri=bri)
            except Exception as e:
                log.info("action failed: %s", e)
            res = []
            if on is not None:
                res.append({"success": {f"/lights/{i}/state/on": on}})
            if bri is not None:
                res.append({"success": {f"/lights/{i}/state/bri": bri}})
            return self.send(res)
        self.send([])


# ---------------------------------------------------------------- SSDP responder
def ssdp():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 1900))
    mreq = struct.pack("4s4s", socket.inet_aton("239.255.255.250"), socket.inet_aton(IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    reply = (
        "HTTP/1.1 200 OK\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "EXT:\r\n"
        "CACHE-CONTROL: max-age=100\r\n"
        f"LOCATION: http://{IP}:{PORT}/description.xml\r\n"
        "SERVER: Linux/3.14.0 UPnP/1.0 IpBridge/1.56.0\r\n"
        f"hue-bridgeid: {BRIDGE_ID}\r\n"
        "ST: urn:schemas-upnp-org:device:basic:1\r\n"
        f"USN: uuid:{UUID}::upnp:rootdevice\r\n\r\n"
    ).encode()
    while True:
        data, addr = sock.recvfrom(2048)
        text = data.decode(errors="ignore").lower()
        if "m-search" in text and ("ssdp:all" in text or "basic:1" in text
                                   or "rootdevice" in text or "hue" in text):
            for _ in range(2):
                sock.sendto(reply, addr)
                time.sleep(0.05)


if __name__ == "__main__":
    threading.Thread(target=ssdp, daemon=True).start()
    log.info("alexa-local Hue bridge on %s:%d (devices: %s)", IP, PORT,
             ", ".join(d["name"] for d in DEVICES.values()))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
