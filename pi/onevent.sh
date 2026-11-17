#!/bin/sh
curl -s -m 2 "http://127.0.0.1:8090/event?e=${PLAYER_EVENT}&v=${VOLUME}" >/dev/null 2>&1 &
exit 0
