"""ADB 文件同步协议（adb push/pull 用的 sync:），直接连 ADB 服务，边收边发、不落临时文件。

exec-in 'cat > 文件' 在数据写完前就返回，也拿不到写入失败；sync 的 SEND 在 DONE 之后才回 OKAY 或带原因的 FAIL。
每条请求是 4 字节命令 + 4 字节小端长度 + 内容；DATA 每块最多 64 KiB。
"""
import os
import re
import socket
import struct
import time

CHUNK = 64 * 1024
TIMEOUT = 30


class SyncError(Exception):
    pass


class SourceError(Exception):
    """上传来源出错（客户端断开、请求体不完整），和 adbd 的拒绝区分开。"""


def upstream():
    """ADB_SERVER_SOCKET=tcp:主机:端口；没设时连本机 5037。"""
    spec = os.environ.get('ADB_SERVER_SOCKET')
    if not spec:
        return '127.0.0.1', 5037
    m = re.fullmatch(r'tcp:([\w.-]+):(\d+)', spec)
    if not m:
        raise ValueError('ADB_SERVER_SOCKET 要是 tcp:<主机>:<端口>')
    return m.group(1), int(m.group(2))


def receive(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError('ADB 连接已关闭')
        data.extend(chunk)
    return bytes(data)


def encode(service):
    """smart socket 请求：4 位十六进制长度 + 服务名。"""
    data = service.encode('latin-1')
    return b'%04x' % len(data) + data


def _status(connection):
    status = receive(connection, 4)
    if status != b'OKAY':
        size = int(receive(connection, 4), 16)
        raise SyncError(receive(connection, size).decode('utf-8', 'replace'))


def _fail_message(connection):
    size = struct.unpack('<I', receive(connection, 4))[0]
    return receive(connection, size).decode('utf-8', 'replace')


class Sync:
    def __init__(self, serial, timeout=TIMEOUT):
        self.connection = socket.create_connection(upstream(), timeout=timeout)
        try:
            self.connection.sendall(encode('host:transport:' + serial))
            _status(self.connection)
            self.connection.sendall(encode('sync:'))
            _status(self.connection)
        except Exception:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            self.connection.sendall(b'QUIT' + struct.pack('<I', 0))
        except OSError:
            pass
        self.connection.close()

    def _request(self, command, path):
        data = path.encode('utf-8')
        self.connection.sendall(command + struct.pack('<I', len(data)) + data)

    def stat(self, path):
        """返回 (mode, size)；文件不存在时 mode 为 0。大小按协议是 32 位。"""
        self._request(b'STAT', path)
        reply = receive(self.connection, 16)
        if reply[:4] != b'STAT':
            raise SyncError('STAT 回复无效')
        mode, size, _ = struct.unpack('<III', reply[4:])
        return mode, size

    def push(self, path, chunks, mode=0o644):
        """chunks 逐块产出字节；手机写完并关闭文件后才返回，失败抛出 adbd 给的原因。"""
        self._request(b'SEND', '%s,%d' % (path, 0o100000 | mode))
        # 来源（HTTP 请求体）出错直接抛出，连接随之关闭，adbd 删掉写了一半的文件；
        # 发往 adbd 失败多半是它打不开目标文件，先回了 FAIL 再断开，下面读出原因
        try:
            for chunk in chunks:
                for i in range(0, len(chunk), CHUNK):
                    piece = chunk[i:i + CHUNK]
                    self.connection.sendall(b'DATA' + struct.pack('<I', len(piece)) + piece)
            self.connection.sendall(b'DONE' + struct.pack('<I', int(time.time())))
        except (BrokenPipeError, ConnectionResetError):
            pass
        reply = receive(self.connection, 4)
        if reply == b'FAIL':
            raise SyncError(_fail_message(self.connection))
        if reply != b'OKAY':
            raise SyncError('SEND 回复无效')
        receive(self.connection, 4)

    def pull(self, path):
        """逐块产出文件内容；失败抛出 adbd 给的原因。"""
        self._request(b'RECV', path)
        while True:
            header = receive(self.connection, 8)
            kind, size = header[:4], struct.unpack('<I', header[4:])[0]
            if kind == b'DATA':
                yield receive(self.connection, size)
            elif kind == b'DONE':
                return
            elif kind == b'FAIL':
                raise SyncError(receive(self.connection, size).decode('utf-8', 'replace'))
            else:
                raise SyncError('RECV 回复无效')
