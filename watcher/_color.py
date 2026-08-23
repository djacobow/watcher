# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Private ANSI color helpers for diagnostic output."""

import os
import sys


CONFIG = {
    "open_escape": bytes([0x1B, ord("[")]).decode("ascii"),
    "close_escape": bytes([ord("m")]).decode("ascii"),
    "colors": [
        "black",
        "red",
        "green",
        "yellow",
        "blue",
        "magenta",
        "cyan",
        "white",
        "nochange",
    ],
    "styles": [
        "normal",
        "bold",
        "dim",
        "italic",
        "underline",
        "blink",
        "fastblink",
        "reverse",
        "hide",
        "strikethrough",
        "nochange",
    ],
}


def should_color() -> bool:
    do_color = sys.stdout.isatty()
    do_color = do_color or os.environ.get("COLORTERM", "") == "truecolor"
    do_color = do_color or os.environ.get("TERM", "") == "xterm-256color"
    return do_color and "WATCHER_NOCOLOR" not in os.environ


def getnextcolor() -> str:
    palette = CONFIG["colors"][1:-1]
    getnextcolor.index = getattr(getnextcolor, "index", -1) + 1
    return palette[getnextcolor.index % len(palette)]


def getnextstyle() -> str:
    palette = CONFIG["styles"][:-1]
    getnextstyle.index = getattr(getnextstyle, "index", -1) + 1
    return palette[getnextstyle.index % len(palette)]


def colorize(
    string: str,
    foreground: str = "nochange",
    background: str = "nochange",
    style: str = "nochange",
    bright_fg: bool = True,
    bright_bg: bool = False,
) -> str:
    fg_prefix = "9" if bright_fg else "3"
    bg_prefix = "10" if bright_bg else "4"
    colorbytes = [
        str(CONFIG["styles"].index(style)),
        fg_prefix + str(CONFIG["colors"].index(foreground)),
        bg_prefix + str(CONFIG["colors"].index(background)),
    ]
    return "".join(
        [
            CONFIG["open_escape"],
            ";".join(colorbytes),
            CONFIG["close_escape"],
            string,
            CONFIG["open_escape"],
            "0",
            CONFIG["close_escape"],
        ]
    )
