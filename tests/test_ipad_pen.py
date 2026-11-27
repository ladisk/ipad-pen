import asyncio
import json
import math
import os
import shutil
import subprocess

import pytest
from evdev import ecodes as e

from ipad_pen.cli import build_parser, run
from ipad_pen.hyprland import Hyprland, right_edge
from ipad_pen.protocol import (
    HELLO_V1,
    HELLO_V2,
    MAX_VIDEO_FRAME,
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
    encode_video,
    read_frame,
    read_video_frame,
    tilt_degrees,
)
from ipad_pen.tablet import PRESSURE_MAX, XY_MAX, Mapper
from ipad_pen.video import FlvParser, nal_types, wf_recorder_argv


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
    future = bytearray(encode(Hello(2, 1.0, 1.0)))
    future[5:7] = (3).to_bytes(2, "little")
    with pytest.raises(ProtocolError):
        decode(bytes(future))


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
            await asyncio.wait_for(run(args, sink=sink, hypr=RecordingHyprland()), 5)
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


# --- protocol version 2 and display mode -----------------------------------

HELLO2 = Hello(2, 1366.0, 1024.0, 2732, 2048, 2.0)
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def test_hello_v2():
    data = encode(HELLO2)
    assert len(data) == HELLO_V2.size == 23
    assert decode(data) == HELLO2 and HELLO2.has_display
    assert len(encode(Hello(1, 1.0, 1.0))) == HELLO_V1.size == 15
    assert not decode(encode(Hello(1, 1366.0, 1024.0))).has_display

    async def read_all():
        r = _reader(encode(Hello(1, 1366.0, 1024.0)) + data + encode(Button(0, 1.0)))
        return [await read_frame(r) for _ in range(4)]

    assert asyncio.run(read_all()) == [Hello(1, 1366.0, 1024.0), HELLO2, Button(0, 1.0), None]


def test_video_framing():
    async def read_all(data):
        r = _reader(data)
        out = []
        while (f := await read_video_frame(r)) is not None:
            out.append(f)
        return out

    aus = [b"\x00\x00\x00\x01\x67abc", b"x" * 70000]
    data = b"".join(encode_video(a) for a in aus)
    assert data[:4] == (8).to_bytes(4, "little")
    assert asyncio.run(read_all(data)) == aus
    with pytest.raises(ProtocolError):
        asyncio.run(read_all((MAX_VIDEO_FRAME + 1).to_bytes(4, "little")))
    with pytest.raises(asyncio.IncompleteReadError):
        asyncio.run(read_all(data[:-1]))


def test_wf_recorder_argv():
    argv = wf_recorder_argv("IPAD", "/dev/dri/renderD129", 20)
    assert argv[:3] == ["wf-recorder", "-o", "IPAD"]
    assert ["-d", "/dev/dri/renderD129"] == argv[5:7]
    for opt in ("bf=0", "g=60", "rc_mode=CBR", "b=20M"):
        assert opt in argv
    assert argv[-4:] == ["-m", "flv", "-f", "pipe:1"]


def ffmpeg_flv_argv(frames: int | None = None) -> list[str]:
    """Live test pattern encoder that writes FLV like wf-recorder (libx264: SPS/PPS only out of band)."""
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
            "-i", "testsrc=size=320x240:rate=30"]
    if frames:
        argv += ["-frames:v", str(frames)]
    return argv + ["-c:v", "libx264", "-bf", "0", "-g", "15", "-tune", "zerolatency", "-f", "flv", "pipe:1"]


@needs_ffmpeg
def test_flv_parser():
    argv = [a for a in ffmpeg_flv_argv(20) if a != "-re"]
    flv = subprocess.run(argv, capture_output=True, check=True).stdout
    whole = FlvParser().feed(flv)
    assert len(whole) == 20
    key, first = whole[0]
    assert key and first.startswith(b"\x00\x00\x00\x01")
    types = nal_types(first)
    assert types.index(7) < types.index(8) < types.index(5)  # SPS, PPS, IDR
    assert all(5 not in nal_types(au) for k, au in whole if not k)
    assert sum(k for k, _ in whole) == 2  # g=15
    p = FlvParser()  # byte by byte gives the same units
    pieces = [au for i in range(len(flv)) for au in p.feed(flv[i:i + 1])]
    assert pieces == whole


def test_right_edge():
    mons = [
        {"name": "eDP-1", "width": 1920, "height": 1080, "x": 0, "y": 864, "scale": 1.25, "transform": 0},
        {"name": "DP-6", "width": 3840, "height": 2160, "x": 1536, "y": 0, "scale": 1.25, "transform": 0},
        {"name": "DP-1", "width": 3840, "height": 2160, "x": 4608, "y": 0, "scale": 1.25, "transform": 0},
        {"name": "IPAD", "width": 2732, "height": 2048, "x": 7680, "y": 0, "scale": 2.0, "transform": 0},
    ]
    assert right_edge(mons, skip="IPAD") == 7680
    mons[2]["transform"] = 1  # rotated: logical width is 2160/1.25
    assert right_edge(mons, skip="IPAD") == 4608 + 1728


