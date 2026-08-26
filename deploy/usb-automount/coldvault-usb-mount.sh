#!/usr/bin/env bash
#
# coldvault-usb-mount.sh  <kernel-name, e.g. sdb1>
# ------------------------------------------------------------------------------
# Mount a freshly-plugged USB partition READ-ONLY under /media/<label> so the
# ColdVault container's watcher (which polls /media) can find its canary and
# upload it to S3 Deep Archive.
#
# Unlike the Nextcloud sync helper, this script does NOT do the transfer and does
# NOT unmount when "done" -- the container uploads while the drive stays mounted.
# Removal is handled separately by coldvault-usb-clean (udev "remove" rule).
#
# It deliberately does NOT run fsck: on a machine that also runs the Nextcloud
# usb-to-nextcloud sync, that helper already repairs dirty exFAT volumes, and
# fsck'ing a device another process may have mounted is unsafe. If a mount fails
# because the volume is dirty, we log it and leave it to the user / the other
# tool; re-plugging after a repair works.
#
# Called by coldvault-usb-mount@.service (started from 99-coldvault-usb.rules).
# Also runnable by hand:  sudo ./coldvault-usb-mount.sh sdb1
# ------------------------------------------------------------------------------
set -o errexit -o nounset -o pipefail

LOG_DIR="/var/log/coldvault-usb"
LOG="${LOG_DIR}/coldvault-usb.log"
mkdir -p "${LOG_DIR}"
log() { printf '%s coldvault-usb: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"${LOG}"; }

# Device name from arg (%I) or the TRIGGER_DEVICE env the service sets.
name="${1:-${TRIGGER_DEVICE:-}}"
name="${name#/dev/}"
[[ -n "${name}" ]] || { log "no device given; nothing to do"; exit 0; }
dev="/dev/${name}"
[[ -b "${dev}" ]] || { log "${dev} is not a block device; skipping"; exit 0; }

# Only mount partitions that actually carry a filesystem (skip partition tables,
# swap, LVM members, etc.). ColdVault is filesystem-agnostic, so we accept any
# fstype the kernel can mount (exfat, ntfs, ext4, hfsplus, ...).
fstype="$(lsblk -rno FSTYPE "${dev}" 2>/dev/null | head -n1 || true)"
[[ -n "${fstype}" ]] || { log "${dev} has no filesystem; skipping"; exit 0; }

# Mountpoint = /media/<sanitised label>, falling back to the kernel name.
label="$(lsblk -rno LABEL "${dev}" 2>/dev/null | head -n1 || true)"
label="$(printf '%s' "${label}" | tr -cd '[:alnum:]._-')"
[[ -n "${label}" ]] || label="${name}"
mp="/media/${label}"

if mountpoint -q "${mp}"; then
    log "${mp} is already a mountpoint; leaving it as-is"
    exit 0
fi

mkdir -p "${mp}"
if timeout 20 mount -o ro "${dev}" "${mp}" 2>>"${LOG}"; then
    log "mounted ${dev} (${fstype}, label='${label}') read-only at ${mp}"
else
    rc=$?
    rmdir "${mp}" 2>/dev/null || true
    if [[ ${rc} -eq 124 ]]; then
        log "mount of ${dev} timed out (volume likely dirty/corrupt) -- not mounted"
    else
        log "mount of ${dev} at ${mp} failed (rc=${rc}) -- not mounted"
    fi
    exit ${rc}
fi
