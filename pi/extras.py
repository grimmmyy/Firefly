"""spotcast extras: everything that makes the RX-V671 act like a current receiver.

Loaded by spotcast.py (runs inside the same process, same PipeWire session):

  Sources      AirPlay 2 (shairport-sync), DLNA/UPnP renderer (gmediarender),
               Bluetooth A2DP (bluez + PipeWire), internet radio played by the Pi,
               plus Spotify Connect (spotcast slots).
  Arbitration  last source wins: the newest source to start is heard, the others
               are muted and (where possible) paused.
  Volume       AirPlay volume moves the receiver's real volume.
  DSP          optional loudness levelling + 15-band room EQ (LADSPA in PipeWire),
               with a room measurement driven by the Firefly app's microphone.
  Receiver     sound program, Straight, Enhancer, bass/treble, night mode
               (Adaptive DRC), receiver scenes, Zone 2, custom scenes.
  Schedules    wake-up with music, timed off, timed scenes.
  History      source starts with titles, minutes per source per day.

Everything goes through the receiver's own YNC API; nothing new to buy.
"""
import base64, json, os, re, struct, subprocess, sys, threading, time, uuid, math, html

sc = sys.modules["__main__"]          # spotcast.py
log = sc.log

STATE = "/var/lib/spotcast-user/extras.json"
SHAIRPORT_CONF = "/var/lib/spotcast-user/shairport.conf"
SHAIRPORT_META = "/tmp/spotcast-shairport-metadata"
HISTORY = "/var/lib/spotcast-user/history.jsonl"

SOUND_PROGRAMS = [
    "Hall in Munich", "Hall in Vienna", "Chamber", "Cellar Club", "The Roxy Theatre",
    "The Bottom Line", "Sports", "Action Game", "Roleplaying Game", "Music Video",
    "Standard", "Spectacle", "Sci-Fi", "Adventure", "Drama", "Mono Movie",
    "2ch Stereo", "7ch Stereo", "Surround Decoder",
]
EQ_HZ = [50, 100, 156, 220, 311, 440, 622, 880, 1250, 1750, 2500, 3500, 5000, 10000, 20000]
KINDS = ("spotify", "airplay", "dlna", "bluetooth", "radio")
LABEL = {"spotify": "Spotify", "airplay": "AirPlay", "dlna": "DLNA cast",
         "bluetooth": "Bluetooth", "radio": "Radio"}

DEFAULTS = {
    "airplay": True, "dlna": True, "bluetooth": True,
    "airplay_volume": True,           # AirPlay slider moves the receiver volume
    "loudness": False, "eq_on": False, "eq": [0.0] * 15,
    "scenes": [
        {"name": "Music Scene", "input": "PC", "program": "2ch Stereo", "straight": False,
         "enhancer": True, "drc": False},
        {"name": "Movie", "input": "HDMI1", "program": "Standard", "straight": False,
         "enhancer": False, "drc": False},
        {"name": "Night Scene", "drc": True, "volume_db": -45},
    ],
    "schedules": [],
    "dlna_uuid": None,
    # sound settings applied when a source becomes active (keys as in set_sound);
    # Pi sources by name, receiver inputs by their id (HDMI1, TUNER, ...)
    "source_sound_on": False,
    "source_sound": {
        "spotify": {"straight": True, "enhancer": True},
        "airplay": {"straight": True, "enhancer": True},
        "bluetooth": {"straight": True, "enhancer": True},
        "radio": {"straight": True, "enhancer": True},
        "HDMI1": {"program": "Standard"},
    },
    # volume cap during quiet hours (receiver remote included)
    "quiet": {"on": False, "from": "22:00", "to": "07:00", "max_db": -40.0},
}

cfg = {}
_lock = threading.Lock()
st = {"active": None, "since": 0.0, "live": set(), "airplay_meta": {}, "radio": None,
      "bt_pair_until": 0.0, "fired": {}, "fade": None, "sleep_until": 0.0}
_procs = {}


# ---------------------------------------------------------------- config
def load():
    global cfg
    try:
        cfg = {**DEFAULTS, **json.load(open(STATE))}
    except Exception:
        cfg = json.loads(json.dumps(DEFAULTS))
    if not cfg.get("dlna_uuid"):
        cfg["dlna_uuid"] = str(uuid.uuid4())
        save()


def save():
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- receiver (YNC)
def put(zone, inner):
    sc.invalidate_basic()
    return sc.ync(f'<YAMAHA_AV cmd="PUT"><{zone}>{inner}</{zone}></YAMAHA_AV>')


def get(zone, inner):
    return sc.ync(f'<YAMAHA_AV cmd="GET"><{zone}>{inner}</{zone}></YAMAHA_AV>')


def _tag(xml, name):
    m = re.search(rf"<{name}>([^<]*)</{name}>", xml or "")
    return m.group(1) if m else None


def _db(xml, name):
    m = re.search(rf"<{name}><Val>(-?\d+)</Val>", xml or "")
    return int(m.group(1)) / 10 if m else None


def sound_state():
    x = sc.basic_status() or ""
    return {
        "program": _tag(x, "Sound_Program"),
        "straight": _tag(x, "Straight") == "On",
        "enhancer": _tag(x, "Enhancer") == "On",
        "pure_direct": _tag(re.sub(r"<Volume>.*?</Volume>", "", x), "Mode") == "On",
        "bass_db": _db(x, "Bass"), "treble_db": _db(x, "Treble"),
        "drc": _tag(x, "Adaptive_DRC") == "Auto",
        "cinema_3d": _tag(x, "_3D_Cinema_DSP") == "Auto",
        "dialogue_lift": int(_tag(x, "Dialogue_Lift") or 0),
        "programs": SOUND_PROGRAMS,
    }


def _onoff(v):
    return "On" if str(v).lower() in ("1", "true", "on", "yes") else "Off"


