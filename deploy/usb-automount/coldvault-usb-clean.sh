#!/usr/bin/env bash
#
# coldvault-usb-clean.sh
# ------------------------------------------------------------------------------
# Runs on USB "remove" (from 99-coldvault-usb.rules). Lazy-unmounts any
# /media/<label> whose backing block device has gone away, and removes the now-
# empty mountpoint directory. Mounts whose device is still present are left
# untouched, so this is safe to run on any USB removal.
#
# Only stale mountpoints are cleaned: a /media entry that is not one of our
# mountpoints, or whose device still exists, is never touched.
# ------------------------------------------------------------------------------
set -o nounset

LOG="/var/log/coldvault-usb/coldvault-usb.log"
_log() { printf '%s coldvault-usb: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"${LOG}" 2>/dev/null || true; }

shopt -s nullglob
for mp in /media/*/; do
    mp="${mp%/}"
    mountpoint -q "${mp}" || continue
    src="$(findmnt -nro SOURCE "${mp}" 2>/dev/null || true)"
    # Backing device gone -> stale mount left behind by an unplugged drive.
    if [[ -n "${src}" && ! -b "${src}" ]]; then
        umount -l "${mp}" 2>/dev/null || true
        rmdir "${mp}" 2>/dev/null || true
        _log "cleaned stale mount ${mp} (device ${src} gone)"
    fi
done
