"""只查询 smart socket 协议版本，不启动 adb 或操作 USB/手机。"""
import socket


def receive(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise RuntimeError('ADB health connection closed')
        data.extend(chunk)
    return bytes(data)


def check(host='127.0.0.1', port=5037):
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.sendall(b'000chost:version')
        if receive(connection, 4) != b'OKAY':
            raise RuntimeError('ADB health request rejected')
        size = int(receive(connection, 4), 16)
        if size != 4 or receive(connection, size) != b'0029':
            raise RuntimeError('ADB server/client protocol mismatch')


if __name__ == '__main__':
    check()
