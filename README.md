# iPad Pen

Use an iPad with an Apple Pencil as a pressure-sensitive pen tablet (like a
Wacom without a display) for a Linux laptop. The connection is the USB cable
only. Pen only, no screen mirroring.

- `ios/`: iPad app. It sends Pencil samples (position, pressure, tilt, double
  tap) at up to 240 Hz. Fingers and the palm are ignored.
- `src/ipad_pen/`: laptop daemon `ipad-pen`. It connects to the app through
  usbmux (the USB channel that iTunes and libimobiledevice use) and creates a
  virtual tablet "iPad Pen" with uinput.
- `PROTOCOL.md`: the wire format.

## Security

The iPad app listens only on its loopback address (127.0.0.1), so it is not
reachable over Wi-Fi; only usbmux (USB) can reach it. Nothing listens on the
laptop: the laptop connects out to the iPad.

## One-time setup on the laptop

1. Give your user access to `/dev/uinput` (now root only). The commands run
   in order:

   ```bash
   cd /data/projects/ipad-pen
   sudo install -m 644 linux/60-ipad-pen-uinput.rules /etc/udev/rules.d/
   sudo udevadm control --reload
   sudo udevadm trigger /dev/uinput
   getfacl /dev/uinput
   ```

   `getfacl` must show `user:<you>:rw-`. No re-login and no `modprobe` are
   necessary (uinput is built into the Omarchy kernel; the uaccess tag is
   applied on the trigger).

2. Install the daemon and start it as a user service:

   ```bash
   cd /data/projects/ipad-pen
   uv tool install .
   install -Dm 644 linux/ipad-pen.service ~/.config/systemd/user/ipad-pen.service
   systemctl --user daemon-reload
   systemctl --user enable --now ipad-pen.service
   ```

   Without the service: `uv run ipad-pen -v`.

3. Map the tablet to one monitor in `~/.config/hypr/input.lua`. Hyprland names
   the device `ipad-pen` (`hyprctl devices` lists it under Tablets). Use the
   connector name from `hyprctl monitors`:

   ```lua
   hl.device({ name = "ipad-pen", output = "DP-1" })
   ```

   The iPad is 4:3. To draw without stretching on a 16:9 monitor, start the
   daemon with `--aspect 16:9`: it then uses the centred 16:9 part of the iPad.
   For the service, change `ExecStart` to `%h/.local/bin/ipad-pen --aspect 16:9`.

## Usage

Plug in the iPad (it must trust this laptop; `usbmuxd` starts by itself when an
iPad is plugged in), open the iPad Pen app. The status line on the iPad shows
"Waiting for laptop" and then "Connected". The daemon reconnects by itself
when the cable or the app goes away.

```
ipad-pen [--udid UDID] [--port 27015] [--aspect W:H] [-v]
ipad-pen --tcp HOST:PORT     # plain TCP source, for testing without an iPad
```

Events: `ABS_X`/`ABS_Y` (0..32767), `ABS_PRESSURE` (0..4095),
`ABS_TILT_X`/`ABS_TILT_Y` (-90..90 deg), `BTN_TOOL_PEN`, `BTN_TOUCH`,
`BTN_STYLUS` (Pencil double tap).

## iPad app build

The app can only be built on macOS, so GitHub Actions builds it. Start a build:

```bash
gh workflow run ios-release.yml -R ladisk/ipad-pen
```

The workflow generates the Xcode project with XcodeGen (`ios/project.yml`),
builds it unsigned and publishes `IPadPen_<version>_unsigned.ipa` as release
`v<version>.<run number>`. The existing sideload timer signs and installs it
with the app ID `si.ladisk.ipadpen.<TEAMID>`.

## Development

```bash
uv run pytest
uv run ruff check
```
