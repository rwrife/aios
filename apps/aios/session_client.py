"""Small local client, separate from the chat worker and model tool registry."""
import argparse
import json
import socket
import sys


def request(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        connection.sendall(json.dumps(payload).encode() + b'\n')
        data = bytearray()
        while not data.endswith(b'\n'):
            chunk = connection.recv(4096)
            if not chunk or len(data) + len(chunk) > 65536:
                raise RuntimeError("Invalid broker response")
            data.extend(chunk)
        return json.loads(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('socket')
    args = parser.parse_args()
    # Requests containing PINs must use stdin, never process arguments or logs.
    print(json.dumps(request(args.socket, json.loads(sys.stdin.readline(65536)))))


if __name__ == '__main__':
    main()
