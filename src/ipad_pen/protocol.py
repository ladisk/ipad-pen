"""Binary frames sent by the iPad app. See PROTOCOL.md."""

import asyncio
import math
import struct
from dataclasses import dataclass

MAGIC = b"IPEN"
VERSION = 2
VERSIONS = (1, 2)
DEFAULT_PORT = 27015
VIDEO_PORT = 27016

TYPE_HELLO = 1
TYPE_SAMPLE = 2
TYPE_BUTTON = 3

PHASE_DOWN = 0
PHASE_MOVE = 1
PHASE_UP = 2
PHASE_CANCEL = 3

# Every frame starts with the type byte; the struct includes it.
HELLO_HEAD = struct.Struct("<B4sH")  # type, magic, version
HELLO_V1 = struct.Struct("<B4sHff")  # + width_pt, height_pt
HELLO_V2 = struct.Struct("<B4sHffHHf")  # + width_px, height_px, scale
HELLO_SIZES = {1: HELLO_V1.size, 2: HELLO_V2.size}
SAMPLE = struct.Struct("<BBfffffd")  # type, phase, x, y, pressure, altitude, azimuth, t
BUTTON = struct.Struct("<BBd")  # type, button, t

FRAME_SIZES = {TYPE_SAMPLE: SAMPLE.size, TYPE_BUTTON: BUTTON.size}

# Video connection: u32 length + one H.264 access unit (Annex-B).
VIDEO_HEADER = struct.Struct("<I")
MAX_VIDEO_FRAME = 16 << 20


class ProtocolError(Exception):
    """Raised on an unknown frame type, wrong magic or unsupported version."""


@dataclass(frozen=True)
class Hello:
    """Connection info sent once by the iPad.

    Attributes
    ----------
    version : int
        Protocol version.
    width, height : float
        Size of the drawing view in points.
    width_px, height_px : int
        Size of the view in display pixels (version 2; 0 in version 1).
    scale : float
        Display pixels per point (version 2; 0 in version 1).
    """

    version: int
    width: float
    height: float
    width_px: int = 0
    height_px: int = 0
    scale: float = 0.0

    @property
    def has_display(self) -> bool:
        """True if the iPad can show the video stream (version 2 with a pixel size)."""
        return self.version >= 2 and self.width_px > 0 and self.height_px > 0


@dataclass(frozen=True)
class Sample:
    """One Pencil sample.

    Attributes
    ----------
    phase : int
        One of ``PHASE_DOWN``, ``PHASE_MOVE``, ``PHASE_UP``, ``PHASE_CANCEL``.
    x, y : float
        Position as a fraction of the view width and height (0..1), y down.
    pressure : float
        Normalised force, 0..1.
    altitude : float
        Angle between the Pencil and the screen in radians (pi/2 = upright).
    azimuth : float
        Direction of the Pencil cap in the screen plane in radians, measured
        from the +x axis towards +y (y down).
    timestamp : float
        iPad uptime in seconds.
    """

    phase: int
    x: float
    y: float
    pressure: float
    altitude: float
    azimuth: float
    timestamp: float


@dataclass(frozen=True)
class Button:
    """A momentary button press (Pencil double tap).

    Attributes
    ----------
    button : int
        Button number, 0 = double tap.
    timestamp : float
        iPad uptime in seconds.
    """

    button: int
    timestamp: float


Frame = Hello | Sample | Button


def encode(frame: Frame) -> bytes:
    """Encode a frame to bytes.

    Parameters
    ----------
    frame : Hello, Sample or Button
        Frame to encode.

    Returns
    -------
    bytes
        The wire representation.
    """
    if isinstance(frame, Hello):
        if frame.version == 1:
            return HELLO_V1.pack(TYPE_HELLO, MAGIC, 1, frame.width, frame.height)
        return HELLO_V2.pack(TYPE_HELLO, MAGIC, frame.version, frame.width, frame.height,
                             frame.width_px, frame.height_px, frame.scale)
    if isinstance(frame, Sample):
        return SAMPLE.pack(
            TYPE_SAMPLE, frame.phase, frame.x, frame.y, frame.pressure,
            frame.altitude, frame.azimuth, frame.timestamp,
        )
    if isinstance(frame, Button):
        return BUTTON.pack(TYPE_BUTTON, frame.button, frame.timestamp)
    raise TypeError(f"not a frame: {frame!r}")


