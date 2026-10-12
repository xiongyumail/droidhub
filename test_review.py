"""真实管道与线程复现输入阻塞、检查过期和长操作取消；不连接手机。"""
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import app
import runtime
from device import Keyboard
from touch import Touch


def fake_phone():
    p = app.Phone('test', 27183)
    t = Touch.__new__(Touch)
    t.adb, t.w, t.h, t.pos = ['fake-adb'], 1080, 2340, None
    t.proc = MagicMock()
    t.proc.poll.return_value = None
    t._send = MagicMock()
    p.touch, p.error = t, None
    p.ensure = MagicMock(return_value=True)
    return p


class InputDeadlineTests(unittest.TestCase):
    def blocked_process(self, buffered=False):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                stdin=subprocess.PIPE, bufsize=-1 if buffered else 0)
        self.addCleanup(runtime.close_hid, proc)
        os.set_blocking(proc.stdin.fileno(), False)
        while True:
            try:
                os.write(proc.stdin.fileno(), b'x' * 4096)
            except BlockingIOError:
                break
        return proc

    def test_touch_and_keyboard_cannot_wait_forever_for_alive_nonreader(self):
        for kind in ('touch', 'keyboard'):
            with self.subTest(kind=kind):
                p = fake_phone()
                target = p.touch if kind == 'touch' else Keyboard('test')
                if kind == 'touch':
                    del target._send
                proc = target.proc = self.blocked_process()
                begin = time.monotonic()
                with patch.object(runtime, 'HID_WRITE_TIMEOUT', .1):
                    with self.assertRaises(runtime.ConnectionFailure):
                        p.command(lambda _: target._send({'command': 'report'}))
                self.assertLess(time.monotonic() - begin, .7)
                self.assertIsNotNone(proc.poll())
                self.assertIsNone(target.proc)
                self.assertTrue(p.lock.acquire(timeout=.1))
                p.lock.release()
                self.assertFalse(p.status()['connected'])
                f = app.Fleet()
                f.phones['test'] = p
                self.assertFalse(f.healthy())

    def test_close_terminates_process_before_flushing_buffered_full_pipe(self):
        proc = self.blocked_process(buffered=True)
        # BufferedWriter 里也留数据，旧实现会在 close 的 flush 中阻塞。
        proc.stdin.write(b'pending')
        os.set_blocking(proc.stdin.fileno(), True)
        t = fake_phone().touch
        t.proc = proc
        begin = time.monotonic()
        t.close()
        self.assertLess(time.monotonic() - begin, .7)
        self.assertIsNotNone(proc.poll())

    def test_stale_phone_cannot_pass_health_just_because_hid_is_alive(self):
        p = fake_phone()
        p.last_activity = time.monotonic() - 120
        p.last_check = time.time() - 120
        f = app.Fleet()
        f.phones['test'] = p
        self.assertFalse(p.status()['connected'])
        self.assertIn('超时', p.status()['error'])
        self.assertFalse(f.healthy())

    def test_readonly_check_refreshes_health_and_screen_rotation(self):
        p = fake_phone()
        p.last_activity = time.monotonic() - 120
        p._adb = MagicMock(return_value=subprocess.CompletedProcess([], 0,
            stdout='Viewport INTERNAL: displayId=0, orientation=3, logicalFrame=[0, 0, 2340, 1080]'))
        p.check()
        self.assertTrue(p.status()['connected'])
        self.assertEqual(p.status()['rotation'], 3)
        self.assertEqual(p._adb.call_args.args, ('shell', 'dumpsys input; dumpsys power | grep mWakefulness='))

    def test_duplicate_subscriber_id_does_not_remove_another_live_subscription(self):
        stream = fake_phone().stream
        with patch.object(app.threading, 'Thread'):
            stream.join('page-a')
            stream.join('page-a')
            stream.join('page-b')
            stream.leave('page-a')
            self.assertEqual(stream.status()['clients'], ['page-a', 'page-b'])
            stream.leave('page-b')
            self.assertEqual(stream.status()['clients'], ['page-a'])
            stream.leave('page-a')
            self.assertEqual(stream.status()['clients'], [])
            self.assertEqual(stream.status()['viewers'], 0)

    def test_readonly_rotation_probe_does_not_block_drag_or_poison_rebuilt_channel(self):
        p = fake_phone()
        p.pressed_at = time.time()
        entered, finish = threading.Event(), threading.Event()
        def probe(*args, **kwargs):
            entered.set()
            finish.wait(1)
            raise subprocess.TimeoutExpired('old adb probe', 3)
        p._adb = probe
        worker = threading.Thread(target=p.check)
        worker.start()
        try:
            self.assertTrue(entered.wait(.5))
            self.assertTrue(p.lock.acquire(timeout=.1), '只读检查不能占住拖动锁')
            p.lock.release()
            p.finger('move', 200, 200)
            # 模拟应用通道在旧探测结束前已经被替换。
            p.touch = fake_phone().touch
            finish.set()
            worker.join(.5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(p.failed.is_set())
            self.assertIsNone(p.error)
        finally:
            finish.set()
            worker.join(2)


class BatchCancellationTests(unittest.TestCase):
    def run_until_cancel(self, p, steps, cancel):
        result = []
        def run():
            try:
                p.run(steps, cancel.is_set)
            except Exception as e:
                result.append(e)
        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(worker.join, 2)
        return worker, result

    def test_batch_releases_old_web_gesture_before_wait_and_cancels_inside_wait(self):
        p = fake_phone()
        p.touch.down(100, 100)
        p.pressed_at = time.time() - 10
        released = threading.Event()
        original = p.touch.up
        def up():
            original()
            released.set()
        p.touch.up = up
        later = MagicMock()
        cancelled = threading.Event()
        worker, result = self.run_until_cancel(p, [app.action({'wait': 30}), later], cancelled)
        try:
            self.assertTrue(released.wait(.5))
            self.assertIsNone(p.touch.pos)
            self.assertIsNone(p.pressed_at)
            begin = time.monotonic()
            cancelled.set()
            worker.join(.5)
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - begin, .5)
            self.assertIsInstance(result[0], app.StepFailure)
            self.assertIsInstance(result[0].cause, runtime.RequestCancelled)
            self.assertEqual(result[0].index, 0)
            later.assert_not_called()
            self.assertFalse(p.failed.is_set(), '取消不能冒充 USB 断线')
        finally:
            cancelled.set()

    def test_cancelled_long_press_and_swipe_release_contact(self):
        for step in ({'long': [100, 100], 'secs': 30},
                     {'swipe': [100, 100, 200, 200], 'secs': 30}):
            with self.subTest(step=step):
                p, cancelled, pressed = fake_phone(), threading.Event(), threading.Event()
                p.touch._send.side_effect = lambda report: pressed.set() if report['report'][1] else None
                worker, result = self.run_until_cancel(p, [app.action(step)], cancelled)
                try:
                    self.assertTrue(pressed.wait(.5))
                    cancelled.set()
                    worker.join(.5)
                    self.assertFalse(worker.is_alive())
                    self.assertIsNone(p.touch.pos)
                    self.assertEqual(p.touch._send.call_args.args[0]['report'][1], 0)
                    self.assertIsInstance(result[0].cause, runtime.RequestCancelled)
                finally:
                    cancelled.set()

    def test_responsive_long_wait_is_healthy_while_watchdog_cannot_take_lock(self):
        p, cancelled = fake_phone(), threading.Event()
        f = app.Fleet()
        f.phones['test'] = p
        worker, _ = self.run_until_cancel(p, [app.action({'wait': 30})], cancelled)
        try:
            with patch.object(app, 'HEALTH_STALE_AFTER', .2):
                time.sleep(.35)
                self.assertTrue(worker.is_alive())
                self.assertFalse(p.lock.acquire(blocking=False))
                self.assertTrue(f.healthy())
        finally:
            cancelled.set()


if __name__ == '__main__':
    unittest.main()
