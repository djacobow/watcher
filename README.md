# Watcher

Watcher is a small synchronous Python library for tests that coordinate with
one or more asynchronous activities. A test can start or connect to several
processes, sockets, SSH sessions, or serial devices; wait for each to reach a
gate; send input; and continue when all required gates have been observed.
UTF-8 line-oriented text is the default, while incremental message decoders
support structured or binary protocols.

Install the base library from the repository with `pip install .`. Optional
features are available as extras:

```shell
pip install '.[ssh]'       # Paramiko backend
pip install '.[serial]'    # pyserial transport
pip install '.[test]'      # test dependencies
```

```python
import watcher

with watcher.Watcher("server").subprocess(["./server"]) as server:
    server.watch_for(r"listening on port \d+", timeout=20)
    server.send("status")
    match = server.watch_for(r"status: (.+)", failpats=[r"fatal error"])
```

`watchFor()` remains available as a compatibility alias for `watch_for()`.

## Gates and event ordering

Every input stream has its own FIFO queue. A call to `watch_for(pattern)`
destructively scans forward through that queue: preceding nonmatching events
are discarded, and the matching event is consumed. This is intentional—the
call describes the next gate of interest, not a search through retained log
history.

Queues belonging to other watchers are independent. Consequently, this test
does not require A and B to become ready in a particular order:

```python
a.watch_for("ready")
b.watch_for("ready")
```

If B emits `ready` first, its event remains in B's queue while the test waits
for A. Once A reaches its gate, the second call immediately consumes B's
already-buffered event. The same independence applies to a subprocess's
stdout and stderr queues.

`watch_for()` blocks efficiently until input, EOF, or its deadline. It returns
a `re.Match` on success and otherwise raises one of:

- `WatcherTimeoutException` when the deadline expires
- `WatcherNotFoundException` when the selected stream ends first
- `WatcherFailPatFoundException` when a forbidden pattern is encountered

The default timeout is five seconds. Use `timeout=None` to wait indefinitely.

## Grouping watchers

`WatcherGroup` owns related watchers and gives them one diagnostic printer.
It is the preferred interface when a test coordinates several asynchronous
processes:

```python
with watcher.WatcherGroup() as group:
    api = group.subprocess("api", ["./api-server"])
    worker = group.subprocess("worker", ["./worker"])

    # Either process may emit ready first. Their queues are independent.
    api.watch_for("ready", timeout=20)
    worker.watch_for("ready", timeout=20)

    group.print("both processes are ready")
```

On exit, the group closes all of its watchers in reverse creation order and
then flushes and stops its printer. This also happens when the test body raises
an exception. The subprocess termination grace period defaults to one second:

```python
with watcher.WatcherGroup(close_timeout=0.25) as group:
    ...
```

If a process has not stopped after that period, it is force-killed and reaped.
Watcher names must be unique within a group and can be retrieved later with
`group.get(name)`.

The convenience methods `subprocess`, `socket`, `serial`, and `ssh` create and
start ordinary watchers. Use `group.watcher()` when constructor configuration
such as a message decoder or encoder is needed:

```python
events = group.watcher(
    "events",
    decoder_factory=watcher.JsonLinesDecoder,
    encoder=watcher.JsonLinesEncoder(),
).socket(("localhost", 1234))
```

The group does not change the I/O concurrency model. Every input stream still
has its own blocking reader thread; the group supplies ownership and shared
diagnostics rather than a common scanning thread.

## Transports

### Subprocess

```python
w = watcher.Watcher("worker").subprocess(["./worker", "--verbose"])
w.watch_for("started")                 # stdout by default
w.watch_for("warning", stderr=True)    # compatibility form
w.watch_for("warning", stream="stderr")
status = w.wait_subp_done(timeout=10)
```

Most `subprocess.Popen` keyword arguments can be passed through.

### TCP socket

```python
w = watcher.Watcher("service").socket(("localhost", 1234), timeout=5)
```

### SSH

```python
w = watcher.Watcher("remote").ssh("user", "host", port=2222)
```

SSH uses the system `ssh` executable by default, preserving OpenSSH
configuration, agents, jump hosts, certificates, and connection sharing.
Normal host-key verification is enabled. For disposable test hosts it can
explicitly be disabled with `insecure_ignore_host_key=True`.

An optional in-process Paramiko backend is available after installing
`watcher[ssh]`:

