"""Ownership and shared diagnostics for related watchers."""

from __future__ import annotations

import threading
from typing import Any, Callable, Sequence

from .core import Watcher
from .display import _DispQueue
from .errors import WatcherException


class WatcherGroup:
    """Own a collection of watchers and their shared diagnostic printer."""

    def __init__(
        self,
        *,
        close_timeout: float = 1.0,
        colorize: bool = True,
        daemon: bool = True,
    ) -> None:
        if close_timeout < 0:
            raise ValueError("close_timeout must not be negative")
        self.close_timeout = close_timeout
        self.displayer = _DispQueue(colorize=colorize, daemon=daemon)
        self._watchers: dict[str, Watcher] = {}
        self._lock = threading.Lock()
        self._closed = False

    def __enter__(self) -> "WatcherGroup":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.close()
        except Exception:
            if exc_type is None:
                raise

    @property
    def watchers(self) -> tuple[Watcher, ...]:
        with self._lock:
            return tuple(self._watchers.values())

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Print through the group's serialized diagnostic stream."""

        self.displayer.print(*args, **kwargs)

    def watcher(self, name: str | None = None, **kwargs: Any) -> Watcher:
        """Create and register a watcher that uses the group printer."""

        if "disper" in kwargs:
            raise TypeError("a WatcherGroup always supplies its watcher displayer")
        with self._lock:
            if self._closed:
                raise WatcherException("WatcherGroup is closed")
            child = Watcher(name, disper=self.displayer, **kwargs)
            if child.name in self._watchers:
                raise WatcherException(
                    f"WatcherGroup already contains a watcher named {child.name!r}"
                )
            self._watchers[child.name] = child
            return child

    def get(self, name: str) -> Watcher:
        """Return a registered watcher by name."""

        with self._lock:
            try:
                return self._watchers[name]
            except KeyError as exc:
                raise WatcherException(
                    f"WatcherGroup has no watcher named {name!r}"
                ) from exc

    def subprocess(
        self, name: str, cmdargs: Sequence[str], *args: Any, **kwargs: Any
    ) -> Watcher:
        """Create a watcher and start a subprocess."""

        return self.watcher(name).subprocess(cmdargs, *args, **kwargs)

    def socket(
        self, name: str, host: tuple[str, int], *args: Any, **kwargs: Any
    ) -> Watcher:
        """Create a watcher and connect it to a TCP socket."""

        return self.watcher(name).socket(host, *args, **kwargs)

    def messages(
        self,
        name: str,
        receive: Callable[[float], Any | None],
        *,
        send: Callable[[Any], Any] | None = None,
        close: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> Watcher:
        """Create a watcher for an already-decoded message source."""

        return self.watcher(name, **kwargs).messages(
            receive,
            send=send,
            close=close,
        )

    def can(
        self,
        name: str,
        bus: Any,
        *,
        decode: Callable[[Any], Any] | None = None,
        own_bus: bool = False,
        **kwargs: Any,
    ) -> Watcher:
        """Create a watcher for a python-can compatible bus."""

        return self.watcher(name, **kwargs).can(
            bus,
            decode=decode,
            own_bus=own_bus,
        )

    def serial(
        self, name: str, port: str, speed: int, *args: Any, **kwargs: Any
    ) -> Watcher:
        """Create a watcher and connect it to a serial port."""

        return self.watcher(name).serial(port, speed, *args, **kwargs)

    def ssh(
        self, name: str, user: str, host: str, *args: str, **kwargs: Any
    ) -> Watcher:
        """Create a watcher and start an SSH session."""

        return self.watcher(name).ssh(user, host, *args, **kwargs)

    def close(self) -> None:
        """Close every child watcher, then flush and stop the printer."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            children = tuple(reversed(self._watchers.values()))

        first_error: Exception | None = None
        for child in children:
            try:
                child.close(timeout=self.close_timeout)
            except Exception as exc:
                self.print(f"failed to close {child.name}: {exc}")
                if first_error is None:
                    first_error = exc
        self.displayer.stop()
        if first_error is not None:
            raise WatcherException("WatcherGroup failed to close a watcher") from first_error
