#!/usr/bin/env python3
"""USB 热插拔恢复回归；不调用真实 adb。"""
import subprocess
import json
import http.client
import threading
import io
import shlex
import struct
import unittest
from unittest.mock import patch, MagicMock

import app
from device import Clipboard, Device, Keyboard, launch_args
from audio import Audio


class DiscoveryTests(unittest.TestCase):
    """设备表里的手机不在 adb 列表里也一直显示；发现只跑 adb devices，不重启 ADB。"""

    def test_missing_usb_phones_remain_visible_without_restarting_adb(self):
        fleet = app.Fleet()
        devices = subprocess.CompletedProcess([], 0, stdout='List of devices attached\n')
        with patch.object(app.subprocess, 'run', return_value=devices) as run, \
                patch.object(app, 'ALLOWED_SERIALS', frozenset({'9e24cca', 'b02683d'})):
            for _ in range(8):
                fleet._discover()
            self.assertEqual([call.args[0] for call in run.call_args_list], [['adb', 'devices']] * 8)
            self.assertEqual({p.serial for p in fleet.all()}, {'9e24cca', 'b02683d'})
            self.assertTrue(all(not p.status()['connected'] for p in fleet.all()))

    def test_returned_phone_is_discovered_without_restarting_adb(self):
        fleet = app.Fleet()
        devices = subprocess.CompletedProcess([], 0, stdout='List of devices attached\n')
        with patch.object(app.subprocess, 'run', return_value=devices) as run, \
                patch.object(app, 'ALLOWED_SERIALS', frozenset({'9e24cca', 'b02683d'})):
            for _ in range(6):
                fleet._discover()
            previous = fleet.get('9e24cca')
            devices.stdout += '9e24cca\tdevice\nb02683d\tdevice\n'
            fleet._discover()
            self.assertIs(fleet.get('9e24cca'), previous)
            self.assertEqual([call.args[0] for call in run.call_args_list], [['adb', 'devices']] * 7)
            self.assertEqual({phone.serial for phone in fleet.all()}, {'9e24cca', 'b02683d'})

    def test_unplugged_phone_does_not_restart_other_phones_adb(self):
        fleet = app.Fleet()
        fleet._add('b02683d')
        devices = subprocess.CompletedProcess([], 0, stdout='List of devices attached\n9e24cca\tdevice\n')
        with patch.object(app.subprocess, 'run', return_value=devices) as run, \
                patch.object(app, 'ALLOWED_SERIALS', frozenset({'9e24cca'})):
            for _ in range(4):
                fleet._discover()
            self.assertTrue(all(call.args[0] == ['adb', 'devices'] for call in run.call_args_list))

    def test_offline_phone_never_restarts_healthy_phones_adb(self):
        fleet = app.Fleet()
        devices = subprocess.CompletedProcess([], 0, stdout='List of devices attached\n9e24cca\tdevice\nb02683d\toffline\n')
        with patch.object(app.subprocess, 'run', return_value=devices) as run, \
                patch.object(app, 'ALLOWED_SERIALS', frozenset({'9e24cca', 'b02683d'})):
            for _ in range(4):
                fleet._discover()
        self.assertTrue(all(call.args[0] == ['adb', 'devices'] for call in run.call_args_list))
        self.assertEqual({p.serial for p in fleet.all()}, {'9e24cca', 'b02683d'})


