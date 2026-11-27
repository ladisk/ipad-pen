"""Capture a Hyprland output, encode it to H.264 and cut it into access units.

wf-recorder (wlr-screencopy, so it also works for a headless output) or
gpu-screen-recorder (KMS, real outputs only, much lighter for Hyprland)
captures the output and encodes it with VAAPI. Both write FLV to a pipe: every FLV
tag holds exactly one encoded frame, so the daemon gets complete access units
without waiting for the next frame's start code.
"""

import asyncio
import contextlib
import logging
import time
from pathlib import Path

log = logging.getLogger("ipad_pen")

START_CODE = b"\x00\x00\x00\x01"
NAL_SPS = 7
NAL_PPS = 8
NAL_IDR = 5
FLV_HEADER = 9 + 4  # file header + first PreviousTagSize
TAG_HEADER = 11
TAG_VIDEO = 9
CODEC_AVC = 7


def find_render_node(drm: Path = Path("/sys/class/drm")) -> str | None:
    """Pick the VAAPI render node, preferring the Intel GPU.

    Parameters
    ----------
    drm : Path, optional
        sysfs DRM class directory.

    Returns
    -------
    str or None
        Device path such as ``/dev/dri/renderD129``, or None if there is none.
    """
    nodes = sorted(drm.glob("renderD*"))
    for node in nodes:
        with contextlib.suppress(OSError):
            if (node / "device" / "vendor").read_text().strip() == "0x8086":
                return f"/dev/dri/{node.name}"
    return f"/dev/dri/{nodes[0].name}" if nodes else None


def fit_size(width: int, height: int, max_width: int) -> tuple[int, int]:
    """Scale a picture down to at most ``max_width`` wide, keeping the aspect ratio.

    Parameters
    ----------
    width, height : int
        Source size in pixels.
    max_width : int
        Largest output width.

    Returns
    -------
    (int, int)
        Even width and height (H.264 with 4:2:0 needs even sizes).
    """
    if width > max_width:
        height = round(height * max_width / width)
        width = max_width
    return width - width % 2, height - height % 2


def wf_recorder_argv(output: str, render_node: str | None = None, bitrate_mbit: float = 20.0,
                     gop: int = 60, program: str = "wf-recorder",
                     size: tuple[int, int] | None = None) -> list[str]:
    """Command that captures ``output`` and writes low-latency H.264 in FLV to stdout.

    Parameters
    ----------
    output : str
        Hyprland output name.
    render_node : str, optional
        VAAPI device; default lets wf-recorder choose.
    bitrate_mbit : float, optional
        Constant bit rate in Mbit/s.
    gop : int, optional
        Frames between IDR frames.
    program : str, optional
        wf-recorder executable.
    size : (int, int), optional
        Scale the picture to this size on the GPU; default is the output's size.

    Returns
    -------
    list of str
        The argv.
    """
    rate = f"{bitrate_mbit:g}M"
    argv = [program, "-o", output, "-c", "h264_vaapi"]
    if render_node:
        argv += ["-d", render_node]
    if size:
        # Replaces wf-recorder's own filter (scale_vaapi=format=nv12:out_range=full).
        argv += ["-F", f"scale_vaapi=w={size[0]}:h={size[1]}:format=nv12:out_range=full"]
    argv += ["-b", "0"]
    for opt in ("bf=0", f"g={gop}", "rc_mode=CBR", f"b={rate}", f"maxrate={rate}", "profile=high"):
        argv += ["-p", opt]
    return argv + ["-m", "flv", "-f", "pipe:1"]


def gsr_argv(output: str, bitrate_mbit: float = 20.0, size: tuple[int, int] | None = None, fps: int = 60,
             program: str = "gpu-screen-recorder") -> list[str]:
    """Command that captures ``output`` through KMS and writes low-latency H.264 in FLV to stdout.

    gpu-screen-recorder reads the scanout buffer directly, so Hyprland does no
    per-frame copy. It sees real outputs only, not headless ones. It picks
    the GPU that drives the output. Constant frame rate keeps frames and
    keyframes (every 1 s, with SPS/PPS) coming on a still picture; B-frames
    are off in its H.264 encoder.

    Parameters
    ----------
    output : str
        Hyprland output name (``--list-capture-options`` names it).
    bitrate_mbit : float, optional
        Constant bit rate in Mbit/s.
    size : (int, int), optional
        Scale the picture down to fit this size; default is the output's size.
    fps : int, optional
        Frame rate.
    program : str, optional
        gpu-screen-recorder executable.

    Returns
    -------
    list of str
        The argv.
    """
    argv = [program, "-w", output, "-c", "flv", "-k", "h264", "-f", str(fps), "-fm", "cfr",
            "-bm", "cbr", "-q", str(round(bitrate_mbit * 1000)), "-keyint", "1", "-cursor", "no"]
    if size:
        argv += ["-s", f"{size[0]}x{size[1]}"]
    return argv + ["-o", "/dev/stdout"]


