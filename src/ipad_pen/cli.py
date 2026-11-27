"""Command line entry point ``ipad-pen``."""

import argparse
import asyncio
import functools
import logging
import shutil
import signal

from ipad_pen.daemon import Display, open_tcp, open_usbmux, serve
from ipad_pen.protocol import DEFAULT_PORT, VIDEO_PORT
from ipad_pen.tablet import Mapper
from ipad_pen.video import find_render_node, wf_recorder_argv
from ipad_pen.wheel import PINCH_GAIN, SCROLL_GAIN, WheelMapper

log = logging.getLogger("ipad_pen")


def parse_aspect(text: str) -> float:
    """Parse ``W:H`` (for example ``16:9``) to a ratio.

    Parameters
    ----------
    text : str
        Aspect ratio as ``W:H`` or a plain number.

    Returns
    -------
    float
        Width divided by height.
    """
    if ":" in text:
        w, h = text.split(":", 1)
        return float(w) / float(h)
    return float(text)


def parse_size(text: str) -> tuple[int, int]:
    """Parse ``WxH`` (for example ``2732x2048``).

    Parameters
    ----------
    text : str
        Size as ``WxH``.

    Returns
    -------
    (int, int)
        Width and height.
    """
    w, h = text.lower().split("x", 1)
    return int(w), int(h)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(prog="ipad-pen", description="iPad + Apple Pencil as a USB pen tablet.")
    p.add_argument("--udid", help="iPad UDID (default: first iPad on USB)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"app port on the iPad (default {DEFAULT_PORT})")
    p.add_argument("--tcp", metavar="HOST:PORT", help="connect to a plain TCP endpoint instead (testing)")
    p.add_argument("--aspect", type=parse_aspect, metavar="W:H",
                   help="pen-only mode: use the centred part of the iPad with this aspect ratio, e.g. 16:9")
    p.add_argument("--mode", choices=("lecture", "desk"), default="lecture",
                   help="lecture: mirror a real output (default); desk: the iPad is an extra virtual monitor")
    p.add_argument("--output", default="auto",
                   help="lecture mode: output to mirror (default auto = the focused output when the iPad connects)")
    p.add_argument("--max-width", type=int, default=1920, help="lecture mode: scale the picture to at most this width")
    p.add_argument("--desk-size", type=parse_size, default=(1366, 1024), metavar="WxH",
                   help="desk mode: virtual monitor size (default 1366x1024; 2732x2048 is sharp)")
    p.add_argument("--desk-scale", type=float,
                   help="desk mode: monitor scale (default: width / 1366, so the layout stays 1366x1024)")
    p.add_argument("--desk-name", default="IPAD", help="desk mode: name of the virtual monitor (default IPAD)")
    p.add_argument("--scroll-gain", type=float, default=SCROLL_GAIN,
                   help=f"two-finger scroll: wheel units (120 = one detent) per iPad point (default {SCROLL_GAIN:g}; "
                        "negative inverts)")
    p.add_argument("--pinch-gain", type=float, default=PINCH_GAIN,
                   help=f"pinch zoom: wheel units per unit of ln(scale) (default {PINCH_GAIN:g}; negative inverts)")
    p.add_argument("--no-gestures", action="store_true", help="ignore two-finger scroll and pinch")
    p.add_argument("--no-display", action="store_true", help="pen only: no video on the iPad")
    p.add_argument("--no-local-ink", action="store_true",
                   help="the iPad does not draw the current stroke itself (only the video shows it)")
    p.add_argument("--video-port", type=int, default=VIDEO_PORT, help=f"video port on the iPad (default {VIDEO_PORT})")
    p.add_argument("--bitrate", type=float, default=20.0, help="video bit rate in Mbit/s (default 20)")
    p.add_argument("--render-node", help="VAAPI device (default: the Intel GPU)")
    p.add_argument("--recorder", default="wf-recorder", help="wf-recorder executable")
    p.add_argument("--once", action="store_true", help="exit after the first connection ends")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def make_display(args: argparse.Namespace, connect_video, hypr=None, encoder_argv=None) -> Display | None:
    """Build the display mode, or None for pen only.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command line.
    connect_video : coroutine function
        Opens the video connection.
    hypr : Hyprland, optional
        Default is a real ``Hyprland``.
    encoder_argv : callable, optional
        ``encoder_argv(output_name, size)``; default runs wf-recorder with VAAPI.
    """
    if args.no_display:
        return None
    if encoder_argv is None:
        if shutil.which(args.recorder) is None:
            log.error("%s not found; pen only (install it: sudo pacman -S wf-recorder)", args.recorder)
            return None
        node = args.render_node or find_render_node()

        def encoder_argv(output: str, size: tuple[int, int] | None) -> list[str]:
            return wf_recorder_argv(output, render_node=node, bitrate_mbit=args.bitrate, program=args.recorder,
                                    size=size)
    if hypr is None:
        from ipad_pen.hyprland import Hyprland

        hypr = Hyprland()
    desk_scale = args.desk_scale or max(1.0, round(args.desk_size[0] / 1366 * 4) / 4)
    return Display(hypr, connect_video, encoder_argv, mode=args.mode, output=args.output,
                   max_width=args.max_width, desk_name=args.desk_name, desk_size=args.desk_size,
                   desk_scale=desk_scale, local_ink=not args.no_local_ink)


async def run(args: argparse.Namespace, sink=None, hypr=None, encoder_argv=None, wheel_sink=None) -> None:
    """Run the daemon.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command line.
    sink : object, optional
        Event sink; default is a new ``UInputSink``.
    hypr : Hyprland, optional
        For tests; default is a real ``Hyprland``.
    encoder_argv : callable, optional
        For tests; default runs wf-recorder.
    wheel_sink : object, optional
        For tests; default is a new ``WheelSink`` (only when ``sink`` is also
        the default), so a test with a fake tablet never creates a real device.
    """
    # SIGTERM (systemctl stop) cancels the main task, so the virtual monitor is removed.
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
    own_sink = sink is None
    own_wheel = False
    if own_sink:
        from ipad_pen.tablet import UInputSink

        sink = UInputSink()
        if wheel_sink is None and not args.no_gestures:
            from ipad_pen.wheel import WheelSink

            try:
                wheel_sink = WheelSink()
                own_wheel = True
            except Exception as exc:  # noqa: BLE001 - the pen works without it
                log.error("cannot create the scroll device (%s); two-finger gestures are off", exc)
    wheel = None
    if wheel_sink is not None and not args.no_gestures:
        wheel = WheelMapper(wheel_sink, args.scroll_gain, args.pinch_gain)
    if args.tcp:
        host, port = args.tcp.rsplit(":", 1)
        connect = functools.partial(open_tcp, host, int(port))
        connect_video = functools.partial(open_tcp, host, args.video_port)
    else:
        connect = functools.partial(open_usbmux, args.port, args.udid)
        connect_video = functools.partial(open_usbmux, args.video_port, args.udid)
    display = make_display(args, connect_video, hypr, encoder_argv)
    try:
        await serve(connect, Mapper(sink, args.aspect), once=args.once, display=display, wheel=wheel)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        if wheel is not None:
            wheel.release()
        if own_wheel:
            wheel_sink.close()
        if own_sink:
            sink.close()


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
