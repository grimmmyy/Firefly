#!/bin/sh
# Keep the rolling receiver capture (rxcap.service) from overwriting itself
# during a test: copy finished files aside every 10 minutes, and after the end
# time copy the file still being written too, then switch off. Reads files only.
#   rxkeep.sh install "YYYY-MM-DD HH:MM"   | run | status | remove
set -eu
SRC=${RXKEEP_SRC:-/var/log/rxcap}
DST=${RXKEEP_DST:-/var/lib/rxkeep}
SYSD=/etc/systemd/system
SELF=/opt/spotcast/rxkeep.sh
MIN_FREE_KB=5000000

# named by last-write time, so a reused ring slot never replaces an older copy
keep() {
  name="rx-$(date -r "$1" '+%Y%m%d-%H%M%S').pcap"
  [ -e "$DST/$name" ] && return 0
  cp -p "$1" "$DST/.part" && mv "$DST/.part" "$DST/$name"
}

run() {
  end=$(cat "$DST/end")
  final=0
  [ "$(date +%s)" -ge "$end" ] && final=1
  free=$(df -Pk "$DST" | awk 'NR==2 {print $4}')
  [ "$free" -gt "$MIN_FREE_KB" ] || { echo "low disk space, nothing copied"; exit 1; }
  newest=$(ls -t "$SRC"/rx.pcap* | head -1)
  for f in "$SRC"/rx.pcap*; do
    [ "$final" = 0 ] && [ "$f" = "$newest" ] && continue
    keep "$f"
  done
  if [ "$final" = 1 ]; then
    systemctl disable --now rxkeep.timer 2>/dev/null || true
    echo "final copy done; timer off"
  fi
}

install_units() {
  mkdir -p "$DST"
  date -d "$1" +%s > "$DST/end"
  printf '[Unit]\nDescription=Copy finished receiver captures aside\n[Service]\nType=oneshot\nExecStart=/bin/sh %s run\n' \
    "$SELF" > "$SYSD/rxkeep.service"
  printf '[Unit]\nDescription=Copy receiver captures every 10 minutes\n[Timer]\nOnActiveSec=5\nOnBootSec=60\nOnUnitActiveSec=10min\n[Install]\nWantedBy=timers.target\n' \
    > "$SYSD/rxkeep.timer"
  systemctl daemon-reload
  systemctl enable --now rxkeep.timer 2>/dev/null
  echo "keeping captures until $(date -d "$1" '+%a %b %d %H:%M')"
}

case "${1:-status}" in
  install) install_units "$2" ;;
  run)     run ;;
  status)  echo "timer: $(systemctl is-active rxkeep.timer || true)"
           echo "kept: $(ls "$DST"/rx-*.pcap 2>/dev/null | wc -l) files, $(du -sh "$DST" | cut -f1)" ;;
  remove)  systemctl disable --now rxkeep.timer 2>/dev/null || true
           rm -f "$SYSD/rxkeep.service" "$SYSD/rxkeep.timer"; systemctl daemon-reload
           echo "removed (copies stay in $DST)" ;;
  *) echo "usage: $0 install \"YYYY-MM-DD HH:MM\" | run | status | remove"; exit 2 ;;
esac
