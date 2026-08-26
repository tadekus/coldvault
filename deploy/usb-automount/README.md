# ColdVault USB auto-mount (Debian/systemd)

Makes plugging in a canary-marked USB/external drive **auto-mount** under `/media`
so the running ColdVault container's watcher detects it and uploads to S3 Deep
Archive — hands-off, no desktop session required.

The ColdVault container does the upload, so this package only has to **mount the
drive read-only under `/media` and leave it there** (removal is cleaned up on
unplug). That makes it much smaller than a full sync tool.

## Files

| File | Installed to | Role |
|------|--------------|------|
| `coldvault-usb-mount.sh` | `/usr/local/sbin/coldvault-usb-mount` | mounts one partition RO under `/media/<label>` |
| `coldvault-usb-clean.sh` | `/usr/local/sbin/coldvault-usb-clean` | lazy-unmounts a stale `/media` mount on unplug |
| `coldvault-usb-mount@.service` | `/etc/systemd/system/` | runs the mount helper for a device instance |
| `99-coldvault-usb.rules` | `/etc/udev/rules.d/` | on USB add → start service; on remove → clean |

## Install

```bash
cd deploy/usb-automount
sudo ./install-udev.sh
```

The installer copies the files, reloads systemd/udev, and **checks the two things
hot-plug into a running container needs** (warns, doesn't fail):

1. **Host `/` propagation is `shared`** — systemd's default; if not:
   `sudo mount --make-rshared /`.
2. **The container's `/media` bind uses `rslave`** — provided by the repo
   `docker-compose.yml`; if not, update it and `docker compose up -d --force-recreate`.

Without both, a drive mounted after the container started won't be visible inside it.

## Use

```bash
touch /path/to/drive/coldvault.canary        # opt this drive in
# plug it in — auto-mounts at /media/<label>, ColdVault uploads it
journalctl -fu 'coldvault-usb-mount@*'        # watch the mount
cd ~/coldvault && docker compose logs -f coldvault | grep -iE 'canary|Session'
```

Test the mount helper without a plug event: `sudo /usr/local/sbin/coldvault-usb-mount sdb1`.

## Uninstall

```bash
sudo ./uninstall-udev.sh          # add --purge to also drop the logs
```

Leaves mounted drives, canaries and ColdVault itself untouched.

## Notes

- Mounts **any** USB filesystem read-only; the **canary is the safety net** —
  ColdVault only *uploads* drives containing `coldvault.canary`, so a stray stick
  just mounts harmlessly. To restrict to specific drives, use Option B (UUID/label)
  in `99-coldvault-usb.rules`.
- **Coexists with the usb-to-nextcloud sync**: both are separate udev rules using
  `+=`, so a stick carrying both canaries triggers both. They mount at different
  paths (`/mnt/usb-sync` vs `/media`), both read-only, and don't interfere. This
  helper intentionally does **not** run `fsck` (the Nextcloud helper already repairs
  dirty exFAT, and fsck'ing a device another mount is using is unsafe).
- Read-only by design: ColdVault never modifies the source drive.
