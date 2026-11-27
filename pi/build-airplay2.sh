#!/bin/sh
# Builds AirPlay 2 (shairport-sync + nqptp) from source: Debian's package is
# classic AirPlay only. ~5 min on a Pi 5. spotcast runs shairport-sync itself.
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq build-essential git autoconf automake libtool libpopt-dev \
  libconfig-dev libasound2-dev avahi-daemon libavahi-client-dev libssl-dev libsoxr-dev \
  libplist-dev libplist-utils libsodium-dev libavutil-dev libavcodec-dev libavformat-dev \
  uuid-dev libgcrypt20-dev xxd libpulse-dev libpipewire-0.3-dev >/dev/null
cd /usr/local/src
[ -d nqptp ] || git clone -q --depth 1 https://github.com/mikebrady/nqptp.git
(cd nqptp && autoreconf -fi >/dev/null 2>&1 && ./configure --with-systemd-startup >/dev/null \
  && make -s -j4 && make -s install)
[ -d shairport-sync ] || git clone -q --depth 1 https://github.com/mikebrady/shairport-sync.git
(cd shairport-sync && autoreconf -fi >/dev/null 2>&1 && ./configure --sysconfdir=/etc \
  --with-alsa --with-pulseaudio --with-pipewire --with-soxr --with-avahi --with-ssl=openssl \
  --with-airplay-2 --with-metadata --with-dbus-interface >/dev/null && make -s -j4 && make -s install)
systemctl daemon-reload
systemctl disable --now shairport-sync 2>/dev/null || true    # spotcast starts it
systemctl enable --now nqptp
/usr/local/bin/shairport-sync -V
