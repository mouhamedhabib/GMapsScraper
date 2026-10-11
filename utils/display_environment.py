"""Validation for displaying a visible browser through WSLg."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import re


class VisibleBrowserUnavailableError(ValueError):
    """A visible browser was requested without an available WSLg display."""


def validate_wslg_display(
    environment: Mapping[str, str] | None = None,
    *,
    path_is_socket: Callable[[Path], bool] | None = None,
) -> None:
    """Require an available WSLg X11 or Wayland socket."""
    env = os.environ if environment is None else environment
    is_socket = (
        (lambda path: path.is_socket())
        if path_is_socket is None else path_is_socket
    )
    checked: list[Path] = []

    display = env.get("DISPLAY", "").strip()
    match = re.fullmatch(r":(\d+)(?:\.\d+)?", display)
    if match:
        checked.append(Path("/tmp/.X11-unix") / f"X{match.group(1)}")

    wayland_display = env.get("WAYLAND_DISPLAY", "").strip()
    runtime_directory = env.get("XDG_RUNTIME_DIR", "").strip()
    if (
        wayland_display
        and Path(wayland_display).name == wayland_display
        and runtime_directory
    ):
        checked.append(Path(runtime_directory) / wayland_display)

    if any(is_socket(path) for path in checked):
        return

    raise VisibleBrowserUnavailableError(
        "--show-browser requires an available WSLg display socket; start the "
        "command from a WSLg-enabled interactive session with DISPLAY or "
        "WAYLAND_DISPLAY configured"
    )
