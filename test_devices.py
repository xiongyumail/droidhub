"""设备表、网络设备连接和 scrcpy 输入后端；全部用假数据，不连接设备。"""
import http.client
import socket
import json
import struct
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

import app
from device import ANDROID_KEYS, ControlKeyboard, ControlTouch, Device
from runtime import ConnectionFailure
from test_recovery import phone

TOUCH = struct.Struct('>BBqiiHHHii')
KEY = struct.Struct('>BBiii')


def control_touch(w=768, h=1280, rotation=0):
    t = ControlTouch.__new__(ControlTouch)
    t.adb, t.w, t.h, t.pos, t.pressed, t.rotation = ['fake-adb'], w, h, None, False, rotation
    t.control = MagicMock()
    t.proc = t.control.proc
    return t


def touches(t):
    return [TOUCH.unpack(c.args[0]) for c in t.control.inject.call_args_list]


class ControlTouchTests(unittest.TestCase):
    def test_tap_is_down_then_up_with_finger_pointer(self):
        t = control_touch()
        t.tap(100, 200, hold=0)
        self.assertEqual(touches(t), [(2, 0, -2, 100, 200, 768, 1280, 0xffff, 0, 0),
                                      (2, 1, -2, 100, 200, 768, 1280, 0, 0, 0)])
        self.assertFalse(t.pressed)

    def test_swipe_moves_between_down_and_up(self):
        t = control_touch()
        with patch('touch.pause'):
            t.swipe(10, 1000, 10, 100, secs=0.05)
        actions = [packet[1] for packet in touches(t)]
        self.assertEqual(actions[0], 0)
        self.assertEqual(actions[-1], 1)
        self.assertEqual(set(actions[1:-1]), {2})
        self.assertEqual(touches(t)[-2][3:5], (10, 100))

    def test_rotation_maps_natural_coordinates_to_current_screen(self):
        cases = {0: (100, 200, 768, 1280), 1: (200, 667, 1280, 768),
                 2: (667, 1079, 768, 1280), 3: (1079, 100, 1280, 768)}
        for rotation, expected in cases.items():
            with self.subTest(rotation=rotation):
                t = control_touch(rotation=rotation)
                t.down(100, 200)
                self.assertEqual(touches(t)[0][3:7], expected)

    def test_size_is_rounded_to_multiple_of_eight_and_scaled(self):
        t = control_touch(w=1084, h=2340)
        t.down(1083, 0)
        self.assertEqual(touches(t)[0][3:7], (1079, 0, 1080, 2336))

    def test_lost_channel_closes_and_reports_connection_failure(self):
        t = control_touch()
        t.control.inject.side_effect = ConnectionFailure('closed')
        with self.assertRaises(ConnectionFailure):
            t.tap(1, 1, hold=0)
        t.control.close.assert_called()
        self.assertIsNone(t.proc)

    def test_out_of_range_is_rejected_before_sending(self):
        t = control_touch()
        with self.assertRaises(ValueError):
            t.tap(768, 0)
        t.control.inject.assert_not_called()


class ControlKeyboardTests(unittest.TestCase):
    def test_keys_map_to_android_codes_and_meta(self):
        control = MagicMock()
        keyboard = ControlKeyboard(control)
        with patch('device.time.sleep'):
            keyboard.key('A', ['ctrl', 'ctrl'])
            keyboard.key('ENTER', [])
            keyboard.key('BACK', [])
        self.assertEqual([c.args for c in control.keycode.call_args_list], [(29, 0x3000), (66, 0), (4, 0)])

    def test_keycode_sends_down_and_up(self):
        from device import Clipboard
        c = Clipboard('vm')
        c.proc, c.sock = MagicMock(), MagicMock()
        c.proc.poll.return_value = None
        c.keycode(ANDROID_KEYS['V'], 0x3000)
        self.assertEqual([KEY.unpack(call.args[0]) for call in c.sock.sendall.call_args_list],
                         [(0, 0, 50, 0, 0x3000), (0, 1, 50, 0, 0x3000)])

    def test_scrcpy_device_pastes_through_shared_channel(self):
        d = Device('vm', 'scrcpy')
        self.assertIs(d.keyboard.control, d.clipboard)
        d.clipboard = d.keyboard.control = MagicMock()
        d.clipboard.alive.return_value = True
        with patch('device.time.sleep'):
            d.text('验证码 123', True)
        self.assertEqual([c[0] for c in d.clipboard.mock_calls if c[0] in ('set', 'keycode')],
                         ['set', 'keycode', 'keycode'])
        self.assertEqual([c.args for c in d.clipboard.keycode.call_args_list], [(29, 0x3000), (50, 0x3000)])