```python
w = watcher.Watcher("remote").ssh(
    "user",
    "host",
    "./service",
    backend="paramiko",
    port=2222,
    key_filename="test-key",
    known_hosts="test-known-hosts",
    pty=False,
)
```

Paramiko loads system host keys and rejects unknown hosts by default. A
`known_hosts` path adds an application-specific host-key file. Setting
`insecure_ignore_host_key=True` accepts unknown keys with a warning but does
not save them. Authentication options such as `password`, `pkey`,
`key_filename`, `allow_agent`, `look_for_keys`, and `passphrase` are forwarded
to `SSHClient.connect()`.

Positional arguments after the host form a safely shell-quoted remote command.
Alternatively, pass an exact command string with `command=...`. With no
command, an interactive shell is opened. A PTY is requested by default for
compatibility with the original backend; set `pty=False` to preserve separate
stdout and stderr streams.

### Serial

```python
w = watcher.Watcher("device").serial("/dev/ttyACM0", 115200)
```

Serial support requires the optional `pyserial` package.

## Sending data

Normal arguments are converted to strings, joined with spaces, and terminated
with CRLF:

```python
w.send("set", "mode", 3)
```

Bytes and JSON values can be sent explicitly:

```python
w.send(raw=b"exact bytes\n")
w.send(json={"command": "status"})
```

JSON is terminated with a newline.

## Message formats and predicate gates

The default `LineDecoder` incrementally splits byte input on newlines and
decodes UTF-8 text. Regular-expression gates therefore work without any
configuration.

For structured messages, provide a decoder factory. A fresh decoder is made
for every stream, which is important because incremental decoders retain
partially received messages:

```python
service = watcher.Watcher(
    "service",
    decoder_factory=watcher.JsonLinesDecoder,
).subprocess(["./service"])

message = service.watch_for(
    predicate=lambda item: (
        item.get("type") == "state" and item.get("value") == "ready"
    ),
    reject=[lambda item: item.get("level") == "fatal"],
    timeout=10,
)
```

A predicate gate returns the decoded message. Like a regex gate, it discards
all preceding messages. A rejecting predicate raises
`WatcherFailPatFoundException`. The older `failpats` argument remains the
regex equivalent for decoded strings.

`DelimiterDecoder` handles other delimiter-framed protocols and accepts a
callback that converts each complete byte frame:

```python
decoder_factory = lambda: watcher.DelimiterDecoder(
    b"\0",
    decode=lambda frame: frame.decode("utf-8").upper(),
)
w = watcher.Watcher("nul-protocol", decoder_factory=decoder_factory)
```

For framing rules that cannot be described by a delimiter, implement the
small incremental decoder protocol:

```python
class MyDecoder:
    def feed(self, data: bytes):
        # Buffer data and yield zero or more complete messages.
        yield from complete_messages

    def finish(self):
        # Optionally emit a final buffered message at EOF.
        return ()
```

`feed()` must tolerate messages split across chunks and multiple messages in
one chunk. The decoder may emit values of any Python type.

Outbound structured messages use a corresponding encoder:

```python
w = watcher.Watcher("service", encoder=watcher.JsonLinesEncoder())
w.send_message({"command": "start"})
```

An encoder is either an object with `encode(message) -> bytes` or a callable
with the same behavior.

## Diagnostics and cleanup

By default all watchers share a display queue that prints timestamped,
source-labelled output without intermixing lines from different reader
threads. ANSI colors are used when supported. Set `WATCHER_NOCOLOR=1` to
disable them, or construct a watcher with `disper=None` to suppress diagnostic
output.

Use a context manager whenever practical. Its exit closes the transport and
terminates a subprocess that is still running:

```python
with watcher.Watcher("worker").subprocess(["./worker"]) as worker:
    worker.watch_for("ready")
```

Otherwise call `w.close()`. For a subprocess, this closes its stdin, requests
termination, waits up to one second, and then force-kills and reaps it if it
has not exited. The grace period can be changed with `w.close(timeout=...)`.
Both `close()` and the singleton display queue's `stop()` method are
idempotent. The display worker is a daemon thread, so explicitly stopping it
is optional but useful when a test needs all pending diagnostic output to be
flushed:

```python
watcher.getDisplayer().stop()
```

## Implementation model

Each watched input stream has one daemon reader thread. It performs blocking
chunk reads, passes those bytes through that stream's incremental decoder, and
puts complete messages into a thread-safe queue. `watch_for()` performs a
blocking queue read with a real deadline; it does not poll. Subprocess exit is
monitored separately so exit status remains available without blocking the
test.
