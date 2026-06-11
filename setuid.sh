#!/usr/bin/env bash

if [[ ${PUID:-0} -gt 0 ]]; then
    usermod -o -u "$PUID" dashcam
fi

if [[ ${PGID:-0} -gt 0 ]]; then
    groupmod -o -g "$PGID" dashcam
fi

# Grant the app user access to the GPU render node(s) so hardware-accelerated
# decode/encode (filmstrips, exports) works. The render node is group-owned
# with no world access and its GID varies by NAS host. We gather candidate
# GIDs from two sources so either deployment style works:
#   1. the group that owns each /dev/dri node  — automatic, no compose change
#   2. groups granted to the container via compose `group_add:` (the standard
#      Synology approach; visible here as this script's own supplementary
#      groups since it runs as root before the gosu drop)
# and add dashcam to each in /etc/group. This is required because entrypoint.sh
# drops privileges with `gosu dashcam`, whose initgroups() reads /etc/group
# — a group_add GID that isn't mirrored there would otherwise be lost.
# Best-effort: a failure here (e.g. no passthrough) must not stop startup.
gpu_gids=""
for dev in /dev/dri/renderD* /dev/dri/card*; do
    [[ -e "$dev" ]] || continue
    g=$(stat -c '%g' "$dev" 2>/dev/null) && gpu_gids="$gpu_gids $g"
done
gpu_gids="$gpu_gids $(id -G 2>/dev/null)"                 # group_add GIDs

for gid in $gpu_gids; do
    [[ "$gid" == 0 || "$gid" == "${PGID:-0}" ]] && continue   # skip root / own primary
    grp=$(awk -F: -v g="$gid" '$3 == g { print $1 }' /etc/group)
    if [[ -z "$grp" ]]; then
        grp="gpu_$gid"
        groupadd -o -g "$gid" "$grp" 2>/dev/null || true
    fi
    usermod -aG "$grp" dashcam 2>/dev/null || true
done

exit 0
