#!/usr/bin/env python3

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

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
