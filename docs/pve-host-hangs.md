# PVE host hangs

**Status (2026-09-23):** root cause suspected, not confirmed. Recovery and detection are
automated by `playbooks/provision/pve.yml` once the steps below are done.

## What happens

Every ~13d 23h 15m the Proxmox host hard-hangs: fans keep spinning, nothing responds, and
the previous boot's journal just stops (no shutdown sequence, no kernel panic, no OOM).
Until 2026-09 it had to be reset by hand, so each hang meant 2–8.5 hours of downtime:

| Last journal entry | Back up | Down |
|---|---|---|
| Fri 2026-07-24 06:43 | 10:26 | 3h43m |
| Fri 2026-08-07 05:58 | 14:24 | 8h26m |
| Fri 2026-08-21 05:15 | 07:25 | 2h10m |
| Fri 2026-09-04 04:31 | 06:30 | 2h |
| Fri 2026-09-18 03:43 | 08:05 | 4h22m |

The interval is 13d23h15m ±2 min, and each hang lands ~45 min earlier than the last. That
cadence comes from a clock, not from random hardware failure. Next expected: **Fri
2026-10-02 around 03:00 PDT**.

During the hang the Raspberry Pis and omega stay up, and their pods crash-loop with
`no route to host` to the API server until the host returns. The RTC coming up as
2017-01-01 on each boot is a separate problem: a dead CMOS battery.

## Suspected cause: UPS self-test on a worn battery

Many UPSes run an automatic self-test every 14 days, switching the load to battery
briefly. With a worn battery, the switch-over can dip the voltage enough to freeze the
board without cutting power. The ~0.2% drift per cycle matches a UPS's internal clock
drifting. The UPS has no USB link to the host, so the host never sees the event.

## What's automated (pve.yml)

- **Hardware watchdog.** `watchdog-mux` uses the B450's FCH TCO timer (`sp5100_tco`)
  instead of `softdog`, which dies with the kernel. A hang now resets the board ~10s
  after `watchdog-mux` stops feeding it. **Takes effect only after the next reboot.**
- **Panic → reboot.** `kernel.panic=10`, `kernel.hardlockup_panic=1`.
- **Unclean-shutdown detection.** `pve-boot-check.service` writes a marker on clean
  shutdown. If the marker is missing at boot, it logs `UNCLEAN SHUTDOWN` at crit, which
  fires the Loki ruler alert `HostUncleanShutdown` after the host is back up. Without
  this, a watchdog-recovered hang would be a 5-minute blip nobody notices, and the
  evidence for the UPS theory would disappear.
- **External heartbeat.** When `vault_pve_heartbeat_url` is set, the host pings it every
  minute, and the external service alerts when the pings stop. Everything else that
  alerts (Prometheus, Alertmanager, Home Assistant) runs on this host and freezes with it.

## One-time setup

1. **Heartbeat URL.** Create a check at healthchecks.io (free tier) with period 1 min and
   grace 10 min, and connect the notification method you want (e.g. the mobile app or
   email). Add the ping URL to vault:
   `ansible-vault edit inventory/group_vars/all/vault.yml` →
   `vault_pve_heartbeat_url: "https://hc-ping.com/<uuid>"`. Push, and CI applies it.
   An unclean boot also posts to `<url>/fail`, so you'll get a "down" notice with the
   timing in the body, followed by "up" a minute later.
2. **CMOS battery.** Replace the CR2032.
3. **PiKVM** (`https://192.168.1.195`, wired to the host's power/reset headers). Log in
   and change both default credentials (web `admin/admin`, SSH `root/root`). It has power
   control over the hypervisor. It's the manual fallback if the watchdog doesn't catch a
   hang.

## Verification (do after merging)

1. Reboot the host at a convenient time. Expect a batch of `Failed` pods and
   `KubePodNotReady` warnings afterwards, the same as after any host restart.
2. Confirm the hardware watchdog is active:
   `journalctl -u watchdog-mux -b` → `Watchdog driver 'SP5100 TCO timer'`, and
   `cat /sys/class/watchdog/watchdog0/identity` → `SP5100 TCO timer`.
3. Confirm the boot check ran: `journalctl -t pve-boot-check -b` → "clean shutdown".
4. **Watchdog test (resets the host):** `kill -STOP $(pidof watchdog-mux)`. The host
   should reset within ~10s, and on boot `HostUncleanShutdown` should fire.
5. **UPS test (may freeze the host):** start a self-test from the UPS front panel. If the
   host freezes, that confirms the cause, and the watchdog should recover it on its own.
   Then replace the UPS battery, or turn off the automatic self-test until you do.

## Caveats

- `watchdog-mux` has `Restart=no`. If it ever crashes without a clean close, the TCO timer
  resets the host. Clusters using PVE HA with a hardware watchdog accept the same trade-off.
- If the voltage dip freezes the FCH itself, the TCO timer may not fire. Step 5 is what
  tests that. If it fails, the next step is an automatic reset driven by the PiKVM.