class RecordingHyprland(Hyprland):
    """Real Hyprland class with hyprctl replaced by a recorder."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.outputs = [{"name": "DP-1", "width": 3840, "height": 2160, "x": 4608, "y": 0, "scale": 1.25}]

    def _run(self, *args):
        self.calls.append(args)
        if args[:2] == ("monitors", "all"):
            return json.dumps(self.outputs)
        if args[:3] == ("output", "create", "headless"):
            self.outputs.append({"name": args[3], "width": 1920, "height": 1080, "x": 0, "y": 0, "scale": 1})
        if args[:2] == ("output", "remove"):
            self.outputs = [m for m in self.outputs if m["name"] != args[2]]
        return "ok"


def test_hyprland_calls():
    h = RecordingHyprland()
    assert h.create_output("IPAD", 2732, 2048, 2.0) == "7680x0"
    h.set_tablet_output("IPAD")
    h.remove_output("IPAD")
    cmds = [c for c in h.calls if c[0] != "monitors"]
    assert cmds == [
        ("output", "create", "headless", "IPAD"),
        ("eval", 'hl.monitor({ output = "IPAD", mode = "2732x2048@60", position = "7680x0", scale = 2 })'),
        ("eval", 'hl.config({ input = { tablet = { output = "IPAD" } } })'),
        ("output", "remove", "IPAD"),
    ]
    with pytest.raises(ValueError):
        h.set_tablet_output('x" }) os.execute("rm')


@needs_ffmpeg
def test_display_end_to_end():
    """Fake iPad on two ports: pen hello v2, then an H.264 stream must arrive on the video port."""
    hypr = RecordingHyprland()
    got = {}

    async def scenario():
        video_done = asyncio.Event()

        async def video(reader, writer):
            got["aus"] = []
            try:
                while len(got["aus"]) < 5:
                    au = await read_video_frame(reader)
                    if au is None:
                        break
                    got["aus"].append(au)
                got["tablet"] = [c for c in hypr.calls if c[0] == "eval" and "tablet" in c[1]]
            finally:
                video_done.set()

        async def pen(reader, writer):
            writer.write(encode(HELLO2))
            writer.write(encode(Sample(PHASE_DOWN, 0.0, 0.0, 0.5, math.pi / 2, 0.0, 1.0)))
            await writer.drain()
            await asyncio.wait_for(video_done.wait(), 10)
            writer.close()

        vserver = await asyncio.start_server(video, "127.0.0.1", 0)
        pserver = await asyncio.start_server(pen, "127.0.0.1", 0)
        vport = vserver.sockets[0].getsockname()[1]
        pport = pserver.sockets[0].getsockname()[1]
        args = build_parser().parse_args(["--tcp", f"127.0.0.1:{pport}", "--video-port", str(vport), "--once",
                                          "--aspect", "16:9"])
        async with vserver, pserver:
            t0 = asyncio.get_running_loop().time()
            await asyncio.wait_for(run(args, sink=FakeSink(), hypr=hypr,
                                       encoder_argv=lambda name: ffmpeg_flv_argv()), 15)
            got["elapsed"] = asyncio.get_running_loop().time() - t0

    asyncio.run(scenario())
    aus = got["aus"]
    assert len(aus) == 5 and got["elapsed"] < 10
    types = nal_types(aus[0])
    assert types[:2] == [7, 8] and 5 in types  # SPS, PPS, IDR first
    assert got["tablet"] == [("eval", 'hl.config({ input = { tablet = { output = "IPAD" } } })')]
    cmds = [c for c in hypr.calls if c[0] != "monitors"]
    assert cmds[0] == ("output", "create", "headless", "IPAD")
    assert cmds[-2:] == [("eval", 'hl.config({ input = { tablet = { output = "current" } } })'),
                         ("output", "remove", "IPAD")]
    assert not hypr.outputs[1:]
    # the encoder was stopped: pytest has no child process left
    assert subprocess.run(["pgrep", "-P", str(os.getpid())], check=False, capture_output=True).returncode == 1


def test_v1_hello_stays_pen_only():
    hypr = RecordingHyprland()

    async def scenario():
        async def ipad(reader, writer):
            writer.write(encode(Hello(1, 1366.0, 1024.0)))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(ipad, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        args = build_parser().parse_args(["--tcp", f"127.0.0.1:{port}", "--once"])
        async with server:
            await asyncio.wait_for(run(args, sink=FakeSink(), hypr=hypr, encoder_argv=lambda n: ["false"]), 5)

    asyncio.run(scenario())
    assert hypr.calls == []
