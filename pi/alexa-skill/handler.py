#!/usr/bin/env python3
"""Alexa skill backend for Firefly: 'Alexa, ask Firefly to play <song>'.

Runs on the Pi, reached from Alexa via a Cloudflare Tunnel (public HTTPS).
Alexa Account Linking passes the user's Spotify OAuth token in each request,
so this process keeps no Spotify credentials of its own.

Intents handled:
  PlayIntent        {query}   - search Spotify and start playback on the bridge
  PlayArtistIntent  {artist}
  PlayPlaylistIntent{playlist}
  AMAZON.PauseIntent
  AMAZON.ResumeIntent
  AMAZON.NextIntent
  AMAZON.PreviousIntent
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

LISTEN_PORT = int(os.environ.get("FIREFLY_ALEXA_PORT", "8091"))
SKILL_APP_ID = os.environ.get("ALEXA_APP_ID", "")  # amzn1.ask.skill.<uuid>
DEVICE_NAME = os.environ.get("SPOTCAST_NAME", "Living Room (RX-V671)")
SPOTIFY_API = "https://api.spotify.com/v1"
MAX_BODY = 262_144
CERT_CACHE: dict[str, tuple[x509.Certificate, float]] = {}

log = logging.getLogger("firefly-alexa")


# ---------- Alexa signature verification ----------

def _fetch_cert_chain(url: str) -> x509.Certificate:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "s3.amazonaws.com":
        raise ValueError("bad cert chain host")
    if parsed.port not in (None, 443):
        raise ValueError("bad cert chain port")
    if not parsed.path.startswith("/echo.api/"):
        raise ValueError("bad cert chain path")

    cached = CERT_CACHE.get(url)
    if cached and cached[1] > time.time():
        return cached[0]

    with urllib.request.urlopen(url, timeout=5) as resp:
        pem = resp.read()
    cert = x509.load_pem_x509_certificate(pem)

    now = time.time()
    if cert.not_valid_before_utc.timestamp() > now:
        raise ValueError("cert not yet valid")
    if cert.not_valid_after_utc.timestamp() < now:
        raise ValueError("cert expired")

    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if "echo-api.amazon.com" not in san.get_values_for_type(x509.DNSName):
        raise ValueError("cert SAN mismatch")

    CERT_CACHE[url] = (cert, now + 3600)
    return cert


def verify_alexa(headers: dict[str, str], raw_body: bytes) -> None:
    chain_url = headers.get("signaturecertchainurl") or headers.get(
        "SignatureCertChainUrl"
    )
    signature_b64 = headers.get("signature") or headers.get("Signature")
    if not chain_url or not signature_b64:
        raise PermissionError("missing Alexa signature headers")

    cert = _fetch_cert_chain(chain_url)
    signature = base64.b64decode(signature_b64)
    try:
        cert.public_key().verify(
            signature,
            raw_body,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as exc:  # cryptography re-raises InvalidSignature
        raise PermissionError(f"signature invalid: {exc}") from exc


# ---------- Spotify Web API helpers ----------

def _sp_request(
    token: str,
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict | None:
    url = SPOTIFY_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 204 or resp.length == 0:
                return None
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"spotify {e.code}: {body[:200]}") from None


def find_firefly_device(token: str) -> str | None:
    data = _sp_request(token, "GET", "/me/player/devices") or {}
    for d in data.get("devices", []):
        if d.get("name") == DEVICE_NAME:
            return d["id"]
    # Loose fallback if the user renamed it
    for d in data.get("devices", []):
        if "firefly" in d.get("name", "").lower() or "rx-v671" in d.get("name", "").lower():
            return d["id"]
    return None


def search_uri(token: str, query: str, kind: str) -> tuple[str, str] | None:
    """kind: 'track' | 'artist' | 'playlist'. Returns (uri, friendly_name)."""
    data = _sp_request(
        token,
        "GET",
        "/search",
        params={"q": query, "type": kind, "limit": 1, "market": "from_token"},
    ) or {}
    items = (data.get(kind + "s") or {}).get("items") or []
    if not items:
        return None
    it = items[0]
    if kind == "track":
        who = ", ".join(a["name"] for a in it.get("artists", []))
        return it["uri"], f"{it['name']} by {who}"
    return it["uri"], it["name"]


def play_on_firefly(
    token: str, context_uri: str | None = None, uris: list[str] | None = None
) -> None:
    device_id = find_firefly_device(token)
    if not device_id:
        raise RuntimeError(
            "I couldn't find the Firefly speaker. Open Spotify and pick it once, "
            "then try again."
        )
    body: dict = {}
    if context_uri:
        body["context_uri"] = context_uri
    if uris:
        body["uris"] = uris
    _sp_request(
        token,
        "PUT",
        "/me/player/play",
        params={"device_id": device_id},
        body=body,
    )


def control(token: str, action: str) -> None:
    device_id = find_firefly_device(token)
    params = {"device_id": device_id} if device_id else None
    path = {
        "pause": "/me/player/pause",
        "resume": "/me/player/play",
        "next": "/me/player/next",
        "previous": "/me/player/previous",
    }[action]
    method = "POST" if action in ("next", "previous") else "PUT"
    _sp_request(token, method, path, params=params)


# ---------- Alexa response builders ----------

def _say(text: str, end: bool = True) -> dict:
    # Strip anything that would be mangled by Alexa's SSML parser.
    safe = re.sub(r"[<>&]", " ", text)
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": safe},
            "shouldEndSession": end,
        },
    }


def _link_account() -> dict:
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": "Open the Alexa app and link your Spotify account to use Firefly.",
            },
            "card": {"type": "LinkAccount"},
            "shouldEndSession": True,
        },
    }


# ---------- Intent dispatch ----------

def handle(request: dict) -> dict:
    session = request.get("session") or {}
    app_id = (session.get("application") or {}).get("applicationId")
    if SKILL_APP_ID and app_id != SKILL_APP_ID:
        raise PermissionError("wrong applicationId")

    token = (session.get("user") or {}).get("accessToken")
    rtype = (request.get("request") or {}).get("type", "")

    if rtype == "LaunchRequest":
        if not token:
            return _link_account()
        return _say(
            "Firefly is ready. Say something like, play Taylor Swift.",
            end=False,
        )

    if rtype == "SessionEndedRequest":
        return {"version": "1.0", "response": {"shouldEndSession": True}}

    if rtype != "IntentRequest":
        return _say("Sorry, I didn't catch that.")

    intent = request["request"]["intent"]
    name = intent.get("name", "")
    slots = {k: v.get("value") for k, v in (intent.get("slots") or {}).items()}

    if not token:
        return _link_account()

    try:
        if name == "PlayIntent":
            q = slots.get("query") or ""
            if not q:
                return _say("What should I play?")
            result = search_uri(token, q, "track")
            if not result:
                return _say(f"I couldn't find anything for {q}.")
            uri, friendly = result
            play_on_firefly(token, uris=[uri])
            return _say(f"Playing {friendly}.")

        if name == "PlayArtistIntent":
            q = slots.get("artist") or ""
            result = search_uri(token, q, "artist")
            if not result:
                return _say(f"I couldn't find the artist {q}.")
            uri, friendly = result
            play_on_firefly(token, context_uri=uri)
            return _say(f"Playing {friendly}.")

        if name == "PlayPlaylistIntent":
            q = slots.get("playlist") or ""
            result = search_uri(token, q, "playlist")
            if not result:
                return _say(f"I couldn't find a playlist for {q}.")
            uri, friendly = result
            play_on_firefly(token, context_uri=uri)
            return _say(f"Playing the {friendly} playlist.")

        if name == "AMAZON.PauseIntent":
            control(token, "pause")
            return _say("Paused.")

        if name == "AMAZON.ResumeIntent":
            control(token, "resume")
            return _say("Resuming.")

        if name == "AMAZON.NextIntent":
            control(token, "next")
            return _say("Next.")

        if name == "AMAZON.PreviousIntent":
            control(token, "previous")
            return _say("Previous.")

        if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return _say("Bye.")

        if name == "AMAZON.HelpIntent":
            return _say(
                "Say, play a song, or ask for an artist or playlist. "
                "You can also say pause, resume, next, or previous.",
                end=False,
            )

        return _say("I don't know how to do that yet.")

    except RuntimeError as e:
        log.warning("intent %s failed: %s", name, e)
        return _say(str(e))


# ---------- HTTP server ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "firefly-alexa/1.0"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _reply(self, code: int, body: dict | None):
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            self._reply(200, {"ok": True})
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/alexa", "/"):
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._reply(400, {"error": "bad length"})
            return
        raw = self.rfile.read(length)

        headers_lc = {k.lower(): v for k, v in self.headers.items()}
        try:
            verify_alexa(headers_lc, raw)
        except (PermissionError, ValueError) as e:
            log.warning("sig reject: %s", e)
            self._reply(400, {"error": "signature"})
            return

        try:
            req = json.loads(raw)
        except Exception:
            self._reply(400, {"error": "json"})
            return

        # Alexa requires request timestamp within 150 seconds.
        ts = ((req.get("request") or {}).get("timestamp")) or ""
        try:
            import datetime
            t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if abs(time.time() - t) > 150:
                raise ValueError("stale timestamp")
        except Exception as e:
            log.warning("timestamp reject: %s", e)
            self._reply(400, {"error": "timestamp"})
            return

        try:
            resp = handle(req)
        except PermissionError as e:
            log.warning("auth: %s", e)
            self._reply(403, {"error": "forbidden"})
            return
        except Exception as e:
            log.exception("handler error: %s", e)
            resp = _say("Firefly hit an error.")
        self._reply(200, resp)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    srv = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    log.info("firefly-alexa on 127.0.0.1:%d (device=%r)", LISTEN_PORT, DEVICE_NAME)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
