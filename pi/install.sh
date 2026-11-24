#!/bin/sh
# spotcast installer: turns a Raspberry Pi (64-bit Debian/Raspberry Pi OS,
# Bookworm or Trixie) into a Spotify Connect bridge for a DLNA receiver.
#
#   sudo ./install.sh                      (asks for anything it needs)
#   sudo SOLOIST_API_KEY=spak_... ./install.sh
#
# Optional env: RECEIVER_IP (found automatically if unset), SPEAKER_NAME,
# NTFY_TOPIC, DISCORD_WEBHOOK, HEARTBEAT_URL (healthchecks.io-style ping URL)
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
if [ -z "${SOLOIST_API_KEY:-}" ] && [ -r /etc/spotcast/soloist.env ]; then
  SOLOIST_API_KEY=$(sed -n 's/^SOLOIST_API_KEY=//p' /etc/spotcast/soloist.env)  # re-install keeps it
fi
if [ -z "${SOLOIST_API_KEY:-}" ] && [ -r /dev/tty ]; then
  printf "Spotify Soloist API key (https://developer.spotify.com/dashboard/soloist): " >/dev/tty
  stty -echo </dev/tty; read -r SOLOIST_API_KEY </dev/tty; stty echo </dev/tty; echo >/dev/tty
fi
: "${SOLOIST_API_KEY:?set SOLOIST_API_KEY (https://developer.spotify.com/dashboard/soloist)}"
RECEIVER_IP="${RECEIVER_IP:-}"
SPEAKER_NAME="${SPEAKER_NAME:-Living Room (RX-V671)}"
HERE="$(cd "$(dirname "$0")" && pwd)"

arch=$(uname -m)
case "$arch" in
  aarch64) SOLOIST_ARCH=arm64 ;;
  armv7l)  SOLOIST_ARCH=arm32 ;;
  x86_64)  SOLOIST_ARCH=x86_64 ;;
  *) echo "unsupported architecture: $arch"; exit 1 ;;
esac

echo "==> packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 ffmpeg curl qrencode pipewire pipewire-pulse wireplumber pulseaudio-utils iproute2 nftables >/dev/null

echo "==> service user"
id spotcast >/dev/null 2>&1 || useradd -r -m -d /var/lib/spotcast-user -s /usr/sbin/nologin spotcast
usermod -aG audio spotcast
loginctl enable-linger spotcast
UID_SC=$(id -u spotcast)

echo "==> Spotify Soloist ($SOLOIST_ARCH)"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
curl -fsSL -o "$T/s.tgz" "https://soloist-builds.spotifycdn.com/soloist_release_${SOLOIST_ARCH}.tar.gz"
tar -xzf "$T/s.tgz" -C "$T"
install -m 755 "$T/soloist" /usr/local/bin/soloist
soloist --version

echo "==> files"
install -d /opt/spotcast /etc/spotcast /var/lib/spotcast
install -m 755 "$HERE/spotcast.py" /opt/spotcast/spotcast.py
install -m 755 "$HERE/onevent.sh" /opt/spotcast/onevent.sh
install -m 755 "$HERE/spotcast-hide.sh" /opt/spotcast/spotcast-hide.sh
install -m 755 "$HERE/soloist-update" /usr/local/sbin/soloist-update
sed -i "s#/soloist_release_arm64.tar.gz#/soloist_release_${SOLOIST_ARCH}.tar.gz#" /usr/local/sbin/soloist-update
install -d -o spotcast /var/lib/spotcast-user/.config/pipewire/pipewire.conf.d
install -m 644 -o spotcast "$HERE/10-spotcast-lowlatency.conf" \
  /var/lib/spotcast-user/.config/pipewire/pipewire.conf.d/
install -m 440 "$HERE/sudoers-spotcast" /etc/sudoers.d/spotcast
visudo -cf /etc/sudoers.d/spotcast >/dev/null

umask 077
{
  echo "SOLOIST_API_KEY=$SOLOIST_API_KEY"
  [ -n "$RECEIVER_IP" ] && echo "SPOTCAST_RECEIVER=$RECEIVER_IP"
  echo "SPOTCAST_NAME=$SPEAKER_NAME"
  [ -n "${NTFY_TOPIC:-}" ] && echo "SPOTCAST_NTFY_TOPIC=$NTFY_TOPIC"
  [ -n "${DISCORD_WEBHOOK:-}" ] && echo "SPOTCAST_DISCORD_WEBHOOK=$DISCORD_WEBHOOK"
  [ -n "${HEARTBEAT_URL:-}" ] && echo "SPOTCAST_HEARTBEAT_URL=$HEARTBEAT_URL"
  true
} > /etc/spotcast/soloist.env
umask 022
chown spotcast /etc/spotcast/soloist.env
chown -R spotcast /var/lib/spotcast /var/lib/spotcast-user

echo "==> systemd"
sed "s/997/$UID_SC/g" "$HERE/spotcast.service" > /etc/systemd/system/spotcast.service
install -m 644 "$HERE/soloist-update.service" /etc/systemd/system/
install -m 644 "$HERE/soloist-update.timer" /etc/systemd/system/
systemctl daemon-reload
sudo -u spotcast XDG_RUNTIME_DIR="/run/user/$UID_SC" systemctl --user restart pipewire pipewire-pulse wireplumber || true
systemctl enable --now spotcast soloist-update.timer

echo
echo "Done. In Spotify (on the same Wi-Fi), pick \"Living Room - Add me\" to link your account."
echo "Status page + app pairing code: http://$(hostname -I | awk '{print $1}'):8090"
[ -n "$RECEIVER_IP" ] || echo "(The receiver is found automatically; see the status page to confirm.)"
