"""真实 ffmpeg 回归：少量 H.264 帧后保持连接，不能等 EOF 才出 JPEG；不访问手机。"""
import shutil
import socket
import struct
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app


@unittest.skipUnless(shutil.which('ffmpeg'), '需要部署镜像中的 ffmpeg')
class VideoLatencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clips = {}
        for width, height in ((736, 1600), (1600, 736)):
            cls.clips[width, height] = subprocess.run([
                'ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i',
                'color=c=blue:s=%dx%d:r=60' % (width, height), '-frames:v', '3',
                '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast',
                '-tune', 'zerolatency', '-f', 'h264', 'pipe:1'],
                capture_output=True, check=True, timeout=10).stdout

    def capture_without_eof(self, data, expected):
        phone = SimpleNamespace(serial='fake', failed=threading.Event())
        stream = app.Stream(phone, 27183)
        sock, server = MagicMock(), MagicMock()
        server.poll.return_value = None
        pending = [data]
        sizes, errors = [], []
        real_popen = subprocess.Popen

        def receive(_):
            if pending:
                return pending.pop()
            # 手机静止后暂时不产帧，连接仍然存活。
            stream.stop.wait(.05)
            raise socket.timeout()

        def publish(jpg):
            sof = jpg.index(b'\xff\xc0')
            height, width = struct.unpack('>HH', jpg[sof + 5:sof + 9])
            with stream.cond:
                sizes.append((width, height))
                stream.cond.notify_all()

        def spawn(args, **kwargs):
            return real_popen(args, **kwargs) if args[0] == 'ffmpeg' else server

        def capture():
            try:
                stream._session(SimpleNamespace(adb=['fake-adb'], w=1080, h=2340))
            except Exception as error:
                errors.append(error)

        sock.recv.side_effect = receive
        stream._publish = publish
        with patch('app.run_command'), patch('app.socket.create_connection', return_value=sock), \
                patch('app.subprocess.Popen', side_effect=spawn):
            worker = threading.Thread(target=capture)
            worker.start()
            try:
                with stream.cond:
                    ready = stream.cond.wait_for(lambda: all(sizes.count(size) >= count
                        for size, count in expected.items()), timeout=3)
                self.assertTrue(ready, '连接仍存活时必须及时产帧，实际：%r' % sizes)
                self.assertEqual(errors, [])
            finally:
                stream.close()
                worker.join(5)
                self.assertFalse(worker.is_alive(), '停止必须释放真实解码器及输入线程')

    def test_three_frames_publish_without_waiting_for_more_input_or_eof(self):
        self.capture_without_eof(self.clips[736, 1600], {(736, 1600): 2})

    def test_landscape_first_frames_publish_without_waiting_for_eof(self):
        self.capture_without_eof(self.clips[1600, 736], {(1600, 736): 2})