class NetworkConnectTests(unittest.TestCase):
    def run_discover(self, devices_output, now=100):
        fleet = app.Fleet()
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[1:] == ['devices']:
                return subprocess.CompletedProcess(command, 0, stdout=devices_output)
            return subprocess.CompletedProcess(command, 0, stdout='connected to vm:5555\n', stderr='')
        with patch.object(app, 'NETWORK_SERIALS', ('vm:5555',)), patch.object(app, 'ALLOWED_SERIALS', frozenset({'vm:5555'})), \
                patch.dict(app.DEVICES, {'vm:5555': {'name': 'VM', 'input': 'scrcpy', 'audio': False, 'connect': True}}), \
                patch('app.subprocess.run', side_effect=run), patch('app.time.monotonic', return_value=now), \
                patch.dict('os.environ', {'ADB_SERVER_SOCKET': 'tcp:adb:5037'}):
            fleet._discover()
            fleet._discover()
        return fleet, [c[1:] for c in calls if c[1:] != ['devices']]

    def test_missing_device_is_connected_once_per_interval(self):
        fleet, calls = self.run_discover('List of devices attached\n')
        self.assertEqual(calls, [['connect', 'vm:5555']])
        self.assertEqual(fleet.phones['vm:5555'].input, 'scrcpy')
        self.assertFalse(fleet.phones['vm:5555'].audio_enabled)

    def test_offline_transport_is_dropped_before_connecting(self):
        _, calls = self.run_discover('List of devices attached\nvm:5555\toffline\n')
        self.assertEqual(calls, [['disconnect', 'vm:5555'], ['connect', 'vm:5555']])

    def test_unresponsive_device_is_reconnected_although_adb_lists_it(self):
        fleet = app.Fleet()
        p = app.Phone('vm:5555', 0)
        p.down_since, p.retry_at = 70, 200
        fleet.phones['vm:5555'] = p
        calls = []
        def run(command, **kwargs):
            calls.append(command[1:])
            if command[1:] == ['devices']:
                return subprocess.CompletedProcess(command, 0, stdout='List of devices attached\nvm:5555\tdevice\n')
            return subprocess.CompletedProcess(command, 0, stdout='connected to vm:5555\n', stderr='')
        with patch.object(app, 'NETWORK_SERIALS', ('vm:5555',)), patch.object(app, 'ALLOWED_SERIALS', frozenset({'vm:5555'})), \
                patch('app.subprocess.run', side_effect=run), patch('app.time.monotonic', return_value=85), \
                patch.dict('os.environ', {'ADB_SERVER_SOCKET': 'tcp:adb:5037'}):
            fleet._discover()   # 才断 15 秒：先不动
            self.assertEqual([c for c in calls if c != ['devices']], [])
            p.down_since = 60   # 断了 25 秒
            fleet._discover()
        self.assertEqual([c for c in calls if c != ['devices']], [['disconnect', 'vm:5555'], ['connect', 'vm:5555']])
        self.assertEqual(p.retry_at, 0)

    def test_connect_timeout_does_not_break_discovery(self):
        fleet = app.Fleet()
        def run(command, **kwargs):
            if command[1:] == ['devices']:
                return subprocess.CompletedProcess(command, 0, stdout='List of devices attached\n')
            raise subprocess.TimeoutExpired(command, 15)
        with patch.object(app, 'NETWORK_SERIALS', ('vm:5555',)), patch.object(app, 'ALLOWED_SERIALS', frozenset({'vm:5555'})), \
                patch('app.subprocess.run', side_effect=run), patch.dict('os.environ', {'ADB_SERVER_SOCKET': 'tcp:adb:5037'}):
            fleet._discover()
        self.assertIn('15 秒', fleet.connect_result['vm:5555'])

    def test_failure_records_when_the_link_went_down(self):
        p = app.Phone('vm:5555', 0)
        with patch('app.time.monotonic', return_value=50):
            p.failure(RuntimeError('a'))
        with patch('app.time.monotonic', return_value=60):
            p.failure(RuntimeError('b'))
        self.assertEqual(p.down_since, 50)

    def test_connected_and_unauthorized_devices_are_left_alone(self):
        for state in ('device', 'unauthorized'):
            with self.subTest(state=state):
                _, calls = self.run_discover('List of devices attached\nvm:5555\t%s\n' % state)
                self.assertEqual(calls, [])