class StreamTests(unittest.TestCase):
    def test_first_viewer_waits_for_a_frame_instead_of_spinning(self):
        stream = app.Stream(MagicMock(serial='P1'), 27183)
        returned = threading.Event()
        result = []

        def wait():
            result.append(stream.wait(-1, timeout=1))
            returned.set()

        worker = threading.Thread(target=wait)
        worker.start()
        try:
            self.assertFalse(returned.wait(0.02))
            stream._publish(b'new JPEG')
            self.assertTrue(returned.wait(1))
            self.assertEqual(result, [(1, b'new JPEG')])
        finally:
            worker.join(2)

    def test_stopping_clears_old_frame_and_allows_a_new_viewer_to_start(self):
        stream = app.Stream(MagicMock(serial='P1'), 27183)
        stream.thread = MagicMock()
        stream.frame = b'old JPEG'
        stream._run()
        self.assertIsNone(stream.thread)
        self.assertIsNone(stream.frame)
        with patch.object(app.threading, 'Thread') as thread:
            stream.join()
            thread.return_value.start.assert_called_once()
        self.assertEqual(stream.viewers, 1)


class AudioTests(unittest.TestCase):
    def test_shared_capture_new_subscriber_does_not_replay_old_sound(self):
        audio = Audio('P1')
        with patch('audio.threading.Thread') as thread:
            epoch, first = audio.join()
            audio._publish(b'old0')
            other_epoch, second = audio.join()
            audio._publish(b'new0')
            self.assertEqual(audio.wait(epoch, first), (2, b'old0new0'))
            self.assertEqual(audio.wait(other_epoch, second), (2, b'new0'))
            thread.return_value.start.assert_called_once()
            audio.leave(epoch)
            self.assertFalse(audio.stop.is_set())
            audio.leave(other_epoch)
            self.assertTrue(audio.stop.is_set())
            self.assertEqual(audio.viewers, 0)

    def test_slow_client_only_receives_recent_audio(self):
        audio = Audio('P1')
        with patch('audio.threading.Thread'), patch('audio.time.monotonic') as clock:
            clock.return_value = 10
            epoch, seq = audio.join()
            audio._publish(b'old0')
            clock.return_value = 10.5
            audio._publish(b'current0')
            self.assertEqual(audio.wait(epoch, seq), (2, b'current0'))
            self.assertEqual(len(audio.chunks), 1)
            audio.leave(epoch)

    def test_partial_tcp_sample_does_not_misalign_new_subscriber(self):
        audio = Audio('P1')
        with patch('audio.threading.Thread'):
            epoch, first = audio.join()
            audio._publish(b'old0abc')
            other_epoch, second = audio.join()
            audio._publish(b'dnew0xy')
            self.assertEqual(audio.wait(epoch, first), (2, b'old0abcdnew0'))
            self.assertEqual(audio.wait(other_epoch, second), (2, b'abcdnew0'))
            self.assertEqual(audio.partial, b'xy')
            audio.leave(epoch)
            audio.leave(other_epoch)

    def test_missing_component_fails_without_leaking_a_subscription(self):
        audio = Audio('P1')
        with patch.object(audio, '_command', return_value=''):
            epoch, seq = audio.join()
            try:
                with self.assertRaisesRegex(RuntimeError, '尚未安装'):
                    audio.wait(epoch, seq)
            finally:
                audio.leave(epoch)
            with audio.cond:
                self.assertTrue(audio.cond.wait_for(lambda: audio.thread is None, 1))
        self.assertEqual(audio.viewers, 0)

    def test_disconnect_closes_only_its_audio_forward_and_service(self):
        audio = Audio('P1')
        connection = MagicMock()
        connection.recv.side_effect = [b'PCM', b'']

        def command(*args, **kwargs):
            return {'pm': 'package:/data/app/sndcpy.apk', 'forward': '34567'}.get(args[1] if args[0] == 'shell' else args[0], '')

        with patch.object(audio, '_command', side_effect=command) as adb, \
                patch('audio.socket.create_connection', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, '已断开'):
                audio._session()
        connection.close.assert_called_once()
        self.assertIn(('forward', '--remove', 'tcp:34567'), [call.args for call in adb.call_args_list])
        self.assertEqual(sum(call.args == ('shell', 'am', 'force-stop', 'com.rom1v.sndcpy')
                             for call in adb.call_args_list), 2)


class AtomicApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        fleet = app.Fleet()
        fleet._add('9e24cca')
        fleet._add('b02683d')
        self.phones = fleet.all()
        for p in self.phones:
            p.ensure = MagicMock(return_value=True)
            p.touch = MagicMock(w=1080, h=2340)
            p.error = None
            p.touch.tap.return_value = None
            p.device = MagicMock()
            p.device.ui.return_value = {'rotation': 0, 'nodes': [{'text': 'P1', 'resource_id': 'id/name'}]}
            p.device.prop.return_value = 'P2'
            p.device.text.return_value = {'characters': 2}
            p.device.key.return_value = {'key': 'A'}
            p.device.launch.return_value = {'output': 'Starting: Intent'}
        self.fleet_patch = patch.object(app, 'fleet', fleet)
        self.names_patch = patch.object(app, 'PHONE_NAMES', {'9e24cca': 'P1', 'b02683d': 'P2'})
        self.fleet_patch.start()
        self.names_patch.start()
        self.addCleanup(self.fleet_patch.stop)
        self.addCleanup(self.names_patch.stop)

    def request(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            c.request(method, path, json.dumps(body) if body is not None else None,
                      {'Content-Type': 'application/json', 'Connection': 'close'})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()

    def test_api_discovery_lists_primitives(self):
        code, body = self.request('GET', '/api')
        self.assertEqual(code, 200)
        self.assertIn('/text', json.dumps(body))
        self.assertIn('/ui', json.dumps(body))
        self.assertIn('/audio', body['get'])

    def test_audio_status_is_read_only_and_routes_by_phone_name(self):
        code, body = self.request('GET', '/audio/status?phone=P2')
        self.assertEqual(code, 200)
        self.assertEqual(body['serial'], 'b02683d')
        self.assertEqual(body['state'], 'idle')
        self.assertIsNone(self.phones[1].audio.thread)

    def test_audio_start_failure_is_json_before_stream_headers(self):
        audio = self.phones[1].audio
        with patch.object(audio, '_command', return_value=''):
            code, body = self.request('GET', '/audio?phone=P2')
        self.assertEqual(code, 503)
        self.assertIn('尚未安装', body['detail'])
        with audio.cond:
            self.assertTrue(audio.cond.wait_for(lambda: audio.viewers == 0, 1))

    def test_audio_stream_metadata_samples_and_disconnect_cleanup(self):
        audio = self.phones[1].audio
        sample = b'\x00\x40\x00\xc0' * 64

        def capture():
            while not audio.stop.is_set():
                audio._publish(sample)
                audio.stop.wait(.01)

        with patch.object(audio, '_session', side_effect=capture):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            response = None
            try:
                connection.request('GET', '/audio?phone=P2')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('X-Audio-Format'), 's16le')
                self.assertEqual(response.getheader('X-Audio-Sample-Rate'), '48000')
                self.assertEqual(response.getheader('X-Audio-Channels'), '2')
                self.assertEqual(response.read(8), sample[:8])
            finally:
                if response:
                    response.close()
                connection.close()
            with audio.cond:
                self.assertTrue(audio.cond.wait_for(lambda: audio.viewers == 0 and audio.thread is None, 2))

    def test_name_selector_routes_text_to_the_correct_phone(self):
        code, body = self.request('POST', '/text?phone=P2', {'text': 'P2', 'replace': True})
        self.assertEqual(code, 200, body)
        self.phones[1].device.text.assert_called_once_with('P2', True)
        self.phones[0].device.text.assert_not_called()

    def test_reads_ui_and_a_system_property(self):
        code, body = self.request('GET', '/ui?serial=9e24cca')
        self.assertEqual(code, 200, body)
        self.assertEqual(body['nodes'][0]['text'], 'P1')
        code, body = self.request('GET', '/prop?phone=P2&name=persist.sys.device_name')
        self.assertEqual(code, 200, body)
        self.assertEqual(body['value'], 'P2')
        self.phones[1].device.prop.assert_called_once_with('persist.sys.device_name')

    def test_reads_do_not_require_a_working_touch_channel(self):
        phone = self.phones[1]
        phone.touch = None
        phone.failure('Touch unavailable')
        phone.ensure = MagicMock(side_effect=AssertionError('reads must not rebuild Touch'))
        for path in ('/prop?phone=P2&name=persist.sys.device_name', '/ui?phone=P2'):
            with self.subTest(path=path):
                code, body = self.request('GET', path)
                self.assertEqual(code, 200, body)
        phone.ensure.assert_not_called()
        self.assertFalse(phone.status()['connected'])

    def test_controls_still_require_a_working_touch_channel(self):
        phone = self.phones[1]
        phone.touch = None
        phone.failure('Touch unavailable')
        phone.ensure = MagicMock(return_value=False)
        code, body = self.request('POST', '/tap?phone=P2', {'x': 100, 'y': 100})
        self.assertEqual(code, 503, body)
        phone.ensure.assert_called_once()

    def test_keyboard_shortcut_and_generic_launch(self):
        code, body = self.request('POST', '/key?phone=P1', {'key': 'A', 'modifiers': ['ctrl']})
        self.assertEqual(code, 200, body)
        self.phones[0].device.key.assert_called_once_with('A', ['ctrl'])
        code, body = self.request('POST', '/launch?phone=P1', {'action': 'android.settings.SETTINGS'})
        self.assertEqual(code, 200, body)
        self.phones[0].device.launch.assert_called_once()

    def test_batch_is_validated_before_any_action(self):
        code, body = self.request('POST', '/run?phone=P1', {'steps': [
            {'tap': [100, 100]}, {'text': '\ud800'},
        ]})
        self.assertEqual(code, 400, body)
        self.phones[0].touch.tap.assert_not_called()
        self.phones[0].device.text.assert_not_called()

    def test_batch_returns_results_and_stops_at_first_failure(self):
        self.phones[0].device.text.side_effect = RuntimeError('keyboard disconnected')
        code, body = self.request('POST', '/run?phone=P1', {'steps': [
            {'tap': [100, 100]}, {'text': 'P1'}, {'key': 'ENTER'},
        ]})
        self.assertEqual(code, 503, body)
        self.assertEqual(body['failed_step'], 1)
        self.assertEqual(body['results'], [None])
        self.phones[0].touch.tap.assert_called_once()
        self.phones[0].device.text.assert_called_once()
        self.phones[0].device.key.assert_not_called()

    def test_invalid_requests_fail_before_phone_operations(self):
        for path, body in [('/text', {'text': '\ud800'}), ('/text', {'text': '\0'}), ('/key', {'key': 'UNKNOWN'}),
                           ('/launch', {'action': 'android.settings.SETTINGS;reboot'}),
                           ('/text', []), ('/text', {'text': 'P1', 'replace': 'yes'})]:
            with self.subTest(path=path, body=body):
                code, result = self.request('POST', path + '?serial=9e24cca', body)
                self.assertEqual(code, 400, result)
        self.phones[0].device.text.assert_not_called()
        self.phones[0].device.launch.assert_not_called()

    def test_text_accepts_unicode_without_per_phone_case_strategies(self):
        code, body = self.request('POST', '/text?phone=P2', {'text': '中文 Aa09-_', 'replace': True})
        self.assertEqual(code, 200, body)
        self.phones[1].device.text.assert_called_once_with('中文 Aa09-_', True)

    def test_no_settings_or_rename_workflow_endpoints(self):
        for path in ('/settings', '/rename'):
            code, body = self.request('POST', path + '?serial=9e24cca', {})
            self.assertEqual(code, 404, body)

    def test_invalid_hold_cannot_leave_a_finger_pressed(self):
        for hold in (-1, float('nan'), 31):
            code, body = self.request('POST', '/tap?phone=P1', {'x': 100, 'y': 100, 'hold': hold})
            self.assertEqual(code, 400, body)
        self.phones[0].touch.tap.assert_not_called()

    def test_multi_action_step_is_rejected(self):
        code, body = self.request('POST', '/run?phone=P1', {'steps': [{'tap': [10, 10], 'text': 'P1'}]})
        self.assertEqual(code, 400, body)
        self.phones[0].touch.tap.assert_not_called()

    def test_successful_batch_keeps_old_touch_actions_and_returns_reads(self):
        code, body = self.request('POST', '/run?phone=P1', {'steps': [
            {'tap': [100, 100]}, {'text': 'P1', 'replace': True}, {'prop': 'persist.sys.device_name'},
        ]})
        self.assertEqual(code, 200, body)
        self.assertEqual(body['results'], [None, {'characters': 2}, 'P2'])
        p = self.phones[0]
        p.touch.tap.assert_called_once_with(100, 100, .08, cancel=p.cancelled, progress=p.progressed)


