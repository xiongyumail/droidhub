"""窗口/视图解析、结束应用、shell、logcat 和文件传输；全部用假数据和假 adb，不连接手机。"""
import http.client
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import adb_sync
import hashlib
import struct
import app
import dumps
from device import package_name
from test_recovery import phone

ANDROID10_WINDOWS = '''WINDOW MANAGER WINDOWS (dumpsys window windows)
  Window #0 Window{795c5ce u0 StatusBar}:
    mOwnerUid=10021 mShowToOwnerOnly=false package=com.android.systemui appop=NONE
    mViewVisibility=0x0 mHaveFrame=true mObscured=false
    mHasSurface=true isReadyForDisplay()=true mWindowRemovalAllowed=false
    mFrame=[0,0][1080,80] last=[0,0][1080,80]
  Window #1 Window{7a234d5 u0 com.example.app/com.ads.Interstitial}:
    mOwnerUid=10002 mShowToOwnerOnly=true package=com.example.app appop=NONE
    mViewVisibility=0x0 mHaveFrame=true mObscured=false
    mHasSurface=true isReadyForDisplay()=true mWindowRemovalAllowed=false
    Frames: containing=[0,80][1080,2210] parent=[0,80][1080,2210]
        content=[108,503][972,1787] visible=[108,503][972,1787]
    mFrame=[108,503][972,1787] last=[108,503][972,1787]
  Window #2 Window{1111111 u0 com.example.app/com.example.app.ui.MainActivity}:
    package=com.example.app appop=NONE
    mViewVisibility=0x8 mHaveFrame=true mObscured=false
    mHasSurface=false isReadyForDisplay()=false
    mFrame=[0,0][1080,2340] last=[0,0][1080,2340]
'''
ANDROID11_WINDOWS = '''  Window #0 Window{abc u0 com.example.app/com.example.app.ui.MainActivity}:
    mOwnerUid=10100 showForAllUsers=false package=com.example.app appop=NONE
    mViewVisibility=0x0 mHaveFrame=true mObscured=false
    mHasSurface=true isReadyForDisplay()=true mWindowRemovalAllowed=false
    WindowStateAnimator{...}:
      Frames: containing=[0,0][768,1280] parent=[0,0][768,1280]
    frame=[0,0][768,1280] last=[0,0][768,1280]
'''
FOCUS = '''  mCurrentFocus=Window{7a234d5 u0 com.example.app/com.ads.Interstitial}
  mFocusedApp=AppWindowToken{15a2348 token=Token{272a9eb ActivityRecord{a82693a u0 com.example.app/com.ads.Interstitial t79}}}
'''
ACTIVITIES = '    mResumedActivity: ActivityRecord{a82693a u0 com.example.app/com.ads.Interstitial t79}\n'
TOP = '''TASK com.example.app id=79 userId=0
  ACTIVITY com.example.app/.ui.MainActivity 1 pid=1
    View Hierarchy:
      DecorView@1[MainActivity]
        android.widget.FrameLayout{1 V.E...... ........ 0,0-1080,2340}
          android.widget.ImageView{2 VFED..C.. ........ 10,20-110,120 #7f01 app:id/main_button}
  ACTIVITY com.example.app/com.ads.Interstitial 2 pid=1
    View Hierarchy:
      DecorView@2[Interstitial]
        android.widget.FrameLayout{3 V.E...... ........ 0,0-864,1284}
          android.widget.LinearLayout{4 V.E...... ........ 682,28-792,93 #7f02 app:id/skip_layout}
            android.widget.LinearLayout{5 VFE...C.. ........ 0,0-110,65 #7f03 app:id/skip}
          android.widget.TextView{6 G.ED..... ........ 0,0-50,50 #7f04 app:id/hidden}
'''