class AudioDisabledTests(unittest.TestCase):
    def test_audio_endpoints_refuse_without_starting_capture(self):
        server = app.HTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        p = phone()
        p.audio_enabled = False
        p.audio = MagicMock()
        fleet = app.Fleet()
        fleet.phones['test'] = p
        with patch.object(app, 'fleet', fleet):
            for path in ('/audio', '/audio/status'):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('GET', path)
                response = connection.getresponse()
                self.assertEqual(response.status, 409, path)
                self.assertIn('没开采音', json.loads(response.read())['detail'])
                connection.close()
        p.audio.join.assert_not_called()
        self.assertFalse(p.status()['audio'])


class SystemKeyTests(unittest.TestCase):
    def test_system_keys_on_both_backends(self):
        from device import CONSUMER_KEYS, Keyboard, key_args
        for key, consumer, code in (('POWER', 0x30, 26), ('VOLUME_UP', 0xE9, 24),
                                    ('VOLUME_DOWN', 0xEA, 25), ('APP_SWITCH', 0x1A2, 187)):
            self.assertEqual(key_args({'key': key.lower()}), (key, []))
            self.assertEqual(CONSUMER_KEYS[key], consumer)
            self.assertEqual(ANDROID_KEYS[key], code)
        with self.assertRaises(ValueError):
            key_args({'key': 'POWER', 'modifiers': ['ctrl']})


class StatusFieldTests(unittest.TestCase):
    def test_density_and_awake(self):
        p = phone()
        p.touch.density = 440
        p.awake = False
        status = p.status()
        self.assertEqual((status['density'], status['awake']), (440, False))

    def test_touch_reads_density(self):
        import touch
        result = subprocess.CompletedProcess([], 0, stdout='Physical size: 1080x2340\nPhysical density: 440\n'
                                                         'Override density: 400\n')
        with patch('touch.subprocess.run', return_value=result):
            t = touch.Touch('P2')
        self.assertEqual((t.w, t.h, t.density), (1080, 2340, 400))

class LeaseTests(unittest.TestCase):
    def test_acquire_renew_conflict_expire_release(self):
        p = phone()
        with patch('app.time.monotonic', return_value=100):
            p.acquire('tygbot', 30)
            p.acquire('tygbot', 30)                      # 同一占用方续期
            with self.assertRaises(app.LeaseHeld) as held:
                p.acquire('web', 30)
            self.assertEqual((held.exception.owner, held.exception.remaining), ('tygbot', 30))
            self.assertFalse(p.release('web'))
        with patch('app.time.monotonic', return_value=131):
            self.assertIsNone(p.holder())                 # 到期自动释放
            p.acquire('web', 10)
            self.assertTrue(p.release('web'))
            self.assertIsNone(p.holder())

    def test_input_checks_lease_after_queueing_but_reads_do_not(self):
        p = phone()
        p.acquire('tygbot', 30)
        with self.assertRaises(app.LeaseHeld):
            p.command(lambda _: 'tapped', owner='web')
        with self.assertRaises(app.LeaseHeld):
            p.command(lambda _: 'tapped', owner=None)
        self.assertEqual(p.command(lambda _: 'tapped', owner='tygbot'), 'tapped')
        self.assertEqual(p.command(lambda _: 'read', require_touch=False), 'read')
        p.finger('up', owner='web')                       # 松手总是放行
        p.touch.up.assert_called()


