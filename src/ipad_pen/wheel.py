"""Turn two-finger gestures into mouse wheel events on a second virtual device."""

import logging
import math

from evdev import UInput
from evdev import ecodes as e

from ipad_pen.protocol import GESTURE_BEGIN, GESTURE_SCROLL, GESTURE_ZOOM, Gesture, Scroll, Zoom

log = logging.getLogger("ipad_pen")

DEVICE_NAME = "iPad Pen Scroll"
HI_RES = 120  # hi-res wheel units per detent
# Hi-res units per iPad point of finger movement (120 / 15 = one detent per 15 pt).
SCROLL_GAIN = 8.0
# Hi-res units per unit of ln(scale): doubling the distance of the fingers gives
# about 6 detents of Ctrl + wheel.
PINCH_GAIN = 1000.0

# libinput calls a device a pointer when it has relative axes and a button,
# and gives it wheel scrolling when it has REL_WHEEL or REL_WHEEL_HI_RES.
CAPABILITIES = {
    e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.KEY_LEFTCTRL],
    e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES],
}


class WheelSink:
    """Virtual mouse (wheel only) backed by ``/dev/uinput``."""

    def __init__(self):
        self.ui = UInput(CAPABILITIES, name=DEVICE_NAME, vendor=0x05AC, product=0x2, version=1,
                         bustype=e.BUS_USB)

    def emit(self, events: list[tuple[int, int, int]]) -> None:
        """Write events followed by one SYN_REPORT."""
        for ev_type, code, value in events:
            self.ui.write(ev_type, code, value)
        self.ui.syn()

    def close(self) -> None:
        """Remove the virtual device (the kernel releases keys still held)."""
        self.ui.close()


class WheelMapper:
    """Map scroll and zoom frames to wheel events, keeping the remainders.

    Parameters
    ----------
    sink : object
        Anything with ``emit(list_of_events)``.
    scroll_gain : float, optional
        Hi-res wheel units per point of finger movement; negative inverts.
    pinch_gain : float, optional
        Hi-res wheel units per unit of ln(scale).

    Notes
    -----
    A pinch holds Left Ctrl while it lasts. ``release`` lets go of it; call it
    when the session ends, whatever the reason.
    """

    def __init__(self, sink, scroll_gain: float = SCROLL_GAIN, pinch_gain: float = PINCH_GAIN):
        self.sink = sink
        self.scroll_gain = scroll_gain
        self.pinch_gain = pinch_gain
        self.ctrl_down = False
        self._acc = {"v": 0.0, "h": 0.0, "z": 0.0}  # fractional hi-res units
        self._legacy = {"v": 0, "h": 0, "z": 0}  # whole hi-res units not yet in a detent

    def handle(self, frame) -> None:
        """Process one decoded frame; other frames are ignored.

        Parameters
        ----------
        frame : Gesture, Scroll or Zoom
            Frame from the iPad.
        """
        try:
            if isinstance(frame, Gesture):
                self._gesture(frame)
            elif isinstance(frame, Scroll):
                self._scroll(frame)
            elif isinstance(frame, Zoom):
                self._zoom(frame)
        except Exception:
            self.release()  # never leave Ctrl held after an error
            raise

    def _gesture(self, g: Gesture) -> None:
        if g.gesture == GESTURE_ZOOM:
            if g.phase == GESTURE_BEGIN:
                self._acc["z"] = 0.0
                self._legacy["z"] = 0
                self._press()
            else:
                self.release()
        elif g.gesture == GESTURE_SCROLL and g.phase == GESTURE_BEGIN:
            self._acc["v"] = self._acc["h"] = 0.0
            self._legacy["v"] = self._legacy["h"] = 0

    def _press(self) -> None:
        if not self.ctrl_down:
            self.ctrl_down = True  # first: if the write fails, release still tries
            self.sink.emit([(e.EV_KEY, e.KEY_LEFTCTRL, 1)])

    def release(self) -> None:
        """Release Left Ctrl if held (idempotent, never raises)."""
        if not self.ctrl_down:
            return
        self.ctrl_down = False
        try:
            self.sink.emit([(e.EV_KEY, e.KEY_LEFTCTRL, 0)])
        except Exception as exc:  # noqa: BLE001 - device may be gone already
            log.warning("could not release Ctrl: %s", exc)

    def _axis(self, axis: str, value: float, hires_code: int, legacy_code: int) -> list[tuple[int, int, int]]:
        """Add ``value`` hi-res units to an axis; return the events to emit."""
        acc = self._acc[axis] + value
        whole = int(acc)  # towards zero
        self._acc[axis] = acc - whole
        if whole == 0:
            return []
        ev = [(e.EV_REL, hires_code, whole)]
        legacy = self._legacy[axis] + whole
        detents = int(legacy / HI_RES)
        self._legacy[axis] = legacy - detents * HI_RES
        if detents:
            ev.append((e.EV_REL, legacy_code, detents))
        return ev

    def _scroll(self, s: Scroll) -> None:
        if self.ctrl_down:
            return  # a pinch is going on; the drift of the fingers must not scroll
        if not (math.isfinite(s.dx) and math.isfinite(s.dy)):
            return
        # Content follows the fingers: fingers down = view up = wheel up (+),
        # fingers right = view left = REL_HWHEEL negative.
        ev = self._axis("v", s.dy * self.scroll_gain, e.REL_WHEEL_HI_RES, e.REL_WHEEL)
        ev += self._axis("h", -s.dx * self.scroll_gain, e.REL_HWHEEL_HI_RES, e.REL_HWHEEL)
        if ev:
            self.sink.emit(ev)

    def _zoom(self, z: Zoom) -> None:
        if not (math.isfinite(z.scale) and z.scale > 0):
            return
        if not self.ctrl_down:
            self._press()  # zoom frame without a begin frame
        ev = self._axis("z", math.log(z.scale) * self.pinch_gain, e.REL_WHEEL_HI_RES, e.REL_WHEEL)
        if ev:
            self.sink.emit(ev)