class DumpParsingTests(unittest.TestCase):
    def test_android10_windows_use_mframe_and_visibility(self):
        data = dumps.windows(ANDROID10_WINDOWS, FOCUS, ACTIVITIES)
        self.assertEqual(data['focus'], 'com.example.app/com.ads.Interstitial')
        self.assertEqual(data['focus_package'], 'com.example.app')
        self.assertEqual(data['focused_app'], 'com.example.app/com.ads.Interstitial')
        self.assertEqual(data['resumed'], 'com.example.app/com.ads.Interstitial')
        self.assertEqual([(w['name'], w['frame'], w['visible']) for w in data['windows']], [
            ('StatusBar', [0, 0, 1080, 80], True),
            ('com.example.app/com.ads.Interstitial', [108, 503, 972, 1787], True),
            ('com.example.app/com.example.app.ui.MainActivity', [0, 0, 1080, 2340], False)])
        self.assertEqual(data['windows'][0]['package'], 'com.android.systemui')

    def test_android11_frame_is_read(self):
        self.assertEqual(dumps.windows(ANDROID11_WINDOWS)['windows'][0]['frame'], [0, 0, 768, 1280])

    def test_views_add_centered_window_offset_and_skip_hidden(self):
        data = dumps.views(TOP, ANDROID10_WINDOWS)
        self.assertEqual(data['activity'], 'com.example.app/com.ads.Interstitial')
        skip = next(v for v in data['views'] if v['id'] == 'app:id/skip')
        self.assertEqual(skip['bounds'], [108 + 682, 503 + 28, 108 + 792, 503 + 93])
        self.assertTrue(skip['clickable'])
        self.assertNotIn('app:id/hidden', [v['id'] for v in data['views']])

    def test_views_for_package_pick_its_topmost_activity(self):
        data = dumps.views(TOP + '  ACTIVITY com.other/.Main 3 pid=2\n', ANDROID10_WINDOWS, 'com.example.app')
        self.assertEqual(data['activity'], 'com.example.app/com.ads.Interstitial')
        self.assertEqual(dumps.views(TOP, '', 'com.missing')['views'], [])

    def test_views_report_missing_hierarchy_of_hung_app(self):
        data = dumps.views(TOP + 'TASK com.hung id=9\n  ACTIVITY com.hung/.Main 4 pid=3\n', '')
        self.assertEqual((data['activity'], data['hierarchy'], data['views']), ('com.hung/com.hung.Main', False, []))
        self.assertTrue(dumps.views(TOP, '')['hierarchy'])


class PackageTests(unittest.TestCase):
    def test_protected_and_invalid_packages_are_rejected(self):
        self.assertEqual(package_name('com.tyg.tygsmart', stop=True), 'com.tyg.tygsmart')
        for bad in ('com.android.systemui', 'com.miui.home', 'com.rom1v.sndcpy'):
            with self.assertRaises(ValueError):
                package_name(bad, stop=True)
        for bad in ('x; reboot', '', 'nodot', None, 'a.' + 'b' * 300):
            with self.assertRaises(ValueError):
                package_name(bad)


class HttpInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.HTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.p = phone()
        fleet = app.Fleet()
        fleet.phones['test'] = self.p
        patcher = patch.object(app, 'fleet', fleet)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 假 adb：shell 回显命令并以 3 退出；logcat 每 0.05 秒输出一行
        directory = tempfile.mkdtemp()
        script = os.path.join(directory, 'adb')
        with open(script, 'w') as f:
            f.write('#!/bin/sh\nshift 2\ncase "$1" in\n'
                    '  shell) echo "out:$2"; echo "err" >&2; [ "$2" = slow ] && sleep 5; exit 3;;\n'
                    '  logcat) [ "$2" = -c ] && exit 0; i=0; while :; do echo "line $i $*"; i=$((i+1)); sleep 0.05; done;;\n'
                    'esac\n')
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        path = patch.dict(os.environ, {'PATH': directory + os.pathsep + os.environ['PATH']})
        path.start()
        self.addCleanup(path.stop)

    def request(self, path, body=None, method='POST', timeout=5):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=timeout)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               {'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            data = response.read()
            return response.status, (json.loads(data) if response.getheader('Content-Type', '').startswith('application/json')
                                      else data.decode())
        finally:
            connection.close()

    def test_windows_and_views_read_without_queueing(self):
        self.p.device.windows.return_value = {'focus': 'x', 'windows': []}
        self.p.device.views.return_value = {'activity': 'a/b', 'views': []}
        self.p.lock.acquire()   # 正在执行长批次也能读
        try:
            self.assertEqual(self.request('/windows', method='GET')[0], 200)
            code, body = self.request('/views?package=com.example.app', method='GET')
        finally:
            self.p.lock.release()
        self.assertEqual((code, body['activity']), (200, 'a/b'))
        self.p.device.views.assert_called_once_with('com.example.app')
        self.assertEqual(self.request('/views?package=bad;pkg', method='GET')[0], 400)

    def test_shot_box_parameters(self):
        self.p.device.shot.return_value = b'PNG'
        self.assertEqual(self.request('/shot?x=1&y=2&w=3&h=4', method='GET'), (200, 'PNG'))
        self.p.device.shot.assert_called_with((1, 2, 3, 4))
        self.request('/shot?y=1500', method='GET')
        self.p.device.shot.assert_called_with((0, 1500, None, None))
        self.request('/shot', method='GET')
        self.p.device.shot.assert_called_with(None)
        for query in ('w=0', 'x=-1', 'y=a', 'h=1.5'):
            status, data = self.request('/shot?' + query, method='GET')
            self.assertEqual(status, 400, query)
        self.p.device.shot.side_effect = ValueError('区块超出屏幕（1080x2340）')
        self.assertEqual(self.request('/shot?y=3000', method='GET'),
                         (400, {'ok': False, 'detail': '区块超出屏幕（1080x2340）'}))

    def test_frame_starts_stream_and_waits_for_newer_frame(self):
        frames = []
        def run(stream):
            for jpg in frames:
                stream._publish(jpg)
        self.p.stream = app.Stream(self.p, 27183)
        with patch.object(app.Stream, '_run', run):
            frames.append(b'jpeg-1')
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            connection.request('GET', '/frame')
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, b'jpeg-1'))
            self.assertEqual(response.getheader('Content-Type'), 'image/jpeg')
            seq = response.getheader('X-Frame-Seq')
            connection.close()
            self.assertEqual(self.p.stream.viewers, 0)
            # 静止画面：等满 wait 仍返回同一帧
            started = time.monotonic()
            self.assertEqual(self.request('/frame?after=%s&wait=0.3' % seq, method='GET'), (200, 'jpeg-1'))
            self.assertGreaterEqual(time.monotonic() - started, 0.25)
            threading.Timer(0.1, self.p.stream._publish, [b'jpeg-2']).start()
            self.assertEqual(self.request('/frame?after=%s&wait=2' % seq, method='GET'), (200, 'jpeg-2'))
        for query in ('wait=11', 'wait=-1', 'after=x'):
            self.assertEqual(self.request('/frame?' + query, method='GET')[0], 400, query)

    def test_stop_rejects_protected_package_before_phone(self):
        code, _ = self.request('/stop', {'package': 'com.miui.home'})
        self.assertEqual(code, 400)
        self.p.device.stop.assert_not_called()
        self.p.device.stop.return_value = {'package': 'com.xiaomi.market'}
        code, body = self.request('/stop', {'package': 'com.xiaomi.market'})
        self.assertEqual((code, body['result']), (200, {'package': 'com.xiaomi.market'}))

    def test_shell_returns_exit_code_and_both_streams(self):
        code, body = self.request('/shell', {'command': 'echo 中文'})
        self.assertEqual(code, 200)
        self.assertEqual((body['exit'], body['stdout'].strip(), body['stderr'].strip()), (3, 'out:echo 中文', 'err'))

    def test_shell_validates_and_times_out(self):
        self.assertEqual(self.request('/shell', {'command': ''})[0], 400)
        self.assertEqual(self.request('/shell', {'command': 'x', 'timeout': 999})[0], 400)
        code, body = self.request('/shell', {'command': 'slow', 'timeout': 0.5})
        self.assertEqual((code, body['stdout'].strip()), (504, 'out:slow'))

    def test_logcat_streams_filtered_lines_until_seconds(self):
        code, text = self.request('/logcat?tag=OpenDoorFragment&level=V&format=raw&seconds=1', method='GET')
        self.assertEqual(code, 200)
        lines = text.splitlines()
        self.assertGreater(len(lines), 3)
        self.assertIn('-v raw -T 1 -s OpenDoorFragment:V', lines[0])
        self.assertEqual(self.request('/logcat?tag=bad;tag', method='GET')[0], 400)


