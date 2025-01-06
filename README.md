# Watcher

This simple python library exists to facilitate writing applications and tests
that must coordinate with one or more asynchronous processes.

The overall concept is to assume that:

* There are some asynchronous activities that can
  be observed and controlled through a line-oriented text interface.
  For example, you might start a program and watch a log grow that
  tells you what that process is doing.
* you want to start one or more of such activities
* you want to look through the logs of those async activities to be
  sure that certain patterns do or do not appear within some
  specified period
* you want to send messages to those activities that might
  affect their behavior

## How it works

To use watcher, you simple instantiate a watcher object and then
call one of the methods that starts that watcher running. There
are currently start methods for:

1. a process
2. a tcp socket
3. a socket via ssh
4. a serial port (requires `pyserial` be installed)

For example, starting a process looks like this:

```python
import watcher
w = watcher.Watcher("proc").subprocess(['./some_other_program.py'])
```

Connecting to a server socket looks very much the same:

```python
import watcher
w = watcher.Watcher("proc").socket(('fooby.com',1234))
```

The important thing is that both of these, once started, work the
same. If you want to send a string (with a `[CR]`) do

```python
w.send('watcher says hi')
```

Finally, most importantly, you can look for things:

```python
w.watchFor(r'the answer is 42', timeout=20)
```

This means that we will look for the string `the answer is 42` in the
output of whatever `w` is connected to. If we see it we will return the
match and continue. If we do not see it within the timeout specified,
we will raise an exception.

We can have multiple watchers working with multiple streams of multiple
types all at once.

That's really the gist of it.

## Under the hood

The way watcher works is by creating a queue to hold the incoming
text from some stream, and a thread to read from that stream and put
the lines into the queue as they come.

Then, you can use the `.watchFor()` member to scan through that queue,
looking for a specified pattern, consuming the lines as it goes.

### DisplayQueue

In order to facilitate debug, one of the features of Watcher is that
it also aggregates all the streams into one stream and prints them to
the screen. The aggregation includes a timestamp (from the beginning of
execution) as well as an indication of which stream sent the line.

To make this work, none of the streams can simply `print`. If they did,
their output would be intermixed and garbled. Instead, all the streams
put their incoming lines into a special queue called the DisplayQueue.
This drains constantintly in a print loop, thus making sure you can see
what the streams are doing.

Generally, you do not need to think about the DisplayQueue, and in fact,
if you do not create one, a singleton DisplayQueue will be furnished by
the function `getDisplayer()`. However, you do need to be aware that this
queues output thread will not stop on its own. Your app or test should
stop it at the end of the test, most easily with: `getDisplayer().stop()`.

NB: if your test fails by raising an exception, you should catch that
exception in order to stop the DisplayQueue thread.

### xformer functions

If, for any reason, you want to, you can provide an `xformer` function
when you create a watcher queue. This function will be called each time
a line of text is received and the return value of that function will
replace that line in the scan queue. This lets you make some adjustments
to data, filter out lines, convert to/from json, or whatever you need.

### failpats

An optional argument to the `.watchFor()` member is a list of one or more
failure patterns. Unlike the first argument to `.watchFor()` which is
the pattern to expect, these patterns are used to trigger an exception.
They are things that you do _not_ want to see in the output.