class LeasePriorityTests(unittest.TestCase):
    def test_higher_priority_takes_over_lower_cannot(self):
        p = phone()
        p.acquire('web', 30, 0)
        p.acquire('tygbot', 60)                           # 默认 5 接管网页
        self.assertEqual(p.holder()[0], 'tygbot')
        with self.assertRaises(app.LeaseHeld):
            p.acquire('web', 30, 0)
        with self.assertRaises(app.LeaseHeld):
            p.acquire('other', 30)                        # 同优先级不能接管
        p.acquire('urgent', 30, 9)
        self.assertEqual(p.holder()[0], 'urgent')


class StreamStopTests(unittest.TestCase):
    def test_stop_ends_active_and_rejects_late_requests(self):
        stops = app.StreamStops()
        event = stops.register('a')
        stops.stop(['a', 'late'])
        self.assertTrue(event.is_set())
        self.assertIsNone(stops.register('late'))
        stops.unregister('a', event)
        with patch('app.time.monotonic', return_value=10 ** 9):
            self.assertIsNotNone(stops.register('late'))   # 过期后不再记着

class StreamLingerTests(unittest.TestCase):
    def test_last_viewer_leaving_keeps_stream_until_linger_ends(self):
        stream = app.Stream(MagicMock(serial='P1'), 27183)
        with patch.object(app.threading, 'Thread'), patch('app.time.monotonic', return_value=100):
            stream.join('a')
            stream.leave('a')
            self.assertFalse(stream.stop.is_set())           # 没立刻关
        with patch('app.time.monotonic', return_value=100 + app.STREAM_LINGER - 1):
            stream.expire()
            self.assertFalse(stream.stop.is_set())
        with patch.object(app.threading, 'Thread'), patch('app.time.monotonic', return_value=200):
            stream.join('b')                                   # 有人回来就不关
        with patch('app.time.monotonic', return_value=10 ** 6):
            stream.expire()
            self.assertFalse(stream.stop.is_set())
            stream.leave('b')
        with patch('app.time.monotonic', return_value=10 ** 6 + app.STREAM_LINGER):
            stream.expire()
            self.assertTrue(stream.stop.is_set())


class LeaseHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = app.HTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.p = phone()
        fleet = app.Fleet()
        fleet.phones['test'] = self.p
        patcher = patch.object(app, 'fleet', fleet)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('POST', path, json.dumps(body or {}), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_lease_blocks_other_inputs_and_shows_in_status(self):
        self.assertEqual(self.post('/lease?owner=tygbot', {'ttl': 20})[0], 200)
        status, data = self.post('/lease?owner=web', {'ttl': 20})
        self.assertEqual((status, data['owner']), (409, 'tygbot'))
        for path in ('/tap?owner=web', '/tap', '/shell?owner=web'):
            status, data = self.post(path, {'x': 1, 'y': 1, 'command': 'input tap 1 1'})
            self.assertEqual((status, data['owner']), (423, 'tygbot'), path)
        self.p.touch.tap.assert_not_called()
        self.assertEqual(self.post('/tap?owner=tygbot', {'x': 1, 'y': 1})[0], 200)
        self.assertEqual(self.p.status()['lease']['owner'], 'tygbot')
        self.assertEqual(self.post('/release?owner=tygbot')[1]['released'], True)
        self.assertEqual(self.post('/tap', {'x': 1, 'y': 1})[0], 200)

    def test_single_frame_is_terminated_by_next_boundary(self):
        # 画面静止只有一帧时也要能立刻显示：帧后面紧跟下一段的段头
        stream = self.p.stream
        stream.thread, stream.frame, stream.seq = MagicMock(), b'JPEG-BYTES', 1
        sock = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5)
        sock.sendall(b'GET /stream?client=one HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        data = b''
        while b'JPEG-BYTES\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n' not in data:
            chunk = sock.recv(4096)
            self.assertTrue(chunk, data)
            data += chunk
        sock.close()
        body = data.split(b'\r\n\r\n', 1)[1]
        self.assertTrue(body.startswith(b'--frame\r\nContent-Type: image/jpeg\r\n\r\nJPEG-BYTES'))

    def test_stream_stop_endpoint(self):
        with patch.object(app, 'stream_stops', app.StreamStops()) as stops:
            self.assertEqual(self.post('/stream/stop', {'clients': ['abc-1']})[0], 200)
            self.assertIn('abc-1', stops.stopped)
            self.assertEqual(self.post('/stream/stop', {'clients': ['bad id']})[0], 400)

    def test_bad_owner_and_ttl(self):
        self.assertEqual(self.post('/lease?owner=a%20b', {'ttl': 5})[0], 400)
        self.assertEqual(self.post('/lease?owner=ok', {'ttl': 301})[0], 400)
        self.assertEqual(self.post('/lease', {'ttl': 5})[0], 400)
        self.assertEqual(self.post('/lease?owner=ok', {'ttl': 5, 'priority': 10})[0], 400)

if __name__ == '__main__':
    unittest.main(verbosity=2)


class UnlockTests(unittest.TestCase):
    LOCKED_ASLEEP = '    isKeyguardShowing=true\n  mWakefulness=Asleep\n'
    UNLOCKED = '    isKeyguardShowing=false\n  mWakefulness=Awake\n'

    def make(self, states, lease=None):
        p = phone()
        if lease:
            p.acquire(lease, 30)
        outputs = iter(states)
        p.shells = []
        def adb(*args, timeout=10):
            p.shells.append(args)
            if 'dismiss-keyguard' in args[-1]:
                return subprocess.CompletedProcess(args, 0, stdout='')
            out = next(outputs)
            if isinstance(out, Exception):
                raise out
            return subprocess.CompletedProcess(args, 0, stdout=out)
        p._adb = adb
        p.pause = MagicMock()
        return p

    def keys(self, p):
        return [c.args[0] for c in p.device.key.call_args_list]

    def test_wakes_and_types_pin_when_locked(self):
        # 解锁时 adbd 可能重启，查状态失败也要接着等
        p = self.make([self.LOCKED_ASLEEP, subprocess.TimeoutExpired('adb', 10), self.UNLOCKED])
        with patch.object(app, 'PINS', {'test': '1234'}):
            self.assertTrue(p.unlock('web'))
        self.assertEqual(self.keys(p), ['POWER', '1', '2', '3', '4', 'ENTER'])
        self.assertIn(('shell', 'wm dismiss-keyguard'), p.shells)

    def test_already_unlocked_types_nothing(self):
        p = self.make([self.UNLOCKED])
        with patch.object(app, 'PINS', {'test': '1234'}):
            self.assertFalse(p.unlock('web'))
        self.assertEqual(self.keys(p), [])

    def test_device_without_pin_is_refused(self):
        p = self.make([])
        with patch.object(app, 'PINS', {}), self.assertRaises(ValueError):
            p.unlock('web')
        self.assertFalse(p.status()['unlock'])

    def test_wrong_pin_disables_further_attempts(self):
        p = self.make([self.LOCKED_ASLEEP] * 30)
        with patch.object(app, 'PINS', {'test': '1234'}), patch('app.time.monotonic', side_effect=range(0, 1000, 3)):
            with self.assertRaises(RuntimeError):
                p.unlock('web')
            p.device.key.reset_mock()
            with self.assertRaises(RuntimeError):
                p.unlock('web')
        self.assertTrue(p.unlock_disabled)
        self.assertEqual(self.keys(p), [])

    def test_does_not_type_into_someone_elses_session(self):
        p = self.make([self.LOCKED_ASLEEP], lease='tygbot')
        with patch.object(app, 'PINS', {'test': '1234'}), self.assertRaises(app.LeaseHeld):
            p.unlock('web')
        self.assertEqual(self.keys(p), [])
        self.assertFalse(p.unlock_disabled)