def decode(data: bytes) -> Frame:
    """Decode one complete frame.

    Parameters
    ----------
    data : bytes
        Exactly one frame, starting with the type byte.

    Returns
    -------
    Hello, Sample or Button
        The decoded frame.

    Raises
    ------
    ProtocolError
        If the type, magic or version is not supported.
    """
    kind = data[0]
    if kind == TYPE_HELLO:
        _, magic, version = HELLO_HEAD.unpack_from(data)
        if magic != MAGIC:
            raise ProtocolError(f"bad magic {magic!r}")
        if version not in VERSIONS:
            raise ProtocolError(f"unsupported protocol version {version}")
        if version == 1:
            return Hello(1, *HELLO_V1.unpack(data)[3:])
        return Hello(version, *HELLO_V2.unpack(data)[3:])
    if kind == TYPE_SAMPLE:
        return Sample(*SAMPLE.unpack(data)[1:])
    if kind == TYPE_BUTTON:
        return Button(*BUTTON.unpack(data)[1:])
    raise ProtocolError(f"unknown frame type {kind}")


async def read_frame(reader) -> Frame | None:
    """Read one frame from an asyncio stream.

    Parameters
    ----------
    reader : asyncio.StreamReader
        Stream from the iPad.

    Returns
    -------
    Hello, Sample, Button or None
        The frame, or None at a clean end of stream.
    """
    head = await reader.read(1)
    if not head:
        return None
    if head[0] == TYPE_HELLO:
        # The hello size depends on the version that follows the magic.
        head += await reader.readexactly(HELLO_HEAD.size - 1)
        _, magic, version = HELLO_HEAD.unpack(head)
        if magic != MAGIC:
            raise ProtocolError(f"bad magic {magic!r}")
        if version not in HELLO_SIZES:
            raise ProtocolError(f"unsupported protocol version {version}")
        rest = await reader.readexactly(HELLO_SIZES[version] - HELLO_HEAD.size)
        return decode(head + rest)
    size = FRAME_SIZES.get(head[0])
    if size is None:
        raise ProtocolError(f"unknown frame type {head[0]}")
    rest = await reader.readexactly(size - 1)
    return decode(head + rest)


def encode_video(au: bytes) -> bytes:
    """Frame one access unit for the video connection.

    Parameters
    ----------
    au : bytes
        One H.264 access unit in Annex-B format.

    Returns
    -------
    bytes
        ``u32`` little-endian length followed by the access unit.
    """
    return VIDEO_HEADER.pack(len(au)) + au


async def read_video_frame(reader) -> bytes | None:
    """Read one framed access unit from the video connection.

    Parameters
    ----------
    reader : asyncio.StreamReader
        Video stream.

    Returns
    -------
    bytes or None
        The access unit, or None at a clean end of stream.

    Raises
    ------
    ProtocolError
        If the length is zero or larger than ``MAX_VIDEO_FRAME``.
    """
    try:
        head = await reader.readexactly(VIDEO_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise
    (size,) = VIDEO_HEADER.unpack(head)
    if size == 0 or size > MAX_VIDEO_FRAME:
        raise ProtocolError(f"bad video frame length {size}")
    return await reader.readexactly(size)


def tilt_degrees(altitude: float, azimuth: float) -> tuple[float, float]:
    """Convert Pencil altitude and azimuth to evdev tilt angles.

    Parameters
    ----------
    altitude : float
        Angle between the Pencil and the screen in radians (pi/2 = upright).
    azimuth : float
        Direction of the Pencil cap in the screen plane in radians, from +x
        towards +y (y down).

    Returns
    -------
    tilt_x, tilt_y : float
        Tilt in degrees, -90..90. Positive when the top of the pen leans
        towards +x (right) or +y (down), as in ``ABS_TILT_X``/``ABS_TILT_Y``.
    """
    c = math.cos(altitude)
    s = max(math.sin(altitude), 1e-6)  # flat pen: avoid atan2(tiny, 0)
    tx = math.degrees(math.atan2(c * math.cos(azimuth), s))
    ty = math.degrees(math.atan2(c * math.sin(azimuth), s))
    return max(-90.0, min(90.0, tx)), max(-90.0, min(90.0, ty))
