# Changelog

## Unreleased (PR #4, branch `claude/setup-connectivity`)

### Receiver freezes
- The bridge left UPnP event subscriptions behind on every restart. Events are
  now off by default and the bridge unsubscribes on exit. This may explain some
  freezes, but not all: long freezes happened before the subscription code.
- Changing sound processing over the network was followed by a freeze in one
  test. Later tests (24 spaced and 138 rapid changes) did not repeat it. As a
  precaution the bridge leaves the receiver alone for 30 s after a sound change,
  refuses a second change inside that window, and sends a scene's sound settings
  together.
- Gentler everywhere: 0.25 s minimum between requests, 3 s settle after power,
  input and scene commands, about a third of the traffic while music plays.
- The Pi answers address lookups only for its own interface (ARP flux), and its
  Wi-Fi is off; it runs on Ethernet.
- Freeze reports: each freeze is recorded with the last requests, recent
  changes, response times, network activity and a 10-minute capture, plus what
  stood out (observations, not proven causes). A report begun by a bridge restart
  during a freeze says so. In the app: Receiver → Freeze history.
- Alerts within about a minute of a freeze (ntfy), and a red notice on Home.
- Freeze rescue (`pi/rescue.py`): 90 s into a freeze the bridge tries the
  receiver's other control port (50000): a query, a network-player restart, then
  a standby cycle if nobody is using it. Each step is recorded. Not yet seen
  against a real freeze.
- Still open: the root cause is unknown. Idle freezes continued with the Pi cast
  (DLNA) service off and with status checks cut to one per 5 minutes.
- `pi/rxbaseline.sh`: a 48 h test with the whole bridge kept away from the
  receiver; it can arm itself to start after the next recovery.
- `pi/rxkeep.sh`: keeps the rolling capture from overwriting itself during a
  test.

### New on the receiver
- AirPlay 2, Bluetooth, Pi cast (DLNA), internet radio on the Pi; last source wins.
- Sound: program, Straight, Pure Direct, Enhancer, CINEMA DSP 3D, Dialogue Lift,
  night mode, bass and treble, receiver scenes; loudness levelling, 15-band EQ,
  room EQ measured with the phone.
- The receiver's own sources: FM/AM tuner (presets, scan, tune), USB stick and
  media-server browsing, input renaming.
- Automations: wake-up with a slow fade (Spotify, FM preset or stream), sleep
  fade ending in standby, quiet hours, sound per source; fades stop when anyone
  touches the volume.
- Zone 2, custom scenes, schedules, listening history.

### Apps and voice
- Receiver tab reorganised: now playing, sound and scenes up front; everything
  else in its own section.
- Android: lower the volume during calls, Receiver quick-settings tile and a
  home-screen remote widget (all work with the app closed).
- Alexa: FM Radio, USB, Dialogue Lift, Sleep Timer, a device per radio preset,
  Night Mode, Zone 2, scenes ("Music Scene", "Night Scene").
- App start no longer sweeps the whole network when the receiver is known.
- Updates: every successful build replaces the `nightly` pre-release with an
  AltStore source (iPhone updates show under AltStore → Updates) and the Pi
  mirrors it to its download page.

### Platforms
All features are in the shared Flutter code and ship on Android, iOS and
Windows, except the Android-only call ducking, quick tile and widget (iOS does
not let apps see calls; an iOS widget needs a WidgetKit extension). Room EQ
needs a phone microphone (Android, iOS).

### Pi housekeeping
- Nightly settings backup (14 days, Soloist key excluded).
- 42 offline tests for the Pi code run in CI on every change to `pi/`
  (they caught an FM tuning bug: 98.1 MHz tuned 98.2).
