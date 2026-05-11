#!/usr/bin/env bash
set -e

mkdir -p /config /recordings
/setuid.sh

exec su-exec dashcam:dashcam python3 -m web.launcher
