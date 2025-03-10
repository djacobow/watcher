#!/usr/bin/env python3

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


def should_color():
    # isatty() should be proof enough we are on a console, but when you use
    # bazel run, isatty() returns false, so we take a few more guesses
    do_color = sys.stdout.isatty()
    do_color = do_color or os.environ.get("COLORTERM", "") == "truecolor"
    do_color = do_color or os.environ.get("TERM", "") == "xterm-256color"
    # if you don't like color or the escape sequeneces are in your logs,
    # disable it by setting this environment variable.
    do_color = do_color and "WATCHER_NOCOLOR" not in os.environ
    return do_color


def getnextcolor():
    getnextcolor.index = getattr(getnextcolor, "index", 0) + 1
    return CONFIG["colors"][1 + (getnextcolor.index % (len(CONFIG["colors"]) - 2))]


def getnextstyle():
    getnextstyle.index = getattr(getnextstyle, "index", 0) + 1
    return CONFIG["styles"][0 + (getnextcolor.index % (len(CONFIG["styles"]) - 1))]


def colorize(
    string,
    foreground="nochange",
    background="nochange",
    style="nochange",
    bright_fg=True,
    bright_bg=False,
):
    fg_prefix = "9" if bright_fg else "3"
    bg_prefix = "10" if bright_bg else "4"

    colorbytes = [
        str(CONFIG["styles"].index(style)),
        fg_prefix + str(CONFIG["colors"].index(foreground)),
        bg_prefix + str(CONFIG["colors"].index(background)),
    ]
    olist = [
        CONFIG["open_escape"],
        ";".join(colorbytes),
        CONFIG["close_escape"],
        string,
        CONFIG["open_escape"],
        "0",
        CONFIG["close_escape"],
    ]
    return "".join(olist)
