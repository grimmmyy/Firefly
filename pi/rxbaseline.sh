#!/bin/sh
# 48-hour baseline: keep every part of the bridge away from the receiver, to see
# whether it still freezes without us. Nothing on the receiver is changed.
#   rxbaseline.sh start [hours]   refuses unless the receiver answers
#   rxbaseline.sh arm | disarm    start by itself after the next recovery
#   rxbaseline.sh status          how long, and what the Pi has sent to it
#   rxbaseline.sh stop            restore (also runs by itself at the end)
# "rxbaseline.sh test" starts without the health check, to try the script.
# rxcap keeps recording; it only listens.
set -eu
FLAG=/etc/rxbaseline.active
UNITS="spotcast.service alexa-local.service spotcast-bt-agent.service soloist-update.service"
RUNNING="spotcast.service alexa-local.service spotcast-bt-agent.service"
END=rxbaseline-end
SYSD=/etc/systemd/system
SELF=/opt/spotcast/rxbaseline.sh
RX_MAC=00:a0:de:86:51:6f
ARMED=/etc/rxbaseline.armed
ARM=rxbaseline-arm.service
SETTLE=${RXB_SETTLE:-300}     # seconds between recovery and the start

# same phone alerts as the bridge, if it has a topic
notify() {
  topic=$(sed -n 's/.*SPOTCAST_NTFY_TOPIC=\([A-Za-z0-9_-]*\).*/\1/p' \
    "$SYSD/spotcast.service.d/alerts.conf" 2>/dev/null | head -1)
  [ -n "$topic" ] || return 0
  curl -s -m 10 -H "Title: Receiver test" -d "$1" "https://ntfy.sh/$topic" >/dev/null || true
}

# outages of 2 min or more that ended; shorter ones are blips that clear alone
count_recoveries() {
  awk '/receiver control recovered after [0-9]+s down/ {
         t = $0; sub(/.*recovered after /, "", t); sub(/s down.*/, "", t)
         if (t + 0 >= 120) n++ } END { print n + 0 }'
}

recoveries() {
  journalctl -u spotcast.service --since "@$(cat "$ARMED")" -o cat --no-pager | count_recoveries
}

arm() {
  [ -e "$FLAG" ] && { echo "The baseline is already running."; exit 1; }
  date +%s > "$ARMED"
  printf '[Unit]
Description=Start the receiver baseline after the next recovery
[Service]
ExecStart=/bin/sh %s waiter
Restart=on-failure
RestartSec=30
[Install]
WantedBy=multi-user.target
'     "$SELF" > "$SYSD/$ARM"
  systemctl daemon-reload
  systemctl enable "$ARM" 2>/dev/null
  systemctl restart "$ARM"
  echo "Armed: the 48-hour test starts $((SETTLE / 60)) min after the receiver next recovers."
}

# does not stop the running unit: the waiter calls this on itself
forget_arm() {
  systemctl disable "$ARM" 2>/dev/null || true
  rm -f "$ARMED" "$SYSD/$ARM"
  systemctl daemon-reload
}

disarm() {
  systemctl stop "$ARM" 2>/dev/null || true
  forget_arm
  echo "Disarmed."
}

# second signal, for when the bridge misses a recovery: the receiver broadcasts
# for an address after its network cable or power is reconnected (listen only)
last_dhcp() {
  for f in $(ls -t /var/log/rxcap/rx.pcap* 2>/dev/null | head -2); do
    tcpdump -r "$f" -n -tt "ether src $RX_MAC and udp dst port 67" 2>/dev/null || true
  done | awk -v s="$1" 'int($1) > s { if (int($1) > m) m = int($1) } END { print m + 0 }'
}

waiter() {
  seen=0
  dhcp_seen=$(cat "$ARMED")
  while [ -e "$ARMED" ]; do
    n=$(recoveries)
    d=$(last_dhcp "$dhcp_seen")
    if [ "$n" -gt "$seen" ] || [ "$d" -gt 0 ]; then
      seen=$n
      [ "$d" -gt 0 ] && dhcp_seen=$d
      notify "Receiver recovery or reconnect seen. Check Network Standby is On. The 48-hour test starts in $((SETTLE / 60)) minutes: close Firefly on all phones."
      sleep "$SETTLE"
      if healthy; then
        forget_arm
        start 48
        notify "48-hour test started. Spotify, AirPlay and Alexa through the Pi are off. Leave the receiver's settings and web page alone."
        return 0
      fi
      notify "The receiver is not answering, so the test did not start. Still waiting for the next recovery."
    fi
    sleep 30
  done
}