def nal_types(au: bytes) -> list[int]:
    """NAL unit types in an Annex-B access unit, in order.

    Parameters
    ----------
    au : bytes
        Annex-B data.

    Returns
    -------
    list of int
        ``nal_unit_type`` of every NAL unit.
    """
    types = []
    i = au.find(b"\x00\x00\x01")
    while i >= 0 and i + 3 < len(au):
        types.append(au[i + 3] & 0x1F)
        i = au.find(b"\x00\x00\x01", i + 3)
    return types


class FlvParser:
    """Incremental FLV demuxer that returns H.264 access units in Annex-B format.

    Keyframes without in-band SPS/PPS get the ones from the AVC sequence
    header in front, so every keyframe can start a decoder.
    """

    def __init__(self):
        self.buf = bytearray()
        self.header_done = False
        self.sps: list[bytes] = []
        self.pps: list[bytes] = []
        self.length_size = 4

    def feed(self, data: bytes) -> list[tuple[bool, bytes]]:
        """Add bytes from the pipe and return the complete access units.

        Parameters
        ----------
        data : bytes
            Next chunk of the FLV stream.

        Returns
        -------
        list of (bool, bytes)
            ``(is_keyframe, access_unit)`` for every complete video packet.
        """
        self.buf += data
        out = []
        if not self.header_done:
            if len(self.buf) < FLV_HEADER:
                return out
            if self.buf[:3] != b"FLV":
                raise ValueError("encoder output is not FLV")
            header_size = int.from_bytes(self.buf[5:9], "big")
            if len(self.buf) < header_size + 4:
                return out
            del self.buf[:header_size + 4]
            self.header_done = True
        while len(self.buf) >= TAG_HEADER:
            size = int.from_bytes(self.buf[1:4], "big")
            end = TAG_HEADER + size + 4
            if len(self.buf) < end:
                break
            kind = self.buf[0]
            body = bytes(self.buf[TAG_HEADER:TAG_HEADER + size])
            del self.buf[:end]
            if kind == TAG_VIDEO:
                au = self._video(body)
                if au is not None:
                    out.append(au)
        return out

    def _video(self, body: bytes) -> tuple[bool, bytes] | None:
        if len(body) < 5 or body[0] & 0x80 or body[0] & 0x0F != CODEC_AVC:
            return None  # not legacy-FLV H.264
        key = body[0] >> 4 == 1
        if body[1] == 0:
            self._config(body[5:])
            return None
        if body[1] != 1:
            return None
        nals = []
        i, n = 5, self.length_size
        while i + n <= len(body):
            length = int.from_bytes(body[i:i + n], "big")
            nals.append(body[i + n:i + n + length])
            i += n + length
        types = [nal[0] & 0x1F for nal in nals if nal]
        if key and NAL_SPS not in types:
            nals = self.sps + self.pps + nals
        return key, b"".join(START_CODE + nal for nal in nals if nal)

    def _config(self, rec: bytes) -> None:
        """Parse an AVCDecoderConfigurationRecord."""
        if len(rec) < 7:
            return
        self.length_size = (rec[4] & 0x03) + 1
        p = 6
        sps = []
        for _ in range(rec[5] & 0x1F):
            length = int.from_bytes(rec[p:p + 2], "big")
            sps.append(rec[p + 2:p + 2 + length])
            p += 2 + length
        pps = []
        count = rec[p] if p < len(rec) else 0
        p += 1
        for _ in range(count):
            length = int.from_bytes(rec[p:p + 2], "big")
            pps.append(rec[p + 2:p + 2 + length])
            p += 2 + length
        self.sps, self.pps = sps, pps


async def _log_stderr(stream) -> None:
    while line := await stream.readline():
        log.debug("encoder: %s", line.decode(errors="replace").rstrip())


async def _stop(proc) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 2.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def stream_encoder(argv: list[str], send, max_quick_fails: int = 3,
                         fallback_argv: list[str] | None = None) -> None:
    """Run the encoder and pass its access units to ``send``, restarting it if it dies.

    Every (re)start begins with SPS/PPS/IDR, so the iPad decoder recovers.
    The encoder is stopped when this coroutine is cancelled.

    Parameters
    ----------
    argv : list of str
        Encoder command; it must write FLV to stdout.
    send : coroutine function
        Called with every access unit (Annex-B bytes).
    max_quick_fails : int, optional
        Give up after this many exits in a row within 3 s of the start.
    fallback_argv : list of str, optional
        Another encoder command; used from the first exit within 3 s of the start.
    """
    fails = 0
    while True:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("encoder started (pid %d)", proc.pid)
        err_task = asyncio.create_task(_log_stderr(proc.stderr))
        parser = FlvParser()
        try:
            while chunk := await proc.stdout.read(1 << 16):
                for _, au in parser.feed(chunk):
                    await send(au)
            await proc.wait()
        finally:
            await _stop(proc)
            err_task.cancel()
        log.warning("encoder exited with code %s", proc.returncode)
        fails = fails + 1 if time.monotonic() - started < 3.0 else 1
        if fallback_argv and time.monotonic() - started < 3.0:
            log.warning("encoder %s failed at start; using %s instead", argv[0], fallback_argv[0])
            argv, fallback_argv, fails = fallback_argv, None, 0
            continue
        if fails >= max_quick_fails:
            raise RuntimeError("encoder keeps failing; run with -v to see its output")
        await asyncio.sleep(1.0)
