"""Create the iPad's virtual monitor in Hyprland and map the tablet to it."""

import json
import logging
import re
import subprocess

log = logging.getLogger("ipad_pen")

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class HyprlandError(Exception):
    """A hyprctl call failed."""


def right_edge(monitors: list[dict], skip: str = "") -> int:
    """Leftmost free x position in layout (logical) pixels right of all monitors.

    Parameters
    ----------
    monitors : list of dict
        ``hyprctl monitors -j`` output.
    skip : str, optional
        Monitor name to ignore (our own output).

    Returns
    -------
    int
        Largest ``x + logical width``.
    """
    edge = 0
    for m in monitors:
        if m["name"] == skip or m.get("disabled"):
            continue
        width = m["height"] if m.get("transform", 0) % 2 else m["width"]
        edge = max(edge, m["x"] + round(width / m["scale"]))
    return edge


class Hyprland:
    """Thin wrapper around ``hyprctl`` (Hyprland 0.56 with the Lua config).

    Parameters
    ----------
    hyprctl : str, optional
        hyprctl executable.
    """

    def __init__(self, hyprctl: str = "hyprctl"):
        self.hyprctl = hyprctl

    def _run(self, *args: str) -> str:
        try:
            res = subprocess.run([self.hyprctl, *args], check=False, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HyprlandError(f"hyprctl {' '.join(args)}: {exc}") from exc
        out = res.stdout.strip()
        if res.returncode != 0:
            raise HyprlandError(f"hyprctl {' '.join(args)}: {out or res.stderr.strip()}")
        return out

    def _ok(self, *args: str) -> None:
        out = self._run(*args)
        if out != "ok":
            raise HyprlandError(f"hyprctl {' '.join(args)}: {out}")

    def monitors(self) -> list[dict]:
        """All monitors, ``hyprctl monitors all -j``."""
        return json.loads(self._run("monitors", "all", "-j"))

    def create_output(self, name: str, width: int, height: int, scale: float, refresh: int = 60) -> str:
        """Create a headless output right of all monitors (replaces a leftover one).

        Parameters
        ----------
        name : str
            Output name, letters, digits, ``_`` and ``-``.
        width, height : int
            Mode in pixels.
        scale : float
            Monitor scale.
        refresh : int, optional
            Refresh rate in Hz.

        Returns
        -------
        str
            Position ``XxY`` it was placed at.
        """
        if not NAME_RE.match(name):
            raise ValueError(f"bad output name {name!r}")
        if any(m["name"] == name for m in self.monitors()):
            self.remove_output(name)
        self._ok("output", "create", "headless", name)
        pos = f"{right_edge(self.monitors(), skip=name)}x0"
        self.eval(f'hl.monitor({{ output = "{name}", mode = "{width}x{height}@{refresh}", '
                  f'position = "{pos}", scale = {scale:g} }})')
        return pos

    def remove_output(self, name: str) -> None:
        """Remove an output created with ``create_output``; a missing one is not an error."""
        out = self._run("output", "remove", name)
        if out not in ("ok", "Output not found"):
            raise HyprlandError(f"hyprctl output remove {name}: {out}")

    def set_tablet_output(self, name: str) -> None:
        """Map all tablets to an output (``"current"`` = the focused monitor)."""
        if not NAME_RE.match(name):
            raise ValueError(f"bad output name {name!r}")
        self.eval(f'hl.config({{ input = {{ tablet = {{ output = "{name}" }} }} }})')

    def eval(self, lua: str) -> None:
        """Run Lua in the Hyprland config (``hyprctl eval``)."""
        self._ok("eval", lua)
