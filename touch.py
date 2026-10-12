"""USB 手机的 uhid 输入：经 adb 在手机上建一个虚拟触摸屏，用像素坐标点、长按、滑动。

不需要 root，也不需要 MIUI 的「USB 调试（安全设置）」：adb shell 用户在 uhid 组里，
用系统自带的 /system/bin/hid 建设备，系统把它当外接触摸屏。由 app.py 的 Phone 常驻持有。
"""
import json
import re
import subprocess
import time

from runtime import ConnectionFailure, close_hid, pause, write_pipe

# hid-multitouch 认的单指触摸屏：Touch Screen 应用 + Contact Identifier，内核才会给 INPUT_PROP_DIRECT，
# 不然 Android 当触控板处理（出光标、坐标走相对位移）。不放 Contact Count Maximum 特性报告，
# hid 工具不回 GET_REPORT，驱动会干等超时
DESCRIPTOR = [
    0x05, 0x0D, 0x09, 0x04, 0xA1, 0x01, 0x85, 0x01,
    0x09, 0x22, 0xA1, 0x02,
    0x09, 0x42, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x01, 0x81, 0x02,
    0x09, 0x32, 0x81, 0x02,
    0x95, 0x06, 0x81, 0x03,
    0x09, 0x51, 0x25, 0x0F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    0x05, 0x01, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x01,
    0x09, 0x30, 0x81, 0x02, 0x09, 0x31, 0x81, 0x02,
    0xC0,
    0x05, 0x0D, 0x09, 0x54, 0x25, 0x0A, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    0xC0,
]
LOGICAL_MAX = 32767
READY_WAIT = 1.5     # 设备注册后系统装好输入设备要一会儿，太早点会丢
STEP = 0.012         # 滑动时报告间隔


class Touch:
    def __init__(self, serial=None):
        self.adb = ['adb'] + (['-s', serial] if serial else [])
        result = subprocess.run(self.adb + ['shell', 'wm size; wm density'], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ConnectionFailure((result.stderr or result.stdout).strip() or '手机 ADB 没有响应')
        out = result.stdout
        m = re.findall(r'(\d+)x(\d+)', out)
        if not m:
            raise RuntimeError('读不到屏幕分辨率：' + out.strip())
        self.w, self.h = map(int, m[-1])   # 有 Override size / density 时取最后一行
        dpi = re.findall(r'density: (\d+)', out)
        self.density = int(dpi[-1]) if dpi else None
        self.proc = None
        self.pos = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        self.close()
        self.proc = subprocess.Popen(self.adb + ['shell', 'hid', '-'], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        try:
            self._send({'id': 1, 'command': 'register', 'name': 'droidhub touch',
                        'vid': 0x1209, 'pid': 0x0001, 'descriptor': DESCRIPTOR})
            time.sleep(READY_WAIT)
            if self.proc.poll() is not None:
                raise ConnectionFailure('UHID 触摸屏注册失败')
        except BaseException:
            self.close()
            raise

    def close(self):
        proc, self.proc, self.pos = self.proc, None, None
        if proc:
            close_hid(proc)

    def _send(self, obj):
        if not self.proc or self.proc.poll() is not None:
            raise ConnectionFailure('hid 进程已退出')
        try:
            write_pipe(self.proc.stdin, (json.dumps(obj) + '\n').encode())
        except ConnectionFailure:
            self.close()
            raise

    def validate(self, x, y):
        if not (0 <= x < self.w and 0 <= y < self.h):
            raise ValueError('坐标超出屏幕 %dx%d：(%s, %s)' % (self.w, self.h, x, y))

    def _report(self, down, x, y):
        self.validate(x, y)
        lx = round(x * LOGICAL_MAX / (self.w - 1))
        ly = round(y * LOGICAL_MAX / (self.h - 1))
        self._send({'id': 1, 'command': 'report',
                    'report': [1, 3 if down else 0, 0, lx & 255, lx >> 8, ly & 255, ly >> 8, 1]})

    def down(self, x, y):
        """按下或按着移动；松手用 up"""
        self.validate(x, y)
        self.pos = (x, y)
        self._report(True, x, y)

    def up(self):
        if getattr(self, 'pos', None):
            try:
                self._report(False, *self.pos)
            except BaseException:
                # 松手消息发不出去时销毁虚拟设备，由系统释放接触点。
                self.close()
                raise
            finally:
                self.pos = None

    def tap(self, x, y, hold=0.08, *, cancel=None, progress=None):
        self.validate(x, y)
        if not 0 <= hold <= 30:
            raise ValueError('hold 要在 0–30 秒')
        try:
            pause(0, cancel, progress)
            self.down(x, y)
            pause(hold, cancel, progress)
        finally:
            self.up()

    def long_press(self, x, y, secs=1.0, *, cancel=None, progress=None):
        self.tap(x, y, hold=secs, cancel=cancel, progress=progress)

    def swipe(self, x1, y1, x2, y2, secs=0.3, *, cancel=None, progress=None):
        self.validate(x1, y1)
        self.validate(x2, y2)
        if not 0 <= secs <= 30:
            raise ValueError('secs 要在 0–30 秒')
        n = max(2, int(secs / STEP))
        try:
            for i in range(n + 1):
                pause(0, cancel, progress)
                self.down(round(x1 + (x2 - x1) * i / n), round(y1 + (y2 - y1) * i / n))
                if i < n:
                    pause(secs / n, cancel, progress)
        finally:
            self.up()

