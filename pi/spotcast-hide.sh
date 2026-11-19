#!/bin/sh
# Runs as root before spotcast starts (ExecStartPre=+). Linked-account Soloist
# slots are started inside the "hidden" cgroup; this firewall rule drops their
# mDNS (LAN discovery) packets, so phones on the network see only the "Add me"
# spare. A linked slot still shows for its own account through Spotify's
# servers, so each person sees one "Living Room (RX-V671)": their own.
set -eu
CG=/sys/fs/cgroup/system.slice/spotcast.service/hidden
mkdir -p "$CG"
chown -R spotcast "$CG"
nft delete table inet spotcast_hide 2>/dev/null || true
nft -f - <<'NFT'
table inet spotcast_hide {
  chain out {
    type filter hook output priority 0; policy accept;
    socket cgroupv2 level 3 "system.slice/spotcast.service/hidden" udp dport 5353 counter drop
    socket cgroupv2 level 3 "system.slice/spotcast.service/hidden" udp sport 5353 counter drop
  }
}
NFT
