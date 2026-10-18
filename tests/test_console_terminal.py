import unittest
from unittest.mock import Mock, patch

from textual import events
from textual.driver import Driver
from textual.messages import InBandWindowResize

from abu_decomp.console.terminal_driver import ConsoleDriver


class ConsoleTerminalTests(unittest.TestCase):
    def driver(self):
        driver = ConsoleDriver.__new__(ConsoleDriver)
        driver._pixel_size_ready = False
        driver._in_band_window_resize = False
        driver._mouse_pixels = False
        driver._mouse = True
        driver.write = Mock()
        return driver

    @patch.object(Driver, "process_message")
    def test_support_without_dimensions_keeps_cell_reporting(self, process):
        driver = self.driver()
        driver.process_message(InBandWindowResize(True, True))
        self.assertTrue(driver._in_band_window_resize)
        driver.write.assert_not_called()
        driver.process_message(events.Resize.from_dimensions((106, 29), None))
        driver.write.assert_not_called()
        driver.process_message(events.Resize.from_dimensions((106, 29), (954, 522)))
        driver.write.assert_called_once_with("\x1b[?1016h")
        self.assertTrue(driver._mouse_pixels)
        driver.process_message(events.Resize.from_dimensions((106, 30), (954, 540)))
        driver.write.assert_called_once()

    @patch.object(Driver, "process_message")
    def test_dimensions_before_support_and_zero_dimensions(self, process):
        driver = self.driver()
        driver.process_message(events.Resize.from_dimensions((106, 29), (0, 0)))
        self.assertFalse(driver._pixel_size_ready)
        driver.process_message(events.Resize.from_dimensions((106, 29), (954, 522)))
        driver.write.assert_not_called()
        driver.process_message(InBandWindowResize(True, True))
        driver.write.assert_called_once_with("\x1b[?1016h")
