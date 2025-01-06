#!/usr/bin/env python3

import sys, os, random

sys.path.insert(0, os.path.join(os.path.dirname(__file__),'..'))

import watcher as w

def test(d):
    if True:
        d.print('Simple test of process interaction')
        w0 = w.Watcher("proc").subprocess(['./helpers/process_companion.py'])
        w0.watchFor(r'i 1')
        w0.send('dave was here')
        w0.watchFor('dave was here')
        w0.send('stop')
        w0.wait_subp_done()

    if True:
        d.print('Simple test of socket (& process) interaction')
        port = random.randint(0, 1000) + 9000
        w1 = w.Watcher("s_proc").subprocess(['./helpers/socket_companion.py', str(port)])
        w1.watchFor(r'listening')
        w2 = w.Watcher('s_sock').socket(('', port))
        w1.watchFor(r'Accepted')
        w2.send('dave was here') 
        w2.watchFor(r'You said: dave was here')
        w2.send('stop') 
        w1.wait_subp_done()
        d.print('w1 is done')    

if __name__ == '__main__':
    d = w.getDisplayer()
    try:
        test(d)
    except Exception as e:
        print(f'test failed: {e}')
    d.stop()

       