class DevicePrimitiveTests(unittest.TestCase):
    def keyboard(self):
        keyboard = Keyboard('test')
        keyboard.proc = MagicMock()
        keyboard.proc.poll.return_value = None
        keyboard.proc.stdin = io.BytesIO()
        # 报告内容测试只记录字节；真实管道的期限另有进程级回归。
        writer = patch('device.write_pipe', side_effect=lambda pipe, data: pipe.write(data))
        writer.start()
        self.addCleanup(writer.stop)
        return keyboard

    def device(self):
        d = Device('test')
        d.keyboard = MagicMock()
        d.clipboard = MagicMock()
        return d

    def test_text_verifies_clipboard_before_replacing_and_pasting_unicode(self):
        d = self.device()
        events = []
        d.clipboard.set.side_effect = lambda text: events.append(('clipboard', text))
        d.keyboard.key.side_effect = lambda key, mods: events.append((key, mods))
        text = '中文 Aa09-_'
        self.assertEqual(d.text(text, True), {'characters': len(text)})
        self.assertEqual(events, [('clipboard', text), ('A', ['ctrl']), ('V', ['ctrl'])])

    def test_append_only_pastes_and_does_not_change_caps_lock(self):
        d = self.device()
        d.text('Aa09-_', False)
        d.clipboard.set.assert_called_once_with('Aa09-_')
        d.keyboard.key.assert_called_once_with('V', ['ctrl'])

    def test_clipboard_failure_cannot_delete_or_paste_old_contents(self):
        d = self.device()
        d.clipboard.set.side_effect = RuntimeError('clipboard mismatch')
        with self.assertRaises(RuntimeError):
            d.text('P1', True)
        d.keyboard.key.assert_not_called()
        d.clipboard.set.assert_called_once()
        d.clipboard.close.assert_called_once()

    def test_empty_replace_clears_without_reading_clipboard(self):
        d = self.device()
        self.assertEqual(d.text('', True), {'characters': 0})
        d.clipboard.set.assert_not_called()
        self.assertEqual([call.args for call in d.keyboard.key.call_args_list], [('A', ['ctrl']), ('BACKSPACE', [])])

    def test_empty_append_has_no_effect(self):
        d = self.device()
        self.assertEqual(d.text('', False), {'characters': 0})
        d.keyboard.open.assert_not_called()
        d.keyboard.key.assert_not_called()
        d.clipboard.set.assert_not_called()

    def test_key_failure_still_sends_release(self):
        k = self.keyboard()
        with patch.object(k, '_send', side_effect=[RuntimeError('failed'), None]) as send:
            with self.assertRaises(RuntimeError):
                k.stroke(19)
        self.assertEqual(send.call_args_list[-1].args[0]['report'], [1] + [0] * 8)

    def test_shortcut_modifier_survives_normal_key_release(self):
        for name, modifier in [('ctrl', 1), ('shift', 2)]:
            with self.subTest(modifier=name):
                k = self.keyboard()
                with patch('device.time.sleep'):
                    k.key('A', [name])
                reports = [json.loads(line)['report'] for line in k.proc.stdin.getvalue().splitlines()]
                pressed = next(i for i, r in enumerate(reports) if r[3] == 4)
                self.assertGreater(pressed, 0, '修饰键要先于普通键按下')
                self.assertEqual(reports[pressed - 1][1:4], [modifier, 0, 0])
                released = next(r for r in reports[pressed + 1:] if r[3] == 0)
                self.assertEqual(released[1], modifier, '普通键松开时修饰键仍须按住')
                self.assertEqual(reports[-1], [1] + [0] * 8)

    def test_modifier_release_failure_still_releases_everything(self):
        k = self.keyboard()
        reports = []
        def send(obj):
            report = obj['report']
            previous = reports[-1] if reports else [1] + [0] * 8
            reports.append(report)
            if previous[3] and not report[3] and report[1]:
                raise RuntimeError('key release failed')
        with patch.object(k, '_send', side_effect=send), patch('device.time.sleep'):
            with self.assertRaises(RuntimeError):
                k.key('A', ['ctrl'])
        self.assertEqual(reports[-1], [1] + [0] * 8)

    def test_home_and_back_use_consumer_reports(self):
        k = self.keyboard()
        with patch('device.time.sleep'):
            k.key('HOME', [])
            k.key('BACK', [])
        reports = [json.loads(line)['report'] for line in k.proc.stdin.getvalue().splitlines()]
        self.assertEqual(reports, [[2, 0x23, 2], [2, 0, 0], [2, 0x24, 2], [2, 0, 0]])

    def test_ui_preserves_tree_and_cleans_up_dump(self):
        d = Device('test')
        xml = '<hierarchy rotation="1"><node text="" bounds="[0,0][100,100]" clickable="true"><node text="P1" resource-id="id/name" bounds="[-1,2][10,20]" focused="true"/></node></hierarchy>'
        with patch.object(d, 'shell', side_effect=['UI hierchary dumped to: file', xml, '']) as shell:
            result = d.ui()
        self.assertEqual(result['rotation'], 1)
        self.assertEqual(result['nodes'][1]['parent'], 0)
        self.assertEqual(result['nodes'][1]['bounds'], [-1, 2, 10, 20])
        self.assertTrue(result['nodes'][1]['focused'])
        self.assertEqual(shell.call_args_list[-1].args[0], ['rm', '-f', d.ui_path])

    def test_failed_ui_dump_does_not_read_an_old_file(self):
        d = Device('test')
        with patch.object(d, 'shell', return_value='ERROR: could not get idle state.') as shell:
            with self.assertRaises(RuntimeError):
                d.ui()
        self.assertEqual(shell.call_count, 1)

    def test_launch_quotes_uri_without_executing_it_as_shell(self):
        d = Device('test')
        uri = 'test://value;$(touch /tmp/wrong)'
        result = subprocess.CompletedProcess([], 0, stdout='Status: ok', stderr='')
        with patch('device.subprocess.run', return_value=result) as run:
            d.launch(launch_args({'action': 'android.intent.action.VIEW', 'data': uri}))
        self.assertEqual(shlex.split(run.call_args.args[0][-1]),
                         ['am', 'start', '-W', '-a', 'android.intent.action.VIEW', '-d', uri])

    def test_android_launch_error_on_stderr_is_not_success(self):
        d = Device('test')
        result = subprocess.CompletedProcess([], 0, stdout='', stderr='Error: Activity not found')
        with patch('device.subprocess.run', return_value=result):
            with self.assertRaises(RuntimeError):
                d.launch(launch_args({'action': 'invalid.action'}))

    def test_repeated_text_reuses_keyboard_registration(self):
        d = self.device()
        d.keyboard.alive.side_effect = [False, True]
        d.text('P1', True)
        d.text('P1', True)
        d.keyboard.open.assert_called_once()
        self.assertEqual(d.clipboard.set.call_count, 2)
        self.assertEqual(d.keyboard.key.call_count, 4)

    def test_partial_text_failure_closes_keyboard_without_replaying(self):
        d = self.device()
        d.keyboard.key.side_effect = [None, RuntimeError('disconnected halfway')]
        with self.assertRaises(RuntimeError):
            d.text('P1', True)
        self.assertEqual(d.keyboard.key.call_count, 2)
        d.clipboard.set.assert_called_once()
        d.keyboard.close.assert_called_once()
        d.clipboard.close.assert_called_once()


