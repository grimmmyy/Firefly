"""Offline tests for pi/rescue.py against a fake YNCA receiver."""
import os
import socket
import sys
import threading
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

if "rescue" not in sys.modules:
    main = sys.modules.get("__main__")
    if not hasattr(main, "log"):                 # run alone: minimal fake bridge
        import logging
        fake = types.ModuleType("__main__")
        fake.log = logging.getLogger("test")
        sys.modules["__main__"] = fake
import rescue  # noqa: E402

Thread = threading.Thread     # test_extras swaps threading.Thread while it runs


class FakeReceiver:
    def __init__(self, main="On", inp="PC", zone2="Standby"):
        self.v = {"@MAIN:PWR": main, "@MAIN:INP": inp, "@ZONE2:PWR": zone2}
        self.sent = []
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            Thread(target=self.client, args=(c,), daemon=True).start()

    def client(self, c):
        with c, c.makefile("rb") as f:
            try:
                for raw in f:
                    k, _, v = raw.decode().strip().partition("=")
                    if v != "?":
                        self.sent.append(f"{k}={v}")
                        self.v[k] = v
                        if k == "@SYS:PWR":      # the real unit moves both zones
                            self.v["@MAIN:PWR"] = self.v["@ZONE2:PWR"] = v
                    c.sendall(f"{k}={self.v.get(k, '')}\r\n".encode())
            except OSError:
                pass


class RescueTest(unittest.TestCase):
    def run_attempt(self, rx, answers=(), playing=False):
        old = rescue.YNCA_PORT
        rescue.YNCA_PORT = rx.port
        it = iter(answers)
        try:
            return rescue.attempt("127.0.0.1", lambda: next(it, False), playing,
                                  sleep=lambda s: None)
        finally:
            rescue.YNCA_PORT = old
            rx.srv.close()

    def test_idle_receiver_is_cycled_and_zones_restored(self):
        rx = FakeReceiver()
        r = self.run_attempt(rx, [False, False, True])
        self.assertEqual(r["result"], "fixed-by-standby-cycle")
        self.assertEqual(rx.v["@ZONE2:PWR"], "Standby")
        self.assertEqual(rx.v["@MAIN:PWR"], "On")

    def test_receiver_that_was_in_standby_goes_back_to_standby(self):
        rx = FakeReceiver(main="Standby")
        r = self.run_attempt(rx)
        self.assertEqual(r["result"], "not-fixed")
        self.assertEqual(rx.v["@MAIN:PWR"], "Standby")

    def test_never_power_cycles_while_in_use(self):
        for kw, playing in (({"inp": "HDMI1"}, False), ({"zone2": "On"}, False), ({}, True)):
            rx = FakeReceiver(**kw)
            r = self.run_attempt(rx, playing=playing)
            self.assertEqual(r["result"], "waiting")
            self.assertFalse([c for c in rx.sent if "PWR" in c])
            self.assertEqual(rx.sent[-1], "@SYS:DMCCONTROL=Enable")

    def test_stops_at_the_first_step_that_works(self):
        rx = FakeReceiver()
        r = self.run_attempt(rx, [True])
        self.assertEqual(r["result"], "fixed-by-ynca-query")
        self.assertEqual(rx.sent, [])

    def test_dead_port_is_reported(self):
        rx = FakeReceiver()
        rx.port = 1                      # nothing listens there
        self.assertEqual(self.run_attempt(rx)["result"], "ynca-dead")

    def test_timing(self):
        self.assertFalse(rescue.due(None, None, 1000))
        self.assertFalse(rescue.due(950, None, 1000))
        self.assertTrue(rescue.due(900, None, 1000))
        self.assertFalse(rescue.due(900, (900, 1000, "not-fixed"), 9999))
        self.assertTrue(rescue.due(900, (900, 1000, "waiting"), 1300))


if __name__ == "__main__":
    unittest.main()
