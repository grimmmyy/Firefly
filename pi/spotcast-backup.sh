#!/bin/sh
# Nightly snapshot of everything needed to rebuild the living-room bridge
# after a bad change: scenes, schedules, EQ, quiet hours, Spotify slots,
# phone pairing, Bluetooth pairings, and the service/unit files.
# Kept 14 days in /var/backups/spotcast (root only).
#
# The Soloist API key (/etc/spotcast/soloist.env) is deliberately NOT included:
# it stays in exactly one place. Restore: tar -xzpf <file> -C /
set -eu
DEST=/var/backups/spotcast
KEEP_DAYS=14
mkdir -p "$DEST"
chmod 700 "$DEST"
out="$DEST/spotcast-$(date +%Y%m%d-%H%M).tar.gz"

# only paths that exist (tar fails on missing ones)
set --
for p in \
  /var/lib/spotcast-user/extras.json \
  /var/lib/spotcast-user/registered.json \
  /var/lib/spotcast-user/previous_input \
  /var/lib/spotcast-user/pair_token \
  /var/lib/spotcast-user/bt_keep.json \
  /var/lib/spotcast-user/history.jsonl \
  /var/lib/spotcast-user/slots \
  /var/lib/bluetooth \
  /opt/spotcast \
  /opt/firefly-dl-server.py \
  /etc/systemd/system/spotcast.service \
  /etc/systemd/system/alexa-local.service \
  /etc/systemd/system/spotcast-bt-agent.service \
  /etc/systemd/system/rxcap.service \
  /etc/systemd/system/firefly-dl.service \
  /etc/systemd/system/firefly-dl-sync.service \
  /etc/systemd/system/firefly-dl-sync.timer \
  /etc/systemd/system/spotcast-backup.service \
  /etc/systemd/system/spotcast-backup.timer \
  /etc/sysctl.d/90-arp-flux.conf
do
  [ -e "$p" ] && set -- "$@" "$p"
done

tar --exclude='*/cache/*' --exclude='*.bak*' --exclude='__pycache__' \
    -czpf "$out.tmp" "$@" 2>/dev/null || [ -s "$out.tmp" ]
chmod 600 "$out.tmp"
mv "$out.tmp" "$out"
find "$DEST" -name 'spotcast-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "backup $(du -h "$out" | cut -f1) $out"
