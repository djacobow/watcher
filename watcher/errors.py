# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Public exceptions raised by Watcher."""


class WatcherException(Exception):
    """Base class for watcher errors."""


class WatcherTimeoutException(WatcherException):
    """Raised when a gate is not reached before its deadline."""


class WatcherNotFoundException(WatcherException):
    """Raised when a stream ends before a gate is reached."""


class WatcherFailPatFoundException(WatcherException):
    """Raised when a forbidden pattern is encountered before a gate."""
