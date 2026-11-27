import asyncio
import math

import pytest
from evdev import ecodes as e

from ipad_pen.cli import build_parser, run
from ipad_pen.protocol import (
    PHASE_DOWN,
    PHASE_MOVE,
    PHASE_UP,
    SAMPLE,
    Button,
    Hello,
    ProtocolError,
    Sample,
    decode,
    encode,
    tilt_degrees,
)
from ipad_pen.tablet import PRESSURE_MAX, XY_MAX, Mapper


class FakeSink:
    def __init__(self):
        self.frames = []

    def emit(self, events):
        self.frames.append(list(events))

    def events(self):
        return [ev for f in self.frames for ev in f]


def test_roundtrip():
    for f in (Hello(1, 1366.0, 1024.0), Sample(PHASE_MOVE, 0.25, 0.75, 0.5, 1.0, -2.0, 123.456), Button(0, 9.5)):
        assert decode(encode(f)) == f


def test_sample_layout():
    data = encode(Sample(PHASE_DOWN, 0.5, 0.5, 1.0, 0.5, 0.0, 1.0))
    assert len(data) == SAMPLE.size == 30
    assert data[0] == 2 and data[1] == PHASE_DOWN


def test_bad_frames():
    with pytest.raises(ProtocolError):
        decode(bytes([99]))
    bad = bytearray(encode(Hello(1, 1.0, 1.0)))
    bad[1:5] = b"XXXX"
    with pytest.raises(ProtocolError):
        decode(bytes(bad))
    with pytest.raises(ProtocolError):
        decode(encode(Hello(2, 1.0, 1.0)))


def test_tilt():
    assert tilt_degrees(math.pi / 2, 0.0) == pytest.approx((0.0, 0.0), abs=1e-9)
    # 45 deg altitude, cap towards +x -> 45 deg tilt in x
    assert tilt_degrees(math.pi / 4, 0.0) == pytest.approx((45.0, 0.0))
    assert tilt_degrees(math.pi / 4, math.pi / 2) == pytest.approx((0.0, 45.0), abs=1e-9)
    assert tilt_degrees(0.0, math.pi) == pytest.approx((-90.0, 0.0), abs=1e-3)


def test_aspect_crop():
    sink = FakeSink()
    m = Mapper(sink, aspect=16 / 9)
    m.handle(Hello(1, 1366.0, 1024.0))  # 4:3, taller than 16:9 -> crop y
    frac = (1366 / (16 / 9)) / 1024
    y_top = (1 - frac) / 2
    assert m._scale(0.0, y_top) == (0, 0)
    assert m._scale(1.0, 1 - y_top) == (XY_MAX, XY_MAX)
    assert m._scale(0.5, 0.0) == (XY_MAX // 2 + 1, 0)


def test_button_out_of_prox():
    sink = FakeSink()
    Mapper(sink).handle(Button(0, 1.0))
    keys = [(c, v) for t, c, v in sink.events() if t == e.EV_KEY]
    assert keys == [(e.BTN_TOOL_PEN, 1), (e.BTN_STYLUS, 1), (e.BTN_STYLUS, 0), (e.BTN_TOOL_PEN, 0)]


def test_end_to_end_tcp():
    stroke = [
        Sample(PHASE_DOWN, 0.0, 0.0, 0.2, math.pi / 2, 0.0, 1.0),
        Sample(PHASE_MOVE, 0.5, 0.5, 1.0, math.pi / 4, 0.0, 1.004),
        Sample(PHASE_UP, 0.5, 0.5, 0.0, math.pi / 4, 0.0, 1.008),
    ]

    async def scenario():
        async def ipad(reader, writer):
            writer.write(encode(Hello(1, 1366.0, 1024.0)))
            for s in stroke:
                writer.write(encode(s))
            writer.write(encode(Button(0, 2.0)))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(ipad, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sink = FakeSink()
        args = build_parser().parse_args(["--tcp", f"127.0.0.1:{port}", "--once"])
        async with server:
            await asyncio.wait_for(run(args, sink=sink), 5)
        return sink

    sink = asyncio.run(scenario())
    f = sink.frames
    assert (e.EV_KEY, e.BTN_TOOL_PEN, 1) in f[0] and (e.EV_KEY, e.BTN_TOUCH, 1) in f[0]
    assert (e.EV_ABS, e.ABS_X, 0) in f[0]
    assert (e.EV_ABS, e.ABS_PRESSURE, round(0.2 * PRESSURE_MAX)) in f[0]
    assert (e.EV_ABS, e.ABS_X, round(0.5 * XY_MAX)) in f[1]
    assert (e.EV_ABS, e.ABS_PRESSURE, PRESSURE_MAX) in f[1]
    assert (e.EV_ABS, e.ABS_TILT_X, 45) in f[1]
    assert (e.EV_KEY, e.BTN_TOUCH, 0) in f[2] and (e.EV_KEY, e.BTN_TOOL_PEN, 0) in f[2]
    keys = [(c, v) for t, c, v in sink.events() if t == e.EV_KEY]
    assert keys[-3:] == [(e.BTN_STYLUS, 1), (e.BTN_STYLUS, 0), (e.BTN_TOOL_PEN, 0)]