def set_sound(key, val):
    if key == "program":
        if val not in SOUND_PROGRAMS:
            raise ValueError("unknown sound program")
        put("Main_Zone", "<Surround><Program_Sel><Current><Straight>Off</Straight>"
            f"<Sound_Program>{html.escape(val)}</Sound_Program></Current></Program_Sel></Surround>")
    elif key in ("straight", "enhancer"):
        tag = "Straight" if key == "straight" else "Enhancer"
        put("Main_Zone", f"<Surround><Program_Sel><Current><{tag}>{_onoff(val)}</{tag}>"
            "</Current></Program_Sel></Surround>")
    elif key == "pure_direct":
        put("Main_Zone", f"<Sound_Video><Pure_Direct><Mode>{_onoff(val)}</Mode>"
            "</Pure_Direct></Sound_Video>")
    elif key in ("bass", "treble"):
        db = max(-6.0, min(6.0, round(float(val) * 2) / 2))
        tag = key.capitalize()
        put("Main_Zone", f"<Sound_Video><Tone><{tag}><Val>{int(db * 10)}</Val><Exp>1</Exp>"
            f"<Unit>dB</Unit></{tag}></Tone></Sound_Video>")
    elif key == "drc":            # "night mode"
        v = "Auto" if _onoff(val) == "On" else "Off"
        put("Main_Zone", f"<Sound_Video><Adaptive_DRC>{v}</Adaptive_DRC></Sound_Video>")
    elif key == "cinema_3d":
        v = "Auto" if _onoff(val) == "On" else "Off"
        put("Main_Zone", f"<Surround><_3D_Cinema_DSP>{v}</_3D_Cinema_DSP></Surround>")
    elif key == "dialogue_lift":
        n = max(0, min(5, int(float(val))))
        put("Main_Zone", f"<Surround><Dialogue_Lift>{n}</Dialogue_Lift></Surround>")
    elif key == "scene":          # the receiver's own Scene 1-4
        if not re.fullmatch(r"Scene [1-4]", val):
            raise ValueError("scene must be 'Scene 1'..'Scene 4'")
        put("Main_Zone", f"<Scene><Scene_Load>{val}</Scene_Load></Scene>")
    elif key == "sleep":          # receiver's own sleep timer
        if val not in ("Off", "120 min", "90 min", "60 min", "30 min"):
            raise ValueError("bad sleep value")
        put("Main_Zone", f"<Power_Control><Sleep>{val}</Sleep></Power_Control>")
    else:
        raise ValueError("unknown setting")


_inputs = {"t": 0, "v": []}


def inputs():
    if time.time() - _inputs["t"] < 3600 and _inputs["v"]:
        return _inputs["v"]
    try:
        x = get("Main_Zone", "<Input><Input_Sel_Item>GetParam</Input_Sel_Item></Input>")
        items = re.findall(r"<Param>([^<]+)</Param><RW>(\w+)</RW><Title>([^<]*)</Title>", x)
        _inputs["v"] = [{"id": p, "title": t.strip() or p} for p, rw, t in items if "W" in rw]
        _inputs["t"] = time.time()
    except Exception:
        pass
    return _inputs["v"]


def zone2_state():
    try:
        x = get("Zone_2", "<Basic_Status>GetParam</Basic_Status>")
    except Exception:
        return {"reachable": False}
    return {"reachable": True, "power": _tag(x, "Power"), "input": _tag(x, "Input_Sel"),
            "volume_db": _db(x, "Lvl"), "mute": _tag(x, "Mute") == "On",
            "output": _tag(x, "Output")}


def zone2_set(power=None, input=None, volume_db=None, mute=None):
    if power is not None:
        v = "On" if _onoff(power) == "On" else "Standby"
        put("Zone_2", f"<Power_Control><Power>{v}</Power></Power_Control>")
    if input:
        put("Zone_2", f"<Input><Input_Sel>{html.escape(input)}</Input_Sel></Input>")
    if volume_db not in (None, ""):
        db = max(-80.5, min(sc.MAX_DB, round(float(volume_db) * 2) / 2))
        put("Zone_2", f"<Volume><Lvl><Val>{int(db * 10)}</Val><Exp>1</Exp><Unit>dB</Unit>"
            "</Lvl></Volume>")
    if mute is not None:
        put("Zone_2", f"<Volume><Mute>{_onoff(mute)}</Mute></Volume>")


def apply_scene(s):
    """Custom scene: any of input, program, straight, enhancer, bass, treble,
    drc, volume_db, zone2 ('on'/'off'/'party')."""
    log.info("scene: %s", s.get("name"))
    pw, _ = sc.receiver_status()
    if pw != "On":
        sc.power_on()
        time.sleep(4)
    if s.get("input"):
        put("Main_Zone", f"<Input><Input_Sel>{html.escape(s['input'])}</Input_Sel></Input>")
        time.sleep(1)
    if s.get("straight"):
        set_sound("straight", True)
    elif s.get("program"):
        set_sound("program", s["program"])
    for k in ("enhancer", "drc", "bass", "treble"):
        if s.get(k) is not None:
            set_sound(k, s[k])
    if s.get("volume_db") is not None:
        sc.set_receiver_volume(s["volume_db"])
    z = s.get("zone2")
    if z == "off":
        zone2_set(power=False)
    elif z in ("on", "party"):
        zone2_set(power=True, input=s.get("input") or "PC" if z == "party" else None)
    sc.event("scene", name=s.get("name"))


# ---------------------------------------------------------------- sources
def _run(name, cmd, env=None):
    """Keep a helper process running until stop(name)."""
    def loop():
        while _procs.get(name, {}).get("want"):
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True, env={**os.environ, **(env or {})})
                _procs[name]["p"] = p
                err = p.stderr.read()
                p.wait()
                if _procs.get(name, {}).get("want"):
                    log.info("%s exited (%s): %s", name, p.returncode, (err or "")[-300:])
                    time.sleep(5)
            except FileNotFoundError:
                log.info("%s is not installed", name)
                return
    if _procs.get(name, {}).get("want"):
        return
    _procs[name] = {"want": True, "p": None}
    threading.Thread(target=loop, daemon=True, name=name).start()


def _stop(name):
    d = _procs.get(name)
    if d:
        d["want"] = False
        if d.get("p") and d["p"].poll() is None:
            d["p"].terminate()


def shairport_bin():
    for b in ("/usr/local/bin/shairport-sync", "/usr/bin/shairport-sync"):
        if os.path.exists(b):
            return b
    return None


def airplay2():
    b = shairport_bin()
    return bool(b) and "AirPlay2" in sc.sh_out([b, "-V"])


def write_shairport_conf():
    name = sc.NAME.replace('"', "")
    conf = f'''general = {{
  name = "{name}";
  output_backend = "pulseaudio";
  ignore_volume_control = "yes";
  run_this_when_volume_is_set = "/opt/spotcast/onvolume.sh";
  volume_range_db = 60;
}};
pulseaudio = {{ application_name = "Shairport Sync"; }};
metadata = {{
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = "{SHAIRPORT_META}";
}};
sessioncontrol = {{ session_timeout = 20; }};
'''
    with open(SHAIRPORT_CONF, "w") as f:
        f.write(conf)


def sync_sources():
    """Start/stop helpers to match cfg."""
    if cfg["airplay"] and shairport_bin():
        write_shairport_conf()
        _run("shairport-sync", [shairport_bin(), "-c", SHAIRPORT_CONF])
    else:
        _stop("shairport-sync")
    if cfg["dlna"]:
        _run("gmediarender", ["gmediarender", "-f", sc.NAME.split(" (")[0] + " (Pi cast)", "-u", cfg["dlna_uuid"], "--port=49494",
            "--gstout-audiosink=pulsesink", "--logfile=/dev/null"])
    else:
        _stop("gmediarender")
    bt = "on" if cfg["bluetooth"] else "off"
    sc.sh_out(["bluetoothctl", "pairable", bt])
    if not cfg["bluetooth"]:
        for d in bt_devices():
            if d["connected"]:
                sc.sh_out(["bluetoothctl", "disconnect", d["mac"]], timeout=10)


