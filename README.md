# iPad Pen

Use an iPad with an Apple Pencil as a display tablet for a Linux laptop
(Hyprland): the iPad shows an extra laptop monitor full screen, and the
Pencil draws on it with pressure and tilt. The connection is the USB cable
only.

- `ios/`: iPad app. It sends Pencil samples (position, pressure, tilt, double
  tap) at up to 240 Hz and shows the H.264 video from the laptop. Fingers and
  the palm are ignored.
- `src/ipad_pen/`: laptop daemon `ipad-pen`. It connects to the app through
  usbmux (the USB channel that iTunes and libimobiledevice use), creates a
  virtual tablet "iPad Pen" with uinput, and, while the iPad is connected, a
  virtual monitor `IPAD` whose picture it streams to the iPad.
- `PROTOCOL.md`: the wire format.

## How the display works

When the iPad connects, the daemon:

1. creates a Hyprland headless output (a monitor without a screen) named
   `IPAD`, 2732x2048 at 60 Hz, scale 2, right of the rightmost monitor;
2. maps the tablet to it (`input.tablet.output = "IPAD"`);
3. starts `wf-recorder`, which captures `IPAD` and encodes it with the Intel
   GPU (VAAPI H.264, 20 Mbit/s constant bit rate, no B-frames, a keyframe
   every 60 pictures), and sends every picture to the iPad on a second USB
   connection (port 27016).

When the iPad goes away (cable, app closed or in the background) or the
daemon stops, it stops the encoder, maps the tablet back to `current` and
removes `IPAD`. Windows on `IPAD` then move to another monitor.

The encoder uses about 10-20 % of one CPU core. Hyprland redraws `IPAD` at
60 Hz also when nothing changes.

## Security

The iPad app listens only on its loopback address (127.0.0.1), so it is not
reachable over Wi-Fi; only usbmux (USB) can reach it. Nothing listens on the
laptop: the laptop connects out to the iPad.

## One-time setup on the laptop

1. Load the uinput kernel module now and at every boot, and give your user
   access to `/dev/uinput` (now root only). The commands run in order:

   ```bash
   cd /data/projects/ipad-pen
   sudo modprobe uinput
   echo uinput | sudo tee /etc/modules-load.d/uinput.conf
   sudo install -m 644 linux/60-ipad-pen-uinput.rules /etc/udev/rules.d/
   sudo udevadm control --reload
   sudo udevadm trigger --name-match=uinput
   getfacl /dev/uinput
   ```

   `getfacl` must show `user:<you>:rw-`. No re-login is necessary (the
   uaccess tag is applied on the trigger). uinput is a module on this kernel,
   not built in; `udevadm trigger /dev/uinput` does not work, use
   `--name-match=uinput`.

2. Install wf-recorder (screen capture for the virtual monitor):

   ```bash
   sudo pacman -S wf-recorder
   ```

   Without it the daemon works as a pen tablet only. gpu-screen-recorder can
   not be used: it captures through KMS (the kernel display API), which only
   sees real monitors, not a Hyprland headless output.

3. Install the daemon and start it as a user service:

   ```bash
   cd /data/projects/ipad-pen
   uv tool install .
   install -Dm 644 linux/ipad-pen.service ~/.config/systemd/user/ipad-pen.service
   systemctl --user daemon-reload
   systemctl --user enable --now ipad-pen.service
   ```

   Without the service: `uv run ipad-pen -v`.

4. Hyprland: keep a fallback mapping for pen-only mode in
   `~/.config/hypr/input.lua`:

   ```lua
   hl.config({ input = { tablet = { output = "current" } } })
   ```

   The daemon sets `input.tablet.output` at run time with `hyprctl eval`
   (Hyprland 0.56 with the Lua config: `hyprctl keyword` does not work). Do
   not map the device `ipad-pen` to a fixed monitor with `hl.device(...)`: a
   per-device `output` wins over the global one. A config reload
   (`hyprctl reload`) resets the mapping to `current`; unplug and plug in the
   iPad to set it again.

## Usage

Plug in the iPad (it must trust this laptop; `usbmuxd` starts by itself when an
iPad is plugged in), open the iPad Pen app. The status line on the iPad shows
"Waiting for laptop", then the laptop monitor `IPAD` replaces it. The daemon
reconnects by itself when the cable or the app goes away.

Put things on the iPad like on any other monitor:

- drag a window across the right edge of the rightmost monitor;
- focused window: `hyprctl dispatch 'hl.dsp.window.move({ monitor = "IPAD" })'`;
- whole workspace 5:
  `hyprctl dispatch 'hl.dsp.workspace.move({ workspace = "5", monitor = "IPAD" })'`;
- `hyprctl dispatch 'hl.dsp.focus({ monitor = "IPAD" })'`, then open the app.

(Hyprland 0.56 with the Lua config takes Lua after `dispatch`; the old
`movewindow mon:IPAD` syntax does not work.)

The Pencil always draws on `IPAD` while it exists, with the full iPad area
(no stretching: both are 4:3).

```
ipad-pen [--udid UDID] [--no-display] [--bitrate 20] [--render-node /dev/dri/renderD129] [-v]
ipad-pen --no-display --aspect 16:9   # pen only, mapped to a 16:9 monitor without stretching
ipad-pen --tcp HOST:PORT              # plain TCP source, for testing without an iPad
```

Pen-only mode (`--no-display`, an old app, or no wf-recorder) uses the
fallback mapping from `input.lua`. The iPad is 4:3; `--aspect 16:9` then uses
the centred 16:9 part of the iPad. Other options: `--output-name`,
`--video-port`, `--recorder` (`ipad-pen --help`). Logs: `journalctl --user -u
ipad-pen -f` (`-v` adds the encoder output).

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
