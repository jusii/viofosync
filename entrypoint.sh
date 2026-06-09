#!/usr/bin/env bash
set -e

mkdir -p /config /recordings
/setuid.sh

# `gosu dashcam` (user form, NOT `dashcam:dashcam`): the user form calls
# initgroups() so the supplementary groups from /etc/group — including the GPU
# render group added in setuid.sh — are applied. The explicit `user:group` form
# would replace them with just that one group, dropping render-node access.
exec gosu dashcam python3 -m web.launcher
