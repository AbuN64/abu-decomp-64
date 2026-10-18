"""Width-aware, anchored scrollback for the Port and Experiment terminals."""

import re
import textwrap
from collections import deque

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def wrap_line(line, width):
    wrapper = textwrap.TextWrapper(
        width=max(1, int(width)),
        expand_tabs=False,
        replace_whitespace=False,
        drop_whitespace=False,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapper.wrap(str(line)) or [""]


class WrappedLog:
    def __init__(self, max_lines=1000):
        self.entries = deque(maxlen=max_lines)
        self.next_seq = 1
        self.follow_tail = True
        self.anchor = None
        self.visual = []
        self.top = 0
        self.visible = 1

    def append(self, line):
        clean = ANSI.sub("", str(line).rstrip("\r\n")).replace("\r", "").expandtabs(4)
        self.entries.append((self.next_seq, clean))
        self.next_seq += 1

    def reset(self, lines=()):
        self.entries.clear()
        self.follow_tail = True
        self.anchor = None
        self.visual = []
        self.top = 0
        for line in lines:
            self.append(line)

    def layout(self, width, visible):
        width = max(1, int(width))
        self.visible = max(1, int(visible))
        rows = []
        for seq, line in self.entries:
            segments = wrap_line(line, width)
            rows.extend((seq, index, segment) for index, segment in enumerate(segments))
        self.visual = rows
        last_top = max(0, len(rows) - self.visible)
        if self.follow_tail:
            self.top = last_top
        elif self.anchor is not None and rows:
            seq, segment = self.anchor
            same = [index for index, row in enumerate(rows) if row[0] == seq]
            if same:
                self.top = min(same[-1], same[0] + segment)
            else:
                self.top = next((index for index, row in enumerate(rows) if row[0] > seq), 0)
            self.top = min(self.top, last_top)
        else:
            self.top = min(self.top, last_top)
        if not self.follow_tail and rows:
            self.anchor = rows[self.top][:2]
        return [row[2] for row in rows[self.top : self.top + self.visible]]

    def scroll(self, delta):
        if not self.visual:
            return
        last_top = max(0, len(self.visual) - self.visible)
        target = min(last_top, max(0, self.top + int(delta)))
        if target == self.top:
            return
        if int(delta) > 0 and target == last_top:
            self.end()
            return
        self.top = target
        self.anchor = self.visual[self.top][:2]
        self.follow_tail = False

    def page(self, direction):
        self.scroll(int(direction) * max(1, self.visible - 2))

    def home(self):
        if self.visual:
            self.top = 0
            self.anchor = self.visual[0][:2]
        self.follow_tail = False

    def end(self):
        self.follow_tail = True
        self.anchor = None

    def position(self):
        return "TAIL" if self.follow_tail else "SCROLL " + str(self.top + 1) + "/" + str(len(self.visual))
