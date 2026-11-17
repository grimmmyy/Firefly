# Alexa Echo Dot control

The Pi bridge ships a Fauxmo shim that pretends to be a set of WeMo smart plugs
on your LAN. Alexa discovers them over Wi-Fi with no cloud account, no Skill
install, no Amazon developer setup.

## Setup

1. Run `install.sh` — it installs and enables `fauxmo.service` alongside `spotcast.service`.
2. On a phone with the Alexa app, same Wi-Fi as the Pi and the Echo Dot, say
   **"Alexa, discover devices."** (Or Devices tab → **+** → Add Device → Other → Discover.)
3. Ten new "plugs" appear; name them in the app if you like.

## Voice commands

| Say | Does |
|---|---|
| "Alexa, turn on Spotify" | play |
| "Alexa, turn off Spotify" | pause |
| "Alexa, turn on next song" | next track |
| "Alexa, turn on previous song" | previous track |
| "Alexa, turn on receiver" / "turn off receiver" | power on / standby |
| "Alexa, turn on mute" / "turn off mute" | mute / unmute |
| "Alexa, turn on sleep timer" | 30-minute sleep timer |
| "Alexa, turn off sleep timer" | cancel sleep timer |
| "Alexa, turn on hand back" | return receiver to its previous input |
| "Alexa, turn on volume low" | receiver → −35 dB |
| "Alexa, turn on volume medium" | receiver → −25 dB |
| "Alexa, turn on volume high" | receiver → −15 dB |

Group them in the Alexa app (e.g. an "Audio" group) if you want to tidy the list.

## What it cannot do

- **Voice-search Spotify to the receiver.** Saying "Alexa, play Taylor Swift"
  plays on the Echo Dot's own speaker, not the Yamaha. Amazon only routes
  catalog playback to devices in Amazon's certified program; the RX-V671 and
  the Pi bridge are not eligible.
- **Read the current track back to you.** Alexa only reports on/off state.
- **Choose a specific Spotify playlist by voice.** Same reason as the first point.

If any of those become important later, the clean workaround is still a 3.5 mm
cable from the Echo Dot's audio-out into the receiver's AUX — but that was
explicitly out of scope for this setup.

## How it works

`fauxmo.service` runs `fauxmo` from `/opt/spotcast-venv` with
`/etc/spotcast/fauxmo.json`. Each JSON entry opens a port on the Pi and
answers Alexa's WeMo discovery multicast. On "turn on X", fauxmo hits the
matching `http://127.0.0.1:8090/api/…` endpoint on the local spotcast HTTP API.

To add or rename commands, edit `/etc/spotcast/fauxmo.json`, restart
`sudo systemctl restart fauxmo`, then re-run device discovery in the Alexa app.