def _kind(block):
    if "bluez" in block:
        return "bluetooth"
    b = re.search(r'application.process.binary = "([^"]+)"', block)
    a = re.search(r'application.name = "([^"]+)"', block)
    b, a = (b.group(1) if b else ""), (a.group(1) if a else "")
    if a == "Spotify" or "soloist" in b:
        return "spotify"
    if "shairport" in b.lower() or a == "Shairport Sync":
        return "airplay"
    if "gmediarender" in b.lower() or "gmediarender" in a.lower():
        return "dlna"
    if b.startswith("gst-launch") or a == "spotcast-radio":
        return "radio"
    return None


def sink_inputs():
    out = []
    for block in sc.sh_out(["pactl", "list", "sink-inputs"]).split("Sink Input #")[1:]:
        idx = block.split("\n", 1)[0].strip()
        out.append({"idx": idx, "kind": _kind(block), "live": "Corked: no" in block,
                    "muted": "Mute: yes" in block, "block": block})
    return out


def mute_spotify():
    """spotcast.enforce_single_output asks this: keep Spotify muted while another
    source is the active one."""
    return st["active"] not in (None, "spotify")


def _pause_others(new):
    if new != "spotify":
        for x in sc.slots.slots:
            if x.playing or x.is_active():
                try:
                    x.ctl("pause")
                except Exception:
                    pass
                x.playing = False
    if new != "bluetooth":
        sc.sh_out(["bluetoothctl", "player.pause"], timeout=4)
    if new != "radio" and st["radio"]:
        radio_stop()


def arbitrate():
    """Last source wins. Called every second."""
    ins = sink_inputs()
    live = {(i["idx"], i["kind"]) for i in ins if i["live"] and i["kind"]}
    started = [k for (idx, k) in live if (idx, k) not in st["live"]]
    st["live"] = live
    if started:
        new = started[-1]
        if new != st["active"]:
            old = st["active"]
            st["active"], st["since"] = new, time.time()
            log.info("source: %s -> %s (last source wins)", old, new)
            history_add(new)
            threading.Thread(target=_pause_others, args=(new,), daemon=True).start()
    elif st["active"] and not any(k == st["active"] for _, k in live):
        # active source went quiet: let whichever is still sounding take over
        rest = [k for _, k in live]
        if rest:
            st["active"], st["since"] = rest[-1], time.time()
            history_add(st["active"])
        elif time.time() - sc.activity["last_sound"] > 30:
            st["active"] = None
    for i in ins:
        if i["kind"] in (None, "spotify"):
            continue                    # spotify muting is done by enforce_single_output
        want = st["active"] is not None and i["kind"] != st["active"]
        if want != i["muted"]:
            sc.sh_out(["pactl", "set-sink-input-mute", i["idx"], "1" if want else "0"])
        if i["kind"] in ("airplay", "dlna", "radio"):
            m = re.search(r"Volume: \S+: (\d+) /", i["block"])
            if m and int(m.group(1)) != 65536:          # receiver is the volume control
                sc.sh_out(["pactl", "set-sink-input-volume", i["idx"], "100%"])


def arbiter():
    n = 0
    while True:
        time.sleep(1)
        n += 1
        try:
            arbitrate()
            if n % 5 == 0:
                pin_capture()
        except Exception as e:
            log.info("arbiter: %s", e)


def airplay_volume(v):
    """shairport-sync volume (-30..0, -144 = mute) -> receiver dB."""
    if not cfg.get("airplay_volume") or st["active"] != "airplay":
        return None
    v = float(v)
    if v <= -144:
        put("Main_Zone", "<Volume><Mute>On</Mute></Volume>")
        return "mute"
    _, mute = sc.receiver_volume()
    if mute:
        put("Main_Zone", "<Volume><Mute>Off</Mute></Volume>")
    lo = max(-80.5, sc.MAX_DB - 50)
    db = lo + (max(-30.0, min(0.0, v)) + 30) / 30 * (sc.MAX_DB - lo)
    return sc.set_receiver_volume(db)


# ---------------------------------------------------------------- AirPlay metadata
def airplay_meta_reader():
    codes = {"6d696e6d": "title", "61736172": "artist", "6173616c": "album"}
    while True:
        try:
            if not os.path.exists(SHAIRPORT_META):
                time.sleep(5)
                continue
            buf = ""
            with open(SHAIRPORT_META, "r", errors="replace") as f:
                for line in f:
                    buf += line
                    if "</item>" not in buf:
                        continue
                    m = re.search(r"<code>(\w+)</code>.*?<data encoding=\"base64\">\s*([^<]*)</data>",
                                  buf, re.S)
                    c = re.search(r"<code>(\w+)</code>", buf)
                    if m and m.group(1) in codes:
                        st["airplay_meta"][codes[m.group(1)]] = base64.b64decode(
                            m.group(2)).decode("utf-8", "replace")
                    elif c and c.group(1) == "6d647374":      # 'mdst': new track
                        st["airplay_meta"] = {}
                    buf = ""
        except Exception as e:
            log.info("airplay metadata: %s", e)
            time.sleep(5)


# ---------------------------------------------------------------- Bluetooth
def bt_devices():
    out = []
    for line in sc.sh_out(["bluetoothctl", "devices", "Paired"]).splitlines():
        m = re.match(r"Device (\S+) (.*)", line.strip())
        if not m:
            continue
        info = sc.sh_out(["bluetoothctl", "info", m.group(1)])
        out.append({"mac": m.group(1), "name": m.group(2),
                    "connected": "Connected: yes" in info, "trusted": "Trusted: yes" in info})
    return out


def bt_pair(secs=180):
    sc.sh_out(["bluetoothctl", "pairable", "on"])
    sc.sh_out(["bluetoothctl", "discoverable-timeout", str(secs)])
    sc.sh_out(["bluetoothctl", "discoverable", "on"])
    st["bt_pair_until"] = time.time() + secs
    sc.event("bt-pair-open")


def bt_watch():
    """Trust newly paired phones so they reconnect by themselves."""
    while True:
        time.sleep(10)
        if not cfg.get("bluetooth"):
            continue
        try:
            for d in bt_devices():
                if not d["trusted"]:
                    sc.sh_out(["bluetoothctl", "trust", d["mac"]])
                    log.info("bluetooth: trusted %s", d["name"])
                    sc.event("bt-paired", name=d["name"])
        except Exception as e:
            log.info("bt_watch: %s", e)


def bt_now_playing():
    out = sc.sh_out(["bluetoothctl", "player.show"], timeout=3)
    t = re.search(r"Title: (.*)", out)
    a = re.search(r"Artist: (.*)", out)
    return {"title": t.group(1).strip() if t else None,
            "artist": a.group(1).strip() if a else None}


