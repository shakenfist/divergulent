'''Progress reporting for long-running whole-machine commands.

Progress goes to stderr (stdout is reserved for tables / ``--json``). On a TTY
it animates a single updating line; off a TTY it prints an occasional plain
line so logs show progress without carriage-return noise. When disabled (e.g.
``--quiet``) it is silent.
'''
from __future__ import annotations

import sys
from typing import TextIO


class Progress:
    '''A terminal-aware progress reporter over a fixed total.'''

    def __init__(self, total: int, *, stream: TextIO | None = None,
                 tty: bool | None = None, enabled: bool = True, every: int = 25) -> None:
        self._total = total
        self._stream = stream if stream is not None else sys.stderr
        self._tty = self._stream.isatty() if tty is None else tty
        self._enabled = enabled
        self._every = every
        self._n = 0

    def step(self, label: str = '') -> None:
        self._n += 1
        if not self._enabled:
            return
        if self._tty:
            # Update a single line in place, clearing to end of line.
            self._stream.write('\r[%d/%d] %s\x1b[K' % (self._n, self._total, label))
            self._stream.flush()
        elif self._n % self._every == 0 or self._n == self._total:
            self._stream.write('[%d/%d] %s\n' % (self._n, self._total, label))
            self._stream.flush()

    def finish(self) -> None:
        if self._enabled and self._tty:
            self._stream.write('\n')
            self._stream.flush()
