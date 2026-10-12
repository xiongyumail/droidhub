"""独立 ADB 的转发、协议健康检查、设备范围和有期限的退出回归；不用真实 USB。"""
import os
import copy
import io
import platform
import sys
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import adb_health
import app
from device import Clipboard
from audio import Audio
from test_review import fake_phone


class SocketHealthTests(unittest.TestCase):
    def check_response(self, response):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            requests = []
            def respond():
                connection, _ = server.accept()
                with connection:
                    requests.append(adb_health.receive(connection, 16))
                    for byte in response:
                        connection.sendall(bytes([byte]))
            worker = threading.Thread(target=respond)
            worker.start()
            try:
                adb_health.check(port=server.getsockname()[1])
            finally:
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(requests, [b'000chost:version'])

    def test_split_tcp_response_is_accepted(self):
        self.check_response(b'OKAY00040029')

    def test_protocol_mismatch_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            self.check_response(b'OKAY00040028')

    def test_closed_response_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.check_response(b'OK')

    @unittest.skipUnless(os.environ.get('DROIDHUB_TEST_ADB_BINARY'), 'optional pinned ADB client binary')
    def test_native_client_does_not_start_a_server_when_remote_gateway_is_absent(self):
        env = dict(os.environ, ADB_SERVER_SOCKET='tcp:droidhub-adb.invalid:5037')
        result = subprocess.run([env['DROIDHUB_TEST_ADB_BINARY'], 'devices'], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot start server on remote host', result.stderr)


@unittest.skipUnless(platform.system() == 'Linux' and platform.machine() == 'x86_64', 'Linux x86_64 filter')
class UsbGuardTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('DROIDHUB_TEST_ADB_BINARY'), 'optional pinned ADB server binary')
    def test_guarded_native_gateway_starts_with_real_listener_and_protocol(self):
        if Path('/dev/bus/usb').exists():
            self.skipTest('native server startup test requires a host without USB nodes')
        with tempfile.TemporaryDirectory() as directory, socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
            probe.close()
            program = 'import os,usb_guard; usb_guard.install(); os.execv(%r,usb_guard.server_args(%d))' % (
                os.environ['DROIDHUB_TEST_ADB_BINARY'], port)
            env = dict(os.environ, ADB_LIBUSB='1', ADB_MDNS_AUTO_CONNECT='none', ANDROID_USER_HOME=directory)
            process = subprocess.Popen([sys.executable, '-B', '-c', program], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while True:
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode('utf-8', 'replace'))
                    try:
                        adb_health.check(port=port)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail('native ADB listener did not become ready')
                        time.sleep(.02)
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                process.stdout.close()
                process.stderr.close()

    def test_reset_is_denied_before_fd_lookup_and_other_ioctl_still_reaches_kernel(self):
        # 全部使用无效 fd，不打开任何 USB；seccomp 拒绝 EPERM，普通 fd 校验返回 EBADF。
        program = '''
import ctypes, errno, threading
import usb_guard
usb_guard.install()
libc=ctypes.CDLL(None,use_errno=True)
for nr,request,expected in ((16,0x5514,errno.EPERM), (16,0x100005514,errno.EPERM),
                            (0x40000202,0x5514,errno.EPERM), (16,0x5500,errno.EBADF)):
    ctypes.set_errno(0)
    result=libc.syscall(ctypes.c_long(nr),ctypes.c_long(-1),ctypes.c_ulong(request),ctypes.c_ulong(0))
    assert result == -1 and ctypes.get_errno()==expected, (nr,request,result,ctypes.get_errno())
results=[]
def child():
    result=libc.syscall(16,-1,0x5514,0)
    results.append((result,ctypes.get_errno()))
worker=threading.Thread(target=child);worker.start();worker.join()
assert results==[(-1,errno.EPERM)], results
'''
        result = subprocess.run([sys.executable, '-B', '-c', program], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_filter_survives_exec(self):
        program = '''
import os,sys,usb_guard
usb_guard.install()
os.execv(sys.executable,[sys.executable,'-c',"import ctypes,errno; c=ctypes.CDLL(None,use_errno=True); r=c.syscall(16,-1,0x5514,0); assert r==-1 and ctypes.get_errno()==errno.EPERM"])
'''
        result = subprocess.run([sys.executable, '-B', '-c', program], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)


class RemoteForwardTests(unittest.TestCase):
    def test_clipboard_connects_to_gateway(self):
        clipboard = Clipboard('9e24cca')
        process = MagicMock()
        process.poll.return_value = None
        connection = MagicMock()
        connection.recv.return_value = b'\0'
        responses = [subprocess.CompletedProcess([], 0),
                     subprocess.CompletedProcess([], 0, stdout='32100\n')]
        with patch.dict(os.environ, {'DROIDHUB_ADB_HOST': 'adb'}), \
                patch('device.subprocess.run', side_effect=responses), \
                patch('device.subprocess.Popen', return_value=process), \
                patch('device.socket.create_connection', return_value=connection) as connect:
            clipboard.open()
        connect.assert_called_once_with(('adb', 32100), timeout=.3)

    def test_audio_connects_and_cleans_up_only_its_gateway_forward(self):
        audio = Audio('9e24cca')
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('forward', 'tcp:0'):
                return '32101'
            return 'package:/data/sndcpy.apk'
        connection = MagicMock()
        connection.recv.side_effect = [b'PCM!', b'']
        with patch.dict(os.environ, {'DROIDHUB_ADB_HOST': 'adb'}), \
                patch.object(audio, '_command', side_effect=command), \
                patch('audio.socket.create_connection', return_value=connection) as connect:
            with self.assertRaisesRegex(RuntimeError, '断开'):
                audio._session()
        connect.assert_called_once_with(('adb', 32101), timeout=.3)
        self.assertIn(('forward', '--remove', 'tcp:32101'), calls)

    def test_video_connects_to_gateway_and_removes_its_forward(self):
        stream = fake_phone().stream
        server = MagicMock()
        server.poll.return_value = None
        connection = MagicMock()
        connection.recv.return_value = b'H264'
        with patch.dict(os.environ, {'DROIDHUB_ADB_HOST': 'adb'}), \
                patch('app.run_command', return_value='') as command, \
                patch('app.socket.create_connection', return_value=connection) as connect, \
                patch('app.subprocess.Popen', side_effect=[server, OSError('decoder spawn failed')]):
            with self.assertRaisesRegex(OSError, 'decoder spawn'):
                stream._session(stream.phone.touch)
        connect.assert_called_once_with(('adb', stream.port), timeout=.3)
        self.assertEqual(command.call_args.args[0][-3:], ['forward', '--remove', 'tcp:%d' % stream.port])


class DiscoveryAndShutdownTests(unittest.TestCase):
    def test_missing_gateway_keeps_configured_phones(self):
        fleet = app.Fleet()
        with patch.object(app, 'ALLOWED_SERIALS', frozenset({'9e24cca', 'b02683d'})), \
                patch.dict(os.environ, {'ADB_SERVER_SOCKET': 'tcp:adb:5037'}), \
                patch('app.subprocess.run', side_effect=subprocess.TimeoutExpired('adb devices', 10)):
            with self.assertRaises(subprocess.TimeoutExpired):
                fleet._discover()
        self.assertEqual({p.serial for p in fleet.all()}, {'9e24cca', 'b02683d'})

    def test_gateway_cannot_add_unconfigured_devices(self):
        fleet = app.Fleet()
        response = subprocess.CompletedProcess([], 0, stdout='List of devices attached\nother\tdevice\n')
        with patch.object(app, 'ALLOWED_SERIALS', frozenset({'9e24cca', 'b02683d'})), \
                patch.dict(os.environ, {'ADB_SERVER_SOCKET': 'tcp:adb:5037'}), \
                patch('app.subprocess.run', return_value=response):
            fleet._discover()
        self.assertEqual({p.serial for p in fleet.all()}, {'9e24cca', 'b02683d'})

    def test_shutdown_budget_includes_stuck_cleanup_and_other_phone_still_closes(self):
        fleet = app.Fleet()
        first, second = fake_phone(), fake_phone()
        first.serial, second.serial = '9e24cca', 'b02683d'
        fleet.phones = {first.serial: first, second.serial: second}
        entered, release = threading.Event(), threading.Event()
        def slow_close():
            entered.set()
            release.wait(2)
        first.device.close = slow_close
        second.device.close = MagicMock()
        first.touch.close = MagicMock()
        second.touch.close = MagicMock()
        started = time.monotonic()
        try:
            fleet.close(timeout=.1)
            self.assertLess(time.monotonic() - started, .4)
            self.assertTrue(entered.is_set())
            second.device.close.assert_called_once()
            second.touch.close.assert_called_once()
            self.assertTrue(all(p.stop.is_set() for p in fleet.all()))
        finally:
            release.set()


class EntryTests(unittest.TestCase):
    def test_entry_requires_remote_server_and_cannot_start_a_local_one(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Path(directory) / 'adb'
            marker = Path(directory) / 'unexpected-adb-call'
            adb.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
            adb.chmod(0o755)
            env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'])
            env.pop('ADB_SERVER_SOCKET', None)
            result = subprocess.run(['sh', str(Path(__file__).with_name('entry.sh'))], env=env,
                                    capture_output=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())


class ComposeTests(unittest.TestCase):
    def test_adb_image_is_pinned_to_adb_version_not_release(self):
        # 升级只换网页镜像；ADB 容器的镜像和配置不变，compose 就不会重建它，USB 手机不用重新认领
        compose = (Path(__file__).with_name('compose.yaml')).read_text()
        adb = compose.split('\n  adb:\n', 1)[1].split('\n  net:\n', 1)[0]
        self.assertIn('image: ghcr.io/xiongyumail/droidhub:adb-37.0.1', adb)
        self.assertNotIn('DROIDHUB_VERSION', adb)
        dockerfile = Path(__file__).with_name('Dockerfile').read_text()
        self.assertIn('ARG ADB_VERSION=37.0.1', dockerfile)

    def test_entrypoint_is_executable(self):
        # 仓库里的文件权限不一定带可执行位，镜像中要显式设置
        dockerfile = Path(__file__).with_name('Dockerfile').read_text()
        self.assertIn('COPY --chmod=755 entry.sh /usr/local/bin/droidhub', dockerfile)


if __name__ == '__main__':
    unittest.main()
