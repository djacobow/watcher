import json
import queue
import socket
import sys
import threading
import types

import pytest

import watcher
import watcher.ssh as watcher_ssh


def python_watcher(source, *, name="test"):
    return watcher.Watcher(name, disper=None).subprocess(
        [sys.executable, "-u", "-c", source]
    )


def test_independent_watchers_allow_gates_to_arrive_in_either_order():
    slow = python_watcher("import time; time.sleep(.15); print('slow ready')", name="slow")
    fast = python_watcher("print('fast ready')", name="fast")
    try:
        assert slow.watchFor("slow ready", timeout=2).group() == "slow ready"
        # This arrived first, but remained buffered in its independent queue.
        assert fast.watchFor("fast ready", timeout=2).group() == "fast ready"
    finally:
        slow.close()
        fast.close()


def test_watch_for_discards_prior_nonmatching_events():
    subject = python_watcher("print('first'); print('second')")
    try:
        subject.watchFor("second", timeout=2)
        with pytest.raises(watcher.WatcherNotFoundException):
            subject.watchFor("first", timeout=2)
    finally:
        subject.close()


def test_stdout_and_stderr_are_independent_streams():
    subject = python_watcher(
        "import sys; print('normal'); print('problem', file=sys.stderr)"
    )
    try:
        subject.watchFor("problem", stderr=True, timeout=2)
        subject.watchFor("normal", timeout=2)
    finally:
        subject.close()


def test_failure_pattern_interrupts_a_gate():
    subject = python_watcher("print('starting'); print('fatal: broken')")
    try:
        with pytest.raises(watcher.WatcherFailPatFoundException):
            subject.watchFor("ready", failpats=["fatal:"], timeout=2)
    finally:
        subject.close()


def test_timeout_reports_the_watcher_and_pattern():
    subject = python_watcher("import time; time.sleep(1)", name="sleepy")
    try:
        with pytest.raises(watcher.WatcherTimeoutException) as raised:
            subject.watchFor("never", timeout=0.05)
        assert "sleepy" in str(raised.value)
        assert "never" in str(raised.value)
    finally:
        subject.close()


def test_wait_subprocess_timeout_and_exit_status():
    subject = python_watcher("import time; time.sleep(.2)")
    try:
        with pytest.raises(watcher.WatcherTimeoutException):
            subject.wait_subp_done(timeout=0.01)
        assert subject.wait_subp_done(timeout=2) == 0
    finally:
        subject.close()


def test_send_text_and_json():
    source = (
        "import json, sys; "
        "print(sys.stdin.readline().strip()); "
        "print(json.loads(sys.stdin.readline())['answer'])"
    )
    subject = python_watcher(source)
    try:
        subject.send("hello")
        subject.send(json={"answer": 42})
        subject.watchFor("hello", timeout=2)
        subject.watchFor("42", timeout=2)
    finally:
        subject.close()


def test_json_decoder_and_message_predicate():
    source = (
        "import json; "
        "print(json.dumps({'type': 'progress', 'value': 10})); "
        "print(json.dumps({'type': 'state', 'value': 'ready'}))"
    )
    subject = watcher.Watcher(
        "json", disper=None, decoder_factory=watcher.JsonLinesDecoder
    ).subprocess([sys.executable, "-u", "-c", source])
    try:
        message = subject.watch_for(
            predicate=lambda item: item.get("type") == "state",
            timeout=2,
        )
        assert message == {"type": "state", "value": "ready"}
    finally:
        subject.close()


def test_predicate_gate_destructively_discards_prior_messages():
    subject = watcher.Watcher(
        "json", disper=None, decoder_factory=watcher.JsonLinesDecoder
    ).subprocess(
        [
            sys.executable,
            "-u",
            "-c",
            "import json; print(json.dumps({'n': 1})); print(json.dumps({'n': 2}))",
        ]
    )
    try:
        assert subject.watch_for(predicate=lambda item: item["n"] == 2, timeout=2) == {
            "n": 2
        }
        with pytest.raises(watcher.WatcherNotFoundException):
            subject.watch_for(predicate=lambda item: item["n"] == 1, timeout=2)
    finally:
        subject.close()


