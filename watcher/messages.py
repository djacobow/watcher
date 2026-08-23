"""Incremental message decoders and outbound encoders."""

from __future__ import annotations

import json
from typing import Any, Callable, Generic, Iterable, Protocol, TypeVar, cast


Message = TypeVar("Message")


class MessageDecoder(Protocol[Message]):
    """Incrementally turn arbitrary byte chunks into complete messages."""

    def feed(self, data: bytes) -> Iterable[Message]: ...

    def finish(self) -> Iterable[Message]: ...


class MessageEncoder(Protocol[Message]):
    """Encode one outbound message for a transport."""

    def encode(self, message: Message) -> bytes: ...


class DelimiterDecoder(Generic[Message]):
    """Split byte input on a delimiter and optionally decode each frame."""

    def __init__(
        self,
        delimiter: bytes,
        decode: Callable[[bytes], Message] | None = None,
        *,
        emit_trailing: bool = True,
    ) -> None:
        if not delimiter:
            raise ValueError("delimiter must not be empty")
        self.delimiter = delimiter
        self.decode = decode if decode is not None else cast(
            Callable[[bytes], Message], lambda frame: frame
        )
        self.emit_trailing = emit_trailing
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterable[Message]:
        self._buffer.extend(data)
        while True:
            boundary = self._buffer.find(self.delimiter)
            if boundary < 0:
                return
            frame = bytes(self._buffer[:boundary])
            del self._buffer[: boundary + len(self.delimiter)]
            yield self.decode(frame)

    def finish(self) -> Iterable[Message]:
        if self._buffer and self.emit_trailing:
            frame = bytes(self._buffer)
            self._buffer.clear()
            yield self.decode(frame)


class LineDecoder(DelimiterDecoder[str]):
    """Decode newline-delimited text, matching Watcher's original behavior."""

    def __init__(self, encoding: str = "utf-8", errors: str = "backslashreplace"):
        def decode(frame: bytes) -> str:
            return frame.decode(encoding, errors=errors).rstrip()

        super().__init__(b"\n", decode=decode)

    def feed(self, data: bytes) -> Iterable[str]:
        yield from (message for message in super().feed(data) if message)

    def finish(self) -> Iterable[str]:
        yield from (message for message in super().finish() if message)


class JsonLinesDecoder:
    """Decode one JSON value per line."""

    def __init__(self, encoding: str = "utf-8", errors: str = "strict") -> None:
        self._lines = LineDecoder(encoding=encoding, errors=errors)

    def feed(self, data: bytes) -> Iterable[Any]:
        yield from (json.loads(line) for line in self._lines.feed(data))

    def finish(self) -> Iterable[Any]:
        yield from (json.loads(line) for line in self._lines.finish())


class JsonLinesEncoder:
    """Encode Python values as newline-delimited JSON."""

    def __init__(self, **json_kwargs: Any) -> None:
        self.json_kwargs = json_kwargs

    def encode(self, message: Any) -> bytes:
        return (json.dumps(message, **self.json_kwargs) + "\n").encode("utf-8")
