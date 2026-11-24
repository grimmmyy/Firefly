#!/usr/bin/env python3
"""Firefly installer page: no caching, versioned file names."""
import os, glob, html
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = "/opt/firefly-dl"

def latest(ext):
    files = sorted(glob.glob(f"{ROOT}/Firefly-*.{ext}"), key=os.path.getmtime)
    return os.path.basename(files[-1]) if files else f"Firefly.{ext}"

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def guess_type(self, path):
        if path.endswith(".apk"):
            return "application/vnd.android.package-archive"
        if path.endswith(".ipa"):
            return "application/octet-stream"
        return super().guess_type(path)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            ipa, apk = latest("ipa"), latest("apk")
            host = self.headers.get("Host", "192.168.1.110:8091")
            ver = ipa.replace("Firefly-", "").replace(".ipa", "")
            body = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Firefly Install</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#0e0e10;color:#eee;margin:0;padding:24px 16px;max-width:480px}}
a.b{{display:block;background:#ffb347;color:#111;text-decoration:none;font-weight:600;padding:16px;border-radius:14px;margin:12px 0;text-align:center}}
a.s{{background:#2a2a30;color:#eee}}p{{color:#aaa;line-height:1.45}}</style>
<h1>Firefly</h1><p>Latest build: <b>{html.escape(ver)}</b></p>
<h3>iPhone</h3>
<a class=b href="altstore://install?url=http%3A%2F%2F{host}%2F{ipa}">Install with AltStore</a>
<a class="b s" href="/{ipa}" download>Download IPA instead</a>
<a class="b s" href="altstore://source?url=https%3A%2F%2Fgithub.com%2Fgrimmmyy%2FFirefly%2Freleases%2Fdownload%2Fnightly%2Faltstore.json">Add update source to AltStore (once)</a>
<p class=s>With the source added, new builds show up in AltStore &rarr; Updates.</p>
<p>If the AltStore button does nothing: download the IPA, then AltStore → My Apps → + → pick <b>{html.escape(ipa)}</b> from Downloads. Stay on home Wi-Fi.</p>
<h3>Android</h3><a class=b href="/{apk}">Download APK ({html.escape(apk)})</a>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/Firefly.ipa", "/Firefly.apk"):   # old links -> newest file
            self.path = "/" + latest(self.path.rsplit(".", 1)[1])
        return super().do_GET()

    def log_message(self, fmt, *a):
        print("%s %s" % (self.client_address[0], fmt % a), flush=True)

ThreadingHTTPServer(("0.0.0.0", 8091), H).serve_forever()
