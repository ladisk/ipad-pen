import asyncio
import json
import math
import os
import shutil
import struct
import subprocess

import pytest
from evdev import ecodes as e

from ipad_pen.cli import build_parser, make_display, run
from ipad_pen.hyprland import Hyprland, right_edge
from ipad_pen.protocol import (
    CONFIG_LOCAL_INK,
    GESTURE,
    GESTURE_BEGIN,
    GESTURE_END,
    GESTURE_SCROLL,
    GESTURE_ZOOM,
    HELLO_V1,
    HELLO_V2,
    KIND_AU,
    KIND_CONFIG,
    KIND_SIZE,
    MAX_VIDEO_FRAME,
    PHASE_DOWN,
    PHASE_MOVE,
    PHASE_UP,
    SAMPLE,
    SCROLL,
    VIDEO_SIZE,
    ZOOM,
    Button,
    Gesture,
    Hello,
    ProtocolError,
    Sample,
    Scroll,
    Zoom,
    decode,
    encode,
    encode_video,
    encode_video_size,
    read_frame,
    read_video_frame,
    tilt_degrees,
)
from ipad_pen.tablet import PRESSURE_MAX, XY_MAX, Mapper
from ipad_pen.video import FlvParser, fit_size, gsr_argv, nal_types, stream_encoder, wf_recorder_argv
from ipad_pen.wheel import CAPABILITIES as WHEEL_CAPS
from ipad_pen.wheel import WheelMapper


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
    future[5:7] = (4).to_bytes(2, "little")
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
    data = encode_video_size(1920, 1080) + b"".join(encode_video(a) for a in aus)
    assert data[:9] == bytes([5, 0, 0, 0, KIND_SIZE, 0x80, 0x07, 0x38, 0x04])
    assert data[9:14] == bytes([9, 0, 0, 0, KIND_AU])
    msgs = asyncio.run(read_all(data))
    assert msgs == [(KIND_SIZE, VIDEO_SIZE.pack(1920, 1080))] + [(KIND_AU, a) for a in aus]
    with pytest.raises(ProtocolError):
        asyncio.run(read_all((MAX_VIDEO_FRAME + 1).to_bytes(4, "little") + bytes([KIND_AU])))
    with pytest.raises(ProtocolError):
        asyncio.run(read_all(bytes([2, 0, 0, 0, 9, 0])))  # unknown kind
    with pytest.raises(asyncio.IncompleteReadError):
        asyncio.run(read_all(data[:-1]))


def test_wf_recorder_argv():
    argv = wf_recorder_argv("IPAD", "/dev/dri/renderD129", 20)
    assert argv[:3] == ["wf-recorder", "-o", "IPAD"]
    assert ["-d", "/dev/dri/renderD129"] == argv[5:7]
    for opt in ("bf=0", "g=60", "rc_mode=CBR", "b=20M"):
        assert opt in argv
    assert argv[-4:] == ["-m", "flv", "-f", "pipe:1"]
    assert "-F" not in argv
    scaled = wf_recorder_argv("DP-1", size=(1920, 1080))
    assert scaled[scaled.index("-F") + 1] == "scale_vaapi=w=1920:h=1080:format=nv12:out_range=full"


def test_gsr_argv():
    argv = gsr_argv("DP-6", 20, (1920, 1080))
    assert argv[:3] == ["gpu-screen-recorder", "-w", "DP-6"]
    opt = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1, 2)}
    assert (opt["-c"], opt["-k"], opt["-f"], opt["-fm"], opt["-bm"], opt["-q"]) == (
        "flv", "h264", "60", "cfr", "cbr", "20000")
    assert (opt["-keyint"], opt["-cursor"], opt["-s"]) == ("1", "no", "1920x1080")
    assert argv[-2:] == ["-o", "/dev/stdout"]
    assert "-s" not in gsr_argv("eDP-1")


@needs_ffmpeg
def test_stream_encoder_fallback():
    got = []

    async def send(au):
        got.append(au)

    async def go():
        await stream_encoder(["false"], send, fallback_argv=ffmpeg_flv_argv(10))

    # the failing encoder exits at once, the fallback runs, then it also ends and the quick-fail limit stops us
    with pytest.raises(RuntimeError):
        asyncio.run(asyncio.wait_for(go(), 20))
    assert len(got) >= 10


