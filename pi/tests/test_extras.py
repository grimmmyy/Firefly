"""Offline tests for pi/extras.py (no receiver, no PipeWire).

extras.py reaches spotcast.py through sys.modules["__main__"]; these tests put
a fake bridge there that records every YNC command instead of sending it, so
they also pin down the exact XML the RX-V671 receives.

Run:  python -m unittest discover -s pi/tests -v
"""
import json
import logging
import os
import sys
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


class FakeBridge(types.ModuleType):
    def __init__(self):
        super().__init__("__main__")
        self.log = logging.getLogger("test")
        self.MAX_DB = -10.0
        self.sent = []                      # every YNC body, in order
        self.power, self.input = "On", "PC"
        self.volume, self.mute = -40.0, False
        self.replies = {}                   # substring of request -> reply XML
        self.rx_stats = {"ok": 0, "fail": 0}

    def ync(self, xml):
        self.sent.append(xml)
        for k, v in self.replies.items():
            if k in xml:
                return v
        return '<YAMAHA_AV rsp="PUT" RC="0"></YAMAHA_AV>'

    def invalidate_basic(self):
        pass

    def receiver_status(self):
        return self.power, self.input

    def receiver_volume(self):
        return self.volume, self.mute

    def set_receiver_volume(self, db):
        db = max(-80.5, min(self.MAX_DB, round(float(db) * 2) / 2))
        self.volume = db
        self.sent.append(f"VOLUME {db}")
        return db

    def power_on(self):
        self.power = "On"

    def basic_status(self):
        return ""

    def event(self, *a, **k):
        pass

    def hand_back_now(self):
        pass

    def sh_out(self, *a, **k):
        return ""

    def rx_latency_report(self):
        return []

    class slots:                            # no Spotify slots in tests
        slots = []
        current = None


bridge = FakeBridge()
sys.modules["__main__"] = bridge
import extras  # noqa: E402

extras.cfg.update(json.loads(json.dumps(extras.DEFAULTS)))
extras.save = lambda: None                  # never write /var/lib on the test machine
extras.history_add = lambda kind: None


def puts():
    return [x for x in bridge.sent if 'cmd="PUT"' in x]


class Base(unittest.TestCase):
    def setUp(self):
        bridge.sent.clear()
        bridge.replies.clear()
        bridge.power, bridge.input, bridge.volume = "On", "PC", -40.0
        extras.st["active"] = None
        extras.st["fade"] = None
        extras.st["sleep_until"] = 0
        bridge.rx_stats["quiet_until"] = 0
        extras._sound_cache.clear()
        extras._tuner_cache.update(t=0.0, v=None)
        extras.time.sleep = lambda s: None  # no real waiting


class QuietHours(Base):
    def test_window_same_day(self):
        self.assertTrue(extras._in_window("13:00", "15:00", "14:00"))
        self.assertFalse(extras._in_window("13:00", "15:00", "15:00"))
        self.assertFalse(extras._in_window("13:00", "15:00", "12:59"))

    def test_window_over_midnight(self):
        for t in ("22:00", "23:59", "00:00", "06:59"):
            self.assertTrue(extras._in_window("22:00", "07:00", t), t)
        for t in ("07:00", "12:00", "21:59"):
            self.assertFalse(extras._in_window("22:00", "07:00", t), t)

    def test_api_validates_time_and_clamps_cap(self):
        with self.assertRaises(ValueError):
            extras.api("/api/x/quiet", lambda k: "", {"from": "9pm"})
        q = extras.api("/api/x/quiet", lambda k: "", {"on": 1, "max_db": 20})
        self.assertEqual(q["max_db"], bridge.MAX_DB)     # never above the app ceiling
        self.assertIs(q["on"], True)


