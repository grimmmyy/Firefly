"""Freeze reports: when the RX-V671's network control stops answering, record
everything that might explain it, so each freeze leaves evidence behind.

Loaded by spotcast.py (sys.modules["__main__"]). One report per freeze in
/var/lib/spotcast-user/freezes/<stamp>.json, plus <stamp>.pcap with the last
10 minutes of the rolling capture (rxcap.service) around the receiver.
"""
import glob, json, os, re, struct, subprocess, sys, threading, time

sc = sys.modules["__main__"]
log = sc.log

DIR = "/var/lib/spotcast-user/freezes"
PCAP_GLOB = "/var/log/rxcap/rx.pcap*"
KEEP = 40
WINDOW_S = 600                       # capture kept from before the freeze
RX = None                            # receiver IP, filled from sc.RECEIVER

# moments other code reports (all epoch seconds)
marks = {"sound_change": 0.0, "heavy_cmd": 0.0, "heavy_what": "", "stream_start": 0.0}


def mark(kind, what=""):
    marks[kind] = time.time()
    if kind == "heavy_cmd":
        marks["heavy_what"] = what[:60]


# ---------------------------------------------------------------- pcap
def _read_pcap(path):
    """Yield (ts, header_bytes, data) from a classic pcap file; tolerate a
    truncated last record (the file is still being written)."""
    with open(path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            return None, []
        magic = struct.unpack("<I", gh[:4])[0]
        if magic not in (0xA1B2C3D4, 0xA1B23C4D):
            return None, []
        nano = magic == 0xA1B23C4D
        recs = []
        while True:
            h = f.read(16)
            if len(h) < 16:
                break
            sec, frac, incl, _ = struct.unpack("<IIII", h)
            d = f.read(incl)
            if len(d) < incl:
                break
            recs.append((sec + frac / (1e9 if nano else 1e6), h, d))
        return gh, recs


def cut_pcap(t_end, out_path, seconds=WINDOW_S):
    """Write the packets from [t_end - seconds, t_end + 30] to out_path."""
    header, picked = None, []
    files = sorted(glob.glob(PCAP_GLOB), key=os.path.getmtime)
    for p in files:
        try:
            if os.path.getmtime(p) < t_end - seconds:
                continue
            gh, recs = _read_pcap(p)
        except OSError:
            continue
        if gh is None:
            continue
        header = header or gh
        picked += [r for r in recs if t_end - seconds <= r[0] <= t_end + 30]
    if not header or not picked:
        return 0
    picked.sort(key=lambda r: r[0])
    with open(out_path, "wb") as f:
        f.write(header)
        for _, h, d in picked:
            f.write(h + d)
    return len(picked)


def summarize_pcap(path, t_freeze):
    """Counts that have pointed at causes before: who floods SSDP, who sweeps
    the LAN with ARP, what the receiver itself was doing."""
    out = {"packets": 0}
    try:
        txt = subprocess.run(["tcpdump", "-nn", "-tt", "-r", path], capture_output=True,
                             text=True, timeout=60).stdout
    except Exception as e:
        return {"error": str(e)}
    notify, arp_from, rx_out, rx_in_http, rx_upnp = {}, {}, 0, 0, 0
    last_rx_reply = None
    for line in txt.splitlines():
        out["packets"] += 1
        # "-i any" captures carry "iface direction" before the payload; "-i eth0"
        # captures don't
        m = re.match(r"(\d+\.\d+)\s+(?:\S+\s+(?:In|Out|M|B|P)\s+)?(.*)", line)
        if not m:
            continue
        ts, rest = float(m.group(1)), m.group(2)
        a = re.match(r"IP (\d+\.\d+\.\d+\.\d+)\.(\d+) > (\d+\.\d+\.\d+\.\d+)\.(\d+)", rest)
        if "ARP, Request who-has" in rest:
            src = re.search(r"tell (\d+\.\d+\.\d+\.\d+)", rest)
            if src:
                arp_from[src.group(1)] = arp_from.get(src.group(1), 0) + 1
            continue
        if not a:
            continue
        s_ip, s_port, d_ip, d_port = a.groups()
        if d_ip == "239.255.255.250" and d_port == "1900":
            notify[s_ip] = notify.get(s_ip, 0) + 1
        if s_ip == RX:
            rx_out += 1
            if s_port in ("80", "49154") and ts < t_freeze:
                last_rx_reply = ts
        if d_ip == RX and d_port == "80":
            rx_in_http += 1
        if d_ip == RX and d_port == "49154":
            rx_upnp += 1
    mins = WINDOW_S / 60
    out.update({
        "ssdp_per_min_by_source": {k: round(v / mins, 1) for k, v in
                                    sorted(notify.items(), key=lambda kv: -kv[1])[:6]},
        "arp_sweeps": {k: v for k, v in arp_from.items() if v >= 20},
        "receiver_packets_out": rx_out,
        "pi_requests_to_receiver_http": rx_in_http,
        "pi_requests_to_receiver_upnp": rx_upnp,
        "last_receiver_reply_s_before": round(t_freeze - last_rx_reply, 1) if last_rx_reply else None,
    })
    return out


# ---------------------------------------------------------------- report
def _recent_events(since):
    ev = []
    try:
        with open(sc.EVENT_LOG) as f:
            for line in f.readlines()[-3000:]:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= since and r.get("kind") in (
                        "rx-down", "rx-recovered", "net-failover", "schedule", "scene",
                        "hand-override", "start", "stream-rest", "connect"):
                    ev.append({k: v for k, v in r.items() if k != "last_requests"})
    except OSError:
        pass
    return ev[-60:]


def _pi_network_changes(minutes=15):
    try:
        out = subprocess.run(["journalctl", "-u", "NetworkManager", "--since", f"-{minutes}min",
                              "--no-pager", "-o", "cat"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return []
    return [l[:160] for l in out.splitlines() if "state change" in l or "dhcp4" in l][-15:]


def _own_and_router_ips():
    """This Pi's and the gateway's addresses: their LAN chatter is routine."""
    ips = set()
    for cmd in (["ip", "-4", "-o", "addr"], ["ip", "-4", "route", "show", "default"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:
            continue
        ips.update(re.findall(r"(?:inet|via) (\d+\.\d+\.\d+\.\d+)", out))
    return ips


def likely_causes(r, routine=()):
    """What stood out, each with its evidence. None of it is a proven cause.
    `routine` holds addresses (this Pi, the router) not worth listing."""
    c = []
    t = r["t"]
    # no request has worked since the bridge started: it began mid-freeze
    reqs = r.get("last_requests") or []
    if r.get("never_answered") and reqs and not any(x["ok"] for x in reqs):
        return ["The receiver has not answered since the bridge restarted, so this "
                "freeze probably began earlier: this report can't show what started it"]
    if marks["sound_change"] and t - marks["sound_change"] < 300:
        c.append(f"Sound change {t - marks['sound_change']:.0f} s before "
                 "(froze after this in one test; later tests did not repeat it)")
    if marks["heavy_cmd"] and t - marks["heavy_cmd"] < 60:
        c.append(f"Power/input command {t - marks['heavy_cmd']:.0f} s before "
                 f"({marks['heavy_what']})")
    p = r.get("capture") or {}
    for ip, n in (p.get("arp_sweeps") or {}).items():
        if ip in routine:
            continue
        c.append(f"Seen, not proven to matter: {ip} sent {n} address lookups (ARP) in "
                 f"the last {WINDOW_S // 60} min")
    flood = {ip: n for ip, n in (p.get("ssdp_per_min_by_source") or {}).items()
             if n > 6 and ip != RX and ip not in routine}
    for ip, n in flood.items():
        c.append(f"Seen, not proven to matter: {ip} sent {n} device announcements "
                 "(SSDP) per minute; a heavier test flood did not freeze the receiver")
    if r.get("pi_network_changes"):
        c.append("The Pi's network changed in the last 15 minutes")
    if r.get("playing"):
        c.append(f"Music was playing ({r.get('active_source') or 'Pi stream'}): "
                 "the receiver's UPnP side was busy")
    up = r.get("since_receiver_came_back_s")
    if up and up > 3 * 3600 and not r.get("playing"):
        c.append(f"Froze while idle, {up / 3600:.1f} h since the receiver last came back")
    hangs = [e for e in r.get("recent_events", []) if e.get("kind") == "rx-down"]
    if len(hangs) > 1:
        c.append(f"{len(hangs) - 1} short hang(s) in the hours before: the receiver was "
                 "already struggling")
    return c or ["Nothing unusual found: see the capture"]


def report(error):
    """Called once when a freeze starts. Runs in its own thread; waits a bit so
    the capture holds the moment itself."""
    def run():
        global RX
        RX = sc.RECEIVER
        t = time.time()
        time.sleep(45)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
        os.makedirs(DIR, exist_ok=True)
        came_back = sc.rx_stats.get("last_recovered") or sc.health.get("started")
        never_answered = not sc.rx_stats.get("last_recovered") and not any(
            x[1] == "ok" for x in sc.rx_stats["log"])
        ex = getattr(sc, "extras", None)
        r = {
            "t": int(t), "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
            "error": error[:160],
            "since_receiver_came_back_s": int(t - came_back) if came_back else None,
            "playing": any(x.playing for x in sc.slots.slots) or bool(
                ex and ex.st.get("active") not in (None, "spotify")),
            "active_source": ex.st.get("active") if ex else None,
            "last_requests": [{"t": round(x[0] - t, 1), "ok": x[1] == "ok", "what": x[2]}
                              for x in sc.rx_stats["log"][-40:]],
            "last_sound_change_s_before": round(t - marks["sound_change"]) if marks["sound_change"] else None,
            "last_heavy_cmd_s_before": round(t - marks["heavy_cmd"]) if marks["heavy_cmd"] else None,
            "latency_last_hours": sc.rx_latency_report()[-6:],
            "recent_events": _recent_events(t - 6 * 3600),
            "pi_network_changes": _pi_network_changes(),
        }
        pcap = os.path.join(DIR, stamp + ".pcap")
        n = cut_pcap(t, pcap)
        r["capture_file"] = pcap if n else None
        r["capture"] = summarize_pcap(pcap, t) if n else {"packets": 0}
        r["never_answered"] = never_answered
        r["likely_causes"] = likely_causes(r, _own_and_router_ips())
        with open(os.path.join(DIR, stamp + ".json"), "w") as f:
            json.dump(r, f, indent=1)
        log.info("freeze report %s: %s", stamp, "; ".join(r["likely_causes"])[:300])
        for old in sorted(glob.glob(os.path.join(DIR, "*.json")))[:-KEEP]:
            for ext in (".json", ".pcap"):
                try:
                    os.remove(old[:-5] + ext)
                except OSError:
                    pass
    threading.Thread(target=run, daemon=True).start()


def reports(limit=20):
    out = []
    for p in sorted(glob.glob(os.path.join(DIR, "*.json")), reverse=True)[:limit]:
        try:
            with open(p) as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        r.pop("recent_events", None)
        r["id"] = os.path.basename(p)[:-5]
        out.append(r)
    return out


def mark_recovered(down_s):
    """Add how long the freeze lasted to its report."""
    files = sorted(glob.glob(os.path.join(DIR, "*.json")))
    if not files:
        return
    try:
        with open(files[-1]) as f:
            r = json.load(f)
        if r.get("down_s") is None and time.time() - r["t"] < down_s + 120:
            r["down_s"] = int(down_s)
            with open(files[-1], "w") as f:
                json.dump(r, f, indent=1)
    except (OSError, ValueError, KeyError):
        pass
