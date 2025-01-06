#!/usr/bin/env python3
import time
import sys
import threading

def main():
    def reader():
        while True:
            l = input().strip()
            print(f'You typed: {l}')   
            if l == 'stop':
                return

    t = threading.Thread(target=reader)
    t.start()
  
    for i in range(5):
        print(f'i {i}')
        sys.stdout.flush()
        time.sleep(0.1)

if __name__ == '__main__':
    main()

