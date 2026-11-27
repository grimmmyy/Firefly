#!/bin/sh
# Build gmediarender ("Living Room (Pi cast)") with a 30-minute SSDP
# advertisement lifetime. The Debian build asks libupnp for 100 s, so it
# re-announces itself every few seconds (~46,000 packets a day), which the
# RX-V671's network module has to process. Installs to /usr/local/bin, which
# comes before /usr/bin in PATH.
set -eu
SRC=/usr/local/src/gmrender
mkdir -p "$SRC"
cd "$SRC"
[ -d gmrender-resurrect ] || git clone --depth 1 https://github.com/hzeller/gmrender-resurrect.git
cd gmrender-resurrect
sed -i 's/UpnpSendAdvertisement(result_device->device_handle, 100)/UpnpSendAdvertisement(result_device->device_handle, 1800)/' src/upnp_device.c
grep -q 'device_handle, 1800)' src/upnp_device.c
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq autoconf automake libtool pkg-config \
  libupnp-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
./autogen.sh
./configure --prefix=/usr/local
make -j3
install -m 755 src/gmediarender /usr/local/bin/gmediarender
echo "gmediarender with 30-minute announcements installed"