class ClipboardProtocolTests(unittest.TestCase):
    class Connection:
        def __init__(self, response):
            self.response = io.BytesIO(response)
            self.sent = []
            self.closed = False

        def recv(self, count):
            return self.response.read(min(count, 2))

        def sendall(self, packet):
            self.sent.append(packet)

        def close(self):
            self.closed = True

    def clipboard(self, response):
        c = Clipboard('test')
        c.proc = MagicMock()
        c.proc.poll.return_value = None
        c.sock = self.Connection(response)
        return c

    def response(self, sequence, text):
        payload = text.encode('utf-8')
        return b'\1' + struct.pack('>Q', sequence) + b'\0' + struct.pack('>I', len(payload)) + payload

    def test_utf8_byte_length_ack_and_readback_on_a_reused_connection(self):
        text = '中文 Aa09-_'
        c = self.clipboard(self.response(1, text) + self.response(2, 'P2'))
        connection = c.sock
        with patch.object(c, 'open') as start:
            c.set(text)
            c.set('P2')
        start.assert_not_called()
        payload = text.encode('utf-8')
        self.assertEqual(connection.sent[0], struct.pack('>BQBI', 9, 1, 0, len(payload)) + payload)
        self.assertEqual(connection.sent[1], bytes([8, 0]))
        self.assertEqual(connection.sent[2], struct.pack('>BQBI', 9, 2, 0, 2) + b'P2')
        self.assertEqual(connection.sent[3], bytes([8, 0]))

    def test_ack_is_not_proof_that_clipboard_changed(self):
        c = self.clipboard(self.response(1, 'old clipboard'))
        connection = c.sock
        with self.assertRaisesRegex(RuntimeError, '未生效'):
            c.set('P1')
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.sent), 2)

    def test_mismatched_ack_closes_transport_without_replaying_set(self):
        c = self.clipboard(b'\1' + struct.pack('>Q', 7))
        connection = c.sock
        with self.assertRaisesRegex(RuntimeError, '确认不匹配'):
            c.set('P1')
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.sent), 1)

    def test_control_channel_is_separate_from_video_and_does_not_inject_paste(self):
        c = Clipboard('P1-serial')
        connection = MagicMock()
        connection.recv.return_value = b'\0'
        process = MagicMock()
        process.poll.return_value = None
        pushed = subprocess.CompletedProcess([], 0, stdout='')
        forwarded = subprocess.CompletedProcess([], 0, stdout='45678\n')
        with patch('device.subprocess.run', side_effect=[pushed, forwarded]) as adb, \
                patch('device.subprocess.Popen', return_value=process) as start, \
                patch('device.socket.create_connection', return_value=connection):
            c.open()
        self.assertEqual(c.port, 45678)
        self.assertEqual(adb.call_args_list[1].args[0][:5], ['adb', '-s', 'P1-serial', 'forward', 'tcp:0'])
        self.assertRegex(adb.call_args_list[1].args[0][-1], r'^localabstract:scrcpy_[0-9a-f]{8}$')
        command = start.call_args.args[0]
        for option in ('video=false', 'audio=false', 'control=true', 'power_on=false', 'clipboard_autosync=false'):
            self.assertIn(option, command)
        self.assertIs(c.sock, connection)


if __name__ == '__main__':
    unittest.main(verbosity=2)
