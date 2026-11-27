"""Create the iPad's virtual monitor in Hyprland and map the tablet to it."""

import asyncio
import json
import logging
import os
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
    event_socket : str, optional
        Path of Hyprland's event socket; default is derived from the environment.
    """

    def __init__(self, hyprctl: str = "hyprctl", event_socket: str | None = None):
        self.hyprctl = hyprctl
        self.event_socket = event_socket

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

    def output_size(self, name: str) -> tuple[int, int]:
        """Size of an output in pixels as captured (rotation applied).

        Parameters
        ----------
        name : str
            Output name.

        Returns
        -------
        (int, int)
            Width and height in pixels.
        """
        for m in self.monitors():
            if m["name"] == name and not m.get("disabled"):
                if m.get("transform", 0) % 2:
                    return m["height"], m["width"]
                return m["width"], m["height"]
        raise HyprlandError(f"no active output {name}")

    def focused_output(self) -> str:
        """Name of the focused output."""
        for m in self.monitors():
            if m.get("focused") and not m.get("disabled"):
                return m["name"]
        raise HyprlandError("no focused output")

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

    def event_socket_path(self) -> str | None:
        """Path of ``.socket2.sock`` of the running Hyprland, or None if unknown."""
        if self.event_socket:
            return self.event_socket
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if not runtime or not signature:
            return None
        return f"{runtime}/hypr/{signature}/.socket2.sock"

    async def watch_reload(self, callback, delay: float = 0.3) -> None:
        """Call ``callback()`` after every ``configreloaded`` event until cancelled.

        Reads Hyprland's event socket (a local Unix socket). If the socket is
        missing or closes, logs once and returns; the caller keeps working.

        Parameters
        ----------
        callback : callable
            Called without arguments in a worker thread (it may run ``hyprctl``).
        delay : float, optional
            Seconds to wait after the event, so the reload is finished.
        """
        path = self.event_socket_path()
        if path is None:
            log.warning("no Hyprland event socket (XDG_RUNTIME_DIR or HYPRLAND_INSTANCE_SIGNATURE unset); "
                        "the tablet mapping is not restored after a config reload")
            return
        try:
            reader, writer = await asyncio.open_unix_connection(path)
        except OSError as exc:
            log.warning("cannot open the Hyprland event socket %s (%s); "
                        "the tablet mapping is not restored after a config reload", path, exc)
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    log.warning("Hyprland event socket closed")
                    return
                if line.startswith(b"configreloaded>>"):
                    await asyncio.sleep(delay)
                    await asyncio.to_thread(callback)
        finally:
            writer.close()
