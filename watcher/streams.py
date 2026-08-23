"""Background stream reading and decoded-message queues."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .display import _DispQueue
from .messages import LineDecoder, MessageDecoder


class _StreamFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error


class _ScanQueue:
    """Decode a byte stream in the background and buffer messages in FIFO order."""

    _EOF = object()

    def __init__(
        self,
        name: str,
        infile: Any = None,
        disper: _DispQueue | None = None,
        decoder: MessageDecoder[Any] | None = None,
    ) -> None:
        self.name = name
        self.fh = infile
        self.disper = disper
        self.decoder = decoder if decoder is not None else LineDecoder()
        self.q: queue.Queue[Any] = queue.Queue()
        self._closed = threading.Event()
        self.t: threading.Thread | None = None
        if infile is not None:
            self.t = threading.Thread(
                target=self.readLineAndQPut,
                name=f"watcher-reader:{name}",
                daemon=True,
            )
            self.t.start()

    def readLineAndQPut(self) -> None:
        try:
            while not self.closed():
                chunk = self._read_chunk()
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                for message in self.decoder.feed(chunk):
                    self.put(message)
            for message in self.decoder.finish():
                self.put(message)
        except Exception as exc:
            if not self.closed():
                self.q.put(_StreamFailure(exc))
        finally:
            try:
                self.fh.close()
            except (OSError, ValueError):
                pass
            self.close()

    def _read_chunk(self, size: int = 65536) -> bytes | str:
        reader = getattr(self.fh, "buffer", self.fh)
        if hasattr(reader, "read1"):
            return reader.read1(size)
        if hasattr(reader, "in_waiting"):
            return reader.read(max(1, reader.in_waiting))
        return reader.read(size)

    def put(self, message: Any) -> None:
        if self.closed():
            return
        value = {"ts": time.monotonic(), "name": self.name, "line": message}
        if self.disper:
            self.disper.put(value.copy())
        self.q.put(value)

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self.q.put(self._EOF)

    def closed(self) -> bool:
        return self._closed.is_set()

    def empty(self) -> bool:
        return self.q.empty()

    def done(self) -> bool:
        return self.closed() and self.q.empty()

    def get(self, timeout: float | None = None) -> dict[str, Any] | object | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_nowait(self) -> dict[str, Any] | object | None:
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None


class _MessageSourceQueue(_ScanQueue):
    """Poll an already-decoded message source in the background."""

    def __init__(
        self,
        name: str,
        receive: Callable[[float], Any | None],
        disper: _DispQueue | None = None,
    ) -> None:
        super().__init__(name, disper=disper)
        self.receive = receive
        self.t = threading.Thread(
            target=self.readMessages,
            name=f"watcher-message-source:{name}",
            daemon=True,
        )
        self.t.start()

    def readMessages(self) -> None:
        try:
            while not self.closed():
                message = self.receive(0.1)
                if message is not None:
                    self.put(message)
        except StopIteration:
            pass
        except Exception as exc:
            if not self.closed():
                self.q.put(_StreamFailure(exc))
        finally:
            self.close()
