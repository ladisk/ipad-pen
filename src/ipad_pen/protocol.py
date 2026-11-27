"""Binary frames sent by the iPad app. See PROTOCOL.md."""

import math
import struct
from dataclasses import dataclass

MAGIC = b"IPEN"
VERSION = 1
DEFAULT_PORT = 27015

TYPE_HELLO = 1
TYPE_SAMPLE = 2
TYPE_BUTTON = 3

PHASE_DOWN = 0
PHASE_MOVE = 1
PHASE_UP = 2
PHASE_CANCEL = 3

# Every frame starts with the type byte; the struct includes it.
HELLO = struct.Struct("<B4sHff")  # type, magic, version, width_pt, height_pt
SAMPLE = struct.Struct("<BBfffffd")  # type, phase, x, y, pressure, altitude, azimuth, t
BUTTON = struct.Struct("<BBd")  # type, button, t

FRAME_SIZES = {TYPE_HELLO: HELLO.size, TYPE_SAMPLE: SAMPLE.size, TYPE_BUTTON: BUTTON.size}


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
    """

    version: int
    width: float
    height: float


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
        return HELLO.pack(TYPE_HELLO, MAGIC, frame.version, frame.width, frame.height)
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
        _, magic, version, w, h = HELLO.unpack(data)
        if magic != MAGIC:
            raise ProtocolError(f"bad magic {magic!r}")
        if version != VERSION:
            raise ProtocolError(f"unsupported protocol version {version}")
        return Hello(version, w, h)
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
    size = FRAME_SIZES.get(head[0])
    if size is None:
        raise ProtocolError(f"unknown frame type {head[0]}")
    rest = await reader.readexactly(size - 1)
    return decode(head + rest)


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