receiver_ip() {
  cat /var/lib/spotcast-user/receiver_ip 2>/dev/null || echo 192.168.1.7
}

healthy() {
  curl -s -m 6 -H "Content-Type: text/xml" \
    -d '<YAMAHA_AV cmd="GET"><Main_Zone><Basic_Status>GetParam</Basic_Status></Main_Zone></YAMAHA_AV>' \
    "http://$(receiver_ip)/YamahaRemoteControl/ctrl" | grep -q "<Power>"
}

start() {
  hours=$1
  [ -e "$FLAG" ] && { echo "Already running."; exit 1; }
  # a condition (not disable/mask) also blocks restarts by other units and reboots
  for u in $UNITS; do
    mkdir -p "$SYSD/$u.d"
    printf '[Unit]\nConditionPathExists=!%s\n' "$FLAG" > "$SYSD/$u.d/rxbaseline.conf"
  done
  printf '[Unit]\nDescription=End the receiver baseline\n[Service]\nType=oneshot\nExecStart=/bin/sh %s stop\n' \
    "$SELF" > "$SYSD/$END.service"
  printf '[Unit]\nDescription=End the receiver baseline\n[Timer]\nOnCalendar=%s\nPersistent=true\n[Install]\nWantedBy=timers.target\n' \
    "$(date -d "+$hours hours" '+%Y-%m-%d %H:%M:%S')" > "$SYSD/$END.timer"
  systemctl daemon-reload
  systemctl enable --now "$END.timer" 2>/dev/null
  date +%s > "$FLAG"
  systemctl stop $RUNNING
  echo "Baseline started; ends by itself $(date -d "+$hours hours" '+%a %b %d %H:%M')."
  echo "Spotify, AirPlay, Alexa and the app's Pi controls are off until then."
}

stop() {
  systemctl disable "$END.timer" 2>/dev/null || true
  systemctl stop "$END.timer" 2>/dev/null || true
  rm -f "$FLAG"
  for u in $UNITS; do
    rm -f "$SYSD/$u.d/rxbaseline.conf"
    rmdir "$SYSD/$u.d" 2>/dev/null || true
  done
  rm -f "$SYSD/$END.service" "$SYSD/$END.timer"
  systemctl daemon-reload
  systemctl start $RUNNING
  echo "Baseline ended; bridge services started."
  notify "Receiver test ended. Pi controls are back on."
}

status() {
  if [ ! -e "$FLAG" ]; then
    if [ -e "$ARMED" ]; then
      echo "Armed since $(date -d "@$(cat "$ARMED")" '+%a %b %d %H:%M'), waiting for a recovery (waiter: $(systemctl is-active "$ARM" || true))."
    else
      echo "No baseline running."
    fi
    return 0
  fi
  since=$(cat "$FLAG")
  echo "Running since $(date -d "@$since" '+%a %b %d %H:%M') ($(( ($(date +%s) - since) / 60 )) min)"
  for u in $RUNNING; do echo "  $u: $(systemctl is-active "$u" || true)"; done
  # IP packets this Pi sent to the receiver, after a 15 s grace for open
  # connections to close; the capture rolls over, so say how far back it goes
  pi=$(ip -4 -o addr show eth0 | awk '{split($4,a,"/"); print a[1]; exit}')
  n=$(for f in /var/log/rxcap/rx.pcap*; do
        tcpdump -r "$f" -n -tt "ip and src host $pi and ether dst $RX_MAC" 2>/dev/null || true
      done | awk -v s="$((since + 15))" '$1 >= s' | wc -l)
  oldest=$(for f in /var/log/rxcap/rx.pcap*; do
        tcpdump -r "$f" -n -tt -c 1 2>/dev/null | awk '{print int($1)}'
      done | sort -n | head -1)
  from=$(( oldest > since ? oldest : since ))
  echo "  Pi packets to the receiver: $n (capture checked from $(date -d "@$from" '+%a %H:%M'))"
}

case "${1:-status}" in
  start)
    healthy || { echo "The receiver isn't answering. Restore it first, then start."; exit 1; }
    [ -e "$ARMED" ] && disarm
    start "${2:-48}" ;;
  arm)    arm ;;
  disarm) disarm ;;
  waiter) waiter ;;
  test)   notify() { :; }; start 1 ;;
  stop)   stop ;;
  status) status ;;
  *) echo "usage: $0 start [hours] | arm | disarm | status | stop"; exit 2 ;;
esac
