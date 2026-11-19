#!/bin/sh
# spotcast installer: turns a Raspberry Pi (64-bit Debian/Raspberry Pi OS,
# Bookworm or Trixie) into a Spotify Connect bridge for a DLNA receiver.
#
#   sudo SOLOIST_API_KEY=spak_... RECEIVER_IP=192.168.1.7 ./install.sh
#
# Optional env: SPEAKER_NAME ("Living Room (RX-V671)"), NTFY_TOPIC, DISCORD_WEBHOOK
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
: "${SOLOIST_API_KEY:?set SOLOIST_API_KEY (https://developer.spotify.com/dashboard/soloist)}"
: "${RECEIVER_IP:?set RECEIVER_IP to your receiver's LAN address}"
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
  python3 ffmpeg curl pipewire pipewire-pulse wireplumber pulseaudio-utils iproute2 nftables >/dev/null

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
  echo "SPOTCAST_RECEIVER=$RECEIVER_IP"
  echo "SPOTCAST_NAME=$SPEAKER_NAME"
  [ -n "${NTFY_TOPIC:-}" ] && echo "SPOTCAST_NTFY_TOPIC=$NTFY_TOPIC"
  [ -n "${DISCORD_WEBHOOK:-}" ] && echo "SPOTCAST_DISCORD_WEBHOOK=$DISCORD_WEBHOOK"
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
echo "Status page: http://$(hostname -I | awk '{print $1}'):8090"
