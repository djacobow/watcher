"""Serialized diagnostic output shared by watchers."""

from __future__ import annotations

import io
import queue
import threading
import time
from typing import Any

from . import _color


class _DispQueue:
    """Serialize diagnostic output from all watcher streams."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.q: queue.Queue[dict[str, Any]] = queue.Queue()
        self.starttime = time.monotonic()
        self.running = True
        self.colorlist: dict[str, dict[str, str]] = {}
        self.colorize = kwargs.get("colorize", True) and _color.should_color()
        self.thread = threading.Thread(
            target=self.printLoop,
            name="watcher-display",
            daemon=kwargs.get("daemon", True),
        )
        self.thread.start()

    def put(self, value: dict[str, Any]) -> None:
        if self.running:
            value.setdefault("ts", time.monotonic())
            self.q.put(value)

    def print(self, *args: Any, **kwargs: Any) -> None:
        output = io.StringIO()
        print(*args, **kwargs, file=output)
        self.put({"name": "DispQueue", "line": output.getvalue().strip()})

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.q.put(
            {"ts": time.monotonic(), "name": "DispQueue", "line": "stopping"}
        )
        if self.thread is not threading.current_thread():
            self.thread.join()

    def printLoop(self) -> None:  # Keep the historical public spelling.
        last_name = ""
        while self.running or not self.q.empty():
            try:
                value = self.q.get(timeout=0.25)
            except queue.Empty:
                continue

            source = value["name"]
            if self.colorize and source not in self.colorlist:
                self.colorlist[source] = {
                    "foreground": _color.getnextcolor(),
                    "background": "nochange",
                    "style": "normal",
                }
            shown_source = source if source != last_name else ""
            last_name = source
            timestamp = value["ts"] - self.starttime
            rendered = f"{timestamp:8.3f} | {shown_source:>17} | {value['line']}"
            if self.colorize:
                rendered = _color.colorize(rendered, **self.colorlist[source])
            print(rendered, flush=True)


def getDisplayer(*args: Any, **kwargs: Any) -> _DispQueue:
    """Return the process-wide diagnostic display queue."""

    displayer = getattr(getDisplayer, "displayer", None)
    if displayer is None or not displayer.running:
        displayer = _DispQueue(*args, **kwargs)
        getDisplayer.displayer = displayer
    return displayer
