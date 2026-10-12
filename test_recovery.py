"""审查问题回归：模拟手机及隔离 HTTP，不向真实设备发送输入。"""
import http.client
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app
import runtime
import touch
from audio import Audio
from device import Device, launch_args


def phone(serial='test'):
    p = app.Phone(serial, 27183)
    p.touch = MagicMock(w=1080, h=2340)
    p.touch.proc.poll.return_value = None
    p.touch.density = None
    p.touch.tap.return_value = None
    p.ensure = MagicMock(return_value=True)
    p.error = None
    p.device = MagicMock()
    return p


def touchscreen():
    t = touch.Touch.__new__(touch.Touch)
    t.adb, t.w, t.h, t.proc, t.pos = ['fake-adb'], 1080, 2340, None, None
    t._send = MagicMock()
    return t


def raw_frame(width, height, pixels, header=16, fmt=1):
    extra = struct.pack('<I', 0) if header == 16 else b''
    return struct.pack('<3I', width, height, fmt) + extra + pixels


def png_pixels(png):
    """只解析本模块写出的 PNG：单个 IDAT、每行过滤类型 0。"""
    assert png.startswith(b'\x89PNG\r\n\x1a\n')
    width, height, depth, color = struct.unpack('>2I2B', png[16:26])
    length = struct.unpack('>I', png[33:37])[0]
    assert png[37:41] == b'IDAT' and (depth, color) == (8, 6)
    rows = zlib.decompress(png[41:41 + length])
    stride = width * 4 + 1
    assert all(rows[i * stride] == 0 for i in range(height))
    return width, height, b''.join(rows[i * stride + 1:(i + 1) * stride] for i in range(height))


class ScreenshotTests(unittest.TestCase):
    def capture(self, *outputs):
        results = [subprocess.CompletedProcess([], 0, stdout=o, stderr=b'') for o in outputs]
        with patch('runtime.subprocess.run', side_effect=results) as run:
            png = runtime.capture_png(['adb', '-s', 'P2'])
        return png, [c.args[0][4:] for c in run.call_args_list]

    def test_raw_frame_is_encoded_on_server_with_one_adb_call(self):
        pixels = bytes(range(256)) * 6   # 4x96 RGBA，含非 255 的 alpha
        for header in (16, 12):
            png, calls = self.capture(raw_frame(4, 96, pixels, header))
            self.assertEqual(calls, [['screencap']])
            self.assertEqual(png_pixels(png), (4, 96, pixels))

    def test_unknown_raw_format_falls_back_to_phone_png(self):
        phone_png = b'\x89PNG\r\n\x1a\nfrom-phone'
        for raw in (raw_frame(2, 2, b'\0' * 8, fmt=4), raw_frame(2, 2, b'\0' * 15), b'short'):
            png, calls = self.capture(raw, phone_png)
            self.assertEqual(png, phone_png)
            self.assertEqual(calls, [['screencap'], ['screencap', '-p']])


