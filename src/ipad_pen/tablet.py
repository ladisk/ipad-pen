"""Turn iPad frames into evdev tablet events."""

from evdev import AbsInfo, UInput
from evdev import ecodes as e

from ipad_pen.protocol import PHASE_CANCEL, PHASE_DOWN, PHASE_UP, Button, Hello, Sample, tilt_degrees

DEVICE_NAME = "iPad Pen"
XY_MAX = 32767
PRESSURE_MAX = 4095
# Units per mm. libinput rejects a tablet without a resolution. Based on the
# 12.9" iPad Pro view (about 263 x 197 mm); only the ratio matters to Hyprland.
RES_X = round(XY_MAX / 263)
RES_Y = round(XY_MAX / 197)

CAPABILITIES = {
    e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOUCH, e.BTN_STYLUS],
    e.EV_ABS: [
        (e.ABS_X, AbsInfo(0, 0, XY_MAX, 0, 0, RES_X)),
        (e.ABS_Y, AbsInfo(0, 0, XY_MAX, 0, 0, RES_Y)),
        (e.ABS_PRESSURE, AbsInfo(0, 0, PRESSURE_MAX, 0, 0, 0)),
        (e.ABS_TILT_X, AbsInfo(0, -90, 90, 0, 0, 57)),  # 57 units/rad = 1 unit/deg
        (e.ABS_TILT_Y, AbsInfo(0, -90, 90, 0, 0, 57)),
    ],
}

Event = tuple[int, int, int]


class UInputSink:
    """Virtual tablet backed by ``/dev/uinput``."""

    def __init__(self):
        self.ui = UInput(
            CAPABILITIES, name=DEVICE_NAME, vendor=0x05AC, product=0x1, version=1,
            bustype=e.BUS_USB, input_props=[e.INPUT_PROP_POINTER],
        )

    def emit(self, events: list[Event]) -> None:
        """Write events followed by one SYN_REPORT.

        Parameters
        ----------
        events : list of (type, code, value)
            Events of one frame.
        """
        for ev_type, code, value in events:
            self.ui.write(ev_type, code, value)
        self.ui.syn()

    def close(self) -> None:
        """Remove the virtual device."""
        self.ui.close()


class Mapper:
    """Map frames to tablet events, keeping pen state between frames.

    Parameters
    ----------
    sink : object
        Anything with ``emit(list_of_events)``.
    aspect : float, optional
        Target width/height ratio. When given, the largest centred part of
        the iPad view with this ratio maps to the full tablet range, so the
        drawing is not stretched on a monitor of this ratio.
    pressure : bool, optional
        Send the Pencil's pressure. When False (default), every contact is
        sent at full pressure, so all apps draw a constant width.
    """

    def __init__(self, sink, aspect: float | None = None, pressure: bool = False):
        self.sink = sink
        self.aspect = aspect
        self.pressure = pressure
        self.view = (4.0, 3.0)
        self.in_prox = False
        self.last_xy = (XY_MAX // 2, XY_MAX // 2)

    def _scale(self, x: float, y: float) -> tuple[int, int]:
        if self.aspect:
            w, h = self.view
            if w / h > self.aspect:
                frac = (h * self.aspect) / w
                x = (x - (1 - frac) / 2) / frac
            else:
                frac = (w / self.aspect) / h
                y = (y - (1 - frac) / 2) / frac
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        return round(x * XY_MAX), round(y * XY_MAX)

    def handle(self, frame) -> None:
        """Process one decoded frame.

        Parameters
        ----------
        frame : Hello, Sample or Button
            Frame from the iPad.
        """
        if isinstance(frame, Hello):
            if frame.width > 0 and frame.height > 0:
                self.view = (frame.width, frame.height)
        elif isinstance(frame, Sample):
            self._sample(frame)
        elif isinstance(frame, Button):
            self._button()

    def _sample(self, s: Sample) -> None:
        if s.phase in (PHASE_UP, PHASE_CANCEL):
            self.lift()
            return
        x, y = self._scale(s.x, s.y)
        tx, ty = tilt_degrees(s.altitude, s.azimuth)
        if self.pressure:
            pressure = round(min(max(s.pressure, 0.0), 1.0) * PRESSURE_MAX)
        else:
            pressure = PRESSURE_MAX
        if s.phase == PHASE_DOWN:
            pressure = max(pressure, 1)  # down must count as contact
        ev = [
            (e.EV_ABS, e.ABS_X, x),
            (e.EV_ABS, e.ABS_Y, y),
            (e.EV_ABS, e.ABS_PRESSURE, pressure),
            (e.EV_ABS, e.ABS_TILT_X, round(tx)),
            (e.EV_ABS, e.ABS_TILT_Y, round(ty)),
        ]
        if not self.in_prox:
            ev += [(e.EV_KEY, e.BTN_TOOL_PEN, 1), (e.EV_KEY, e.BTN_TOUCH, 1)]
            self.in_prox = True
        self.last_xy = (x, y)
        self.sink.emit(ev)

    def lift(self) -> None:
        """Lift the pen out of contact and proximity (also on disconnect)."""
        if not self.in_prox:
            return
        self.sink.emit([
            (e.EV_ABS, e.ABS_PRESSURE, 0),
            (e.EV_KEY, e.BTN_TOUCH, 0),
            (e.EV_KEY, e.BTN_TOOL_PEN, 0),
        ])
        self.in_prox = False

    def _button(self) -> None:
        # libinput ignores buttons while the tool is out of proximity, so
        # bring the pen in at its last position for the click.
        x, y = self.last_xy
        enter = not self.in_prox
        if enter:
            self.sink.emit([
                (e.EV_ABS, e.ABS_X, x), (e.EV_ABS, e.ABS_Y, y),
                (e.EV_KEY, e.BTN_TOOL_PEN, 1),
            ])
        self.sink.emit([(e.EV_KEY, e.BTN_STYLUS, 1)])
        self.sink.emit([(e.EV_KEY, e.BTN_STYLUS, 0)])
        if enter:
            self.sink.emit([(e.EV_KEY, e.BTN_TOOL_PEN, 0)])