class Tuner(Base):
    PLAY_INFO = ("<Tuner><Play_Info><Preset><Preset_Sel>2</Preset_Sel></Preset><Tuning>"
                 "<Band>FM</Band><Freq><Current><Val>9810</Val><Exp>2</Exp><Unit>MHz</Unit>"
                 "</Current></Freq></Tuning><Signal_Info><Tuned>Assert</Tuned>"
                 "<Stereo>Negate</Stereo></Signal_Info></Play_Info></Tuner>")
    PRESETS = ("<Item_1><Param>No Preset</Param><RW>R</RW><Title></Title></Item_1>"
               "<Item_2><Param>1</Param><RW>RW</RW><Title>1 : FM 98.10 MHz</Title></Item_2>")

    def setUp(self):
        super().setUp()
        bridge.replies["<Play_Info>GetParam"] = self.PLAY_INFO
        bridge.replies["Preset_Sel_Item"] = self.PRESETS

    def test_state_parsing(self):
        s = extras.tuner_state()
        self.assertEqual((s["band"], s["freq"], s["unit"]), ("FM", 98.1, "MHz"))
        self.assertEqual(s["presets"], [{"n": 1, "title": "FM 98.10 MHz"}])
        self.assertTrue(s["tuned"])
        self.assertFalse(s["stereo"])

    def test_fm_frequency_rounds_to_200khz_steps(self):
        extras.tuner_set("freq", "98.13", "FM")
        self.assertIn("<FM><Val>9810</Val><Exp>2</Exp><Unit>MHz</Unit></FM>", puts()[-1])
        extras.tuner_set("freq", "101.5", "FM")
        self.assertIn("<Val>10150</Val>", puts()[-1])
        extras.tuner_set("freq", "98.2", "FM")           # between channels: nearest valid
        self.assertRegex(puts()[-1], "<Val>(9810|9830)</Val>")
        extras.tuner_set("freq", "87.5", "FM")
        self.assertIn("<Val>8750</Val>", puts()[-1])
        extras.tuner_set("freq", "107.9", "FM")
        self.assertIn("<Val>10790</Val>", puts()[-1])

    def test_am_frequency_rounds_to_10khz(self):
        extras.tuner_set("freq", "1073", "AM")
        self.assertIn("<AM><Val>1070</Val><Exp>0</Exp><Unit>kHz</Unit></AM>", puts()[-1])

    def test_out_of_band_rejected(self):
        for band, v in (("FM", "86"), ("FM", "110"), ("AM", "500"), ("AM", "1800")):
            with self.assertRaises(ValueError, msg=f"{band} {v}"):
                extras.tuner_set("freq", v, band)

    def test_switches_input_and_pauses_pi_sources_first(self):
        bridge.input = "PC"
        extras.tuner_set("preset", "1")
        p = puts()
        self.assertIn("<Input_Sel>TUNER</Input_Sel>", p[0])
        self.assertIn("<Preset_Sel>1</Preset_Sel>", p[-1])
        self.assertEqual(extras.st["active"], "receiver")

    def test_no_input_switch_when_already_on_tuner(self):
        bridge.input = "TUNER"
        extras.tuner_set("preset", "1")
        self.assertFalse(any("Input_Sel" in x for x in puts()))


class TunerCache(Tuner):
    def test_repeated_reads_hit_the_receiver_once(self):
        extras.tuner_state(); extras.tuner_state(); extras.tuner_state()
        self.assertEqual(len([x for x in bridge.sent if "<Tuner>" in x]), 2)  # info + presets


class Browse(Base):
    LIST = ("<USB><List_Info><Menu_Status>Ready</Menu_Status><Menu_Layer>2</Menu_Layer>"
            "<Menu_Name>Music</Menu_Name><Current_List>"
            "<Line_1><Txt>Albums &amp; Singles</Txt><Attribute>Container</Attribute></Line_1>"
            "<Line_2><Txt>song.mp3</Txt><Attribute>Item</Attribute></Line_2>"
            "<Line_3><Txt></Txt><Attribute>Unselectable</Attribute></Line_3>"
            "</Current_List><Cursor_Position><Current_Line>9</Current_Line>"
            "<Max_Line>20</Max_Line></Cursor_Position></List_Info></USB>")

    def test_list_parsing_and_paging(self):
        bridge.replies["<List_Info>"] = self.LIST
        s = extras.browse_state("usb")
        self.assertEqual([l["text"] for l in s["lines"]], ["Albums & Singles", "song.mp3"])
        self.assertEqual((s["first"], s["total"], s["layer"]), (9, 20, 2))

    def test_placeholder_metadata_hidden(self):
        bridge.replies["<Play_Info>"] = ("<Play_Info><Meta_Info><Artist>---</Artist>"
                                         "<Song>Real Song</Song></Meta_Info></Play_Info>")
        s = extras.browse_state("server")
        self.assertEqual(s["artist"], "")
        self.assertEqual(s["song"], "Real Song")

    def test_media_server_is_the_pc_input(self):
        bridge.input = "HDMI1"
        extras.browse_do("server", "open", "1")
        self.assertIn("<Input_Sel>PC</Input_Sel>", puts()[0])
        self.assertIn("<PC><List_Control><Direct_Sel>Line_1</Direct_Sel>", puts()[1])

    def test_opening_does_not_send_bad_lines(self):
        extras.browse_do("usb", "open", "99")
        self.assertIn("Line_8", puts()[-1])

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            extras.browse_do("usb", "format_drive")


class InputNames(Base):
    def test_rename_trims_to_nine_and_escapes(self):
        bridge.replies["Input_Name>GetParam"] = "<Input_Name><HDMI_1>Fire TV</HDMI_1></Input_Name>"
        extras.rename_input("HDMI1", "  Fire TV Stick 4K ")
        self.assertIn("<HDMI_1>Fire TV S</HDMI_1>", puts()[-1])

    def test_rejects_unknown_input_and_markup(self):
        with self.assertRaises(ValueError):
            extras.rename_input("TUNER", "x")
        with self.assertRaises(ValueError):
            extras.rename_input("HDMI1", "é<b>")

    def test_empty_name_resets(self):
        extras.rename_input("AV1", "")
        self.assertIn("<AV_1>AV1</AV_1>", puts()[-1])


