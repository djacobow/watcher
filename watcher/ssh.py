# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SSH backend helpers."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import WatcherException

try:
    import paramiko as paramiko_module
except ImportError:  # In-process SSH support is optional.
    paramiko_module = None


class ParamikoReader:
    """Present one side of a Paramiko channel as a blocking byte reader."""

    def __init__(self, channel: Any, *, stderr: bool = False) -> None:
        self.channel = channel
        self.stderr = stderr

    def read(self, size: int) -> bytes:
        if self.stderr:
            return self.channel.recv_stderr(size)
        return self.channel.recv(size)

    def close(self) -> None:
        pass


@dataclass
class ParamikoSession:
    client: Any
    channel: Any
    stdout: ParamikoReader
    stderr: ParamikoReader | None


def build_openssh_command(
    user: str,
    host: str,
    args: Sequence[str],
    *,
    port: int | None,
    insecure_ignore_host_key: bool,
    pty: bool,
    command: str | None,
) -> list[str]:
    if command is not None and args:
        raise TypeError("SSH command may be supplied by command= or arguments, not both")
    ssh_args = ["ssh"]
    if pty:
        ssh_args.extend(["-t", "-t"])
    if port is not None:
        ssh_args.extend(["-p", str(port)])
    if insecure_ignore_host_key:
        ssh_args.extend(["-o", "StrictHostKeyChecking=no"])
    ssh_args.append(f"{user}@{host}")
    ssh_args.extend([command] if command is not None else args)
    return ssh_args


def connect_paramiko(
    user: str,
    host: str,
    args: Sequence[str],
    **kwargs: Any,
) -> ParamikoSession:
    if paramiko_module is None:
        raise WatcherException("Paramiko SSH support requires installing watcher[ssh]")

    port = kwargs.pop("port", 22)
    insecure = kwargs.pop("insecure_ignore_host_key", False)
    known_hosts = kwargs.pop("known_hosts", None)
    pty = kwargs.pop("pty", True)
    term = kwargs.pop("term", "vt100")
    width = kwargs.pop("width", 80)
    height = kwargs.pop("height", 24)
    command = kwargs.pop("command", None)
    if command is not None and args:
        raise TypeError("SSH command may be supplied by command= or arguments, not both")
    if command is None and args:
        command = shlex.join(args)

    connect_names = {
        "password",
        "pkey",
        "key_filename",
        "timeout",
        "allow_agent",
        "look_for_keys",
        "compress",
        "banner_timeout",
        "auth_timeout",
        "channel_timeout",
        "passphrase",
        "disabled_algorithms",
        "auth_strategy",
    }
    unknown = set(kwargs) - connect_names
    if unknown:
        raise TypeError(
            f"unexpected Paramiko SSH arguments: {', '.join(sorted(unknown))}"
        )
    connect_kwargs = {name: kwargs[name] for name in connect_names if name in kwargs}

    client = paramiko_module.SSHClient()
    client.load_system_host_keys()
    if known_hosts is not None:
        client.load_host_keys(known_hosts)
    if insecure:
        client.set_missing_host_key_policy(paramiko_module.WarningPolicy())

    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            **connect_kwargs,
        )
        transport = client.get_transport()
        if transport is None or not getattr(transport, "is_active", lambda: True)():
            raise WatcherException(f"Paramiko connection to {host} is not active")
        channel = transport.open_session()
        if pty:
            channel.get_pty(term=term, width=width, height=height)
        if command is None:
            channel.invoke_shell()
        else:
            channel.exec_command(command)
    except Exception:
        client.close()
        raise

    return ParamikoSession(
        client=client,
        channel=channel,
        stdout=ParamikoReader(channel),
        stderr=None if pty else ParamikoReader(channel, stderr=True),
    )