def test_structured_reject_predicate_interrupts_gate():
    subject = watcher.Watcher(
        "json", disper=None, decoder_factory=watcher.JsonLinesDecoder
    ).subprocess(
        [
            sys.executable,
            "-u",
            "-c",
            "import json; print(json.dumps({'level': 'fatal', 'reason': 'broken'}))",
        ]
    )
    try:
        with pytest.raises(watcher.WatcherFailPatFoundException) as raised:
            subject.watch_for(
                predicate=lambda item: item.get("state") == "ready",
                reject=[lambda item: item.get("level") == "fatal"],
                timeout=2,
            )
        assert "broken" in str(raised.value)
    finally:
        subject.close()


def test_delimiter_decoder_handles_messages_without_newlines():
    source = (
        "import sys, time; "
        "sys.stdout.buffer.write(b'alpha\\x00be'); sys.stdout.buffer.flush(); "
        "time.sleep(.05); "
        "sys.stdout.buffer.write(b'ta\\x00'); sys.stdout.buffer.flush()"
    )
    subject = watcher.Watcher(
        "binary",
        disper=None,
        decoder_factory=lambda: watcher.DelimiterDecoder(b"\0"),
    ).subprocess([sys.executable, "-u", "-c", source])
    try:
        assert subject.watch_for(predicate=lambda item: item == b"alpha", timeout=2) == b"alpha"
        assert subject.watch_for(predicate=lambda item: item == b"beta", timeout=2) == b"beta"
    finally:
        subject.close()


def test_decoder_callback_replaces_the_old_xformer_hook():
    subject = watcher.Watcher(
        "transformed",
        disper=None,
        decoder_factory=lambda: watcher.DelimiterDecoder(
            b"\n",
            decode=lambda frame: frame.decode("utf-8").removeprefix("LOG: ").upper(),
        ),
    ).subprocess(
        [sys.executable, "-u", "-c", "print('LOG: service ready')"]
    )
    try:
        assert subject.watch_for(
            predicate=lambda message: message == "SERVICE READY", timeout=2
        ) == "SERVICE READY"
    finally:
        subject.close()


def test_xformer_is_rejected_with_decoder_migration_guidance():
    with pytest.raises(TypeError, match="message decoder"):
        watcher.Watcher("old-api", disper=None, xformer=lambda line: line.upper())


def test_send_message_uses_configured_encoder():
    source = "import sys; print(sys.stdin.readline().strip())"
    subject = watcher.Watcher(
        "encoded", disper=None, encoder=watcher.JsonLinesEncoder()
    ).subprocess([sys.executable, "-u", "-c", source])
    try:
        subject.send_message({"command": "start"})
        match = subject.watch_for(r'\{"command":\s*"start"\}', timeout=2)
        assert json.loads(match.group()) == {"command": "start"}
    finally:
        subject.close()


def test_message_source_receives_structured_values_and_sends_encoded_values():
    incoming = queue.Queue()
    sent = []
    incoming.put({"kind": "progress", "value": 1})
    incoming.put({"kind": "ready", "value": 2})

    subject = watcher.Watcher(
        "events",
        disper=None,
        encoder=lambda message: {"wrapped": message},
    ).messages(
        lambda timeout: incoming.get(timeout=timeout),
        send=sent.append,
    )
    try:
        assert subject.watch_for(
            predicate=lambda message: message["kind"] == "ready",
            timeout=2,
        ) == {"kind": "ready", "value": 2}
        subject.send_message("go")
    finally:
        subject.close()

    assert sent == [{"wrapped": "go"}]


