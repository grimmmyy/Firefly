#!/bin/sh
# shairport-sync calls this with the AirPlay volume (-30..0, -144 = mute) appended.
exec curl -s -m 3 -o /dev/null "http://127.0.0.1:8090/api/x/airplay_volume?v=$1"
