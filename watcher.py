#!/usr/bin/env python3

import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import types

import ansi_color

try:
    import serial
except Exception:
    serial = None

# This class exists only as a way to serialize the output of multiple
# threads so that their output does not get intermixed. When you have
# multipler Watchers running and printing things, you will want to use
# this to receive messages and see them.
#
# Unless this is created with daemon=True, you will need to call .stop()
# for you program to exit.
class _DispQueue:
    def __init__(self, *args, **kwargs):

        self.q = queue.Queue()
        self.starttime = time.time()
        self.running = True
        self.colorlist = {}
        t = threading.Thread(target=self.printLoop, args=[])
        t.daemon = kwargs.get("daemon", True)
        self.thread = t

        do_color = sys.stdout.isatty()
        do_color = do_color or os.environ.get("COLORTERM", "") == "truecolor"
        do_color = do_color or os.environ.get("TERM", "") == "xterm-256color"
        do_color = do_color and "WATCHER_NOCOLOR" not in os.environ
        do_color = do_color and kwargs.get("colorize", True)
        self.colorize = ansi_color.should_color()
        t.start()

    def put(self, v):
        if not "ts" in v:
            v["ts"] = time.time()
        self.q.put(v)

    def print(self, *args, **kwargs):
        sio = io.StringIO()
        print(*args, **kwargs, file=sio)
        self.put({"name": "DispQueue", "line": sio.getvalue().strip()})

    def stop(self):
        self.running = False
        self.put({"name": "DispQueue", "line": "stopping"})
        self.thread.join()

    def printLoop(self):
        last_name = ""
        while self.running or not self.q.empty():
            try:
                v = self.q.get(True, timeout=1)
                if v is not None:
                    timestamp = v["ts"] - self.starttime
                    name = v["name"]
                    if name not in self.colorlist:
                        if self.colorize:
                            self.colorlist[name] = {
                                "foreground": ansi_color.getnextcolor(),
                                "background": "nochange",
                                "style": "normal",
                            }
                    if name != last_name:
                        last_name = name
                    else:
                        name = ""
                    line = v["line"]
                    os = f"{timestamp:8.3f} | {name:>17} | {line}"
                    if self.colorize:
                        print(ansi_color.colorize(os, **self.colorlist[v["name"]]))
                    else:
                        print(os)
                    sys.stdout.flush()
            except queue.Empty:
                pass
            except TimeoutError:
                pass


def getDisplayer(*args, **kwargs):
    if not hasattr(getDisplayer, "displayer"):
        getDisplayer.displayer = _DispQueue(*args, **kwargs)
    return getDisplayer.displayer


class _ScanQueue:
    def __init__(self, name, infile=None, disper=None, xformer=None):
        self.name = name
        self.fh = infile
        self.disper = disper
        self.xformer = xformer

        self.q = queue.Queue()
        self._closed = False

        if self.fh is not None:
            self.t = threading.Thread(target=self.readLineAndQPut)
            self.t.daemon = True
            self.t.start()

    def readLineAndQPut(self):
        while not self.closed():
            try:
                line = self.fh.readline()
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="backslashreplace")
            except Exception as e:
                self.put("<<Exception on stream: %s>>" % (e))
                self.fh.close()
                self.close()
                return

            if not line:
                self.fh.close()
                self.close()
                return

            self.put(line.rstrip())

    def put(self, line):
        assert not self.closed()
        if self.xformer:
            line = self.xformer(line)
        # allow inclusion of non-string things, but not null str's
        v = {"ts": time.time(), "name": self.name, "line": line}
        if line is not None and not (isinstance(line, str) and not len(line)):
            if self.disper:
                self.disper.put(v)

            self.q.put(v)

    def close(self):
        self.put("<<EOF>>")
        self._closed = True

    def closed(self):
        return self._closed

    def empty(self):
        return self.q.empty()

    def done(self):
        return self.empty() and self.closed()

    def get(self):
        if not self.q.empty():
            return self.q.get(False)
        return None


class WatcherException(Exception):
    pass


class WatcherTimeoutException(WatcherException):
    pass


class WatcherNotFoundException(WatcherException):
    pass


class WatcherFailPatFoundException(WatcherException):
    pass


