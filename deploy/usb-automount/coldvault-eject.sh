#!/usr/bin/env bash
#
# coldvault-eject.sh
# ------------------------------------------------------------------------------
# Unmounts drives that ColdVault reports as finished. The container cannot
# unmount a host filesystem, so it only FLAGS a drive once its canary upload
# completed with zero failures; this host-side helper does the actual unmount
# and reports back.
#
# Run by coldvault-eject.timer (every 30s). Safe to run by hand.
#
# Requires COLDVAULT_EJECT_AFTER_UPLOAD=true in ColdVault's .env — otherwise the
# pending list is always empty and this does nothing.
# ------------------------------------------------------------------------------
set -o nounset -o pipefail

API="${COLDVAULT_API:-http://127.0.0.1:9999}"
LOG="/var/log/coldvault-usb/coldvault-eject.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log() { printf '%s coldvault-eject: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG" 2>/dev/null || true; }

# Is $1 a mount point? Returns 2 when we genuinely can't tell, so the caller
# attempts the unmount rather than assuming it's already gone.
is_mounted() {
    if command -v findmnt >/dev/null 2>&1; then findmnt -rn --target "$1" --mountpoint "$1" >/dev/null 2>&1; return $?; fi
    if command -v mountpoint >/dev/null 2>&1; then mountpoint -q "$1"; return $?; fi
    if [ -r /proc/mounts ]; then grep -qE "[[:space:]]${1//\//\\/}[[:space:]]" /proc/mounts; return $?; fi
    return 2
}

command -v curl >/dev/null 2>&1 || { log "curl missing"; exit 0; }

pending="$(curl -fsS --max-time 10 "${API}/api/eject/pending" 2>/dev/null)" || exit 0
printf '%s' "$pending" | grep -q '"mount"' || exit 0

# Extract mount paths without needing jq.
mounts="$(printf '%s' "$pending" \
  | tr ',' '\n' | grep -o '"mount": *"[^"]*"' | sed 's/.*"mount": *"//; s/"$//')"
[ -n "$mounts" ] || exit 0

printf '%s\n' "$mounts" | while IFS= read -r mp; do
    [ -n "$mp" ] || continue
    is_mounted "$mp"; state=$?
    if [ "$state" -eq 1 ]; then
        log "$mp is not mounted — reporting done"
        curl -fsS --max-time 10 -X POST "${API}/api/eject/done" \
             -H 'Content-Type: application/json' \
             -d "{\"mount\":\"${mp}\",\"ok\":true,\"detail\":\"already unmounted\"}" >/dev/null 2>&1
        continue
    fi
    sync
    if err="$(umount "$mp" 2>&1)"; then
        rmdir "$mp" 2>/dev/null || true
        log "unmounted $mp — safe to remove"
        ok=true
    elif printf '%s' "$err" | grep -qiE 'not mounted|no mount point'; then
        log "$mp was already unmounted"
        err="already unmounted"; ok=true
    else
        log "umount $mp failed: $err"
        ok=false
    fi
    curl -fsS --max-time 10 -X POST "${API}/api/eject/done" \
         -H 'Content-Type: application/json' \
         -d "{\"mount\":\"${mp}\",\"ok\":${ok},\"detail\":\"$(printf '%s' "${err:-}" | tr -d '"' | head -c 200)\"}" \
         >/dev/null 2>&1
done
