"""Keep curses dimensions aligned with the terminal after a Wayland resize."""

import curses
import os


def resize_to_tty(fd: int) -> None:
    size = os.get_terminal_size(fd)
    curses.resizeterm(size.lines, size.columns)
