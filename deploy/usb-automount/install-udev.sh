#!/usr/bin/env bash
#
# install-udev.sh
# ------------------------------------------------------------------------------
# One-time installer for ColdVault's USB auto-mount-on-plug-in. Run as root from
# this directory. It installs a mount helper, a cleanup helper, a templated
# systemd service and a udev rule, then reloads systemd + udev.
#
# Unlike the Nextcloud sync installer, this does NOT need rclone/curl -- it only
# mounts the drive; the ColdVault container does the upload. It DOES check the
# two things hot-plug needs to reach a running container:
#   1. host / mount propagation is "shared"
#   2. the coldvault container's /media bind uses rslave propagation
# and warns (does not fail) if either is missing.
# ------------------------------------------------------------------------------
set -o errexit -o nounset -o pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MOUNT_SRC="${SRC_DIR}/coldvault-usb-mount.sh"
CLEAN_SRC="${SRC_DIR}/coldvault-usb-clean.sh"
SERVICE_SRC="${SRC_DIR}/coldvault-usb-mount@.service"
RULES_SRC="${SRC_DIR}/99-coldvault-usb.rules"

MOUNT_DST="/usr/local/sbin/coldvault-usb-mount"
CLEAN_DST="/usr/local/sbin/coldvault-usb-clean"
SERVICE_DST="/etc/systemd/system/coldvault-usb-mount@.service"
RULES_DST="/etc/udev/rules.d/99-coldvault-usb.rules"
EJECT_SRC="${SRC_DIR}/coldvault-eject.sh"
EJECT_DST="/usr/local/sbin/coldvault-eject"
EJECT_SVC="/etc/systemd/system/coldvault-eject.service"
EJECT_TIMER="/etc/systemd/system/coldvault-eject.timer"

for f in "${MOUNT_SRC}" "${CLEAN_SRC}" "${SERVICE_SRC}" "${RULES_SRC}"; do
    [[ -f "${f}" ]] || { echo "Missing: ${f}" >&2; exit 1; }
done

echo "Installing mount helper   -> ${MOUNT_DST}"
install -o root -g root -m 0755 "${MOUNT_SRC}"   "${MOUNT_DST}"
echo "Installing cleanup helper -> ${CLEAN_DST}"
install -o root -g root -m 0755 "${CLEAN_SRC}"   "${CLEAN_DST}"
echo "Installing service        -> ${SERVICE_DST}"
install -o root -g root -m 0644 "${SERVICE_SRC}" "${SERVICE_DST}"
echo "Installing udev rule      -> ${RULES_DST}"
install -o root -g root -m 0644 "${RULES_SRC}"   "${RULES_DST}"

# --- auto-eject after a successful upload (optional but installed by default) ---
if [[ -f "${EJECT_SRC}" ]]; then
    echo "Installing eject helper    -> ${EJECT_DST}"
    install -o root -g root -m 0755 "${EJECT_SRC}" "${EJECT_DST}"
    install -o root -g root -m 0644 "${SRC_DIR}/coldvault-eject.service" "${EJECT_SVC}"
    install -o root -g root -m 0644 "${SRC_DIR}/coldvault-eject.timer"   "${EJECT_TIMER}"
fi

echo "Reloading systemd and udev..."
systemctl daemon-reload
udevadm control --reload
if [[ -f "${EJECT_TIMER}" ]]; then
    systemctl enable --now coldvault-eject.timer >/dev/null 2>&1 \
      && echo "  auto-eject timer enabled (polls every 30s)" \
      || echo "  WARN could not enable coldvault-eject.timer"
fi

echo
echo "Checking hot-plug prerequisites (warnings only):"

prop="$(findmnt -no PROPAGATION / 2>/dev/null || true)"
case "${prop}" in
    *shared*) echo "  OK    host / propagation = '${prop}'." ;;
    *)        echo "  WARN  host / propagation = '${prop:-unknown}' -- new mounts won't reach the container."
              echo "        Fix: sudo mount --make-rshared /   (systemd normally sets this at boot)" ;;
esac

if command -v docker >/dev/null 2>&1; then
    media_prop="$(docker inspect coldvault \
        --format '{{range .Mounts}}{{if eq .Destination "/media"}}{{.Propagation}}{{end}}{{end}}' \
        2>/dev/null || true)"
    case "${media_prop}" in
        rslave|slave|rshared|shared) echo "  OK    container /media propagation = '${media_prop}'." ;;
        "") echo "  WARN  couldn't read the coldvault container's /media mount."
            echo "        Is the container running with '/media' bound? (docker ps | grep coldvault)" ;;
        *)  echo "  WARN  container /media propagation = '${media_prop}' -- need 'rslave'."
            echo "        Use the repo docker-compose.yml (rslave block) and: docker compose up -d --force-recreate" ;;
    esac
fi

cat <<'DONE'

Installed.

Next:
  1. Put the canary in the drive's root:   touch /path/to/drive/coldvault.canary
     (or JSON for a stable label:  echo '{"name":"movie-drive-01"}' > .../coldvault.canary)
  2. Plug the drive in. It auto-mounts read-only under /media/<label>, and the
     ColdVault container's watcher uploads it to S3 Deep Archive.

Watch it happen:
  journalctl -fu 'coldvault-usb-mount@*'
  cd ~/coldvault && docker compose logs -f coldvault | grep -iE 'canary|Session|verified'

Test the mount helper without a real plug-in (replace sdb1 with your partition):
  sudo /usr/local/sbin/coldvault-usb-mount sdb1

To pin to one specific drive instead of any USB filesystem, edit
/etc/udev/rules.d/99-coldvault-usb.rules (Option B) then: sudo udevadm control --reload
DONE
