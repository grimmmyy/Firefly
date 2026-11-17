# Firefly

Cast local audio, podcasts, and internet radio from your phone to the Yamaha RX-V671 (or any DLNA/UPnP MediaRenderer) over Wi-Fi — no extra adapters, no computer.

## What it does

- Discovers DLNA/UPnP receivers on your LAN via SSDP.
- Runs a tiny on-device HTTP server (with HTTP Range support) so the receiver can pull local audio files directly over Wi-Fi.
- Drives the receiver via UPnP AVTransport (play / pause / stop) and RenderingControl (volume).
- Comes with a few preloaded internet radio streams and lets you save your own.

## What it does NOT do

- **Spotify / Apple Music / Tidal / Netflix audio.** Those apps mark their audio as DRM-protected at the OS level. On Android the stream comes through as silence; on iOS third-party apps can't capture other apps' audio at all. This is a platform limitation, not a Firefly one. For Spotify specifically, use Spotify Connect on a smart TV, Fire Stick, Chromecast, Apple TV, or console that's HDMI'd into the receiver.
- iOS has no system-audio capture for any app, so local files and stream URLs are the only inputs on iPhone.

## Receiver setup (one-time)

1. Plug an Ethernet cable from the RX-V671's `NETWORK` port to your home router.
2. On the Yamaha's on-screen `Setup → Network`, confirm it got an IP.
3. On the Yamaha remote, press `NET` once so the receiver is listening on the network input. Set `Network Standby` to `On` if you want to wake it from the app.

Your phone must be on the same Wi-Fi network (and not on a "Guest" SSID with client isolation).

## Installing the builds

CI builds both targets on every push. Grab them from the GitHub Actions run for your commit → "Artifacts":

- **`firefly-android-apk`** → `app-release.apk`. Copy to the phone and open it (allow "install from unknown sources"). The APK is self-signed with the debug key so it installs without a Play Store account.
- **`firefly-ios-unsigned-ipa`** → `Firefly-unsigned.ipa`. Install with [AltStore](https://altstore.io/) or [Sideloadly](https://sideloadly.io/) using your free Apple ID. It will need re-signing every 7 days (that's Apple's free-tier limit, not something the app can change).

## Building locally

Requirements: Flutter 3.24+, Android SDK (for APK), Xcode 15+ on macOS (for IPA).

```bash
flutter create . --platforms=android,ios --org com.firefly --project-name firefly
flutter pub get
flutter build apk --release        # Android
flutter build ios --release --no-codesign   # iOS (macOS only)
```

Unsigned IPA packaging is done in `.github/workflows/build.yml`.

## Repo layout

```
lib/
  main.dart
  ui/home_page.dart         # Tabs: Receiver / Stations / Files + transport bar
  services/
    ssdp.dart               # M-SEARCH + device description parsing
    upnp_client.dart        # SOAP calls for AVTransport + RenderingControl
    media_server.dart       # Local HTTP server with Range support for local files
    cast_controller.dart    # App-level state, orchestration
android/                    # Minimal Android host; `flutter create` fills the rest in CI
ios/                        # Minimal iOS host; `flutter create` fills the rest in CI
.github/workflows/build.yml # Produces APK + unsigned IPA
```

## License

See `LICENSE`.