class FakeSyncServer:
    """ADB 服务端 + adbd 的 sync 协议：files 是手机上的文件，/readonly/ 下拒绝写入。"""
    def __init__(self):
        self.files = {'/sdcard/a.bin': bytes(range(256)) * 600}
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, c):
        receive = adb_sync.receive
        with c:
            try:
                for _ in range(2):
                    receive(c, int(receive(c, 4), 16))   # host:transport:序列号、sync:
                    c.sendall(b'OKAY')
                while True:
                    kind, size = receive(c, 4), struct.unpack('<I', receive(c, 4))[0]
                    arg = receive(c, size).decode() if size and kind != b'QUIT' else ''
                    if kind == b'STAT':
                        data = self.files.get(arg)
                        c.sendall(b'STAT' + struct.pack('<III', 0o100644 if data is not None else
                                                         (0o40755 if arg == '/sdcard' else 0),
                                                         len(data or b''), 0))
                    elif kind == b'RECV':
                        data = self.files.get(arg)
                        if data is None:
                            message = b'No such file or directory'
                            c.sendall(b'FAIL' + struct.pack('<I', len(message)) + message)
                            continue
                        for i in range(0, len(data), 65536):
                            c.sendall(b'DATA' + struct.pack('<I', len(data[i:i + 65536])) + data[i:i + 65536])
                        c.sendall(b'DONE' + struct.pack('<I', 0))
                    elif kind == b'SEND':
                        path = arg.rsplit(',', 1)[0]
                        if path.startswith('/readonly/'):
                            message = b'couldn\'t create file: Read-only file system'
                            c.sendall(b'FAIL' + struct.pack('<I', len(message)) + message)
                            return
                        data = bytearray()
                        while True:
                            kind, size = receive(c, 4), struct.unpack('<I', receive(c, 4))[0]
                            if kind == b'DONE':
                                break
                            data += receive(c, size)
                        self.files[path] = bytes(data)
                        c.sendall(b'OKAY' + struct.pack('<I', 0))
                    else:
                        return
            except (ConnectionError, OSError, ValueError):
                return


class FileTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.HTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.adb = FakeSyncServer()
        self.addCleanup(self.adb.listener.close)
        fleet = app.Fleet()
        fleet.phones['test'] = phone()
        for patcher in (patch.object(app, 'fleet', fleet),
                        patch.dict(os.environ, {'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:%d' % self.adb.port})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body)
            response = connection.getresponse()
            return response.status, response.getheader('Content-Length'), response.read()
        finally:
            connection.close()

    def test_download_streams_whole_file_with_length(self):
        code, length, data = self.request('GET', '/file?path=/sdcard/a.bin')
        self.assertEqual((code, int(length), data), (200, 153600, self.adb.files['/sdcard/a.bin']))

    def test_download_missing_directory_and_relative_paths(self):
        self.assertEqual(self.request('GET', '/file?path=/sdcard/none')[0], 404)
        self.assertEqual(self.request('GET', '/file?path=/sdcard')[0], 400)
        self.assertEqual(self.request('GET', '/file?path=sdcard/a.bin')[0], 400)

    def test_upload_writes_bytes_and_returns_hash(self):
        payload = os.urandom(200000)
        code, _, body = self.request('PUT', '/file?path=/sdcard/Download/x.apk', payload)
        body = json.loads(body)
        self.assertEqual((code, body['size'], body['sha256']), (200, len(payload), hashlib.sha256(payload).hexdigest()))
        self.assertEqual(self.adb.files['/sdcard/Download/x.apk'], payload)

    def test_upload_reports_phone_refusal_and_bad_arguments(self):
        code, _, body = self.request('PUT', '/file?path=/readonly/x', b'abc')
        self.assertEqual(code, 400)
        self.assertIn('Read-only file system', json.loads(body)['detail'])
        self.assertEqual(self.request('PUT', '/file?path=/sdcard/x&mode=999', b'a')[0], 400)
        self.assertEqual(self.request('PUT', '/other', b'a')[0], 404)


if __name__ == '__main__':
    unittest.main()
