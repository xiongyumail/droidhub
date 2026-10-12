"""应用播放声：每台一条 PCM 采集连接，按需启动、共享给订阅者。

Android 11 起用 scrcpy 服务端自带的采音（不用在设备上装应用）；Android 10 用 sndcpy 1.1 应用。
两种都输出 48 kHz 双声道 s16le。"""
from collections import deque
import logging
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from runtime import adb_forward_host, connection_error, run_command

log = logging.getLogger('droidhub.audio')
PACKAGE = 'com.rom1v.sndcpy'
SAMPLE_RATE = 48000
CHANNELS = 2
FORMAT = 's16le'
SCRCPY_SDK = 30
SCRCPY_JAR = Path(__file__).with_name('scrcpy-server-v2.4')
# 和画面、控制通道分开放：覆盖正在运行的 jar 会让那边的进程出错
SCRCPY_REMOTE = '/data/local/tmp/droidhub-audio.jar'


class Audio:
    def __init__(self, serial, on_failure=None):
        self.serial = serial
        self.adb = ['adb', '-s', serial]
        self.cond = threading.Condition()
        self.thread = self.sock = None
        self.stop = threading.Event()
        self.viewers = self.epoch = self.seq = 0
        self.chunks = deque()
        self.partial = b''
        self.error = None
        self.on_failure = on_failure

    def status(self):
        with self.cond:
            state = ('stopping' if self.thread and self.stop.is_set() else
                     'error' if self.error else
                     'streaming' if self.thread and self.chunks else
                     'starting' if self.thread else 'idle')
            return {'state': state, 'viewers': self.viewers, 'error': self.error,
                    'sample_rate': SAMPLE_RATE, 'channels': CHANNELS, 'format': FORMAT}

    def join(self):
        with self.cond:
            # 上一条连接清理完成后才可再次启动，避免旧 force-stop 杀掉新采音服务。
            deadline = time.monotonic() + 10
            while ((self.thread and (self.stop.is_set() or self.error)) or
                   (not self.thread and self.viewers)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('声音正在停止，请稍后重试')
                self.cond.wait(remaining)
            self.viewers += 1
            if not self.thread:
                self.epoch += 1
                self.error = None
                self.chunks.clear()
                self.partial = b''
                self.stop.clear()
                self.thread = threading.Thread(target=self._run, daemon=True)
                try:
                    self.thread.start()
                except Exception:
                    self.thread = None
                    self.viewers -= 1
                    self.cond.notify_all()
                    raise
            return self.epoch, self.seq

    def leave(self, epoch):
        with self.cond:
            if epoch != self.epoch or self.viewers <= 0:
                return
            self.viewers -= 1
            if not self.viewers:
                self.stop.set()
                if self.sock:
                    try:
                        self.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            self.cond.notify_all()

    def wait(self, epoch, seq, timeout=3):
        with self.cond:
            ready = self.cond.wait_for(lambda: self.seq != seq or self.error or
                                       not self.thread or self.stop.is_set() or epoch != self.epoch, timeout)
            if epoch != self.epoch or self.stop.is_set():
                raise RuntimeError('声音已停止')
            if self.error:
                raise RuntimeError(self.error)
            if not self.thread:
                raise RuntimeError('声音中断，请重新开启')
            if not ready:
                return seq, b''
            # 新订阅者从当前 seq 开始，不重播先前的声音；慢客户端最多补最近 300ms。
            data = b''.join(chunk for number, _, chunk in self.chunks if number > seq)
            return self.seq, data

    def _publish(self, data):
        with self.cond:
            # TCP 可在任意字节处拆包；订阅和丢弃旧块必须从完整立体声帧开始。
            data = self.partial + data
            length = len(data) // (CHANNELS * 2) * (CHANNELS * 2)
            self.partial = data[length:]
            if not length:
                return
            now = time.monotonic()
            self.seq += 1
            self.chunks.append((self.seq, now, data[:length]))
            while len(self.chunks) > 1 and self.chunks[0][1] < now - .3:
                self.chunks.popleft()
            self.cond.notify_all()

    def _command(self, *args, timeout=5, cleanup=False):
        try:
            return run_command(self.adb + list(args), timeout, None if cleanup else self.stop)
        except subprocess.CalledProcessError as error:
            log.info('%s adb 音频命令失败：%s', self.serial, str(error.stderr or '').strip())
            if connection_error(error):
                raise
            raise RuntimeError('手机采音启动失败，请检查连接和录音权限')

    def _run(self):
        try:
            self._session()
        except Exception as error:
            if not self.stop.is_set() and connection_error(error) and self.on_failure:
                self.on_failure(error)
            with self.cond:
                if not self.stop.is_set():
                    self.error = str(error) if isinstance(error, RuntimeError) else '手机采音连接中断，请重新开启'
                    log.info('%s 声音中断：%s', self.serial, error)
                self.cond.notify_all()
        finally:
            with self.cond:
                self.sock = self.thread = None
                self.chunks.clear()
                self.partial = b''
                self.cond.notify_all()

    def _session(self):
        sdk = self._command('shell', 'getprop', 'ro.build.version.sdk').strip()
        if sdk.isdigit() and int(sdk) >= SCRCPY_SDK:
            self._scrcpy_session()
        else:
            self._sndcpy_session()

    def _receive(self, connection, first):
        connection.settimeout(.5)
        with self.cond:
            self.sock = connection
        self._publish(first)
        last_data = time.monotonic()
        while not self.stop.is_set():
            try:
                data = connection.recv(8192)
            except socket.timeout:
                if time.monotonic() - last_data > 3:
                    raise RuntimeError('手机采音没有数据，请重新开启')
                continue
            if not data:
                raise RuntimeError('手机采音已断开，请重新开启')
            last_data = time.monotonic()
            self._publish(data)

    def _connect(self, port, seconds, alive=lambda: True):
        """连上转发端口并读到第一段数据；服务端还没监听时 adb 会接受后立刻关闭，读到空就重连。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stop.is_set() and alive():
            connection = None
            try:
                connection = socket.create_connection((adb_forward_host(), port), timeout=.3)
                first = connection.recv(8192)
                if first:
                    return connection, first
            except OSError:
                pass
            if connection:
                connection.close()
            self.stop.wait(.1)
        return None, b''

    def _scrcpy_session(self):
        port = server = connection = None
        try:
            self._command('push', str(SCRCPY_JAR), SCRCPY_REMOTE, timeout=10)
            scid = '%08x' % (uuid.uuid4().int & 0x7fffffff)
            port = int(self._command('forward', 'tcp:0', 'localabstract:scrcpy_' + scid))
            if self.stop.is_set():
                return
            # raw_stream：不发设备信息、编码头和帧头，连接上就是裸 PCM
            server = subprocess.Popen(self.adb + [
                'shell', 'CLASSPATH=' + SCRCPY_REMOTE, 'app_process', '/', 'com.genymobile.scrcpy.Server', '2.4',
                'scid=' + scid, 'tunnel_forward=true', 'video=false', 'audio=true', 'control=false',
                'audio_codec=raw', 'raw_stream=true', 'cleanup=false', 'power_on=false', 'log_level=warn'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            connection, first = self._connect(port, 8, lambda: server.poll() is None)
            if self.stop.is_set():
                return
            if not connection:
                raise RuntimeError('采音未就绪：设备上的 scrcpy 采音没有起来')
            self._receive(connection, first)
        finally:
            if connection:
                connection.close()
            if server:
                server.terminate()
                try:
                    server.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=2)
            if port:
                try:
                    self._command('forward', '--remove', 'tcp:%d' % port, timeout=3, cleanup=True)
                except Exception:
                    pass

    def _sndcpy_session(self):
        port = connection = None
        started = False
        try:
            if not self._command('shell', 'pm', 'path', PACKAGE).startswith('package:'):
                raise RuntimeError('手机尚未安装采音组件 sndcpy')
            if self.stop.is_set():
                return
            self._command('shell', 'am', 'force-stop', PACKAGE)
            port = int(self._command('forward', 'tcp:0', 'localabstract:sndcpy'))
            if self.stop.is_set():
                return
            # adb 可能在手机已经启动 Activity 后超时，仍需 force-stop。
            started = True
            result = self._command('shell', 'am', 'start', '-n', PACKAGE + '/.MainActivity')
            if 'Error:' in result or 'Exception' in result:
                raise RuntimeError('手机采音组件启动失败')
            connection, first = self._connect(port, 8)
            if self.stop.is_set():
                return
            if not connection:
                raise RuntimeError('采音未就绪，请允许手机上的 sndcpy 录音授权后重试')
            self._receive(connection, first)
        finally:
            if connection:
                connection.close()
            # sndcpy 的 accept/read 线程不能靠 interrupt 完整关闭，force-stop 释放采音及通知。
            if started:
                try:
                    self._command('shell', 'am', 'force-stop', PACKAGE, timeout=3, cleanup=True)
                except Exception:
                    pass
            if port:
                try:
                    self._command('forward', '--remove', 'tcp:%d' % port, timeout=3, cleanup=True)
                except Exception:
                    pass
