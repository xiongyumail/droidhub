"""有期限、可取消的 adb 子进程；取消不重放手机操作。"""
import os
import select
import struct
import subprocess
import time
import zlib


class ConnectionFailure(RuntimeError):
    pass


class RequestCancelled(RuntimeError):
    pass


HID_WRITE_TIMEOUT = 1


def adb_forward_host():
    """forward 的监听端在 ADB server 所在容器，三个媒体/控制通道共用此设置。"""
    return os.environ.get('DROIDHUB_ADB_HOST', '127.0.0.1')


def pause(seconds, cancel=None, progress=None):
    """长等待也检查取消；清理时的松手不使用此取消条件。"""
    deadline = time.monotonic() + seconds
    while True:
        if cancel and cancel():
            raise RequestCancelled('请求已取消')
        if progress:
            progress()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.05, remaining))


def write_pipe(pipe, data, timeout=None):
    """直接写非阻塞 fd，期限包括部分写入，不能在 flush 中无限等待。"""
    timeout = HID_WRITE_TIMEOUT if timeout is None else timeout
    fd = pipe.fileno()
    os.set_blocking(fd, False)
    deadline = time.monotonic() + timeout
    pending = memoryview(data)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectionFailure('UHID 输入写入超时')
        try:
            size = os.write(fd, pending)
            if not size:
                raise ConnectionFailure('UHID 输入通道已关闭')
            pending = pending[size:]
        except BlockingIOError:
            select.select([], [fd], [], remaining)
        except OSError as e:
            raise ConnectionFailure('UHID 输入通道已断开：' + str(e)) from e


def close_hid(proc):
    """先结束本地 hid 客户端，再关闭管道；旧的缓冲管道也不会卡在 close。"""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=2)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def connection_error(error):
    if isinstance(error, (ConnectionFailure, subprocess.TimeoutExpired, ConnectionError)):
        return True
    if isinstance(error, subprocess.CalledProcessError):
        output = str(error.stderr or error.output or '').lower()
        return any(s in output for s in ('device offline', 'not found', 'closed', 'disconnected'))
    return False


RAW_RGBA_8888 = 1


def capture_png(adb):
    """截图读取只依赖 ADB，既不创建触屏也不重连设备。"""
    # 手机端压 PNG 约 2.4 秒；拉原始帧（约 10 MB）在服务端压缩，合计约 1 秒。
    png = encode_raw(screencap(adb))
    if png is None:
        # 头部或像素格式不认识时退回手机端编码，宁慢不错。
        png = screencap(adb, '-p')
    if not png.startswith(b'\x89PNG'):
        raise RuntimeError('截图失败')
    return png


def screencap(adb, *args):
    result = subprocess.run(adb + ['exec-out', 'screencap', *args], capture_output=True, timeout=15)
    if result.returncode:
        raise ConnectionFailure(result.stderr.decode('utf-8', 'replace').strip() or '手机截图命令失败')
    return result.stdout


def encode_raw(raw):
    """screencap 原始帧：宽、高、格式各 4 字节小端，Android 9 起再加 4 字节色彩空间，之后是逐行像素。

    只认 RGBA_8888；其他情况返回 None。"""
    if len(raw) < 12:
        return None
    width, height, pixel_format = struct.unpack_from('<3I', raw)
    stride = width * 4
    header = len(raw) - stride * height
    if pixel_format != RAW_RGBA_8888 or not width or not height or header not in (12, 16):
        return None
    pixels = memoryview(raw)
    # 每行前加过滤类型 0；level 1 在服务器上约 0.1 秒，再高压缩率只多省一成体积。
    rows = b''.join(part for offset in range(header, len(raw), stride)
                    for part in (b'\0', pixels[offset:offset + stride]))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 1)) + chunk(b'IEND', b''))


def run_command(args, timeout, cancel=None):
    """communicate 可继续等待同一个进程，避免重新执行已经发送的命令。"""
    if cancel and cancel.is_set():
        raise InterruptedError('会话已停止')
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError('会话已停止')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                output, error = proc.communicate(timeout=min(.2, remaining))
                if proc.returncode:
                    raise subprocess.CalledProcessError(proc.returncode, args, output, error)
                return output.strip()
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=3)
        finally:
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