def test_fit_size():
    assert fit_size(3840, 2160, 1920) == (1920, 1080)
    assert fit_size(1920, 1200, 1920) == (1920, 1200)
    assert fit_size(2560, 1600, 1920) == (1920, 1200)
    assert fit_size(3440, 1440, 1920) == (1920, 804)
    assert fit_size(1366, 768, 1920) == (1366, 768)


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
        self.reload_callbacks = []
        self.outputs = [{"name": "DP-1", "width": 3840, "height": 2160, "x": 4608, "y": 0, "scale": 1.25,
                         "focused": True},
                        {"name": "HDMI-A-1", "width": 1920, "height": 1080, "x": 0, "y": 0, "scale": 1}]

    async def watch_reload(self, callback, delay=0.3):
        self.reload_callbacks.append(callback)
        await asyncio.Event().wait()

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


def run_display(extra_args: list[str], hypr) -> dict:
    """Fake iPad on two ports: pen hello v2, then collect the video messages."""
    got = {"msgs": []}

    async def scenario():
        video_done = asyncio.Event()

        async def video(reader, writer):
            try:
                while len(got["msgs"]) < 7:  # config + size + 5 access units
                    msg = await read_video_frame(reader)
                    if msg is None:
                        break
                    got["msgs"].append(msg)
                got["tablet"] = [c for c in hypr.calls if c[0] == "eval" and "tablet" in c[1]]
            finally:
                video_done.set()

        async def pen(reader, writer):
            writer.write(encode(HELLO2))
            writer.write(encode(Sample(PHASE_DOWN, 0.0, 0.0, 0.5, math.pi / 2, 0.0, 1.0)))
            await writer.drain()
            await asyncio.wait_for(video_done.wait(), 10)
            writer.close()

        def encoder(name, size):
            got["encoder"] = (name, size)
            return ffmpeg_flv_argv()

        vserver = await asyncio.start_server(video, "127.0.0.1", 0)
        pserver = await asyncio.start_server(pen, "127.0.0.1", 0)
        vport = vserver.sockets[0].getsockname()[1]
        pport = pserver.sockets[0].getsockname()[1]
        args = build_parser().parse_args(["--tcp", f"127.0.0.1:{pport}", "--video-port", str(vport), "--once",
                                          "--aspect", "16:9", *extra_args])
        async with vserver, pserver:
            t0 = asyncio.get_running_loop().time()
            await asyncio.wait_for(run(args, sink=FakeSink(), hypr=hypr, encoder_argv=encoder), 15)
            got["elapsed"] = asyncio.get_running_loop().time() - t0

    asyncio.run(scenario())
    msgs = got["msgs"]
    assert len(msgs) == 7 and got["elapsed"] < 10
    got["config"] = msgs.pop(0)
    assert all(kind == KIND_AU for kind, _ in msgs[1:])
    types = nal_types(msgs[1][1])
    assert types[:2] == [7, 8] and 5 in types  # SPS, PPS, IDR first
    # the encoder was stopped: pytest has no child process left
    assert subprocess.run(["pgrep", "-P", str(os.getpid())], check=False, capture_output=True).returncode == 1
    return got


@needs_ffmpeg
def test_lecture_mode_end_to_end():
    hypr = RecordingHyprland()
    got = run_display([], hypr)  # default: lecture mode, focused output
    assert got["config"] == (KIND_CONFIG, bytes([CONFIG_LOCAL_INK]))
    assert got["msgs"][0] == (KIND_SIZE, VIDEO_SIZE.pack(1920, 1080))
    assert got["encoder"] == ("DP-1", (1920, 1080))  # 4K scaled down
    assert got["tablet"] == [("eval", 'hl.config({ input = { tablet = { output = "DP-1" } } })')]
    cmds = [c for c in hypr.calls if c[0] != "monitors"]
    assert cmds[-1] == ("eval", 'hl.config({ input = { tablet = { output = "current" } } })')
    assert not any(c[0] == "output" for c in cmds)  # no headless output


@needs_ffmpeg
def test_lecture_mode_named_output():
    hypr = RecordingHyprland()
    got = run_display(["--output", "HDMI-A-1", "--no-local-ink"], hypr)
    assert got["config"] == (KIND_CONFIG, bytes([0]))
    assert got["encoder"] == ("HDMI-A-1", None)  # 1920 wide: no scaling
    assert got["msgs"][0] == (KIND_SIZE, VIDEO_SIZE.pack(1920, 1080))
    assert got["tablet"] == [("eval", 'hl.config({ input = { tablet = { output = "HDMI-A-1" } } })')]