class Sound(Base):
    def test_dialogue_lift_clamped(self):
        extras.set_sound("dialogue_lift", "9")
        self.assertIn("<Dialogue_Lift>5</Dialogue_Lift>", puts()[-1])

    def test_cinema_3d(self):
        extras.set_sound("cinema_3d", "false")
        self.assertIn("<_3D_Cinema_DSP>Off</_3D_Cinema_DSP>", puts()[-1])

    def test_bad_program_rejected(self):
        with self.assertRaises(ValueError):
            extras.set_sound("program", "Stadium Rock")


class SoundQuietWindow(Base):
    def test_sound_change_opens_quiet_window(self):
        extras.set_sound("enhancer", "1")
        self.assertGreater(extras.sound_wait(), 25)

    def test_second_sound_change_refused_while_settling(self):
        extras.set_sound("enhancer", "1")
        with self.assertRaises(extras.SoundBusy):
            extras.set_sound("straight", "1")
        self.assertEqual(len(puts()), 1)            # nothing more reached the receiver

    def test_volume_and_input_do_not_open_window(self):
        extras.put("Main_Zone", "<Input><Input_Sel>TUNER</Input_Sel></Input>")
        extras.put("Main_Zone", "<Volume><Mute>On</Mute></Volume>")
        self.assertEqual(extras.sound_wait(), 0)

    def test_scene_sends_all_sound_settings_in_one_batch_after_volume(self):
        extras.apply_scene({"name": "Movie", "program": "Standard", "enhancer": False,
                            "drc": True, "volume_db": -35})
        sent = [x for x in bridge.sent if 'cmd="PUT"' in x or x.startswith("VOLUME")]
        self.assertTrue(sent[0].startswith("VOLUME"))          # volume before sound
        self.assertEqual(len([x for x in sent if "<Surround>" in x or "<Sound_Video>" in x]), 3)
        self.assertGreater(extras.sound_wait(), 25)

    def test_api_reports_what_was_set_without_asking_receiver(self):
        extras._sound_cache.update({"program": "2ch Stereo", "straight": True})
        before = len(bridge.sent)
        r = extras.api("/api/x/sound", {"key": "program", "value": "Standard"}.get)
        self.assertEqual((r["program"], r["straight"]), ("Standard", False))
        self.assertGreater(r["settling_s"], 25)
        self.assertEqual(len(bridge.sent), before + 1)       # just the PUT


class Fades(Base):
    def run_threads_inline(self):
        extras.threading.Thread = lambda target, args=(), daemon=None: types.SimpleNamespace(
            start=lambda: target(*args))

    def test_fade_reaches_target_and_runs_then(self):
        self.run_threads_inline()
        done = []
        bridge.volume = -50.0
        extras.fade_volume(-50, -45, 1, then=lambda: done.append(1))
        self.assertEqual(bridge.volume, -45.0)
        self.assertEqual(done, [1])

    def test_fade_stops_when_volume_moved_by_hand(self):
        self.run_threads_inline()
        real = bridge.set_receiver_volume
        calls = []

        def set_then_user_moves(db):
            calls.append(db)
            out = real(db)
            if len(calls) == 2:
                bridge.volume = -20.0          # someone turned it up with the remote
            return out
        bridge.set_receiver_volume = set_then_user_moves
        stopped = []
        bridge.volume = -50.0
        try:
            extras.fade_volume(-50, -40, 1, on_stop=lambda: stopped.append(1))
        finally:
            bridge.set_receiver_volume = real
        self.assertEqual(len(calls), 2)
        self.assertEqual(stopped, [1])
        self.assertEqual(bridge.volume, -20.0)

    def test_sleep_ends_in_standby_at_old_volume(self):
        self.run_threads_inline()
        bridge.volume = -35.0
        extras.sleep_fade(1)
        self.assertTrue(any("<Power>Standby</Power>" in x for x in puts()))
        self.assertEqual(bridge.volume, -35.0)
        self.assertEqual(extras.st["sleep_until"], 0)


class Schedules(Base):
    def test_validation(self):
        bad = [{"time": "7am", "action": "wake"}, {"time": "07:00", "action": "explode"}]
        for s in bad:
            with self.assertRaises(ValueError):
                extras.api("/api/x/schedules", lambda k: "", [s])
        ok = extras.api("/api/x/schedules", lambda k: "",
                        [{"time": "07:00", "action": "sleep", "fade_min": 20}])
        self.assertTrue(ok["schedules"][0]["id"])


class Duck(Base):
    def test_duck_and_restore(self):
        bridge.volume = -30.0
        arg = {"on": "1"}
        self.assertTrue(extras.api("/api/x/duck", lambda k: arg.get(k, ""))["ducked"])
        self.assertEqual(bridge.volume, -50.0)
        extras.api("/api/x/duck", lambda k: arg.get(k, ""))      # second ring: no double duck
        self.assertEqual(bridge.volume, -50.0)
        arg["on"] = "0"
        self.assertFalse(extras.api("/api/x/duck", lambda k: arg.get(k, ""))["ducked"])
        self.assertEqual(bridge.volume, -30.0)


if __name__ == "__main__":
    unittest.main()
