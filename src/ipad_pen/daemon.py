"""Connect to the iPad and feed its frames to the virtual tablet."""

import asyncio
import contextlib
import logging

from ipad_pen.protocol import (
    Gesture,
    Hello,
    ProtocolError,
    Scroll,
    Zoom,
    encode_video,
    encode_video_config,
    encode_video_size,
    read_frame,
)
from ipad_pen.tablet import Mapper
from ipad_pen.video import fit_size, stream_encoder

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
    """Show a laptop output on the iPad: video stream plus tablet mapping.

    Lecture mode mirrors a real output (the projector); desk mode creates a
    headless output (a virtual monitor) for the iPad.

    Parameters
    ----------
    hypr : Hyprland
        Queries outputs, creates the headless one, maps the tablet.
    connect_video : coroutine function
        Returns ``(reader, writer)`` of the video connection to the iPad.
    encoder_argv : callable
        ``encoder_argv(output_name, size)`` returns the encoder command (FLV on
        stdout); ``size`` is the picture size to scale to, or None.
    mode : str, optional
        ``"lecture"`` or ``"desk"``.
    output : str, optional
        Lecture mode: output to mirror, ``"auto"`` = the focused output when the
        iPad connects.
    max_width : int, optional
        Lecture mode: scale the picture down to at most this width.
    desk_name : str, optional
        Desk mode: name of the headless output.
    desk_size : (int, int), optional
        Desk mode: size of the headless output in pixels.
    desk_scale : float, optional
        Desk mode: Hyprland scale of the headless output.
    local_ink : bool, optional
        Tell the iPad to draw the current stroke itself until the video shows it.
    """

    def __init__(self, hypr, connect_video, encoder_argv, mode: str = "lecture", output: str = "auto",
                 max_width: int = 1920, desk_name: str = "IPAD", desk_size: tuple[int, int] = (1366, 1024),
                 desk_scale: float = 1.0, local_ink: bool = True):
        if mode not in ("lecture", "desk"):
            raise ValueError(f"unknown mode {mode!r}")
        self.hypr = hypr
        self.connect_video = connect_video
        self.encoder_argv = encoder_argv
        self.mode = mode
        self.output = output
        self.max_width = max_width
        self.desk_name = desk_name
        self.desk_size = desk_size
        self.desk_scale = desk_scale
        self.local_ink = local_ink

    def _prepare(self) -> tuple[str, tuple[int, int], tuple[int, int] | None]:
        """Pick or create the output. Returns (name, picture size, encoder scale size or None)."""
        if self.mode == "desk":
            w, h = self.desk_size
            pos = self.hypr.create_output(self.desk_name, w, h, self.desk_scale)
            log.info("desk mode: virtual monitor %s %dx%d scale %g at %s", self.desk_name, w, h,
                     self.desk_scale, pos)
            return self.desk_name, (w, h), None
        name = self.output
        if name == "auto":
            name = self.hypr.focused_output()
        native = self.hypr.output_size(name)
        size = fit_size(*native, self.max_width)
        log.info("lecture mode: mirroring %s (%dx%d, sent as %dx%d)%s", name, *native, *size,
                 " [focused output]" if self.output == "auto" else "")
        return name, size, (size if size != native else None)

    async def run(self, hello: Hello, mapper: Mapper) -> None:
        """Show the output on the iPad until the video connection or the encoder ends.

        Parameters
        ----------
        hello : Hello
            Version 2 hello.
        mapper : Mapper
            Its aspect crop is turned off while the display runs (the iPad
            sends pen positions relative to the picture).
        """
        try:
            reader, writer = await self.connect_video()
        except Exception as exc:  # noqa: BLE001 - old app without video port, unplugged, usbmux errors
            log.info("no video connection (%s); pen only", exc)
            return
        aspect = mapper.aspect
        watcher = None
        try:
            name, size, scale_to = self._prepare()
            self.hypr.set_tablet_output(name)
            watcher = asyncio.create_task(self._keep_mapping(name))
            mapper.aspect = None
            writer.write(encode_video_config(self.local_ink))
            writer.write(encode_video_size(*size))

            async def send(au: bytes) -> None:
                writer.write(encode_video(au))
                await writer.drain()

            async def ipad_closed() -> None:
                while await reader.read(4096):
                    pass

            stream = asyncio.create_task(stream_encoder(self.encoder_argv(name, scale_to), send))
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
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
            self._teardown()

    async def _keep_mapping(self, name: str) -> None:
        """Map the tablet to ``name`` again after every Hyprland config reload.

        A reload resets ``input.tablet.output`` to the value in the user's config.
        """

        def reapply() -> None:
            try:
                self.hypr.set_tablet_output(name)
                log.info("Hyprland reloaded its config; tablet mapped to %s again", name)
            except Exception as exc:  # noqa: BLE001 - keep watching
                log.warning("could not map the tablet again: %s", exc)

        await self.hypr.watch_reload(reapply)

    def _teardown(self) -> None:
        steps = [lambda: self.hypr.set_tablet_output("current")]
        if self.mode == "desk":
            steps.append(lambda: self.hypr.remove_output(self.desk_name))
        for step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - best effort, also during shutdown
                log.warning("cleanup failed: %s", exc)
        log.info("display stopped, tablet mapped to the focused monitor again")


async def _run_display(display: Display, hello: Hello, mapper: Mapper) -> None:
    try:
        await display.run(hello, mapper)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep the pen session running
        log.error("display mode failed: %s", exc)


async def run_session(reader, mapper: Mapper, display: Display | None = None, wheel=None) -> None:
    """Read frames until the iPad closes the connection.

    Parameters
    ----------
    reader : asyncio.StreamReader
        Stream from the iPad.
    mapper : Mapper
        Receives every frame.
    display : Display, optional
        Started on the first version 2 hello; None means pen only.
    wheel : WheelMapper, optional
        Receives the two-finger gesture frames; None ignores them.
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
            if isinstance(frame, (Gesture, Scroll, Zoom)):
                if wheel is not None:
                    wheel.handle(frame)
            else:
                mapper.handle(frame)
    finally:
        mapper.lift()
        if wheel is not None:
            wheel.release()
        if display_task is not None:
            display_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await display_task


async def serve(connect, mapper: Mapper, once: bool = False, display: Display | None = None,
                wheel=None) -> None:
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
    wheel : WheelMapper, optional
        Scroll and zoom from two-finger gestures; None ignores them.
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
            await run_session(reader, mapper, display, wheel)
        except (ProtocolError, asyncio.IncompleteReadError, ConnectionError) as exc:
            log.warning("session ended: %s", exc)
        finally:
            writer.close()
        log.info("disconnected")
        if once:
            return
        await asyncio.sleep(delay)