@needs_ffmpeg
def test_desk_mode_end_to_end():
    hypr = RecordingHyprland()
    got = run_display(["--mode", "desk"], hypr)
    assert got["msgs"][0] == (KIND_SIZE, VIDEO_SIZE.pack(1366, 1024))
    assert got["encoder"] == ("IPAD", None)
    assert got["tablet"] == [("eval", 'hl.config({ input = { tablet = { output = "IPAD" } } })')]
    cmds = [c for c in hypr.calls if c[0] != "monitors"]
    assert cmds[:2] == [
        ("output", "create", "headless", "IPAD"),
        ("eval", 'hl.monitor({ output = "IPAD", mode = "1366x1024@60", position = "7680x0", scale = 1 })'),
    ]
    assert cmds[-2:] == [("eval", 'hl.config({ input = { tablet = { output = "current" } } })'),
                         ("output", "remove", "IPAD")]
    assert [m["name"] for m in hypr.outputs] == ["DP-1", "HDMI-A-1"]


def test_desk_scale_default():
    args = build_parser().parse_args(["--mode", "desk", "--desk-size", "2732x2048"])
    display = make_display(args, None, hypr=RecordingHyprland(), encoder_argv=lambda n, s: [])
    assert display.desk_size == (2732, 2048) and display.desk_scale == 2.0


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
            await asyncio.wait_for(run(args, sink=FakeSink(), hypr=hypr, encoder_argv=lambda n, s: ["false"]), 5)

    asyncio.run(scenario())
    assert hypr.calls == []


# --- two-finger gestures (protocol version 3) ---------------------------------

def test_gesture_frames_roundtrip():
    frames = (Gesture(GESTURE_SCROLL, GESTURE_BEGIN), Gesture(GESTURE_ZOOM, GESTURE_END),
              Scroll(-3.5, 12.25), Zoom(1.5), Hello(3, 1366.0, 1024.0, 2732, 2048, 2.0))
    for f in frames:
        assert decode(encode(f)) == f
    assert (GESTURE.size, SCROLL.size, ZOOM.size) == (3, 9, 5)
    assert encode(Scroll(1.0, 2.0)) == bytes([5]) + struct.pack("<ff", 1.0, 2.0)
    assert Hello(3, 1.0, 1.0, 2, 2, 2.0).has_display


def test_gesture_frames_stream():
    data = b"".join(encode(f) for f in (Hello(3, 1366.0, 1024.0, 2732, 2048, 2.0), Scroll(1, 2), Zoom(1.1)))

    async def read_all():
        r = _reader(data)
        return [await read_frame(r) for _ in range(4)]

    got = asyncio.run(read_all())
    assert got[1:3] == [Scroll(1, 2), Zoom(pytest.approx(1.1, rel=1e-6))] and got[3] is None


def wheel_events(sink, code):
    return [v for t, c, v in sink.events() if t == e.EV_REL and c == code]


def test_wheel_device_capabilities():
    assert {e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES} <= set(
        WHEEL_CAPS[e.EV_REL])
    assert {e.BTN_LEFT, e.KEY_LEFTCTRL} <= set(WHEEL_CAPS[e.EV_KEY])


def test_scroll_direction_and_accumulation():
    sink = FakeSink()
    w = WheelMapper(sink, scroll_gain=7.5)
    # 1 pt = 7.5 units: ten steps of 1 pt give 75 hi-res units, remainders carried
    for _ in range(10):
        w.handle(Scroll(0.0, 1.0))
    assert sum(wheel_events(sink, e.REL_WHEEL_HI_RES)) == 75  # fingers down = wheel up
    assert wheel_events(sink, e.REL_WHEEL) == []  # no full detent yet
    for _ in range(6):
        w.handle(Scroll(0.0, 1.0))  # 120 units in total
    assert sum(wheel_events(sink, e.REL_WHEEL_HI_RES)) == 120
    assert wheel_events(sink, e.REL_WHEEL) == [1]
    w.handle(Scroll(2.0, 0.0))  # fingers right = view left
    assert sum(wheel_events(sink, e.REL_HWHEEL_HI_RES)) == -15
    w.handle(Scroll(0.0, -32.0))  # 240 units up/down: two detents the other way
    assert wheel_events(sink, e.REL_WHEEL) == [1, -2]
    assert sink.frames and all(len(f) <= 4 for f in sink.frames)


def test_scroll_ignores_garbage_and_inverts():
    sink = FakeSink()
    w = WheelMapper(sink, scroll_gain=-10.0)
    w.handle(Scroll(math.nan, 1.0))
    assert sink.frames == []
    w.handle(Scroll(0.0, 3.0))
    assert wheel_events(sink, e.REL_WHEEL_HI_RES) == [-30]


def keys(sink):
    return [(c, v) for t, c, v in sink.events() if t == e.EV_KEY]


