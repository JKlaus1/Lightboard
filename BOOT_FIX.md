# Boot Fix — SPI Touchscreen Pi (`Lights`)

Fast-boot fix for the Lightboard Pi 5 running the MHS 3.5" SPI touchscreen.
Two small systemd config files cut boot from **~93s to ~12.5s**.

## Symptom
Boot to graphical target took ~1m33s. `systemd-analyze blame` showed
`plymouth-quit-wait.service` holding ~79s. The journal showed the real cause:
boot was waiting on `/dev/dri/card0` and `/dev/dri/renderD128` (the GPU's
DRM/KMS device nodes), which never appear — so systemd sat through its default
90s device timeout before `lightdm` could start.

## Root cause
The MHS 3.5" SPI display is set up via the goodtft LCD-show driver, which:
- comments out `dtoverlay=vc4-kms-v3d` in `/boot/firmware/config.txt` (line 26),
  disabling the KMS GPU driver — so **no `/dev/dri/*` nodes are ever created**;
- adds the `mhs35` SPI framebuffer overlay (the kiosk renders on `/dev/fb1`).

Upstream `lightdm.service` (`/usr/lib/systemd/system/lightdm.service`) declares:

    Wants=dev-dri-card0.device dev-dri-renderD128.device
    After=... dev-dri-card0.device dev-dri-renderD128.device

On a normal KMS Pi those appear in milliseconds. With KMS disabled they never
appear, so lightdm pulled them into the boot, waited the full 90s device
timeout every boot, then fell back to the SPI framebuffer.

## Fix — two files

### 1. `/etc/systemd/system/lightdm.service.d/no-dri-wait.conf`  *(does the real work)*
Resets lightdm's `Wants`/`After` to drop the missing GPU device deps, keeping the
real ordering (user sessions + plymouth-quit). Once lightdm no longer waits,
`plymouth-quit-wait` stops hanging too (lightdm quits plymouth itself).

    [Unit]
    Wants=
    After=
    After=systemd-user-sessions.service plymouth-quit.service

### 2. `/etc/systemd/system.conf.d/device-timeout.conf`  *(backstop)*
Caps systemd's device wait at 10s (the 90s default is excessive). Belt-and-
suspenders: even if some future missing device is waited on, boot can't stall
more than 10s.

    [Manager]
    DefaultDeviceTimeoutSec=10s

## Apply (run on the Pi over SSH)

    sudo mkdir -p /etc/systemd/system/lightdm.service.d /etc/systemd/system.conf.d

    sudo tee /etc/systemd/system/lightdm.service.d/no-dri-wait.conf >/dev/null <<'EOF'
    [Unit]
    Wants=
    After=
    After=systemd-user-sessions.service plymouth-quit.service
    EOF

    sudo tee /etc/systemd/system.conf.d/device-timeout.conf >/dev/null <<'EOF'
    [Manager]
    DefaultDeviceTimeoutSec=10s
    EOF

    sudo reboot

> **Note:** `systemctl daemon-reload` does NOT apply the dependency reset to the
> live graph — `systemctl show lightdm.service` will still list the device deps
> until a full reboot rebuilds the dependency graph. The reboot is required; it's
> the real test.

## Verify

    systemd-analyze                   # ~12.5s total instead of ~93s
    systemd-analyze blame | head -5   # plymouth-quit-wait should be gone

After the fix, the largest remaining userspace item is
`NetworkManager-wait-online.service` (~6s), which is genuine wifi-association
time, not a hang. Optional further shave: disable it for a ~6–7s boot (services
that expect the network at boot, like the cloudflared tunnel, will start a beat
early and retry — harmless for this stack).

## IMPORTANT — when moving to a KMS display
If you ever re-enable `dtoverlay=vc4-kms-v3d` (e.g. upgrading to a DSI/MIPI
capacitive display), **delete `no-dri-wait.conf`**. With KMS on, `/dev/dri/card0`
will actually exist, and you want lightdm to wait for the real GPU again.
The `device-timeout.conf` cap can stay regardless.
