"""Offline tests for visible-browser WSLg validation."""

from pathlib import Path
from unittest import TestCase

from utils.display_environment import (
    VisibleBrowserUnavailableError,
    validate_wslg_display,
)


class DisplayEnvironmentTests(TestCase):
    def test_x11_socket_allows_visible_browser(self):
        validate_wslg_display(
            {"DISPLAY": ":0"},
            path_is_socket=lambda path: path == Path("/tmp/.X11-unix/X0"),
        )

    def test_wayland_socket_allows_visible_browser(self):
        validate_wslg_display(
            {
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
            path_is_socket=lambda path: path == Path("/run/user/1000/wayland-0"),
        )

    def test_missing_display_socket_fails_clearly(self):
        for environment in ({}, {"DISPLAY": ":0"}):
            with self.subTest(environment=environment), self.assertRaisesRegex(
                VisibleBrowserUnavailableError,
                "requires an available WSLg display socket",
            ):
                validate_wslg_display(
                    environment, path_is_socket=lambda path: False
                )
