"""Connect to the iPad and feed its frames to the virtual tablet."""

import asyncio
import contextlib
import logging

from ipad_pen.protocol import Hello, ProtocolError, encode_video, read_frame
from ipad_pen.tablet import Mapper
from ipad_pen.video import stream_encoder

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


class Display:
    """Virtual monitor on the iPad: headless output, tablet mapping and video stream.

    Parameters
    ----------
    hypr : Hyprland
        Creates the output and maps the tablet.
    connect_video : coroutine function
        Returns ``(reader, writer)`` of the video connection to the iPad.
    encoder_argv : callable
        ``encoder_argv(output_name)`` returns the encoder command (FLV on stdout).
    name : str, optional
        Name of the headless output.
    """

    def __init__(self, hypr, connect_video, encoder_argv, name: str = "IPAD"):
        self.hypr = hypr
        self.connect_video = connect_video
        self.encoder_argv = encoder_argv
        self.name = name

    async def run(self, hello: Hello, mapper: Mapper) -> None:
        """Show the virtual monitor on the iPad until the video connection or the encoder ends.

        Parameters
        ----------
        hello : Hello
            Version 2 hello with the pixel size and scale.
        mapper : Mapper
            Its aspect crop is turned off while the display runs.
        """
        try:
            reader, writer = await self.connect_video()
        except Exception as exc:  # noqa: BLE001 - old app without video port, unplugged, usbmux errors
            log.info("no video connection (%s); pen only", exc)
            return
        aspect = mapper.aspect
        try:
            scale = hello.scale if hello.scale > 0 else 2.0
            pos = self.hypr.create_output(self.name, hello.width_px, hello.height_px, scale)
            log.info("virtual monitor %s %dx%d scale %g at %s", self.name, hello.width_px, hello.height_px,
                     scale, pos)
            self.hypr.set_tablet_output(self.name)
            mapper.aspect = None  # the iPad shows the whole output: use the full area

            async def send(au: bytes) -> None:
                writer.write(encode_video(au))
                await writer.drain()

            async def ipad_closed() -> None:
                while await reader.read(4096):
                    pass

            stream = asyncio.create_task(stream_encoder(self.encoder_argv(self.name), send))
            closed = asyncio.create_task(ipad_closed())
            try:
                done, _ = await asyncio.wait({stream, closed}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()  # raise encoder errors
                log.info("video connection closed")
            finally:
                for task in (stream, closed):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
        finally:
            mapper.aspect = aspect
            writer.close()
            self._teardown()

    def _teardown(self) -> None:
        for step in (lambda: self.hypr.set_tablet_output("current"), lambda: self.hypr.remove_output(self.name)):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - best effort, also during shutdown
                log.warning("cleanup failed: %s", exc)
        log.info("virtual monitor %s removed", self.name)


async def _run_display(display: Display, hello: Hello, mapper: Mapper) -> None:
    try:
        await display.run(hello, mapper)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep the pen session running
        log.error("display mode failed: %s", exc)


async def run_session(reader, mapper: Mapper, display: Display | None = None) -> None:
    """Read frames until the iPad closes the connection.

    Parameters
    ----------
    reader : asyncio.StreamReader
        Stream from the iPad.
    mapper : Mapper
        Receives every frame.
    display : Display, optional
        Started on the first version 2 hello; None means pen only.
    """
    display_task = None
    try:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                return
            if isinstance(frame, Hello):
                log.info("iPad view %.0f x %.0f pt (%d x %d px), protocol v%d", frame.width, frame.height,
                         frame.width_px, frame.height_px, frame.version)
                if display is not None and display_task is None and frame.has_display:
                    display_task = asyncio.create_task(_run_display(display, frame, mapper))
            mapper.handle(frame)
    finally:
        mapper.lift()
        if display_task is not None:
            display_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await display_task


async def serve(connect, mapper: Mapper, once: bool = False, display: Display | None = None) -> None:
    """Connect, run a session, and reconnect with backoff forever.

    Parameters
    ----------
    connect : callable
        Coroutine function returning ``(reader, writer)``.
    mapper : Mapper
        Receives the frames.
    once : bool, optional
        Return after the first session (used by tests).
    display : Display, optional
        Virtual monitor for version 2 iPads; None means pen only.
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
            await run_session(reader, mapper, display)
        except (ProtocolError, asyncio.IncompleteReadError, ConnectionError) as exc:
            log.warning("session ended: %s", exc)
        finally:
            writer.close()
        log.info("disconnected")
        if once:
            return
        await asyncio.sleep(delay)