class TouchRecoveryTests(unittest.TestCase):
    def test_screenshot_reads_once_and_reports_offline_without_resetting(self):
        result = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'error: device offline')
        with patch('runtime.subprocess.run', return_value=result) as run:
            with self.assertRaisesRegex(runtime.ConnectionFailure, 'device offline'):
                Device('P2').shot()
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [['adb', '-s', 'P2', 'exec-out', 'screencap']])

    def test_offline_phone_reports_adb_error_without_resetting_it(self):
        result = subprocess.CompletedProcess([], 1, stdout='', stderr='error: device offline')
        with patch('touch.subprocess.run', return_value=result) as run:
            with self.assertRaisesRegex(runtime.ConnectionFailure, 'device offline'):
                touch.Touch('P2')
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [['adb', '-s', 'P2', 'shell', 'wm size; wm density']])

    def test_invalid_swipe_endpoint_cannot_send_down(self):
        t = touchscreen()
        with self.assertRaises(ValueError):
            t.swipe(100, 100, 2000, 100, 0)
        t._send.assert_not_called()
        self.assertIsNone(t.pos)

    def test_interrupted_hold_sends_up(self):
        t = touchscreen()
        with patch('touch.time.sleep', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                t.tap(100, 100)
        self.assertEqual([c.args[0]['report'][1] for c in t._send.call_args_list], [3, 0])
        self.assertIsNone(t.pos)

    def test_partial_swipe_failure_releases_last_valid_position(self):
        t = touchscreen()
        t._send.side_effect = [None, RuntimeError('write failed'), None]
        with self.assertRaises(RuntimeError):
            t.swipe(100, 100, 200, 200, 0)
        self.assertEqual(t._send.call_args_list[-1].args[0]['report'][1], 0)
        self.assertIsNone(t.pos)

    def test_registration_exit_is_failure_and_reaped(self):
        t = touchscreen()
        del t._send
        proc = MagicMock()
        proc.poll.side_effect = [None, 1, 1]
        with patch('touch.subprocess.Popen', return_value=proc), patch('touch.time.sleep'), \
                patch('touch.write_pipe'):
            with self.assertRaises(runtime.ConnectionFailure):
                t.open()
        proc.stdin.close.assert_called_once()
        proc.wait.assert_called_once()
        self.assertIsNone(t.proc)

    def test_registration_write_failure_closes_process(self):
        t = touchscreen()
        del t._send
        proc = MagicMock()
        proc.poll.return_value = None
        with patch('touch.subprocess.Popen', return_value=proc), \
                patch('touch.write_pipe', side_effect=runtime.ConnectionFailure('failed')):
            with self.assertRaises(runtime.ConnectionFailure):
                t.open()
        proc.stdin.close.assert_called_once()
        proc.wait.assert_called_once()
        self.assertIsNone(t.proc)

    def test_close_reaps_after_kill(self):
        t = touchscreen()
        proc = t.proc = MagicMock()
        proc.poll.return_value = None
        t.close()
        proc.kill.assert_called_once()
        proc.wait.assert_called_once_with(timeout=2)


class PhoneRecoveryTests(unittest.TestCase):
    def test_screenshot_does_not_require_or_rebuild_touch(self):
        p = app.Phone('P2', 27184)
        p.device = MagicMock()
        p.device.shot.return_value = b'\x89PNGread-only'
        p.ensure = MagicMock(side_effect=AssertionError('must not rebuild Touch'))
        p.failure('Touch unavailable')
        self.assertEqual(p.shot(), b'\x89PNGread-only')
        self.assertFalse(p.status()['connected'])
        p.ensure.assert_not_called()
        p.device.shot.assert_called_once()

    def test_cached_transport_state_cannot_hide_unresponsive_shell(self):
        p = phone()
        p.ensure = MagicMock()
        p._adb = MagicMock(side_effect=subprocess.TimeoutExpired('adb shell', 3))
        p.check()
        self.assertTrue(p.failed.is_set())
        self.assertFalse(p.status()['connected'])
        self.assertEqual(p._adb.call_args.args, ('shell', 'dumpsys input; dumpsys power | grep mWakefulness='))
        p.ensure.assert_called_once()

    def test_recovery_rebuilds_app_channel_without_resetting_transport(self):
        p = app.Phone('P2', 27184)
        p.device = MagicMock()
        p.device.close.side_effect = OSError('cleanup failed')
        p._adb = MagicMock()
        t = MagicMock(w=1080, h=2340)
        t.proc.poll.return_value = None
        with patch.object(app, 'Touch', return_value=t), patch('app.time.monotonic', return_value=100):
            p.failure('timeout')
            self.assertTrue(p.ensure())
            p.failure('timeout again')
            self.assertTrue(p.ensure())
            self.assertTrue(p.status()['connected'])
        p._adb.assert_not_called()
        self.assertEqual(t.open.call_count, 2)

    def test_failed_recovery_waits_before_retry_and_then_returns(self):
        p = app.Phone('P2', 27184)
        p.device = MagicMock()
        p._adb = MagicMock()
        t = MagicMock(w=1080, h=2340)
        t.proc.poll.return_value = None
        t.open.side_effect = RuntimeError('offline')
        with patch.object(app, 'Touch', return_value=t) as create, \
                patch('app.time.monotonic', return_value=100) as now:
            self.assertFalse(p.ensure())
            self.assertFalse(p.status()['connected'])
            now.return_value = 115
            self.assertFalse(p.ensure())
            self.assertEqual(create.call_count, 1)
            now.return_value = 130
            self.assertFalse(p.ensure())
            self.assertEqual(create.call_count, 2)
            now.return_value = 160
            t.open.side_effect = None
            self.assertTrue(p.ensure())
            self.assertTrue(p.status()['connected'])
        p._adb.assert_not_called()

    def test_failed_connection_closes_unpublished_touch(self):
        p = app.Phone('P2', 27184)
        p.device = MagicMock()
        t = MagicMock()
        t.open.side_effect = RuntimeError('failed')
        with patch.object(app, 'Touch', return_value=t):
            self.assertFalse(p._connect())
        t.close.assert_called_once()

    def test_dead_hid_is_not_connected(self):
        p = phone()
        p.touch.proc.poll.return_value = 1
        self.assertFalse(p.status()['connected'])

    def test_closed_touch_channel_reports_disconnected_and_reconnects(self):
        # Touch 写失败会先 close() 置空 proc，再由上层设置 failed；之间的状态查询和重连不能抛错。
        p = app.Phone('test', 27183)
        p.touch, p.error = touchscreen(), None
        self.assertFalse(p.status()['connected'])
        self.assertFalse(p._alive())
        with patch.object(p, '_connect', return_value=True) as connect:
            self.assertTrue(p.ensure())
        connect.assert_called_once()

    def test_batch_does_not_block_other_phone_release(self):
        p1, p2 = phone('P1'), phone('P2')
        entered, gate, released = threading.Event(), threading.Event(), threading.Event()
        p2.pressed_at = time.time() - 5
        p2.touch.up.side_effect = released.set
        p2.check = MagicMock()
        def step(p):
            entered.set()
            gate.wait(2)
        batch = threading.Thread(target=lambda: p1.run([step]))
        watcher = threading.Thread(target=p2.watch)
        batch.start()
        try:
            self.assertTrue(entered.wait(1))
            watcher.start()
            self.assertTrue(released.wait(.5))
            self.assertTrue(batch.is_alive())
        finally:
            gate.set()
            p2.stop.set()
            batch.join(2)
            watcher.join(2)

    def test_watch_survives_connection_exception(self):
        p = phone()
        p._adb = MagicMock(return_value=SimpleNamespace(returncode=0, stdout='Viewport INTERNAL: orientation=0,'))
        done = threading.Event()
        def ensure():
            if p.ensure.call_count == 1:
                raise OSError('cleanup failed')
            done.set()
            return True
        p.ensure.side_effect = ensure
        with patch.object(app, 'CHECK_EVERY', 0):
            thread = threading.Thread(target=p.watch)
            thread.start()
            try:
                self.assertTrue(done.wait(2))
                self.assertTrue(thread.is_alive())
            finally:
                p.stop.set()
                thread.join(2)

    def test_cancelled_queued_request_does_not_execute(self):
        p = phone()
        action = MagicMock()
        p.lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, '取消'):
                p.command(action, cancel=lambda: True)
        finally:
            p.lock.release()
        action.assert_not_called()

    def test_health_detects_dead_discovery_watchdog(self):
        f = app.Fleet()
        f.phones['test'] = phone()
        f.heartbeat = time.monotonic() - 61
        self.assertFalse(f.healthy())


