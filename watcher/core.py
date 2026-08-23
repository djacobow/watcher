"""The primary synchronous Watcher implementation."""

from __future__ import annotations

import io
import json
import re
import socket as socket_module
import subprocess as subprocess_module
import threading
import time
from typing import Any, Callable, Pattern, Sequence

from . import ssh as ssh_support
from .display import _DispQueue, getDisplayer
from .errors import (
    WatcherException,
    WatcherFailPatFoundException,
    WatcherNotFoundException,
    WatcherTimeoutException,
)
from .messages import LineDecoder, MessageDecoder, MessageEncoder
from .streams import _ScanQueue, _StreamFailure

try:
    import serial as serial_module
except ImportError:  # Serial support is optional.
    serial_module = None


_UNSET = object()


class Watcher:
    """Interact synchronously with an asynchronous message stream."""

    creation_number = 0
    _creation_lock = threading.Lock()

    def __init__(
        self,
        name: str | None = None,
        *args: Any,
        disper: _DispQueue | None | object = _UNSET,
        decoder_factory: Callable[[], MessageDecoder[Any]] = LineDecoder,
        encoder: MessageEncoder[Any] | Callable[[Any], bytes] | None = None,
        **kwargs: Any,
    ) -> None:
        if args:
            raise TypeError("Watcher accepts only one positional argument")
        if "xformer" in kwargs:
            raise TypeError(
                "xformer has been removed; perform conversion in a message decoder"
            )
        if kwargs:
            raise TypeError(
                f"unexpected Watcher arguments: {', '.join(sorted(kwargs))}"
            )
        with type(self)._creation_lock:
            number = type(self).creation_number
            type(self).creation_number += 1
        self.name = name if name is not None else f"watcher:{number}"
        self.disper = getDisplayer() if disper is _UNSET else disper
        self.decoder_factory = decoder_factory
        self.encoder = encoder
        self.queues: dict[str, _ScanQueue] = {}
        self.istream: Any = None
        self.proc_handle: subprocess_module.Popen[Any] | None = None
        self.retcode: int | None = None
        self.started = False
        self._closed = False
        self._socket: socket_module.socket | None = None
        self._send_bytes: Callable[[bytes], Any] | None = None
        self._ssh_client: Any = None
        self._ssh_channel: Any = None

    def _new_decoder(self) -> MessageDecoder[Any]:
        decoder = self.decoder_factory()
        if not hasattr(decoder, "feed") or not hasattr(decoder, "finish"):
            raise TypeError("decoder_factory must return a MessageDecoder")
        return decoder

    def __enter__(self) -> "Watcher":
        if not self.started:
            raise WatcherException(f"{self.name} has not been started")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _print(self, *args: Any, **kwargs: Any) -> None:
        if self.disper is not None:
            self.disper.print(*args, **kwargs)

    def _ensure_not_started(self) -> None:
        if self.started:
            raise WatcherException(f"{self.name} has already been started")

    def _subp_watchForExit(self) -> None:
        if self.proc_handle is not None:
            self.retcode = self.proc_handle.wait()
            self._print(f"{self.name} exited status {self.retcode}")

    def wait_subp_done(self, timeout: float | None = None) -> int:
        """Wait for a subprocess to exit and return its status."""

        if self.proc_handle is None:
            raise WatcherException(f"{self.name} is not watching a subprocess")
        try:
            self.retcode = self.proc_handle.wait(timeout=timeout)
        except subprocess_module.TimeoutExpired as exc:
            raise WatcherTimeoutException(
                f"timed out waiting for {self.name} to complete"
            ) from exc
        return self.retcode

    def subprocess(self, cmdargs: Sequence[str], *args: Any, **kwargs: Any) -> "Watcher":
        """Start a subprocess and watch its stdout and stderr."""

        self._ensure_not_started()
        text_mode = kwargs.pop("text", kwargs.pop("universal_newlines", True))
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess_module.PIPE,
            "stderr": subprocess_module.PIPE,
            "stdin": subprocess_module.PIPE,
            "cwd": kwargs.pop("cwd", None),
            "env": kwargs.pop("env", None),
            "shell": kwargs.pop("shell", False),
            "bufsize": kwargs.pop("bufsize", 1 if text_mode else 0),
            "text": text_mode,
            "close_fds": kwargs.pop("close_fds", True),
        }
        if text_mode:
            popen_kwargs["errors"] = kwargs.pop("errors", "replace")
        popen_kwargs.update(kwargs)
        self.proc_handle = subprocess_module.Popen(cmdargs, **popen_kwargs)
        self.istream = self.proc_handle.stdin
        self.queues["stdout"] = _ScanQueue(
            f"{self.name}:stdout",
            self.proc_handle.stdout,
            self.disper,
            self._new_decoder(),
        )
        self.queues["stderr"] = _ScanQueue(
            f"{self.name}:stderr",
            self.proc_handle.stderr,
            self.disper,
            self._new_decoder(),
        )
        threading.Thread(
            target=self._subp_watchForExit,
            name=f"watcher-process:{self.name}",
            daemon=True,
        ).start()
        self.started = True
        return self

    def proc_running(self) -> bool:
        return self.proc_handle is not None and self.proc_handle.poll() is None

    def terminate(self) -> None:
        if self.proc_running():
            assert self.proc_handle is not None
            self.proc_handle.terminate()

    def serial(self, port: str, speed: int, *args: Any, **kwargs: Any) -> "Watcher":
        """Open and watch a hardware serial port."""

        self._ensure_not_started()
        if serial_module is None:
            raise WatcherException("serial support requires the pyserial package")
        stream = serial_module.Serial(port, speed, *args, **kwargs)
        self._print(f"Opened serial port {port} at {speed} b/s")
        self.istream = stream
        self.queues[self.name] = _ScanQueue(
            self.name, stream, self.disper, self._new_decoder()
        )
        self.started = True
        return self

    def socket(self, host: tuple[str, int], *args: Any, **kwargs: Any) -> "Watcher":
        """Connect to and watch a TCP socket."""

        self._ensure_not_started()
        timeout = kwargs.pop("timeout", None)
        sock = socket_module.create_connection(host, timeout=timeout, **kwargs)
        sock.settimeout(None)
        self._socket = sock
        self.istream = sock
        reader = sock.makefile("rb")
        self._print("Socket opened successfully")
        self.queues[self.name] = _ScanQueue(
            self.name, reader, self.disper, self._new_decoder()
        )
        self.started = True
        return self

    def ssh(self, user: str, host: str, *args: str, **kwargs: Any) -> "Watcher":
        """Open an SSH session using OpenSSH (default) or Paramiko."""

        self._ensure_not_started()
        backend = kwargs.pop("backend", "openssh")
        if backend == "paramiko":
            session = ssh_support.connect_paramiko(user, host, args, **kwargs)
            self._ssh_client = session.client
            self._ssh_channel = session.channel
            self.istream = session.channel
            self._send_bytes = session.channel.sendall
            self.queues["stdout"] = _ScanQueue(
                f"{self.name}:stdout",
                session.stdout,
                self.disper,
                self._new_decoder(),
            )
            if session.stderr is not None:
                self.queues["stderr"] = _ScanQueue(
                    f"{self.name}:stderr",
                    session.stderr,
                    self.disper,
                    self._new_decoder(),
                )
            self.started = True
            return self
        if backend != "openssh":
            raise WatcherException(f"unknown SSH backend {backend!r}")

        command = ssh_support.build_openssh_command(
            user,
            host,
            args,
            port=kwargs.pop("port", None),
            insecure_ignore_host_key=kwargs.pop(
                "insecure_ignore_host_key", False
            ),
            pty=kwargs.pop("pty", True),
            command=kwargs.pop("command", None),
        )
        return self.subprocess(command, **kwargs)

    @staticmethod
    def _compile_pattern(pattern: str | Pattern[str]) -> Pattern[str]:
        return re.compile(pattern) if isinstance(pattern, str) else pattern

    def watch_for(
        self, pattern: str | Pattern[str] | object = _UNSET, *args: Any, **kwargs: Any
    ) -> Any:
        """Destructively scan forward to a regex or predicate gate.

        Regex gates return ``re.Match`` for compatibility. Predicate gates
        return the decoded message that made the predicate true.
        """

        if not self.started:
            raise WatcherException(f"{self.name} has not been started")
        stream = kwargs.pop("stream", None)
        if stream is None and kwargs.pop("stderr", False):
            stream = "stderr"
        if stream is None:
            stream = "stdout" if len(self.queues) == 2 else next(iter(self.queues))
        timeout = kwargs.pop("timeout", kwargs.pop("to", 5))
        kwargs.pop("iterdelay", None)
        fail_patterns = kwargs.pop("failpats", kwargs.pop("failpat", ()))
        predicate = kwargs.pop("predicate", None)
        reject = kwargs.pop("reject", ())
        if kwargs:
            raise TypeError(f"unexpected watch_for arguments: {', '.join(kwargs)}")
        if args:
            raise TypeError("watch_for accepts only one positional argument")
        if pattern is _UNSET and predicate is None:
            raise TypeError("watch_for requires either a pattern or predicate")
        if pattern is not _UNSET and predicate is not None:
            raise TypeError("watch_for accepts either a pattern or predicate, not both")
        if fail_patterns is None:
            fail_patterns = ()
        if isinstance(fail_patterns, (str, re.Pattern)):
            fail_patterns = [fail_patterns]
        if reject is None:
            reject = ()
        elif callable(reject):
            reject = [reject]

        scan_queue = self.queues.get(stream)
        if scan_queue is None:
            raise WatcherException(f"{self.name} has no stream named {stream!r}")
        expected = self._compile_pattern(pattern) if pattern is not _UNSET else None
        forbidden = [self._compile_pattern(item) for item in fail_patterns]
        description = repr(expected.pattern) if expected is not None else repr(predicate)
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                raise WatcherTimeoutException(
                    f"{self.name} timed out after {timeout}s waiting for "
                    f"{description} on {scan_queue.name}"
                )
            value = scan_queue.get(timeout=remaining)
            if value is None:
                raise WatcherTimeoutException(
                    f"{self.name} timed out after {timeout}s waiting for "
                    f"{description} on {scan_queue.name}"
                )
            if value is _ScanQueue._EOF:
                raise WatcherNotFoundException(
                    f"{self.name} stream {scan_queue.name} ended while waiting "
                    f"for {description}"
                )
            if isinstance(value, _StreamFailure):
                raise WatcherException(
                    f"{self.name} failed decoding {scan_queue.name}: {value.error}"
                ) from value.error
            message = value["line"]
            for rejection in reject:
                if rejection(message):
                    raise WatcherFailPatFoundException(
                        f"{self.name} rejected a message on {scan_queue.name}: "
                        f"{message!r}"
                    )
            if isinstance(message, str):
                for fail_pattern in forbidden:
                    if fail_pattern.search(message):
                        raise WatcherFailPatFoundException(
                            f"{self.name} observed forbidden pattern "
                            f"{fail_pattern.pattern!r} on {scan_queue.name}: "
                            f"{message!r}"
                        )
            if predicate is not None:
                if predicate(message):
                    return message
            elif isinstance(message, str):
                assert expected is not None
                match = expected.search(message)
                if match:
                    return match

    def watchFor(
        self, pattern: str | Pattern[str], *args: Any, **kwargs: Any
    ) -> Any:
        """Compatibility alias for :meth:`watch_for`."""

        return self.watch_for(pattern, *args, **kwargs)

    def send_message(self, message: Any) -> None:
        """Encode and send one message with the configured encoder."""

        if self.encoder is None:
            raise WatcherException(f"{self.name} has no message encoder")
        if hasattr(self.encoder, "encode"):
            payload = self.encoder.encode(message)  # type: ignore[union-attr]
        else:
            payload = self.encoder(message)
        if not isinstance(payload, bytes):
            raise TypeError("message encoder must return bytes")
        self.send(raw=payload)

    def send(self, *args: Any, **kwargs: Any) -> None:
        """Send a CRLF-terminated message, raw bytes, or a JSON value."""

        if self.istream is None or self._closed:
            raise WatcherException(f"{self.name} is not connected")
        if "raw" in kwargs and "json" in kwargs:
            raise TypeError("send accepts either raw or json, not both")
        unknown = set(kwargs) - {"raw", "json"}
        if unknown:
            raise TypeError(f"unexpected send arguments: {', '.join(sorted(unknown))}")

        if "raw" in kwargs:
            payload = kwargs["raw"]
            if not isinstance(payload, bytes):
                raise TypeError("raw must be bytes")
        elif "json" in kwargs:
            payload = (json.dumps(kwargs["json"]) + "\n").encode("utf-8")
        else:
            payload = (" ".join(str(value).strip() for value in args) + "\r\n").encode(
                "utf-8"
            )

        if self._send_bytes is not None:
            self._send_bytes(payload)
        elif self._socket is not None:
            self._socket.sendall(payload)
        elif serial_module is not None and isinstance(self.istream, serial_module.Serial):
            self.istream.write(payload)
        elif isinstance(self.istream, io.TextIOBase):
            self.istream.write(payload.decode("utf-8"))
            self.istream.flush()
        else:
            self.istream.write(payload)
            self.istream.flush()

    def close(self, timeout: float = 1.0) -> None:
        """Close the transport and ensure a started subprocess is reaped."""

        if self._closed:
            return
        if timeout < 0:
            raise ValueError("close timeout must not be negative")
        self._closed = True
        if self.istream is not None:
            try:
                self.istream.close()
            except (OSError, ValueError):
                pass
        if self._socket is not None:
            try:
                self._socket.shutdown(socket_module.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
        if self._ssh_channel is not None:
            self._ssh_channel.close()
        if self._ssh_client is not None:
            self._ssh_client.close()
        if self.proc_handle is not None and self.proc_handle.poll() is None:
            self.proc_handle.terminate()
            try:
                self.retcode = self.proc_handle.wait(timeout=timeout)
            except subprocess_module.TimeoutExpired:
                self.proc_handle.kill()
                self.retcode = self.proc_handle.wait()
        elif self.proc_handle is not None:
            self.retcode = self.proc_handle.returncode
        for scan_queue in self.queues.values():
            scan_queue.close()
