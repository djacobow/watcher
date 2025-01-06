#!/usr/bin/env python3

import socket, sys

def flushy_print(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()

def setup_sock(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', port))
    s.listen(1)
    flushy_print('listening')
    c, a = s.accept()
    flushy_print(f'Accepted connection from {str(a)}')
    return c, s

def sock_loop(c, s):
    f = c.makefile()
    while True:
        try:
            l = f.readline().strip()
        except:
            flushy_print('Error on socket')
            c.close()
            return
        
        #flushy_print(f'You said: {l}')
        c.send(f'You said: {l}\r\n'.encode('utf-8'))
        if l == 'stop':
            c.close()
            return


if __name__ == '__main__':
    port = 9998
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    c, s  = setup_sock(port)
    sock_loop(c, s)