class Watcher:
    """Class to handler interacting with asynchronous streams."""

    creation_number = 0

    def __init__(self, name=None, *args, **kwargs):
        """
        optional keyword args:
            `disper=xxx` where xxx is a DispQueue object
            `xformer=xxx` where xxx is function that takes a string and returns a string
        """
        if name is None:
            self.name = f"watcher:{self.creation_number}"
        else:
            self.name = name
        self.creation_number += 1
        self.disper = kwargs.get("disper", getDisplayer())
        self.xformer = kwargs.get("xformer")
        self.queues = {}
        self.istream = None
        self.ostream = None
        self.retcode = None
        self.started = False

    def _print(self, *args, **kwargs):
        if self.disper is not None:
            self.disper.print(*args, **kwargs)

    def _subp_watchForExit(self):
        if not self.proc_handle:
            return
        self.proc_handle.wait()
        self.retcode = self.proc_handle.returncode
        self._print(f"{self.name} exited status {self.retcode}")

    def wait_subp_done(self, timeout=0):
        """Block and wait for a process created by `.subprocess()` to complete.

        Returns the process's exit code, of it timeout, raises an exception.
        """
        now = time.time()
        while time.time() < (now + timeout) or timeout == 0:
            if self.retcode is not None:
                return self.retcode
        raise WatcherTimeoutException(
            f"ran out of time wating for {self.name} to complete"
        )

    def subprocess(self, cmdargs, *args, **kwargs):
        """Create streams by starting up a new process and running it with popen.

        The first argument should be a list of arguments to use to start
        the process.

        Most keyword arguments that can be send to Popen will be passed through.
        """
        if self.started:
            self._print("Error: this class can only do one stream at a time")
            return

        self.proc_handle = subprocess.Popen(
            cmdargs,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            cwd=kwargs.pop("cwd", None),
            env=kwargs.pop("env", None),
            shell=kwargs.pop("shell", False),
            bufsize=1,  # this implies "line buffered"
            universal_newlines=kwargs.pop("universal_newlines", True),
            errors="replace",
            close_fds=kwargs.pop("close_fds", True),
            **kwargs,
        )

        def our_sendall(s, b):
            sys.stdout.flush()
            s.stdin.write(b.decode("utf-8"))
            s.stdin.flush()

        self.istream = self.proc_handle.stdin
        self.istream.sendall = types.MethodType(our_sendall, self.proc_handle)

        self.queues["stdout"] = _ScanQueue(
            f"{self.name}:stdout",
            infile=self.proc_handle.stdout,
            disper=self.disper,
            xformer=self.xformer,
        )
        self.queues["stderr"] = _ScanQueue(
            f"{self.name}:stderr",
            infile=self.proc_handle.stderr,
            disper=self.disper,
            xformer=self.xformer,
        )

        self.t_wfx = threading.Thread(target=self._subp_watchForExit)
        self.t_wfx.daemon = True
        self.t_wfx.start()
        self.started = True
        return self

    def proc_running(self):
        if self.proc_handle is None:
            return False
        still_going = self.proc_handle.poll() == None
        return still_going

    def terminate(self):
        if self.proc_handle and self.proc_running():
            try:
                self.proc_handle.terminate()
            except Exception as e:
                print(
                    "Exception {} while killing {}; probably died before we could"
                    " kill it.".format(repr(e), self.name),
                    flush=True,
                )

    def serial(self, port, speed, *args, **kwargs):
        """Create a stream based on a hardware serial port.

        Arguments are port (like /dev/ACM0) and speed. Both
        are required.
        """
        if serial is None:
            self._print("Error: pyserial module not instaled")
            return

        if self.started:
            self._print("Error: this class can only do one stream at a time")
            return

        def our_sendall(s, b):
            return s.write(b)

        s = serial.Serial(port, speed)
        self._print(f"Opened serial port {port} at {speed} b/s")
        s.sendall = types.MethodType(our_sendall, s)
        self.istream = s
        self.queues[self.name] = _ScanQueue(
            self.name, infile=s, disper=self.disper, xformer=self.xformer
        )
        self.started = True
        return self

    def socket(self, host, *args, **kwargs):
        """Create a stream based on an ssh connection.

        First argument should be a tuple of (hostname, port).
        """
        if self.started:
            self._print("Error: this class can only do one stream at a time")
            return

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(host)
        self._print("Socket opened successfully")
        f = s.makefile("rb", errors="replace")
        self.istream = s
        self.queues[self.name] = _ScanQueue(
            self.name, infile=f, disper=self.disper, xformer=self.xformer
        )
        self.started = True
        return self

    def ssh(self, user, host, *args, **kwargs):
        """Create a stream based on an ssh connection.

        The first argument is the username, second is the host.
        If you want to specify a port, include keyworkd `port=nnn`.
        """
        if self.started:
            self._print("Error: this class can only do one stream at a time")
            return
        port = kwargs.get("port")
        ssh_args = [
            "ssh",
            "-t",
            "-t",
            "-o",
            "StrictHostKeyChecking=no",
            "{}@{}".format(user, host),
        ]
        if port is not None:
            ssh_args += ["-p", str(port)]

        return self.subprocess(ssh_args, *args, **kwargs)

    def _internalSearcher(self, q, pat, failpats):
        while not q.empty():
            v = q.get()
            line = v["line"]
            if line is not None and len(line):
                # check for things we do NOT want to see
                for fp in failpats:
                    fm = fp.search(line)
                    if fm:
                        raise (WatcherFailPatFoundException(f" while look for {fp}"))
                        return False

                # check for the thing we DO want to see
                m = pat.search(line)
                if m:
                    return m
        if q.empty() and q.closed():
            raise WatcherNotFoundException(f"while looking for {pat}")
            return False
        return None

    def watchFor(self, pattern, *args, **kwargs):
        """Search a stream for patterns

        The first and only required argument is the regex pattern
        to search for.

        In the case of streams opened by calling `.process()` there
        will be two queues you can search: stderr, and stdout. The
        default will be stdout. If you want to look in the stderr
        queue, include the keyword argument `stderr=True`.

        The search will fail after a timeout. There is a default, but
        you should probably overrride it in each call by specifying
        `timeout=xxx` or, `to=xxx`.

        Optionally, you can also provide a list of "failure patterns".
        These are regex patterns that will be compared to each input
        line in the queue, and if present, will immediately trigger
        an exception.

        On success, this function returns the matching pattern --
        useful if you need to extract elements from the match.
        """
        if len(self.queues) == 1:
            qname = list(self.queues.keys())[0]
        elif len(self.queues) == 2:
            qname = "stderr" if kwargs.get("stderr", False) else "stdout"
        timeout = kwargs.get("timeout", kwargs.get("to", 5))
        iterdel = kwargs.get("iterdelay", 0.01)
        failpats = kwargs.get("failpats", kwargs.get("failpat", []))
        if failpats is not None and isinstance(failpats, str):
            failpats = [failpats]

        pat = re.compile(pattern)
        failpats = [re.compile(fp) for fp in failpats]

        q = self.queues.get(qname)
        if q is None:
            self._print(f"Queue {qname} not found")
            raise WatcherException("queue not found")

        start = time.time()
        while True:
            if time.time() > (start + timeout):
                raise WatcherTimeoutException(f"while looking for {pattern}")
            rv = self._internalSearcher(q, pat, failpats)
            if rv is not None:
                return rv
            time.sleep(iterdel)

    def send(self, *args, **kwargs):
        """Send a message to a stream.

        All the *args are rolled together into one message, with
        each argument separated by a space. The entire string is
        then stripped, and a newline appended.

        If you would like to send raw bytes, then use the
        keyword argument "raw" instead of providing one or more
        regular arguments.

        If you would like to send json from a python object, you
        can use the "json" keyword argument.

        No return value.
        """

        if self.istream is not None:
            raw = kwargs.get("raw")
            js = kwargs.get("json")
            if raw is not None:
                self.istream.sendall(raw)
            elif js is not None:
                obytes = "".join([json.dump(js), "\n"]).encode("utf-8")
                self.istream.sendall(obytes)
            else:
                omsg = " ".join(
                    [
                        s.strip()
                        for s in [x if isinstance(x, str) else str(x) for x in args]
                    ]
                )
                omsg += "\r\n"
                self.istream.sendall(omsg.encode("utf-8"))