# ---------------------------------------------------------------- radio on the Pi
def radio_play(url, name=""):
    if not re.match(r"https?://", url or ""):
        raise ValueError("radio url must be http(s)")
    radio_stop()
    p = subprocess.Popen(["gst-launch-1.0", "-q", "playbin", f"uri={url}",
                          'audio-sink="pulsesink client-name=spotcast-radio"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    st["radio"] = {"p": p, "url": url, "name": name or url}
    sc.event("radio", name=name or url)


def radio_stop():
    r = st.get("radio")
    st["radio"] = None
    if r and r["p"].poll() is None:
        r["p"].terminate()


# ---------------------------------------------------------------- DSP chain
def _modules():
    out = {}
    for line in sc.sh_out(["pactl", "list", "short", "modules"]).splitlines():
        f = line.split("\t")
        if len(f) > 2 and "spotcast_dsp_" in f[2]:
            out[f[0]] = f[2]
    return out


def pin_capture():
    """The streamer's parec must always read spotcast_sink.monitor (the end of the
    chain). WirePlumber moves it to the default sink's monitor when that changes."""
    for block in sc.sh_out(["pactl", "list", "source-outputs"]).split("Source Output #")[1:]:
        if 'application.name = "parec"' not in block:
            continue
        idx = block.split(chr(10), 1)[0].strip()
        src = re.search(r"Source: (\d+)", block)
        mon = sc.sh_out(["pactl", "list", "short", "sources"])
        want = next((l.split(chr(9))[0] for l in mon.splitlines()
                     if chr(9) + "spotcast_sink.monitor" + chr(9) in l), None)
        if want and src and src.group(1) != want:
            sc.sh_out(["pactl", "move-source-output", idx, "spotcast_sink.monitor"])
            log.info("dsp: capture moved back to spotcast_sink.monitor")


def apply_dsp():
    """spotcast_sink is what gets streamed. Optional filters sit in front of it:
    sources -> [EQ] -> [loudness] -> spotcast_sink. The default sink is the head."""
    for mid in _modules():
        sc.sh_out(["pactl", "unload-module", mid])
    head = "spotcast_sink"
    if cfg.get("loudness"):
        # sc4: rms/peak, attack ms, release ms, threshold dB, ratio, knee dB, makeup dB
        sc.sh_out(["pactl", "load-module", "module-ladspa-sink",
                   "sink_name=spotcast_dsp_level", f"sink_master={head}",
                   "plugin=sc4_1882", "label=sc4", "control=0.5,15,300,-24,4,8,9",
                   "sink_properties=device.description=spotcast_dsp_level"])
        head = "spotcast_dsp_level"
    if cfg.get("eq_on") and any(abs(g) > 0.05 for g in cfg["eq"]):
        gains = ",".join(f"{max(-12, min(12, g)):.1f}" for g in cfg["eq"]) + ",0"
        sc.sh_out(["pactl", "load-module", "module-ladspa-sink",
                   "sink_name=spotcast_dsp_eq", f"sink_master={head}",
                   "plugin=mbeq_1197", "label=mbeq", f"control={gains}",
                   "sink_properties=device.description=spotcast_dsp_eq"])
        head = "spotcast_dsp_eq"
    sinks = sc.sh_out(["pactl", "list", "short", "sinks"])
    if head not in sinks:
        log.info("dsp: %s did not load; playing without it", head)
        head = "spotcast_sink"
    sc.sh_out(["pactl", "set-default-sink", head])
    for i in sink_inputs():
        mod = re.search(r"Owner Module: (\d+)", i["block"])
        if mod and mod.group(1) in _modules():
            continue                    # the filters' own streams
        sc.sh_out(["pactl", "move-sink-input", i["idx"], head])
    time.sleep(0.5)
    pin_capture()
    log.info("dsp: loudness=%s eq=%s head=%s", cfg.get("loudness"), cfg.get("eq_on"), head)
    return head


# ---------------------------------------------------------------- room EQ
ROOM_LEAD = 1.0          # 1 kHz marker the app uses to line up its recording
ROOM_GAP = 0.35
ROOM_TONE = 1.4
ROOM_BANDS = [h for h in EQ_HZ if h <= 10000]


def room_plan():
    t, bands = ROOM_LEAD + 0.8, []
    for h in ROOM_BANDS:
        bands.append({"hz": h, "start": round(t, 3), "dur": ROOM_TONE})
        t += ROOM_TONE + ROOM_GAP
    return {"marker_hz": 1000, "marker_s": ROOM_LEAD, "bands": bands, "total_s": round(t, 2)}


def room_play():
    """Play the measurement tones through the receiver (EQ bypassed)."""
    rate, amp = 44100, 0.25
    pcm = bytearray()

    def tone(hz, secs):
        n = int(rate * secs)
        for i in range(n):
            env = min(1.0, i / 800, (n - i) / 800)
            v = int(32767 * amp * env * math.sin(2 * math.pi * hz * i / rate))
            pcm.extend(struct.pack("<hh", v, v))

    def silence(secs):
        pcm.extend(b"\0\0\0\0" * int(rate * secs))

    tone(1000, ROOM_LEAD)
    silence(0.8)
    for h in ROOM_BANDS:
        tone(h, ROOM_TONE)
        silence(ROOM_GAP)
    eq_was = cfg.get("eq_on")
    if eq_was:
        cfg["eq_on"] = False
        apply_dsp()

    def go():
        try:
            subprocess.run(["pacat", "--playback", "--raw", "--format=s16le", "--rate=44100",
                            "--channels=2", "--client-name=spotcast-roomeq",
                            "--device=spotcast_sink"], input=bytes(pcm), timeout=120)
        finally:
            if eq_was:
                cfg["eq_on"] = True
                apply_dsp()
    _pause_others("roomeq")
    sc.ensure_playing()
    time.sleep(1.0)                     # receiver picks up the stream
    threading.Thread(target=go, daemon=True).start()
    return room_plan()


def room_apply(levels):
    """levels: measured dB per ROOM_BANDS entry (relative). Flatten toward the
    median, gently: cut peaks fully, boost dips at most +4 dB."""
    lv = [float(x) for x in levels]
    if len(lv) != len(ROOM_BANDS):
        raise ValueError(f"need {len(ROOM_BANDS)} levels")
    ref = sorted(lv)[len(lv) // 2]
    eq = []
    for h in EQ_HZ:
        if h in ROOM_BANDS:
            d = ref - lv[ROOM_BANDS.index(h)]
            eq.append(round(max(-8.0, min(4.0, d * 0.8)), 1))
        else:
            eq.append(0.0)
    cfg["eq"], cfg["eq_on"] = eq, True
    save()
    apply_dsp()
    sc.event("room-eq", eq=eq)
    return eq


# ---------------------------------------------------------------- schedules
def run_schedule(s):
    a = s.get("action")
    log.info("schedule: %s %s", a, s.get("name", ""))
    sc.event("schedule", action=a, name=s.get("name"))
    if a == "off":
        sc.hand_back_now()
        put("Main_Zone", "<Power_Control><Power>Standby</Power></Power_Control>")
        zone2_set(power=False)
    elif a == "scene":
        sc_ = next((x for x in cfg["scenes"] if x["name"] == s.get("scene")), None)
        if sc_:
            apply_scene(sc_)
    elif a == "wake":
        # Source: an FM/AM preset or frequency, an internet radio stream, or
        # Spotify. Volume fades up from very quiet over fade_min minutes.
        target = float(s.get("volume_db", -40))
        start = target - 25
        st["fade"] = None                              # cancels a running fade
        pw, _ = sc.receiver_status()
        if pw != "On":
            sc.power_on()
            time.sleep(4)
        sc.set_receiver_volume(start)
        if s.get("tuner_preset") or s.get("tuner_freq"):
            if s.get("tuner_preset"):
                tuner_set("preset", s["tuner_preset"])
            else:
                tuner_set("freq", s["tuner_freq"], s.get("tuner_band", "FM"))
        elif s.get("radio"):
            radio_play(s["radio"], s.get("radio_name", ""))
        else:
            cur = sc.slots.current
            if cur:
                cur.ctl("play")
        fade_volume(start, target, float(s.get("fade_min", 10)))
    elif a == "sleep":
        sleep_fade(float(s.get("fade_min", 20)))


def fade_volume(start, target, minutes, then=None, on_stop=None):
    """Move the receiver volume start->target in 0.5 dB steps over `minutes`.
    Stops (calling on_stop) when a newer fade or sleep_cancel replaces it, or
    when someone moves the volume by hand (app, remote, Alexa)."""
    token = object()
    st["fade"] = token
    steps = max(1, int(abs(target - start) * 2))
    gap = max(2.0, minutes * 60 / steps)          # at least 2 s between requests

    def run():
        last = start
        try:
            for i in range(1, steps + 1):
                time.sleep(gap)
                if st["fade"] is not token:
                    log.info("fade: replaced")
                    if on_stop:
                        on_stop()
                    return
                now, _ = sc.receiver_volume()
                if now is not None and abs(now - last) > 1.0:
                    log.info("fade: volume moved by hand (%.1f -> %.1f), stopping", last, now)
                    st["fade"] = None
                    if on_stop:
                        on_stop()
                    return
                try:
                    last = sc.set_receiver_volume(start + (target - start) * i / steps)
                except Exception as e:
                    log.info("fade: %s", e)
            if st["fade"] is token:
                st["fade"] = None
                if then:
                    then()
        except Exception as e:
            log.info("fade: %s", e)
    threading.Thread(target=run, daemon=True).start()


def sleep_fade(minutes):
    """Fade to silence over `minutes`, then pause everything and go to standby."""
    cur, _ = sc.receiver_volume()
    if cur is None:
        return

    def done():
        st["sleep_until"] = 0
        _pause_others("none")
        sc.hand_back_now()
        # restore the level first (the receiver may ignore volume in standby),
        # muted so the jump back up is not heard
        put("Main_Zone", "<Volume><Mute>On</Mute></Volume>")
        sc.set_receiver_volume(cur)                 # next time starts at the old level
        put("Main_Zone", "<Power_Control><Power>Standby</Power></Power_Control>")
        time.sleep(3)
        try:
            put("Main_Zone", "<Volume><Mute>Off</Mute></Volume>")
        except Exception:
            pass

    def stopped():
        st["sleep_until"] = 0
        log.info("sleep: cancelled")
    st["sleep_until"] = time.time() + minutes * 60
    fade_volume(cur, max(-80.0, cur - 30), minutes, then=done, on_stop=stopped)
    log.info("sleep: fading out over %.0f min", minutes)


def sleep_cancel():
    st["fade"] = None
    st["sleep_until"] = 0


# ---------------------------------------------------------------- automations
def _in_window(frm, to, now_hm):
    return frm <= now_hm < to if frm <= to else (now_hm >= frm or now_hm < to)


def automations():
    """Quiet-hours volume cap and per-source sound, checked every 20 s from
    the receiver status spotcast already polls (no extra receiver traffic
    beyond a PUT when something has to change)."""
    last_src = None
    while True:
        time.sleep(20)
        try:
            pw, inp = sc.receiver_status()
            if pw != "On":
                last_src = None
                continue
            q = cfg.get("quiet") or {}
            if q.get("on") and _in_window(q.get("from", "22:00"), q.get("to", "07:00"),
                                          time.strftime("%H:%M")):
                vol, _ = sc.receiver_volume()
                cap = float(q.get("max_db", -40))
                if vol is not None and vol > cap and not st.get("fade"):
                    sc.set_receiver_volume(cap)
                    log.info("quiet hours: volume %.1f capped to %.1f", vol, cap)
            # the source that is heard: a Pi source when the receiver is on PC,
            # otherwise the receiver input itself
            src = (st["active"] or "spotify") if inp == "PC" else inp
            if src != last_src:
                last_src = src
                prof = (cfg.get("source_sound") or {}).get(src)
                if cfg.get("source_sound_on") and prof:
                    log.info("source sound: %s -> %s", src, prof)
                    if prof.get("straight"):
                        set_sound("straight", True)
                    elif prof.get("program"):
                        set_sound("program", prof["program"])
                    for k in ("enhancer", "drc", "pure_direct", "cinema_3d",
                              "dialogue_lift", "bass", "treble"):
                        if prof.get(k) is not None:
                            set_sound(k, prof[k])
        except Exception as e:
            log.info("automations: %s", e)


def scheduler():
    while True:
        time.sleep(15)
        try:
            now = time.localtime()
            hm = time.strftime("%H:%M", now)
            day = time.strftime("%Y-%m-%d", now)
            for s in cfg.get("schedules", []):
                if not s.get("enabled", True) or s.get("time") != hm:
                    continue
                if now.tm_wday not in s.get("days", list(range(7))):
                    continue
                key = f"{s.get('id')}|{day}"
                if st["fired"].get(key):
                    continue
                st["fired"][key] = True
                threading.Thread(target=run_schedule, args=(s,), daemon=True).start()
        except Exception as e:
            log.info("scheduler: %s", e)


# ---------------------------------------------------------------- history / now playing
def now_playing():
    a = st["active"]
    if a == "spotify":
        np = sc.now_playing()
        if isinstance(np, dict):
            return np
        return {"title": np} if np else {}
    if a == "airplay":
        return dict(st["airplay_meta"])
    if a == "bluetooth":
        return bt_now_playing()
    if a == "radio" and st["radio"]:
        return {"title": st["radio"]["name"]}
    return {}


def history_add(kind):
    rec = {"t": int(time.time()), "source": kind}
    try:
        with open(HISTORY, "a") as f:
            f.write(json.dumps(rec) + "\n")
        if os.path.getsize(HISTORY) > 1_000_000:
            lines = open(HISTORY).readlines()[-4000:]
            open(HISTORY, "w").writelines(lines)
    except OSError:
        pass

    def later():                         # titles arrive a moment after audio starts
        time.sleep(6)
        np = now_playing()
        if np.get("title"):
            sc.event("now-playing", source=kind, **{k: v for k, v in np.items()
                                                    if k in ("title", "artist")})
    threading.Thread(target=later, daemon=True).start()


def history(days=7):
    try:
        recs = [json.loads(l) for l in open(HISTORY)]
    except Exception:
        recs = []
    cutoff = time.time() - days * 86400
    recs = [r for r in recs if r["t"] >= cutoff]
    # minutes per source per day, from source start to the next start (cap 3 h)
    per = {}
    for i, r in enumerate(recs):
        end = recs[i + 1]["t"] if i + 1 < len(recs) else time.time()
        mins = min(end - r["t"], 3 * 3600) / 60
        d = time.strftime("%Y-%m-%d", time.localtime(r["t"]))
        per.setdefault(d, {}).setdefault(r["source"], 0)
        per[d][r["source"]] += mins
    tracks = []
    try:
        for l in open(sc.EVENT_LOG).readlines()[-3000:]:
            e = json.loads(l)
            if e.get("kind") == "now-playing" and e["t"] >= cutoff:
                tracks.append(e)
    except Exception:
        pass
    return {"minutes": {d: {k: round(v) for k, v in s.items()} for d, s in per.items()},
            "recent": tracks[-40:][::-1],
            "problems": sc.recent_problems(20)}


# ---------------------------------------------------------------- API
def status():
    return {
        "active": st["active"], "label": LABEL.get(st["active"]),
        "since": int(st["since"]) if st["since"] else None,
        "now_playing": now_playing(),
        "sources": {"airplay": cfg["airplay"] and bool(shairport_bin()),
                    "airplay2": airplay2(), "dlna": cfg["dlna"],
                    "bluetooth": cfg["bluetooth"], "airplay_volume": cfg["airplay_volume"]},
        "radio": {"name": st["radio"]["name"]} if st["radio"] else None,
        "dsp": {"loudness": cfg["loudness"], "eq_on": cfg["eq_on"], "eq": cfg["eq"],
                "eq_hz": EQ_HZ},
        "bt_pairing_s": max(0, int(st["bt_pair_until"] - time.time())),
    }


def _b(v):
    return str(v).lower() in ("1", "true", "on", "yes")


# ---------------------------------------------------------------- receiver sources
# The receiver's own FM/AM tuner, its front USB port and its media-server
# client ("SERVER" input, YNC tag "PC"). Switching to one of them makes the
# receiver the active source: the Pi's streams are paused.
# The media-server client is the receiver's "PC" input, the same input the
# Pi streams to; switching to it pauses the Pi's sources first, so the
# watchdog in spotcast.py does not take it back.
RX_SOURCES = {"tuner": ("TUNER", "Tuner"), "usb": ("USB", "USB"), "server": ("PC", "PC")}


def rx_select(src):
    inp, _ = RX_SOURCES[src]
    pw, _ = sc.receiver_status()
    if pw != "On":
        sc.power_on()
        time.sleep(4)
    if st["active"] != "receiver":
        st["active"], st["since"] = "receiver", time.time()
        history_add("receiver " + src)
        _pause_others("receiver")        # before switching: no fight with the watchdog
    _, cur = sc.receiver_status()
    if cur != inp:
        put("Main_Zone", f"<Input><Input_Sel>{inp}</Input_Sel></Input>")


def tuner_state():
    x = get("Tuner", "<Play_Info>GetParam</Play_Info>") or ""
    p = get("Tuner", "<Play_Control><Preset><Preset_Sel_Item>GetParam</Preset_Sel_Item>"
            "</Preset></Play_Control>") or ""
    band = _tag(x, "Band") or "FM"
    m = re.search(r"<Current><Val>(\d+)</Val><Exp>(\d)</Exp>", x)
    freq = int(m.group(1)) / 10 ** int(m.group(2)) if m else None
    presets = [{"n": int(n), "title": t.split(":", 1)[-1].strip()}
               for n, t in re.findall(r"<Param>(\d+)</Param><RW>\w+</RW><Title>([^<]*)</Title>", p)]
    sig = re.search(r"<Tuned>(\w+)</Tuned><Stereo>(\w+)</Stereo>", x)
    return {"band": band, "freq": freq, "unit": "kHz" if band == "AM" else "MHz",
            "preset": _tag(x, "Preset_Sel"), "presets": presets,
            "tuned": bool(sig and sig.group(1) == "Assert"),
            "stereo": bool(sig and sig.group(2) == "Assert")}


def tuner_set(action, value=None, band=None):
    rx_select("tuner")
    pc = lambda inner: put("Tuner", f"<Play_Control>{inner}</Play_Control>")
    if action == "preset":
        pc(f"<Preset><Preset_Sel>{int(value)}</Preset_Sel></Preset>")
    elif action == "band":
        if value not in ("FM", "AM"):
            raise ValueError("band must be FM or AM")
        pc(f"<Tuning><Band>{value}</Band></Tuning>")
    elif action == "freq":
        band = (band or "FM").upper()
        if band == "FM":
            v = round(float(value) * 5) * 20          # 87.5..107.9 MHz, 0.2 steps
            if not 8750 <= v <= 10790:
                raise ValueError("FM is 87.5 to 107.9 MHz")
            f = f"<FM><Val>{v}</Val><Exp>2</Exp><Unit>MHz</Unit></FM>"
        else:
            v = round(float(value) / 10) * 10
            if not 530 <= v <= 1710:
                raise ValueError("AM is 530 to 1710 kHz")
            f = f"<AM><Val>{v}</Val><Exp>0</Exp><Unit>kHz</Unit></AM>"
        pc(f"<Tuning><Band>{band}</Band><Freq>{f}</Freq></Tuning>")
    elif action in ("scan_up", "scan_down"):
        b = tuner_state()["band"]
        d = "Auto Up" if action == "scan_up" else "Auto Down"
        pc(f"<Tuning><Freq><{b}><Val>{d}</Val></{b}></Freq></Tuning>")
    elif action != "select":
        raise ValueError("unknown tuner action")
    time.sleep(0.5)
    return tuner_state()


def browse_state(src):
    _, tag = RX_SOURCES[src]
    cfgx = get(tag, "<Config>GetParam</Config>") or ""
    li = get(tag, "<List_Info>GetParam</List_Info>") or ""
    pi = get(tag, "<Play_Info>GetParam</Play_Info>") or ""
    lines = [{"line": int(n), "text": html.unescape(t), "kind": a}
             for n, t, a in re.findall(
                 r"<Line_(\d)><Txt>([^<]*)</Txt><Attribute>([^<]*)</Attribute></Line_\d>", li)
             if a != "Unselectable"]
    cur = int(_tag(li, "Current_Line") or 1)
    mx = int(_tag(li, "Max_Line") or 0)
    return {"source": src, "ready": _tag(cfgx, "Feature_Availability") == "Ready",
            "menu": html.unescape(_tag(li, "Menu_Name") or ""),
            "layer": int(_tag(li, "Menu_Layer") or 1),
            "busy": _tag(li, "Menu_Status") == "Busy",
            "lines": lines, "first": cur - (cur - 1) % 8, "total": mx,
            "playing": _tag(pi, "Playback_Info") == "Play",
            "artist": html.unescape(_tag(pi, "Artist") or ""),
            "album": html.unescape(_tag(pi, "Album") or ""),
            "song": html.unescape(_tag(pi, "Song") or ""),
            "repeat": _tag(pi, "Repeat"), "shuffle": _tag(pi, "Shuffle")}


def browse_do(src, action, value=None):
    _, tag = RX_SOURCES[src]
    lc = lambda inner: put(tag, f"<List_Control>{inner}</List_Control>")
    pc = lambda inner: put(tag, f"<Play_Control>{inner}</Play_Control>")
    if action == "select_input":
        rx_select(src)
    elif action == "open":                      # a line on the current page, 1..8
        rx_select(src)
        lc(f"<Direct_Sel>Line_{max(1, min(8, int(value)))}</Direct_Sel>")
    elif action == "back":
        lc("<Cursor>Back</Cursor>")
    elif action == "home":
        lc("<Cursor>Back to Home</Cursor>")
    elif action == "page":                      # first line of the wanted page
        lc(f"<Jump_Line>{max(1, int(value))}</Jump_Line>")
    elif action in ("Play", "Stop", "Pause", "Skip Fwd", "Skip Rev"):
        if action == "Play":
            rx_select(src)
        pc(f"<Playback>{action}</Playback>")
    elif action == "repeat" and value in ("Off", "Single", "All"):
        pc(f"<Play_Mode><Repeat>{value}</Repeat></Play_Mode>")
    elif action == "shuffle" and value in ("Off", "On"):
        pc(f"<Play_Mode><Shuffle>{value}</Shuffle></Play_Mode>")
    else:
        raise ValueError("unknown browse action")
    # the receiver answers "Busy" while it reads the folder; poll only the
    # list status (one request per 0.6 s, at most 8), then read everything once
    for _ in range(8):
        time.sleep(0.6)
        li = get(tag, "<List_Info>GetParam</List_Info>") or ""
        if _tag(li, "Menu_Status") != "Busy":
            break
    return browse_state(src)


INPUT_TAGS = {"HDMI1": "HDMI_1", "HDMI2": "HDMI_2", "HDMI3": "HDMI_3", "HDMI4": "HDMI_4",
              "HDMI5": "HDMI_5", "AV1": "AV_1", "AV2": "AV_2", "AV3": "AV_3", "AV4": "AV_4",
              "AV5": "AV_5", "AV6": "AV_6", "V-AUX": "V_AUX", "AUDIO1": "AUDIO_1",
              "AUDIO2": "AUDIO_2", "DOCK": "DOCK", "USB": "USB"}


def input_names():
    x = sc.ync('<YAMAHA_AV cmd="GET"><System><Input_Output><Input_Name>GetParam'
               '</Input_Name></Input_Output></System></YAMAHA_AV>') or ""
    return [{"id": i, "name": html.unescape(_tag(x, t) or i)} for i, t in INPUT_TAGS.items()]


def rename_input(inp, name):
    t = INPUT_TAGS.get(inp)
    if not t:
        raise ValueError("this input can't be renamed")
    name = (name or "").strip()[:9] or inp            # the display holds 9 characters
    if not re.fullmatch(r"[ -~]+", name):
        raise ValueError("use plain letters, numbers and symbols")
    sc.ync(f'<YAMAHA_AV cmd="PUT"><System><Input_Output><Input_Name><{t}>{html.escape(name)}'
           f'</{t}></Input_Name></Input_Output></System></YAMAHA_AV>')
    _inputs["t"] = 0                                  # input list carries the titles
    return input_names()


def api(path, arg, body=None):
    """/api/x/... ; arg(k) reads the query string, body is parsed JSON (POST)."""
    p = path[len("/api/x/"):]
    if p == "status":
        return status()
    if p == "sound":
        if arg("key"):
            set_sound(arg("key"), arg("value"))
        return sound_state()
    if p == "inputs":
        return {"inputs": inputs()}
    if p == "input_names":
        if arg("id"):
            return {"inputs": rename_input(arg("id"), arg("name"))}
        return {"inputs": input_names()}
    if p == "tuner":
        if arg("action"):
            return tuner_set(arg("action"), arg("value"), arg("band"))
        return tuner_state()
    if p in ("usb", "server"):
        if arg("action"):
            return browse_do(p, arg("action"), arg("value"))
        return browse_state(p)
    if p == "zone2":
        if any(arg(k) for k in ("power", "input", "volume_db", "mute")):
            zone2_set(power=arg("power") or None, input=arg("input") or None,
                      volume_db=arg("volume_db") or None, mute=arg("mute") or None)
        return zone2_state()
    if p == "sources":
        for k in ("airplay", "dlna", "bluetooth", "airplay_volume"):
            if arg(k):
                cfg[k] = _b(arg(k))
        save()
        sync_sources()
        return status()["sources"]
    if p == "bt":
        if arg("pair"):
            bt_pair()
        if arg("forget"):
            sc.sh_out(["bluetoothctl", "remove", arg("forget")])
        if arg("disconnect"):
            sc.sh_out(["bluetoothctl", "disconnect", arg("disconnect")], timeout=10)
        return {"devices": bt_devices(),
                "pairing_s": max(0, int(st["bt_pair_until"] - time.time())),
                "name": "Living Room Speaker"}
    if p == "radio":
        if arg("stop"):
            radio_stop()
        elif arg("url"):
            radio_play(arg("url"), arg("name"))
        return {"radio": {"name": st["radio"]["name"]} if st["radio"] else None}
    if p == "airplay_volume":
        return {"db": airplay_volume(arg("v"))}
    if p == "dsp":
        if arg("loudness"):
            cfg["loudness"] = _b(arg("loudness"))
        if arg("eq_on"):
            cfg["eq_on"] = _b(arg("eq_on"))
        if arg("eq"):
            g = [float(x) for x in arg("eq").split(",")]
            if len(g) != 15:
                raise ValueError("eq needs 15 values")
            cfg["eq"] = [max(-12.0, min(12.0, x)) for x in g]
        save()
        apply_dsp()
        return status()["dsp"]
    if p == "roomeq/start":
        return room_play()
    if p == "roomeq/plan":
        return room_plan()
    if p == "roomeq/apply":
        return {"eq": room_apply(arg("levels").split(","))}
    if p == "scenes":
        if body is not None:
            if not isinstance(body, list) or not all(isinstance(s, dict) and s.get("name")
                                                     for s in body):
                raise ValueError("scenes: list of {name, ...}")
            cfg["scenes"] = body
            save()
        if arg("apply"):
            s = next((x for x in cfg["scenes"] if x["name"] == arg("apply")), None)
            if not s:
                raise ValueError("no such scene")
            threading.Thread(target=apply_scene, args=(s,), daemon=True).start()
        return {"scenes": cfg["scenes"], "programs": SOUND_PROGRAMS}
    if p == "schedules":
        if body is not None:
            if not isinstance(body, list):
                raise ValueError("schedules: list")
            for s in body:
                s.setdefault("id", uuid.uuid4().hex[:8])
                if not re.fullmatch(r"\d\d:\d\d", s.get("time", "")):
                    raise ValueError("time must be HH:MM")
                if s.get("action") not in ("wake", "off", "scene", "sleep"):
                    raise ValueError("action must be wake/off/scene/sleep")
            cfg["schedules"] = body
            save()
        if arg("run"):
            s = next((x for x in cfg["schedules"] if x.get("id") == arg("run")), None)
            if s:
                threading.Thread(target=run_schedule, args=(s,), daemon=True).start()
        return {"schedules": cfg["schedules"]}
    if p == "history":
        return history(int(arg("days") or 7))
    if p == "sleep":                         # ?minutes=20 starts, ?cancel=1 stops
        if arg("cancel"):
            sleep_cancel()
        elif arg("minutes"):
            sleep_fade(max(1.0, min(120.0, float(arg("minutes")))))
        return {"sleep_s": max(0, int(st["sleep_until"] - time.time()))}
    if p == "fade":                          # ?to=-35&minutes=1 (volume_db)
        cur, _ = sc.receiver_volume()
        if cur is None:
            raise ValueError("receiver not answering")
        fade_volume(cur, float(arg("to")), float(arg("minutes") or 1))
        return {"from": cur, "to": float(arg("to"))}
    if p == "quiet":
        if body is not None:
            q = {**cfg["quiet"], **body}
            for k in ("from", "to"):
                if not re.fullmatch(r"\d\d:\d\d", q[k]):
                    raise ValueError("time must be HH:MM")
            q["max_db"] = max(-80.0, min(sc.MAX_DB, float(q["max_db"])))
            q["on"] = bool(q["on"])
            cfg["quiet"] = q
            save()
        return cfg["quiet"]
    if p == "source_sound":
        if body is not None:
            if "on" in body:
                cfg["source_sound_on"] = bool(body["on"])
            if isinstance(body.get("profiles"), dict):
                cfg["source_sound"] = body["profiles"]
            save()
        return {"on": cfg["source_sound_on"], "profiles": cfg["source_sound"],
                "programs": SOUND_PROGRAMS}
    if p == "quick":                         # Android tile / home-screen widget
        d = arg("do")
        if d == "power":
            pw, _ = sc.receiver_status()
            if pw == "On":
                put("Main_Zone", "<Power_Control><Power>Standby</Power></Power_Control>")
            else:
                sc.power_on()
        elif d in ("vol_up", "vol_down"):
            cur, _ = sc.receiver_volume()
            if cur is not None:
                sc.set_receiver_volume(cur + (2 if d == "vol_up" else -2))
        elif d == "night":
            set_sound("drc", not sound_state()["drc"])
        elif d == "mute":
            _, m = sc.receiver_volume()
            put("Main_Zone", f"<Volume><Mute>{'Off' if m else 'On'}</Mute></Volume>")
        sc.invalidate_basic()
        pw, inp = sc.receiver_status()
        vol, mute = sc.receiver_volume()
        return {"power": pw, "input": inp, "volume_db": vol, "mute": mute,
                "night": sound_state()["drc"]}
    if p == "duck":                          # phone call: ?on=1 lowers, ?on=0 restores
        cur, _ = sc.receiver_volume()
        if _b(arg("on")):
            if st.get("duck_from") is None and cur is not None:
                st["duck_from"], st["fade"] = cur, None
                sc.set_receiver_volume(max(-80.0, cur - float(arg("db") or 20)))
        elif st.get("duck_from") is not None:
            back, st["duck_from"] = st["duck_from"], None
            sc.set_receiver_volume(back)            # restore in one step
        return {"ducked": st.get("duck_from") is not None}
    if p == "health":
        return {"latency": sc.rx_latency_report(), "rx_ok": sc.rx_stats["ok"],
                "rx_fail": sc.rx_stats["fail"]}
    raise KeyError(p)


def html_section():
    s = status()
    np = s["now_playing"] or {}
    np_txt = " - ".join(x for x in (np.get("title"), np.get("artist")) if x) or "&nbsp;"
    srcs = ", ".join(LABEL[k] for k in ("airplay", "dlna", "bluetooth")
                     if s["sources"].get(k)) or "none"
    h = history(1)
    mins = h["minutes"].get(time.strftime("%Y-%m-%d"), {})
    today = ", ".join(f"{LABEL.get(k, k)} {v} min" for k, v in mins.items()) or "nothing yet"
    tracks = "".join(
        f"<li>{time.strftime('%H:%M', time.localtime(e['t']))} &middot; "
        f"{html.escape(LABEL.get(e.get('source'), ''))}: {html.escape(e.get('title', ''))}"
        f"{(' - ' + html.escape(e['artist'])) if e.get('artist') else ''}</li>"
        for e in h["recent"][:10]) or "<li>none</li>"
    return f"""<h3>Now playing</h3>
<p><b>{html.escape(s['label'] or 'Nothing')}</b> &middot; {html.escape(np_txt) if np_txt != '&nbsp;' else ''}</p>
<p><small>Sources on: {srcs}{' (AirPlay 2)' if s['sources']['airplay2'] else ''}
&middot; loudness {'on' if s['dsp']['loudness'] else 'off'}
&middot; room EQ {'on' if s['dsp']['eq_on'] else 'off'}</small></p>
<h3>Today</h3><p>{html.escape(today)}</p><ul>{tracks}</ul>"""


def start():
    load()
    try:
        apply_dsp()
    except Exception as e:
        log.info("dsp: %s", e)
    sync_sources()
    for f in (arbiter, airplay_meta_reader, bt_watch, scheduler, automations):
        threading.Thread(target=f, daemon=True, name=f.__name__).start()
    log.info("extras: airplay=%s (AirPlay 2: %s) dlna=%s bluetooth=%s",
             cfg["airplay"], airplay2(), cfg["dlna"], cfg["bluetooth"])
