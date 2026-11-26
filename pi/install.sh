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
  python3 ffmpeg curl qrencode pipewire pipewire-pulse wireplumber pulseaudio-utils iproute2 nftables   bluez bluez-tools gmediarender swh-plugins gstreamer1.0-tools gstreamer1.0-plugins-good   gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pulseaudio >/dev/null
systemctl disable --now gmediarender 2>/dev/null || true      # spotcast starts it

if [ "${SKIP_AIRPLAY2:-0}" != 1 ] && ! /usr/local/bin/shairport-sync -V 2>/dev/null | grep -q AirPlay2; then
  echo "==> AirPlay 2 (building from source, ~5 min)"
  sh "$HERE/build-airplay2.sh" || echo "AirPlay 2 build failed; continuing without AirPlay"
fi

echo "==> service user"
id spotcast >/dev/null 2>&1 || useradd -r -m -d /var/lib/spotcast-user -s /usr/sbin/nologin spotcast
usermod -aG audio,bluetooth spotcast
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
install -m 644 "$HERE/extras.py" /opt/spotcast/extras.py
install -m 755 "$HERE/onvolume.sh" /opt/spotcast/onvolume.sh
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
install -m 644 "$HERE/spotcast-bt-agent.service" /etc/systemd/system/
systemctl daemon-reload
sudo -u spotcast XDG_RUNTIME_DIR="/run/user/$UID_SC" systemctl --user restart pipewire pipewire-pulse wireplumber || true
systemctl enable --now spotcast soloist-update.timer spotcast-bt-agent

echo
echo "Done. In Spotify (on the same Wi-Fi), pick \"Living Room - Add me\" to link your account."
echo "Status page + app pairing code: http://$(hostname -I | awk '{print $1}'):8090"
[ -n "$RECEIVER_IP" ] || echo "(The receiver is found automatically; see the status page to confirm.)"

# The Pi sits on the LAN twice (eth0 + wlan0). Default Linux ARP lets each
# interface answer for the other's address ("ARP flux"); stop that so the
# receiver's 2011 network stack always sees one MAC per address.
printf 'net.ipv4.conf.all.arp_ignore=1\nnet.ipv4.conf.all.arp_announce=2\nnet.ipv4.conf.default.arp_ignore=1\nnet.ipv4.conf.default.arp_announce=2\n' > /etc/sysctl.d/90-arp-flux.conf
sysctl -q -p /etc/sysctl.d/90-arp-flux.conf

# Download page mirror: pull each new nightly build from GitHub (public assets).
install -m 644 "$HERE/firefly-dl-sync.service" "$HERE/firefly-dl-sync.timer" /etc/systemd/system/
install -m 755 "$HERE/firefly-dl-sync.sh" /opt/spotcast/
systemctl daemon-reload
systemctl enable --now firefly-dl-sync.timer

# Nightly settings snapshot (14 days, /var/backups/spotcast; excludes the Soloist key).
install -m 755 "$HERE/spotcast-backup.sh" /opt/spotcast/
install -m 644 "$HERE/spotcast-backup.service" "$HERE/spotcast-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now spotcast-backup.timer

# Freeze diagnosis: rolling capture of the receiver's control traffic; each
# freeze then leaves a report in /var/lib/spotcast-user/freezes (freezelog.py).
install -m 644 "$HERE/freezelog.py" /opt/spotcast/freezelog.py
command -v tcpdump >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tcpdump
mkdir -p /var/log/rxcap
install -m 644 "$HERE/rxcap.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rxcap.service
