#!/bin/sh
# One-command spotcast install on a fresh Raspberry Pi:
#   curl -fsSL https://raw.githubusercontent.com/grimmmyy/Firefly/main/pi/get.sh | sudo sh
# Optional: FIREFLY_REF=<branch or tag> (default main), plus any install.sh env.
set -eu
REF="${FIREFLY_REF:-main}"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
echo "==> fetching Firefly ($REF)"
curl -fsSL "https://github.com/grimmmyy/Firefly/archive/$REF.tar.gz" | tar -xz -C "$T"
sh "$T"/Firefly-*/pi/install.sh
