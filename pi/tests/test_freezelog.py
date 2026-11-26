"""Offline tests for pi/freezelog.py: pcap cutting and the cause guesses."""
import os
import struct
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

if "freezelog" not in sys.modules:
    main = sys.modules.get("__main__")
    if not hasattr(main, "log"):                 # run alone: minimal fake bridge
        import logging
        fake = types.ModuleType("__main__")
        fake.log = logging.getLogger("test")
        sys.modules["__main__"] = fake
import freezelog  # noqa: E402


def write_pcap(path, times):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 512, 276))
        for t in times:
            d = b"x" * 10
            f.write(struct.pack("<IIII", int(t), int((t % 1) * 1e6), len(d), len(d)) + d)


class CutPcap(unittest.TestCase):
    def test_keeps_only_the_window_across_files(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            write_pcap(os.path.join(d, "rx.pcap1"), [now - 2000, now - 500, now - 100])
            write_pcap(os.path.join(d, "rx.pcap0"), [now - 50, now + 10, now + 100])
            # truncated record at the end (file still being written)
            with open(os.path.join(d, "rx.pcap0"), "ab") as f:
                f.write(struct.pack("<IIII", int(now), 0, 50, 50) + b"short")
            freezelog.PCAP_GLOB = os.path.join(d, "rx.pcap*")
            out = os.path.join(d, "cut.pcap")
            n = freezelog.cut_pcap(now, out, seconds=600)
            self.assertEqual(n, 4)               # -500, -100, -50, +10
            gh, recs = freezelog._read_pcap(out)
            self.assertEqual(len(recs), 4)
            self.assertEqual([r[0] for r in recs], sorted(r[0] for r in recs))

    def test_no_capture_files(self):
        freezelog.PCAP_GLOB = "/nonexistent/rx.pcap*"
        self.assertEqual(freezelog.cut_pcap(time.time(), os.devnull), 0)


class Causes(unittest.TestCase):
    def setUp(self):
        freezelog.RX = "192.168.1.7"
        freezelog.marks.update(sound_change=0.0, heavy_cmd=0.0, heavy_what="")

    def test_sound_change_and_scan_and_flood(self):
        t = time.time()
        freezelog.marks["sound_change"] = t - 40
        r = {"t": t, "capture": {"arp_sweeps": {"192.168.1.10": 120},
                                 "ssdp_per_min_by_source": {"192.168.1.110": 26.0,
                                                            "192.168.1.7": 9.0}}}
        c = " | ".join(freezelog.likely_causes(r))
        self.assertIn("Sound change 40 s before", c)
        self.assertIn("192.168.1.10 scanned the network", c)
        self.assertIn("192.168.1.110: 26.0/min", c)
        self.assertNotIn("192.168.1.7:", c)       # the receiver's own announcements

    def test_idle_build_up(self):
        r = {"t": time.time(), "since_receiver_came_back_s": 6 * 3600, "playing": False}
        self.assertIn("Idle build-up: 6.0 h", " ".join(freezelog.likely_causes(r)))

    def test_nothing_found(self):
        self.assertEqual(freezelog.likely_causes({"t": time.time()}),
                         ["Nothing unusual found: see the capture"])


if __name__ == "__main__":
    unittest.main()
