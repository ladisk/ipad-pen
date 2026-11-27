"""Command line entry point ``ipad-pen``."""

import argparse
import asyncio
import functools
import logging

from ipad_pen.daemon import open_tcp, open_usbmux, serve
from ipad_pen.protocol import DEFAULT_PORT
from ipad_pen.tablet import Mapper


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


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(prog="ipad-pen", description="iPad + Apple Pencil as a USB pen tablet.")
    p.add_argument("--udid", help="iPad UDID (default: first iPad on USB)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"app port on the iPad (default {DEFAULT_PORT})")
    p.add_argument("--tcp", metavar="HOST:PORT", help="connect to a plain TCP endpoint instead (testing)")
    p.add_argument("--aspect", type=parse_aspect, metavar="W:H",
                   help="use the centred part of the iPad with this aspect ratio, e.g. 16:9")
    p.add_argument("--once", action="store_true", help="exit after the first connection ends")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def run(args: argparse.Namespace, sink=None) -> None:
    """Run the daemon.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command line.
    sink : object, optional
        Event sink; default is a new ``UInputSink``.
    """
    own_sink = sink is None
    if own_sink:
        from ipad_pen.tablet import UInputSink

        sink = UInputSink()
    if args.tcp:
        host, port = args.tcp.rsplit(":", 1)
        connect = functools.partial(open_tcp, host, int(port))
    else:
        connect = functools.partial(open_usbmux, args.port, args.udid)
    try:
        await serve(connect, Mapper(sink, args.aspect), once=args.once)
    finally:
        if own_sink:
            sink.close()


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