def test_message_source_none_is_a_poll_timeout_and_close_callback_runs():
    calls = []
    attempts = 0

    def receive(timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return None
        return "ready"

    subject = watcher.Watcher("events", disper=None).messages(
        receive,
        close=lambda: calls.append("closed"),
    )
    try:
        assert subject.watch_for("ready", timeout=2).group() == "ready"
    finally:
        subject.close()

    assert calls == ["closed"]


def test_can_adapter_decodes_receives_encodes_sends_and_owns_bus():
    class FakeCanBus:
        def __init__(self):
            self.incoming = queue.Queue()
            self.sent = []
            self.closed = False

        def recv(self, timeout):
            try:
                return self.incoming.get(timeout=timeout)
            except queue.Empty:
                return None

        def send(self, message):
            self.sent.append(message)

        def shutdown(self):
            self.closed = True

    bus = FakeCanBus()
    bus.incoming.put({"id": 0x101, "data": 41})
    bus.incoming.put({"id": 0x102, "data": 42})
    subject = watcher.Watcher(
        "can0",
        disper=None,
        encoder=lambda message: {**message, "encoded": True},
    ).can(
        bus,
        decode=lambda frame: {**frame, "decoded": True},
        own_bus=True,
    )
    try:
        event = subject.watch_for(
            predicate=lambda frame: frame["id"] == 0x102,
            timeout=2,
        )
        subject.send_message({"id": 0x201, "data": 7})
    finally:
        subject.close()

    assert event == {"id": 0x102, "data": 42, "decoded": True}
    assert bus.sent == [{"id": 0x201, "data": 7, "encoded": True}]
    assert bus.closed


def test_drain_consumes_current_messages():
    subject = python_watcher("print('one'); print('two'); print('three')")
    try:
        subject.watch_for("one", timeout=2)
        assert subject.drain() == ["two", "three"]
    finally:
        subject.close()


def test_none_timeout_waits_until_message_arrives():
    subject = python_watcher("import time; time.sleep(.05); print('ready')")
    try:
        assert subject.watch_for("ready", timeout=None).group() == "ready"
    finally:
        subject.close()


def test_generated_names_are_unique():
    first = watcher.Watcher(disper=None)
    second = watcher.Watcher(disper=None)
    assert first.name != second.name


def test_context_manager_terminates_a_running_process():
    with python_watcher("import time; time.sleep(30)") as subject:
        process = subject.proc_handle
        assert subject.proc_running()
    process.wait(timeout=2)
    assert process.poll() is not None


def test_close_is_idempotent():
    subject = python_watcher("import time; time.sleep(30)")
    subject.close()
    subject.close()
    subject.proc_handle.wait(timeout=2)


def test_close_kills_and_reaps_a_process_that_ignores_termination():
    subject = python_watcher(
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready'); time.sleep(30)"
    )
    subject.watch_for("ready", timeout=2)

    subject.close(timeout=0.05)

    assert subject.proc_handle.poll() is not None


def test_ssh_options_precede_destination_and_disabling_host_checks_is_explicit():
    subject = watcher.Watcher("ssh", disper=None)
    captured = {}

    def fake_subprocess(self, command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return self

    subject.subprocess = types.MethodType(fake_subprocess, subject)
    returned = subject.ssh(
        "dave",
        "example.test",
        "uptime",
        port=2222,
        insecure_ignore_host_key=True,
        cwd="/tmp",
    )

    assert returned is subject
    assert captured["command"] == [
        "ssh",
        "-t",
        "-t",
        "-p",
        "2222",
        "-o",
        "StrictHostKeyChecking=no",
        "dave@example.test",
        "uptime",
    ]
    assert captured["kwargs"] == {"cwd": "/tmp"}


def test_paramiko_backend_connects_decodes_sends_and_closes(monkeypatch):
    class FakeChannel:
        def __init__(self):
            self.stdout = [b'{"state":"ready"}\n', b""]
            self.stderr = [b""]
            self.sent = []
            self.command = None
            self.pty = None
            self.closed = False

        def get_pty(self, **kwargs):
            self.pty = kwargs

        def exec_command(self, command):
            self.command = command

        def invoke_shell(self):
            self.command = "<shell>"

        def recv(self, size):
            return self.stdout.pop(0)

        def recv_stderr(self, size):
            return self.stderr.pop(0)

        def sendall(self, payload):
            self.sent.append(payload)

        def close(self):
            self.closed = True

    channel = FakeChannel()

    class FakeTransport:
        def open_session(self):
            return channel

    class FakeClient:
        def __init__(self):
            self.system_keys_loaded = False
            self.known_hosts = None
            self.policy = None
            self.connect_kwargs = None
            self.closed = False

        def load_system_host_keys(self):
            self.system_keys_loaded = True

        def load_host_keys(self, filename):
            self.known_hosts = filename

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.connect_kwargs = kwargs

        def get_transport(self):
            return FakeTransport()

        def close(self):
            self.closed = True

    client = FakeClient()
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: client,
        WarningPolicy=lambda: "accept-with-warning",
    )
    monkeypatch.setattr(watcher_ssh, "paramiko_module", fake_paramiko)

    subject = watcher.Watcher(
        "remote",
        disper=None,
        decoder_factory=watcher.JsonLinesDecoder,
        encoder=watcher.JsonLinesEncoder(),
    ).ssh(
        "dave",
        "example.test",
        "service",
        "--verbose",
        backend="paramiko",
        port=2222,
        pty=False,
        known_hosts="test-known-hosts",
        key_filename="test-key",
    )
    try:
        assert subject.watch_for(
            predicate=lambda message: message["state"] == "ready", timeout=2
        ) == {"state": "ready"}
        subject.send_message({"command": "stop"})
    finally:
        subject.close()

    assert client.system_keys_loaded
    assert client.known_hosts == "test-known-hosts"
    assert client.policy is None
    assert client.connect_kwargs == {
        "hostname": "example.test",
        "port": 2222,
        "username": "dave",
        "key_filename": "test-key",
    }
    assert channel.command == "service --verbose"
    assert channel.pty is None
    assert channel.sent == [b'{"command": "stop"}\n']
    assert channel.closed
    assert client.closed


def test_paramiko_backend_requires_optional_dependency(monkeypatch):
    monkeypatch.setattr(watcher_ssh, "paramiko_module", None)
    with pytest.raises(watcher.WatcherException, match=r"watcher\[ssh\]"):
        watcher.Watcher("remote", disper=None).ssh(
            "dave", "example.test", backend="paramiko"
        )


def test_package_root_exports_the_supported_public_api():
    expected = {
        "DelimiterDecoder",
        "JsonLinesDecoder",
        "JsonLinesEncoder",
        "LineDecoder",
        "MessageDecoder",
        "MessageEncoder",
        "Watcher",
        "WatcherException",
        "WatcherFailPatFoundException",
        "WatcherGroup",
        "WatcherNotFoundException",
        "WatcherTimeoutException",
        "getDisplayer",
    }
    assert set(watcher.__all__) == expected
    assert watcher.Watcher.__module__ == "watcher.core"
    assert watcher.WatcherGroup.__module__ == "watcher.group"


def test_watcher_group_owns_watchers_and_a_shared_displayer():
    with watcher.WatcherGroup(colorize=False) as group:
        first = group.subprocess(
            "first", [sys.executable, "-u", "-c", "print('first ready')"]
        )
        second = group.subprocess(
            "second", [sys.executable, "-u", "-c", "print('second ready')"]
        )

        assert first.disper is group.displayer
        assert second.disper is group.displayer
        first.watch_for("first ready", timeout=2)
        second.watch_for("second ready", timeout=2)

    assert first.proc_handle.poll() is not None
    assert second.proc_handle.poll() is not None
    assert not group.displayer.running


def test_watcher_group_closes_all_processes_on_exception():
    first = second = None
    with pytest.raises(RuntimeError):
        with watcher.WatcherGroup(close_timeout=0.05, colorize=False) as group:
            first = group.subprocess(
                "first", [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
            )
            second = group.subprocess(
                "second", [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
            )
            raise RuntimeError("test failed")

    assert first.proc_handle.poll() is not None
    assert second.proc_handle.poll() is not None


def test_watcher_group_rejects_duplicate_names():
    with watcher.WatcherGroup(colorize=False) as group:
        group.watcher("duplicate")
        with pytest.raises(watcher.WatcherException, match="duplicate"):
            group.watcher("duplicate")


def test_watcher_group_is_the_diagnostic_printer(capsys):
    group = watcher.WatcherGroup(colorize=False)
    group.print("group message")
    group.close()

    assert "group message" in capsys.readouterr().out


def test_tcp_socket_round_trip():
    try:
        listener = socket.socket()
    except PermissionError:
        pytest.skip("local sockets are prohibited by this test environment")
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    address = listener.getsockname()

    def server():
        connection, _ = listener.accept()
        with connection, connection.makefile("rb") as incoming:
            line = incoming.readline().strip()
            connection.sendall(b"reply: " + line + b"\n")
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    subject = watcher.Watcher("socket", disper=None).socket(address)
    try:
        subject.send("ping")
        subject.watchFor("reply: ping", timeout=2)
    finally:
        subject.close()
        thread.join(timeout=2)
