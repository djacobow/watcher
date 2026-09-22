import threading

import watcher
from watcher.streams import _ScanQueue


def test_serial_reader_and_owner_close_exactly_once():
    class Stream:
        def __init__(self):
            self.wake = threading.Event()
            self.reader_closing = threading.Event()
            self.calls = 0

        def read(self, size):
            assert self.wake.wait(2)
            return b""

        def close(self):
            self.calls += 1
            self.wake.set()
            assert self.reader_closing.wait(2)

    class Queue(_ScanQueue):
        def close_file(self):
            if threading.current_thread() is self.t:
                self.fh.reader_closing.set()
            super().close_file()

    stream = Stream()
    reader = Queue("serial", stream)
    subject = watcher.Watcher("serial")
    subject.istream = stream
    subject.queues["serial"] = reader
    subject.started = True
    subject.close()
    reader.t.join(2)
    assert not reader.t.is_alive()
    assert reader.closed()
    assert stream.calls == 1
    subject.close()
    assert stream.calls == 1
