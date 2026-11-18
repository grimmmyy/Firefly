# Firefly Alexa skill — "Alexa, ask Firefly to play Taylor Swift"

Voice control that plays Spotify's full catalog on your Pi bridge → RX-V671.
Nothing cables anywhere. Requires **Spotify Premium** (Web API playback is
Premium-only — hard Spotify rule, not negotiable).

The invocation is **"Alexa, ask Firefly to…"** because Amazon reserves the
bare "Alexa, play X" phrasing for the handful of certified Music Skill
partners (Spotify, Apple Music, Pandora, …). That program has been closed to
third parties for years — no workaround flips it.

## What this installs

- `alexa-handler.py` on the Pi (listens on `127.0.0.1:8091`)
- `alexa-skill.service` — systemd unit that keeps it up
- `cloudflared.service` — a free Cloudflare Tunnel that gives Alexa a public
  HTTPS URL that forwards to `127.0.0.1:8091`. No port forwarding.

Alexa calls the public URL, this handler verifies the request signature,
looks up the Pi's Spotify Connect endpoint by name (`SPOTCAST_NAME` from
`/etc/spotcast/soloist.env`), and tells Spotify to play there.

## Setup (one time)

### 1. Create a Spotify "app" (for Alexa account linking)

1. Open [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) → **Create app**.
2. Name: `Firefly Alexa`. Redirect URIs: you'll fill these in step 3 after
   Alexa gives you its callback URLs. Leave blank for now.
3. Save **Client ID** and **Client Secret** — you need them in step 3.

### 2. Set up a Cloudflare Tunnel (free)

1. Sign in or create an account at [one.dash.cloudflare.com](https://one.dash.cloudflare.com)
   → **Networks → Tunnels → Create tunnel**. Pick **Cloudflared**, name it
   `firefly-alexa`.
2. Copy the token Cloudflare shows (starts with `eyJ…`).
3. On the Pi:

   ```sh
   sudo install -d /etc/cloudflared
   echo "TUNNEL_TOKEN=<that token>" | sudo tee /etc/cloudflared/tunnel.env
   sudo useradd -r -s /usr/sbin/nologin cloudflared || true
   sudo chown -R cloudflared /etc/cloudflared
   ```
4. Install `cloudflared` from Cloudflare's apt repo (one-liner they give you
   in the dashboard) and then:

   ```sh
   sudo systemctl enable --now cloudflared
   ```
5. Back in the Cloudflare dashboard, add a **Public Hostname** on this tunnel:
   - Subdomain: anything (e.g. `firefly-yourname`)
   - Domain: a domain you own that's on Cloudflare, or Cloudflare's free `*.trycloudflare.com`
   - Service: `http://127.0.0.1:8091`
6. The resulting URL (e.g. `https://firefly-yourname.example.com`) is your
   **Alexa endpoint**. Save it; you need it in step 3.

### 3. Create the Alexa skill

1. [developer.amazon.com/alexa/console/ask](https://developer.amazon.com/alexa/console/ask) →
   **Create Skill**. Name: `Firefly`. Model: **Custom**. Backend: **Provision your own**.
2. **Interaction Model → JSON Editor** → paste `pi/alexa-skill/interaction-model.json` from this repo. Save & Build.
3. **Endpoint → HTTPS → Default region**: paste your Cloudflare URL with `/alexa` appended
   (e.g. `https://firefly-yourname.example.com/alexa`).
   Certificate: **My development endpoint has a certificate from a trusted authority**.
4. **Account Linking** → turn on:
   - Auth Grant Type: **Auth Code Grant**
   - Authorization URI: `https://accounts.spotify.com/authorize`
   - Access Token URI: `https://accounts.spotify.com/api/token`
   - Client ID: your Spotify Client ID from step 1
   - Client Secret: your Spotify Client Secret from step 1
   - Scopes (one per line):
     ```
     user-read-playback-state
     user-modify-playback-state
     user-read-currently-playing
     ```
   - Client Authentication Scheme: **HTTP Basic (Recommended)**
   - Save. Alexa will show three **Redirect URLs**. Copy them.
5. Back in the Spotify Developer Dashboard → your `Firefly Alexa` app →
   **Edit Settings → Redirect URIs**: paste those three Alexa URLs. Save.
6. On the Alexa skill → **Permissions** page → copy the **Application ID**
   (`amzn1.ask.skill.<uuid>`).

### 4. Point the Pi handler at the skill

```sh
sudo install -d /etc/spotcast
sudo tee /etc/spotcast/alexa.env <<EOF
ALEXA_APP_ID=amzn1.ask.skill.<paste yours>
FIREFLY_ALEXA_PORT=8091
EOF
sudo chown spotcast /etc/spotcast/alexa.env
sudo chmod 640 /etc/spotcast/alexa.env
sudo systemctl enable --now alexa-skill
```

### 5. Link your Spotify account in the Alexa app

1. Alexa app → **More → Skills & Games → Your Skills → Dev**.
2. Tap **Firefly → Settings → Link Account**. Log into Spotify. Approve.
3. Done. The handler gets your Spotify token in every request from Alexa and
   never stores it on the Pi.

### 6. Make sure Spotify sees the Pi

Open Spotify on any device and play something on the "Living Room (RX-V671)"
endpoint at least once — that registers it against your account, after which
the Alexa skill can target it.

## Try it

- "Alexa, ask Firefly to play Taylor Swift" → first track by that artist, on the receiver
- "Alexa, ask Firefly to play music by Fleet Foxes"
- "Alexa, ask Firefly to play my Daily Mix 1 playlist"
- "Alexa, tell Firefly to pause"
- "Alexa, tell Firefly next"

## Troubleshooting

**"I couldn't find the Firefly speaker."** Open the Spotify app, pick the
"Living Room (RX-V671)" speaker once, play 10 seconds of anything. Try again.

**"Open the Alexa app and link your Spotify account."** You skipped step 5.

**Nothing happens, no response at all.**
`sudo journalctl -u alexa-skill -f` on the Pi while you ask Alexa. You should
see POSTs coming in. If not, test the tunnel itself: on your laptop,
`curl https://firefly-yourname.example.com/healthz` should return `{"ok":true}`.

**"Signature" errors in the log.** The handler enforces Alexa's request
signing. Make sure the tunnel really is HTTPS (Cloudflare is by default) and
that nothing is rewriting headers.

## Security notes

- The handler runs as the unprivileged `spotcast` user.
- Only Alexa-signed requests are accepted, and the skill `applicationId` is
  verified against `ALEXA_APP_ID`, so even if someone discovers the Cloudflare
  URL they cannot drive your account.
- Your Spotify token never touches disk on the Pi.
- The tunnel exposes only `127.0.0.1:8091` through Cloudflare's edge — not the
  rest of the Pi's network.
