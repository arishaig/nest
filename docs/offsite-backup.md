# Offsite backup (Google Drive)

An encrypted, versioned, one-way copy of the state that cannot be rebuilt from
Git, kept in the owner's **personal** Google Drive. Nothing depends on it: no
service needs Drive to run or deploy. It exists so losing the Proxmox host (or
its Tank pool) does not lose the unrecoverable parts of the lab.

**Not included, by design:** media, and `Tank/personal` (a copy of the owner's
Google Drive, so Drive already holds it). Home Assistant is not included; check
whether HA's own Settings → System → Backups offers Google Drive.

## What is backed up

`/usr/local/sbin/offsite-backup` on the PVE host (`pve.yml`), nightly at 05:15:

| Source | Why |
|---|---|
| App config volumes (`offsite_config_volumes` in `pve.yml`) | Authelia users/TOTP, tunarr, Jellyfin plugin/config, *arr configs, small app state. Read from a ZFS snapshot (`rpool/data/k8s-configs@offsite`) because several are live SQLite files. Excludes artwork, logs, caches, search indexes and `jellyfin/metadata,data` (rebuilt by a rescan). |
| Newest Postgres dumps (shared + audiomuse) | The real database backups; live PGDATA is not copied. |
| Newest etcd snapshot | Contains every Secret; safe only because restic encrypts it. |
| OpenTofu state (CI runner's `/opt/terraform-state`) | See [disaster-recovery.md](disaster-recovery.md) step 1. |
| `/etc/pve` | VM/CT configs, storage, users, firewall. |

Retention: 7 daily, 4 weekly, 6 monthly (grouped by host+tag). Sundays also
prune, `restic check --read-data-subset=2%`, and enforce a size cap
(`offsite_cap_gb`, default 50 GB — the set is a few GB). Exceeding the cap fails
the unit, which pages through `OffsiteBackupFailed`. Uploads are throttled
(`offsite_upload_limit_kib`).

## One-time setup (owner)

1. **Google OAuth client** in your *personal* account (Cloud Console →
   APIs & Services → Credentials → OAuth client ID, "Desktop app"; enable the
   Drive API). Set the consent screen to **In production** — in "Testing" mode
   Google expires refresh tokens after 7 days. **Never change this client
   afterwards**: with the `drive.file` scope a different client cannot see the
   existing repository.
2. **Token**, on a machine with a browser, signed in to the *personal* account
   (not ECRS): `rclone authorize "drive" "<client_id>" "<client_secret>"`
   (choose scope `drive.file` if asked) and copy the JSON it prints.
3. `scripts/store-drive-credentials.sh` stores client id, secret and token in
   SOPS. Commit `inventory/group_vars/all.sops.yaml`; the pve deploy installs
   the config and enables the timer.
4. **Create the repository once, by hand** (the script never does this, so a
   re-auth can't silently start a second empty repo):
   `ssh root@pve offsite-restic init`
5. Run the first backup: `systemctl start offsite-backup` and watch
   `journalctl -fu offsite-backup`.
6. **Save the restic password in Bitwarden** (with the client id/secret):
   `sops -d --extract '["vault_restic_password"]' inventory/group_vars/all.sops.yaml`.
   Without it the backup is unreadable, and the usual route to it
   (age key → GitHub → SOPS) may not exist in a disaster.

## Restore (bare metal)

You need only: Bitwarden (restic password, OAuth client id/secret), the `restic`
and `rclone` binaries, and a browser to re-authorize if the token is gone.

```sh
rclone config   # remote "gdrive", type drive, scope drive.file, SAME client id/secret
export RESTIC_PASSWORD='…from Bitwarden…'
export RESTIC_REPOSITORY=rclone:gdrive:nest-restic
restic snapshots
restic restore latest --tag offsite --target /restore
```

Restore targets: Authelia/app volumes under `…/k8s-configs/…`; Postgres dumps
per [postgres.md](postgres.md#restore); etcd via `talosctl bootstrap
--recover-from` ([disaster-recovery.md](disaster-recovery.md)); OpenTofu state
per DR step 1.

## Notes

- Old 2025 rclone remotes exist in `/root/.config/rclone` on PVE and PBS. This
  job ignores them (`RCLONE_CONFIG=/etc/rclone/rclone.conf`); revoke those
  tokens in the Google account's security settings.
- Alerts: `OffsiteBackupFailed`, `OffsiteBackupStale` (>36 h),
  `OffsiteBackupMissing` (timer absent for 48 h — i.e. setup unfinished).
- Test without Drive: `OFFSITE_REPOSITORY=/some/local/dir offsite-restic init`,
  then run the script with the same variable.