def test_pinch_holds_ctrl_and_releases():
    sink = FakeSink()
    w = WheelMapper(sink, pinch_gain=1000.0)
    w.handle(Gesture(GESTURE_ZOOM, GESTURE_BEGIN))
    assert keys(sink) == [(e.KEY_LEFTCTRL, 1)] and w.ctrl_down
    w.handle(Zoom(math.exp(0.1)))
    w.handle(Zoom(math.exp(-0.05)))
    w.handle(Scroll(0.0, 50.0))  # drift during the pinch: ignored
    assert wheel_events(sink, e.REL_WHEEL_HI_RES) == [100, -49]  # remainder carried
    assert wheel_events(sink, e.REL_HWHEEL_HI_RES) == []
    w.handle(Gesture(GESTURE_ZOOM, GESTURE_END))
    assert keys(sink) == [(e.KEY_LEFTCTRL, 1), (e.KEY_LEFTCTRL, 0)] and not w.ctrl_down
    w.release()  # idempotent
    assert len(keys(sink)) == 2
    w.handle(Scroll(0.0, 4.0))  # scrolling works again
    assert wheel_events(sink, e.REL_WHEEL_HI_RES)[-1] == 32


def test_zoom_without_begin_presses_ctrl_and_release_works():
    sink = FakeSink()
    w = WheelMapper(sink)
    w.handle(Zoom(2.0))
    assert w.ctrl_down
    w.release()
    assert keys(sink) == [(e.KEY_LEFTCTRL, 1), (e.KEY_LEFTCTRL, 0)]


def test_ctrl_released_on_error():
    class BrokenSink(FakeSink):
        def emit(self, events):
            if any(t == e.EV_REL for t, _, _ in events):
                raise OSError("device gone")
            super().emit(events)

    sink = BrokenSink()
    w = WheelMapper(sink, pinch_gain=1000.0)
    w.handle(Gesture(GESTURE_ZOOM, GESTURE_BEGIN))
    with pytest.raises(OSError):
        w.handle(Zoom(2.0))
    assert not w.ctrl_down and keys(sink) == [(e.KEY_LEFTCTRL, 1), (e.KEY_LEFTCTRL, 0)]


def test_ctrl_released_on_disconnect():
    """The iPad drops the connection in the middle of a pinch."""
    wheel_sink = FakeSink()

    async def scenario():
        async def ipad(reader, writer):
            writer.write(encode(Hello(3, 1366.0, 1024.0)))
            writer.write(encode(Gesture(GESTURE_ZOOM, GESTURE_BEGIN)))
            writer.write(encode(Zoom(1.2)))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(ipad, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        args = build_parser().parse_args(["--tcp", f"127.0.0.1:{port}", "--once"])
        async with server:
            await asyncio.wait_for(run(args, sink=FakeSink(), hypr=RecordingHyprland(), wheel_sink=wheel_sink), 5)

    asyncio.run(scenario())
    assert keys(wheel_sink) == [(e.KEY_LEFTCTRL, 1), (e.KEY_LEFTCTRL, 0)]
    assert wheel_events(wheel_sink, e.REL_WHEEL_HI_RES) == [round(math.log(1.2) * 1000)]


# --- Hyprland config reload ---------------------------------------------------

def test_watch_reload(tmp_path):
    sock = str(tmp_path / ".socket2.sock")
    called = []

    async def scenario():
        async def events(reader, writer):
            writer.write(b"workspace>>1\nconfigreloaded>>\nfocusedmon>>DP-1,1\nconfigreloaded>>\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(events, sock)
        async with server:
            await asyncio.wait_for(Hyprland(event_socket=sock).watch_reload(lambda: called.append(1), delay=0), 5)

    asyncio.run(scenario())
    assert called == [1, 1]


def test_watch_reload_missing_socket(tmp_path, caplog):
    h = Hyprland(event_socket=str(tmp_path / "none.sock"))
    asyncio.run(asyncio.wait_for(h.watch_reload(lambda: 1 / 0), 5))  # returns, no exception
    assert "cannot open the Hyprland event socket" in caplog.text


def test_display_reapplies_mapping_on_reload():
    from ipad_pen.daemon import Display

    hypr = RecordingHyprland()
    display = Display(hypr, None, None)

    async def scenario():
        task = asyncio.create_task(display._keep_mapping("DP-1"))
        await asyncio.sleep(0)
        hypr.reload_callbacks[0]()
        task.cancel()

    asyncio.run(scenario())
    assert hypr.calls == [("eval", 'hl.config({ input = { tablet = { output = "DP-1" } } })')]