class StreamRecoveryTests(unittest.TestCase):
    def setUp(self):
        # 这里测断流和停止；没人看后立刻关，不等延迟关闭
        patcher = patch.object(app, 'STREAM_LINGER', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_static_screen_is_streaming_until_transport_fails(self):
        stream = app.Stream(phone(), 27183)
        stream.thread = MagicMock()
        stream._publish(b'static JPEG')
        stream.frame_at -= 30
        self.assertEqual(stream.status()['state'], 'streaming')

    def test_wait_does_not_replay_last_frame(self):
        stream = app.Stream(phone(), 27183)
        stream._publish(b'old JPEG')
        self.assertEqual(stream.wait(stream.seq, .01), (stream.seq, None))

    def test_spawn_failure_removes_owned_forward(self):
        stream = app.Stream(phone(), 27183)
        with patch('app.run_command', return_value='') as run, patch('app.subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError):
                stream._session(SimpleNamespace(adb=['fake-adb'], w=1080, h=2340))
        self.assertEqual(run.call_args.args[0][-3:], ['forward', '--remove', 'tcp:27183'])

    def test_stop_wakes_silent_decoder(self):
        stream = app.Stream(phone(), 27183)
        stream.viewers = 1
        stream._feed = lambda *args: None
        reader, writer = os.pipe()
        ff = MagicMock()
        ff.stdout = os.fdopen(reader, 'rb')
        ff.poll.return_value = None
        server = MagicMock()
        server.poll.return_value = None
        sock = MagicMock()
        sock.recv.return_value = b'H264'
        entered = threading.Event()
        original_select = app.select.select
        def select(*args):
            entered.set()
            return original_select(*args)
        errors = []
        def capture():
            try:
                stream._session(SimpleNamespace(adb=['fake-adb'], w=1080, h=2340))
            except Exception as e:
                errors.append(e)
        with patch('app.run_command', return_value=''), patch('app.subprocess.Popen', side_effect=[server, ff]), \
                patch('app.socket.create_connection', return_value=sock), patch('app.select.select', side_effect=select):
            thread = threading.Thread(target=capture)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                stream.leave()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                ff.kill.assert_called_once()
                server.kill.assert_called_once()
                self.assertEqual(errors, [])
            finally:
                stream.close()
                os.close(writer)
                thread.join(2)
                if not ff.stdout.closed:
                    ff.stdout.close()


class HttpRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.HTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        linger = patch.object(app, 'STREAM_LINGER', 0)   # 测断开即释放，不等延迟关闭
        linger.start()
        self.addCleanup(linger.stop)
        self.p = phone()
        fleet = app.Fleet()
        fleet.phones['test'] = self.p
        patcher = patch.object(app, 'fleet', fleet)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, body=None, headers=None, method='POST'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            default = {'Content-Type': 'application/json', 'Connection': 'close'}
            default.update(headers or {})
            connection.request(method, path, json.dumps(body) if body is not None else None, default)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_cross_origin_plaintext_cannot_control_phone(self):
        code, _ = self.request('/tap', {'x': 100, 'y': 100}, {'Origin': 'http://foreign.example', 'Content-Type': 'text/plain'})
        self.assertEqual(code, 403)
        self.p.touch.tap.assert_not_called()

    def test_same_origin_json_still_works(self):
        code, _ = self.request('/tap', {'x': 100, 'y': 100}, {'Origin': 'http://127.0.0.1:%d' % self.server.server_port})
        self.assertEqual(code, 200)

    def test_plaintext_is_rejected_even_without_origin(self):
        code, _ = self.request('/tap', {'x': 100, 'y': 100}, {'Content-Type': 'text/plain'})
        self.assertEqual(code, 400)
        self.p.touch.tap.assert_not_called()

    def test_dns_rebinding_host_is_rejected(self):
        code, _ = self.request('/tap', {'x': 100, 'y': 100}, {'Host': 'foreign.example'})
        self.assertEqual(code, 403)
        self.p.touch.tap.assert_not_called()

    def test_cross_site_get_cannot_start_audio(self):
        code, _ = self.request('/audio', headers={'Sec-Fetch-Site': 'cross-site'}, method='GET')
        self.assertEqual(code, 403)
        self.assertIsNone(self.p.audio.thread)

    def test_same_site_get_from_other_port_cannot_start_stream(self):
        code, _ = self.request('/stream', headers={'Sec-Fetch-Site': 'same-site'}, method='GET')
        self.assertEqual(code, 403)
        self.assertIsNone(self.p.stream.thread)

    def test_page_own_get_is_allowed(self):
        code, _ = self.request('/phones', headers={'Sec-Fetch-Site': 'same-origin'}, method='GET')
        self.assertEqual(code, 200)

    def test_batch_bounds_are_validated_before_first_action(self):
        code, _ = self.request('/run', {'steps': [{'tap': [100, 100]}, {'tap': [9999, 100]}]})
        self.assertEqual(code, 400)
        self.p.touch.tap.assert_not_called()

    def test_nonfinite_coordinate_returns_json_error(self):
        code, body = self.request('/tap', {'x': float('inf'), 'y': 0})
        self.assertEqual(code, 400)
        self.assertFalse(body['ok'])
        self.p.touch.tap.assert_not_called()

    def test_disconnect_before_first_video_frame_releases_subscription(self):
        stream = self.p.stream
        stream._session = lambda t: stream.stop.wait(2)
        self._disconnect_before_data('/stream', stream)

    def test_disconnect_before_first_audio_data_releases_subscription(self):
        audio = self.p.audio
        audio._session = lambda: audio.stop.wait(2)
        self._disconnect_before_data('/audio', audio)

    def _disconnect_before_data(self, path, resource):
        client = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2)
        client.sendall(('GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n' % path).encode())
        deadline = time.monotonic() + 1
        while not resource.viewers and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(resource.viewers, 1)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        client.close()
        with resource.cond:
            self.assertTrue(resource.cond.wait_for(lambda: resource.viewers == 0 and resource.thread is None, 1))

    def test_body_trickle_cannot_extend_deadline(self):
        with patch.object(app, 'READ_TIMEOUT', .2):
            client = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2)
            client.sendall(b'POST /tap HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n{')
            stop = threading.Event()
            def trickle():
                while not stop.wait(.03):
                    try:
                        client.sendall(b' ')
                    except OSError:
                        return
            writer = threading.Thread(target=trickle)
            writer.start()
            try:
                self.assertIn(b'408', client.recv(8192))
                self.p.touch.tap.assert_not_called()
            finally:
                stop.set()
                writer.join(1)
                client.close()

    def test_max_clients_rejects_excess_connections(self):
        server = app.HTTPServer(('127.0.0.1', 0), app.Handler, max_clients=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        first = socket.create_connection(server.server_address, timeout=2)
        second = None
        try:
            deadline = time.monotonic() + 1
            while server.slots._value and time.monotonic() < deadline:
                time.sleep(.01)
            second = socket.create_connection(server.server_address, timeout=2)
            self.assertIn(b'503', second.recv(8192))
        finally:
            first.close()
            if second:
                second.close()
            server.shutdown()
            server.server_close()
            thread.join(2)


class AudioAndLaunchRecoveryTests(unittest.TestCase):
    def test_audio_start_timeout_still_stops_helper(self):
        audio = Audio('test')
        commands = []
        def command(*args, **kwargs):
            commands.append(args)
            if args[:3] == ('shell', 'pm', 'path'):
                return 'package:/sndcpy.apk'
            if args[:2] == ('forward', 'tcp:0'):
                return '34567'
            if args[:3] == ('shell', 'am', 'start'):
                raise subprocess.TimeoutExpired('start', 5)
            return ''
        with patch.object(audio, '_command', side_effect=command):
            with self.assertRaises(subprocess.TimeoutExpired):
                audio._session()
        self.assertEqual(sum(c[:3] == ('shell', 'am', 'force-stop') for c in commands), 2)
        self.assertIn(('forward', '--remove', 'tcp:34567'), commands)

    def test_package_launch_resolves_explicit_component(self):
        device = Device('test')
        with patch.object(device, 'shell', side_effect=['isDefault=false\ncom.example/.Main', 'Status: ok']) as shell:
            device.launch(launch_args({'package': 'com.example'}))
        self.assertEqual(shell.call_args.args[0], ['am', 'start', '-W', '-n', 'com.example/.Main'])

    def test_invalid_resolution_never_launches_another_package(self):
        device = Device('test')
        with patch.object(device, 'shell', return_value='com.other/.Main') as shell:
            with self.assertRaises(RuntimeError):
                device.launch(launch_args({'package': 'com.example'}))
        self.assertEqual(shell.call_count, 1)

    def test_cancel_kills_pending_subprocess_without_replay(self):
        stop = threading.Event()
        timer = threading.Timer(.1, stop.set)
        timer.start()
        original = subprocess.Popen
        with patch('runtime.subprocess.Popen', wraps=original) as popen:
            with self.assertRaises(InterruptedError):
                runtime.run_command([sys.executable, '-c', 'import time; time.sleep(5)'], 5, stop)
        timer.join(1)
        self.assertEqual(popen.call_count, 1)


if __name__ == '__main__':
    unittest.main()
