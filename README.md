# iPad Pen

Use an iPad with an Apple Pencil as a display tablet for a Linux laptop
(Hyprland). The connection is the USB cable only.

- Lecture mode (default): the iPad shows exactly what goes to the projector
  (or any other laptop output), and the Pencil draws on it.
- Desk mode (`--mode desk`): the iPad is an extra monitor.
- Pen only (`--no-display`): a pressure-sensitive pen tablet without a picture.

Parts:

- `ios/`: iPad app. It sends Pencil samples (position, pressure, tilt, double
  tap) at up to 240 Hz and shows the H.264 video from the laptop. Fingers and
  the palm are ignored.
- `src/ipad_pen/`: laptop daemon `ipad-pen`. It connects to the app through
  usbmux (the USB channel that iTunes and libimobiledevice use), creates a
  virtual tablet "iPad Pen" with uinput, and streams the picture to the iPad.
- `PROTOCOL.md`: the wire format.

## How the display works

When the iPad connects, the daemon:

1. picks the output to show:
   - lecture mode: `--output NAME`, or with `--output auto` (default) the
     output that has the focus at that moment (the log says which). Focus the
     projector (move the mouse to it) before you plug in the iPad or open the
     app. It is chosen again at every reconnect;
   - desk mode: it creates a Hyprland headless output (a monitor without a
     screen) named `IPAD`, 1366x1024 at 60 Hz, scale 1, right of the
     rightmost monitor (`--desk-size 2732x2048` is sharper, scale 2);
2. maps the tablet to that output (`input.tablet.output`);
3. starts `wf-recorder`, which captures the output and encodes it with the
   Intel GPU (VAAPI H.264, 20 Mbit/s constant bit rate, no B-frames, a
   keyframe every 60 pictures). In lecture mode the GPU scales the picture to
   at most 1920 px wide (`--max-width`). Every picture goes to the iPad on a
   second USB connection (port 27016).

The iPad shows the picture aspect-fit, with black bars when the aspect ratio
differs (a 16:9 projector on the 4:3 iPad). The pen position is relative to
the picture, so the pen lands where you see it. A stroke that starts in a
black bar does nothing.

Local ink: the video comes back from the laptop with a short delay. To hide
it, the iPad draws the current stroke itself as a thin grey line and fades it
out about 300 ms after the pen goes up, when the real stroke is in the video.
`--no-local-ink` turns it off.

When the iPad goes away (cable, app closed or in the background) or the
daemon stops, it stops the encoder and maps the tablet back to `current`. In
desk mode it also removes `IPAD`; windows on it then move to another monitor.

The encoder uses about 10-20 % of one CPU core.

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

2. Install wf-recorder (screen capture):

   ```bash
   sudo pacman -S wf-recorder
   ```

   Without it the daemon works as a pen tablet only. gpu-screen-recorder is
   not used: it captures through KMS (the kernel display API), which does not
   see the Hyprland headless output of desk mode.

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
"Waiting for laptop", then the picture replaces it. The daemon reconnects by
itself when the cable or the app goes away.

Lecture mode: connect the projector (mirrored or extended), put the focus on
it, then connect the iPad. Draw in the presentation app or a whiteboard app
(Saber, Xournal++) on the projector output.

Desk mode: put things on `IPAD` like on any other monitor:

- drag a window across the right edge of the rightmost monitor;
- focused window: `hyprctl dispatch 'hl.dsp.window.move({ monitor = "IPAD" })'`;
- whole workspace 5:
  `hyprctl dispatch 'hl.dsp.workspace.move({ workspace = "5", monitor = "IPAD" })'`;
- `hyprctl dispatch 'hl.dsp.focus({ monitor = "IPAD" })'`, then open the app.

(Hyprland 0.56 with the Lua config takes Lua after `dispatch`; the old
`movewindow mon:IPAD` syntax does not work.)

```
ipad-pen                              # lecture mode, focused output
ipad-pen --output HDMI-A-1            # lecture mode, this output
ipad-pen --mode desk [--desk-size 2732x2048]
ipad-pen --no-display --aspect 16:9   # pen only, mapped to a 16:9 monitor without stretching
ipad-pen --tcp HOST:PORT              # plain TCP source, for testing without an iPad
```

Pen-only mode (`--no-display`, an old app, or no wf-recorder) uses the
fallback mapping from `input.lua`. The iPad is 4:3; `--aspect 16:9` then uses
the centred 16:9 part of the iPad. Other options: `--bitrate`,
`--render-node`, `--no-local-ink`, `--desk-scale`, `--desk-name`,
`--video-port`, `--recorder` (`ipad-pen --help`). For the service, put the
options in `ExecStart` in `~/.config/systemd/user/ipad-pen.service`. Logs:
`journalctl --user -u ipad-pen -f` (`-v` adds the encoder output).

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
