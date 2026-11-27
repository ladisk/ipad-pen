"""Pen colour and width of the Saber note app, for the iPad's local ink."""

import asyncio
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ipad_pen")

FILE_NAME = "saber-ink.json"
MAX_FILE_SIZE = 4096
MAX_WIDTH = 2000.0  # logical pixels; larger values are not a stroke width
COLOR_RE = re.compile(r"^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")

TOOL_DEFAULT = 0
TOOL_PEN = 1
TOOL_HIGHLIGHTER = 2
TOOL_ERASER = 3
TOOLS = {"pen": TOOL_PEN, "highlighter": TOOL_HIGHLIGHTER, "eraser": TOOL_ERASER, "other": TOOL_DEFAULT}


@dataclass(frozen=True)
class InkFile:
    """A validated ``saber-ink.json``: width is in logical pixels of Saber's monitor."""

    tool: int
    rgba: tuple[int, int, int, int]
    width: float
    pid: int


@dataclass(frozen=True)
class InkStyle:
    """Ink style as sent to the iPad: width is in video pixels."""

    tool: int = TOOL_DEFAULT
    rgba: tuple[int, int, int, int] = (0, 0, 0, 0)
    width: float = 0.0


DEFAULT_STYLE = InkStyle()


def default_path() -> Path | None:
    """Path of Saber's ink file, or None if ``XDG_RUNTIME_DIR`` is unset."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / FILE_NAME if runtime else None


def parse_ink_file(text: str | bytes) -> InkFile | None:
    """Validate the content of ``saber-ink.json``.

    Parameters
    ----------
    text : str or bytes
        File content.

    Returns
    -------
    InkFile or None
        None if the content is malformed in any way.
    """
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1 or isinstance(data.get("v"), bool):
        return None
    tool = TOOLS.get(data.get("tool")) if isinstance(data.get("tool"), str) else None
    color = data.get("color")
    m = COLOR_RE.match(color) if isinstance(color, str) else None
    width, pid = data.get("width"), data.get("pid")
    if tool is None or m is None:
        return None
    if isinstance(width, bool) or not isinstance(width, (int, float)):
        return None
    if not math.isfinite(width) or not 0 < width <= MAX_WIDTH:
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    rgb = bytes.fromhex(m.group(1))
    alpha = int(m.group(2), 16) if m.group(2) else 255
    return InkFile(tool, (rgb[0], rgb[1], rgb[2], alpha), float(width), pid)


def video_width(logical: float, monitor_scale: float, physical_width: int, sent_width: int) -> float:
    """Convert a stroke width from logical pixels to video pixels.

    Parameters
    ----------
    logical : float
        Width in logical pixels of the monitor (zoom included).
    monitor_scale : float
        Hyprland scale of the mirrored output.
    physical_width : int
        Width of the output in physical pixels.
    sent_width : int
        Width of the picture sent to the iPad.

    Returns
    -------
    float
        Width in video pixels.
    """
    return logical * monitor_scale * sent_width / physical_width


def pid_alive(pid: int) -> bool:
    """True if a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class InkTracker:
    """Poll the ink file and report changes of the effective style.

    Parameters
    ----------
    path : Path
        The ink file.
    scale : callable
        Returns ``(monitor_scale, physical_width)`` of the mirrored output;
        called when the file changes.
    sent_width : int
        Width of the picture sent to the iPad.
    alive : callable, optional
        ``alive(pid)``; default checks the process table.
    """

    def __init__(self, path: Path, scale, sent_width: int, alive=pid_alive):
        self.path = path
        self.scale = scale
        self.sent_width = sent_width
        self.alive = alive
        self.current = DEFAULT_STYLE
        self._stamp = None
        self._file: InkFile | None = None

    def poll(self) -> InkStyle | None:
        """Check the file; return the new style if it changed, else None."""
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp = stamp
            self._file = self._read() if stamp is not None else None
        style = DEFAULT_STYLE
        if self._file is not None and self.alive(self._file.pid):
            f = self._file
            style = InkStyle(f.tool, f.rgba, self._convert(f.width))
        if style == self.current:
            return None
        self.current = style
        return style

    def _read(self) -> InkFile | None:
        try:
            with self.path.open("rb") as fh:
                raw = fh.read(MAX_FILE_SIZE + 1)
        except OSError:
            return None
        if len(raw) > MAX_FILE_SIZE:
            return None
        parsed = parse_ink_file(raw)
        if parsed is None:
            log.debug("ignoring malformed %s", self.path)
        return parsed

    def _convert(self, logical: float) -> float:
        try:
            monitor_scale, physical = self.scale()
            return video_width(logical, monitor_scale, physical, self.sent_width)
        except Exception as exc:  # noqa: BLE001 - hyprctl failed; keep the grey ink
            log.warning("cannot convert the ink width: %s", exc)
            return 0.0


async def watch_ink(tracker: InkTracker, send, interval: float = 0.15) -> None:
    """Call ``send(style)`` for every change of the effective style until cancelled.

    Parameters
    ----------
    tracker : InkTracker
        Source of changes (its first poll runs in a worker thread).
    send : callable
        Called with an ``InkStyle`` in the event loop.
    interval : float, optional
        Seconds between polls.
    """
    while True:
        style = await asyncio.to_thread(tracker.poll)
        if style is not None:
            send(style)
        await asyncio.sleep(interval)
