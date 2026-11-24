#!/bin/sh
# Mirror the rolling "nightly" GitHub pre-release into the Pi's download page
# (/opt/firefly-dl, served on :8091). Runs from firefly-dl-sync.timer.
# Public release assets only: no GitHub credentials are stored on the Pi.
set -eu
REPO="grimmmyy/Firefly"
DIR=/opt/firefly-dl
BASE="https://github.com/$REPO/releases/download/nightly"
KEEP=4                     # builds kept per platform

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

curl -fsSL -m 30 "$BASE/version.json" -o "$tmp/version.json" || exit 0
V=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$tmp/version.json")
case "$V" in *[!0-9.]*|"") echo "bad version '$V'"; exit 1;; esac

[ -f "$DIR/Firefly-$V.ipa" ] && [ -f "$DIR/Firefly-$V.apk" ] && exit 0

echo "new build $V"
curl -fsSL -m 600 "$BASE/Firefly-ios.ipa" -o "$tmp/f.ipa"
curl -fsSL -m 600 "$BASE/Firefly-android.apk" -o "$tmp/f.apk"
# sanity: both are zip archives
for f in "$tmp/f.ipa" "$tmp/f.apk"; do
  [ "$(head -c 2 "$f")" = "PK" ] || { echo "not a zip: $f"; exit 1; }
done
install -m 644 "$tmp/f.ipa" "$DIR/Firefly-$V.ipa"
install -m 644 "$tmp/f.apk" "$DIR/Firefly-$V.apk"

# keep only the newest $KEEP of each
for ext in ipa apk; do
  ls -1t "$DIR"/Firefly-*."$ext" 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f -- "$old"
  done
done
echo "published Firefly-$V"
