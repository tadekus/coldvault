#!/usr/bin/env bash
#
# uninstall-udev.sh
# ------------------------------------------------------------------------------
# Reverses install-udev.sh: stops any running mount instances, removes the two
# helper scripts, the systemd service and the udev rule, then reloads.
#
# It does NOT unmount drives that are currently mounted under /media (your data
# stays put), and it NEVER touches the USB drive, any canary, or the ColdVault
# container / its index. Pass --yes to skip the confirmation prompt; --purge to
# also delete the helper's logs in /var/log/coldvault-usb.
# ------------------------------------------------------------------------------
set -o errexit -o nounset -o pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }

PURGE_LOGS=false
ASSUME_YES=false
for arg in "$@"; do
    case "${arg}" in
        --purge)   PURGE_LOGS=true ;;
        --yes|-y)  ASSUME_YES=true ;;
        -h|--help) echo "Usage: $0 [--yes] [--purge]"; exit 0 ;;
        *) echo "Unknown option: ${arg}" >&2; exit 1 ;;
    esac
done

MOUNT_DST="/usr/local/sbin/coldvault-usb-mount"
CLEAN_DST="/usr/local/sbin/coldvault-usb-clean"
SERVICE_DST="/etc/systemd/system/coldvault-usb-mount@.service"
RULES_DST="/etc/udev/rules.d/99-coldvault-usb.rules"
LOG_DIR="/var/log/coldvault-usb"

echo "This removes the ColdVault USB auto-mount integration:"
echo "  ${MOUNT_DST}"
echo "  ${CLEAN_DST}"
echo "  ${SERVICE_DST}"
echo "  ${RULES_DST}"
[[ "${PURGE_LOGS}" == "true" ]] && echo "  ${LOG_DIR}   [--purge]" \
    || echo "Logs in ${LOG_DIR} will be KEPT (use --purge to remove)."
echo "Currently-mounted drives under /media are left as-is."
echo

if [[ "${ASSUME_YES}" != "true" ]]; then
    read -r -p "Proceed? [y/N] " reply
    case "${reply}" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

echo "Stopping any running mount instances..."
mapfile -t UNITS < <(systemctl list-units --all --plain --no-legend \
    'coldvault-usb-mount@*.service' 2>/dev/null | awk '{print $1}' \
    | grep -E '^coldvault-usb-mount@.+\.service$' || true)
for unit in "${UNITS[@]:-}"; do
    [[ -n "${unit}" ]] || continue
    echo "  stopping ${unit}"
    systemctl stop "${unit}" 2>/dev/null || true
    systemctl reset-failed "${unit}" 2>/dev/null || true
done

# Remove the udev rule first so no new instances can be triggered.
for f in "${RULES_DST}" "${SERVICE_DST}" "${MOUNT_DST}" "${CLEAN_DST}"; do
    if [[ -e "${f}" ]]; then echo "Removing ${f}"; rm -f "${f}"; fi
done

echo "Reloading systemd and udev..."
systemctl daemon-reload
udevadm control --reload

if [[ "${PURGE_LOGS}" == "true" && -d "${LOG_DIR}" ]]; then
    echo "Removing logs ${LOG_DIR}"; rm -rf "${LOG_DIR}"
fi

echo
echo "Uninstall complete. USB drives, canaries and ColdVault itself were not touched."
