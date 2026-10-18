"""Negotiate pixel mouse reporting only after the parser has pixel dimensions."""

from __future__ import annotations

from textual import events
from textual.drivers.linux_driver import LinuxDriver


class ConsoleDriver(LinuxDriver):
    def __init__(self, *args, **kwargs):
        self._pixel_size_ready = False
        super().__init__(*args, **kwargs)

    def _enable_mouse_pixels(self) -> None:
        # A terminal may acknowledge resize support without sending an initial
        # resize report. Until that report, the parser expects character cells.
        if self._pixel_size_ready:
            super()._enable_mouse_pixels()

    def process_message(self, message) -> None:
        if isinstance(message, events.Resize):
            pixels = message.pixel_size
            if pixels and pixels.width > 0 and pixels.height > 0:
                self._pixel_size_ready = True
                if self._in_band_window_resize and not self._mouse_pixels:
                    self._enable_mouse_pixels()
        super().process_message(message)
