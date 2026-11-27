"""Freeze rescue: when the RX-V671's web control stops answering, try to bring
it back through its other control port (YNCA, TCP 50000) and record what
happened at each step.

Loaded by spotcast.py (sys.modules["__main__"]). One record per attempt in
/var/lib/spotcast-user/freezes/rescue.log (JSON lines). Whether YNCA survives a
freeze, and whether any step clears one, is not known yet: these records are
how we find out.
"""
import json, os, socket, sys, threading, time

sc = sys.modules["__main__"]
log = sc.log

LOG = "/var/lib/spotcast-user/freezes/rescue.log"
YNCA_PORT = 50000
AFTER_S = 90            # ignore short hangs, which clear by themselves
RETRY_S = 300           # while the receiver is in use, look again this often
CYCLE_OFF_S = 20
SETTLE_S = 30


def ynca(ip, *cmds, wait=0.6, timeout=4):
    """Send YNCA lines on one connection; return {"@SUB:KEY": value} replies."""
    buf = b""
    with socket.create_connection((ip, YNCA_PORT), timeout) as s:
        s.settimeout(wait)
        for c in cmds:
            s.sendall((c + "\r\n").encode())
            end = time.monotonic() + wait
            while time.monotonic() < end:
                try:
                    d = s.recv(4096)
                except socket.timeout:
                    break
                if not d:
                    break
                buf += d
    out = {}
    for line in buf.decode("utf-8", "replace").split("\r\n"):
        k, sep, v = line.partition("=")
        if sep:
            out[k] = v
    return out


def port_open(ip, port):
    try:
        socket.create_connection((ip, port), 3).close()
        return True
    except OSError:
        return False


def blocker(state, playing):
    """Why a standby cycle would interrupt someone, or None if it is safe."""
    if playing:
        return "something is playing"
    main, zone2 = state.get("@MAIN:PWR"), state.get("@ZONE2:PWR")
    if main is None or zone2 is None:
        return "its power state is unknown"
    if zone2 != "Standby":
        return "Zone 2 is on"
    if main != "Standby" and state.get("@MAIN:INP") != "PC":
        return f"the main zone is in use ({state.get('@MAIN:INP')})"
    return None


def attempt(ip, alive, playing, sleep=time.sleep):
    """One pass. alive() says whether web control answers again."""
    r = {"t": int(time.time()), "when": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": ip,
         "ports": {str(p): port_open(ip, p) for p in (80, 49154, YNCA_PORT)},
         "steps": [], "result": None}
    try:
        state = ynca(ip, "@MAIN:PWR=?", "@MAIN:INP=?", "@ZONE2:PWR=?")
    except OSError as e:
        r["result"], r["error"] = "ynca-dead", str(e)
        return r
    r["state"] = state
    if "@MAIN:PWR" not in state:
        r["result"] = "ynca-silent"
        return r

    def check(step, settle):
        sleep(settle)
        ok = alive()
        r["steps"].append({"step": step, "web_control_back": ok})
        if ok:
            r["result"] = "fixed-by-" + step
        return ok

    if check("ynca-query", 5):
        return r
    try:
        # restarts the receiver's network-player service; makes no sound
        ynca(ip, "@SYS:DMCCONTROL=Disable")
        sleep(3)
        ynca(ip, "@SYS:DMCCONTROL=Enable")
        if check("dmc-toggle", SETTLE_S):
            return r
        why = blocker(state, playing)
        if why:
            r["result"], r["why"] = "waiting", why
            return r
        try:
            ynca(ip, "@SYS:PWR=Standby")
            sleep(CYCLE_OFF_S)
            for _ in range(3):
                if ynca(ip, "@SYS:PWR=On").get("@SYS:PWR") == "On":
                    break
                sleep(5)
        finally:
            # "system on" wakes both zones: put each back the way it was
            sleep(8)
            ynca(ip, "@ZONE2:PWR=" + state["@ZONE2:PWR"],
                 "@MAIN:PWR=" + state["@MAIN:PWR"])
        if check("standby-cycle", SETTLE_S):
            return r
        r["result"] = "not-fixed"
    except OSError as e:
        r["result"], r["error"] = "ynca-lost", str(e)
    return r


def record(r):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps(r) + "\n")
    except OSError:
        pass


def message(r):
    res = r["result"]
    if res.startswith("fixed-by-"):
        return f"Receiver froze and was recovered automatically ({res[9:]})"
    if res == "waiting":
        return (f"Receiver web control is frozen but port 50000 still answers. Not "
                f"power cycling it because {r['why']}")
    if res in ("ynca-dead", "ynca-silent"):
        return "Receiver is frozen on both control ports - re-seat its network cable"
    return ("Receiver is frozen and automatic recovery did not work - re-seat its "
            "network cable")


def due(first_fail, last, now):
    """last = (freeze start, time, result) of the previous attempt, or None."""
    if not first_fail or now - first_fail < AFTER_S:
        return False
    if not last or last[0] != first_fail:
        return True
    return last[2] == "waiting" and now - last[1] >= RETRY_S


def watch():
    last = None
    while True:
        time.sleep(15)
        try:
            first = sc.rx_stats.get("first_fail")
            if not due(first, last, time.time()):
                continue
            ip = sc.RECEIVER
            if sc._ync_alive(ip):
                continue
            ex = getattr(sc, "extras", None)
            playing = any(x.playing for x in sc.slots.slots) or bool(
                ex and ex.st.get("active") not in (None, "spotify"))
            r = attempt(ip, lambda: sc._ync_alive(ip), playing)
            r["down_s"] = int(time.time() - first)
            record(r)
            log.info("rescue: %s %s", r["result"], json.dumps(r["steps"]))
            last = (first, time.time(), r["result"])
            sc.event("rescue", result=r["result"])
            sc.alert("rescue-" + r["result"], message(r), every=1800)
        except Exception as e:
            log.exception("rescue failed: %s", e)


def start():
    threading.Thread(target=watch, daemon=True, name="rescue").start()
