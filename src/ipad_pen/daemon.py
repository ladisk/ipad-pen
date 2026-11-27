"""Connect to the iPad and feed its frames to the virtual tablet."""

import asyncio
import logging

from ipad_pen.protocol import Hello, ProtocolError, read_frame
from ipad_pen.tablet import Mapper

log = logging.getLogger("ipad_pen")


class NoDevice(Exception):
    """No matching iPad is attached over USB."""


async def open_usbmux(port: int, udid: str | None = None):
    """Open a TCP connection to a port on the iPad through usbmuxd.

    Parameters
    ----------
    port : int
        TCP port the iPad app listens on (device loopback).
    udid : str, optional
        Device to use; default is the first USB device.

    Returns
    -------
    reader, writer : asyncio.StreamReader, asyncio.StreamWriter
        The connection.
    """
    from pymobiledevice3.usbmux import select_device

    device = await select_device(udid, connection_type="USB")
    if device is None:
        raise NoDevice("no iPad found over USB" + (f" with UDID {udid}" if udid else ""))
    # MuxDevice.connect(port) returns a non-blocking socket already
    # forwarded to the device port (it does the htons itself).
    sock = await device.connect(port)
    return await asyncio.open_connection(sock=sock)


async def open_tcp(host: str, port: int):
    """Open a plain TCP connection (for testing without an iPad).

    Parameters
    ----------
    host : str
        Host name or address.
    port : int
        TCP port.

    Returns
    -------
    reader, writer : asyncio.StreamReader, asyncio.StreamWriter
        The connection.
    """
    return await asyncio.open_connection(host, port)


async def run_session(reader, mapper: Mapper) -> None:
    """Read frames until the iPad closes the connection.

    Parameters
    ----------
    reader : asyncio.StreamReader
        Stream from the iPad.
    mapper : Mapper
        Receives every frame.
    """
    try:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                return
            if isinstance(frame, Hello):
                log.info("iPad view %.0f x %.0f pt, protocol v%d", frame.width, frame.height, frame.version)
            mapper.handle(frame)
    finally:
        mapper.lift()


async def serve(connect, mapper: Mapper, once: bool = False) -> None:
    """Connect, run a session, and reconnect with backoff forever.

    Parameters
    ----------
    connect : callable
        Coroutine function returning ``(reader, writer)``.
    mapper : Mapper
        Receives the frames.
    once : bool, optional
        Return after the first session (used by tests).
    """
    delay = 0.5
    while True:
        try:
            reader, writer = await connect()
        except Exception as exc:  # unplugged, app not running, usbmuxd down
            log.debug("connect failed: %s", exc)
            if once:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
            continue
        log.info("connected")
        delay = 0.5
        try:
            await run_session(reader, mapper)
        except (ProtocolError, asyncio.IncompleteReadError, ConnectionError) as exc:
            log.warning("session ended: %s", exc)
        finally:
            writer.close()
        log.info("disconnected")
        if once:
            return
        await asyncio.sleep(delay)
