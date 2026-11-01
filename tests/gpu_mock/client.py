#!/usr/bin/env python3
"""Test CLI boundary only. The stateful server, not this client, decides results."""
import json
import os
import socket
import sys
from pathlib import Path


def main():
    name = Path(sys.argv[0]).name
    data = json.dumps(sys.argv[1:]).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(os.environ['GPU_MOCK_SOCKET'])
        sock.sendall(('cli\t%s\t%d\t%d\n' % (name, len(data), os.getpid())).encode() + data)
        with sock.makefile('rb') as stream:
            error, size = map(int, stream.readline().split())
            if error:
                print('mock RPC errno=%s' % error, file=sys.stderr)
                return 70
            reply = json.loads(stream.read(size))
    sys.stdout.write(reply.get('stdout', ''))
    sys.stderr.write(reply.get('stderr', ''))
    return reply['exit']


if __name__ == '__main__':
    raise SystemExit(main())
