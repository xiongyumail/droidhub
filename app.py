#!/usr/bin/env python3
"""droidhub 服务：一直连着设备、各保持一个常驻输入通道，HTTP 接口点屏、截图和实时画面。

USB 手机用 uhid 虚拟触摸屏；网络设备（容器里的 Android、无线调试的平板）由这里发起 adb connect，
输入可选 scrcpy 控制通道。接口用 ?serial= 指定，不指定用默认那台。
每 5 秒检查设备；断线只重建应用控制通道，失败后 30 秒再试，不重置 USB。
"""
import base64
import json
import logging
import math
import os
import re
import select
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from touch import Touch
from device import ControlTouch, Device, text_args, key_args, launch_args, package_name, prop_name
from audio import Audio, SAMPLE_RATE, CHANNELS, FORMAT
import adb_sync
import config
import hashlib
from runtime import ConnectionFailure, RequestCancelled, adb_forward_host, connection_error, pause, run_command

PORT = int(os.environ.get('DROIDHUB_PORT', '8791'))
CONFIG = config.load()
DEVICES = CONFIG['devices']
DEFAULT_SERIAL = CONFIG['default']   # 不指定 serial 时用哪台，没设就用序列号排第一的
PINS = {serial: d['pin'] for serial, d in DEVICES.items() if d['pin']}
PHONE_NAMES = {serial: c['name'] for serial, c in DEVICES.items()}  # 界面名称；adb 和接口仍按序列号定位
ALLOWED_SERIALS = frozenset(DEVICES)
NETWORK_SERIALS = tuple(serial for serial, c in DEVICES.items() if c['address'])
CHECK_EVERY = 5
HEALTH_STALE_AFTER = 30   # 检查、命令和可取消等待都需持续有进展
# 输入通道重连失败后的等待：USB 手机 30 秒；网络设备的 adb 连接断了通常几秒内就回来，3 秒后再试
CONNECT_RETRY_EVERY = 30
CONNECT_RETRY_NETWORK = 3
# 每 5 秒检查的超时：USB 手机 3 秒；网络设备熄屏后 Wi-Fi 省电，偶尔要 5 秒多才回，按 3 秒算会反复误判断开
CHECK_TIMEOUT = 3
CHECK_TIMEOUT_NETWORK = 10
# 网络设备掉线（如 Wi-Fi 断开）时 adb 那条 TCP 连接可能几分钟都显示在线；检查连续失败这么久就主动断开重连
STALE_LINK_AFTER = 20
RELEASE_AFTER = 3         # 网页按着手指但这么久没消息（断网、关页面），自动松手
PAGE = Path(__file__).with_name('index.html')
LOGIN_PAGE = Path(__file__).with_name('login.html')
SESSION_COOKIE = 'droidhub_session'
STREAM_MAX_SIZE = 1600    # 画面长边像素
STREAM_LINGER = 30        # 没人看之后投屏再留这么久：网页来回切设备时不用每次冷启动（约 1 秒）
PART_HEAD = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
STREAM_FPS = 60
SCRCPY_JAR = Path(__file__).with_name('scrcpy-server-v2.4')   # 官方 release，sha256 93c272b7…ce7ba3
SCRCPY_VERSION = '2.4'
SCRCPY_REMOTE = '/data/local/tmp/scrcpy-server.jar'
SCRCPY_PORT = 27183
STREAM_BITRATE = 8000000
FRAME_START = 15         # /frame 冷启动投屏时等首帧的上限
FRAME_WAIT_MAX = 10      # /frame?after= 最多等这么久
STREAM_QUALITY = 4        # MJPEG 质量，2 最好 31 最差
READ_TIMEOUT = 5
SHELL_TIMEOUT = 120
SHELL_OUTPUT = 8 * 1024 * 1024
FILE_MAX = 1024 * 1024 * 1024
FILE_IDLE = 30           # 上传、下载中途无数据的最长等待
MAX_CLIENTS = int(os.environ.get('DROIDHUB_MAX_CLIENTS', '64'))
ALLOWED_HOSTS = CONFIG['hosts']
PASSWORD = CONFIG['password']
AUTHORIZED = set()
LOGIN_LOCK = threading.Lock()   # 网页登录和 HTTP Basic 逐个验证密码，失败后等 1 秒，限制猜密码的速度
CONFIG_CHECK_EVERY = 5
INSTANCE = uuid.uuid4().hex

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%m-%d %H:%M:%S')
log = logging.getLogger('droidhub')

API = {
    'selector': '?serial=<真实序列号> 或 ?phone=<序列号/显示名称>，省略时用默认手机',
    'auth': '设置密码时：程序用 HTTP Basic（用户名任意）；浏览器在 /auth 登录，Cookie 一年有效',
    'get': {'/phones': '手机列表', '/status': '连接状态', '/shot': 'PNG 截图；?x=&y=&w=&h= 只截一块（省略的边到屏幕边），只传覆盖的行，更快',
            '/frame': '投屏的一帧 JPEG（长边 1600，有损）；?after=<上次的 X-Frame-Seq>&wait=1 等到新帧再返回。'
                      '宽高取 8 的倍数，换算截图坐标时宽、高分别按比例；Android 11 及以下包含 FLAG_SECURE 界面；投屏没开时先开，约 1 秒',
            '/health': '服务健康及每台手机状态；至少一台连通为 200',
            '/stream': 'MJPEG 实时画面', '/audio': '应用播放声 PCM 流（s16le / 48000Hz / 双声道）',
            '/audio/status': '采音状态；查询不会启动采集', '/ui': '控件树 JSON；?format=xml 返回 XML',
            '/prop?name=...': '读取一个系统属性',
            '/windows': '焦点窗口、前台界面及各窗口位置（dumpsys，不排队）',
            '/views?package=...': '最上层界面（或指定包最上层界面）的可见视图及屏幕坐标；不依赖 uiautomator，动画页面也能读',
            '/logcat?tag=...&level=V&format=time&tail=1&clear=0&seconds=600': 'logcat 文本流；tag 可重复，省略时全部',
            '/file?path=/sdcard/...': '下载手机上的文件（adb pull 的同步协议），带 Content-Length'},
    'put': {'/file?path=/sdcard/...&mode=644': '请求体原样写入手机文件（adb push 的同步协议），返回 size/sha256；最多 1 GiB'},
    'post': {'/lease?owner=tygbot': {'ttl': 30}, '/release?owner=tygbot': {},
             '/touch': {'type': 'down|move|up', 'x': 540, 'y': 600},
             '/tap': {'x': 540, 'y': 600}, '/long': {'x': 540, 'y': 600, 'secs': 1},
             '/swipe': {'x1': 540, 'y1': 1800, 'x2': 540, 'y2': 700, 'secs': .4},
             '/text': {'text': 'P2', 'replace': True},
             '/key': {'key': 'A', 'modifiers': ['ctrl']},
             '/launch': {'action': '<Intent action>'},
             '/run': {'steps': [{'text': 'P2', 'replace': True}, {'key': 'ENTER'}]},
             '/stop': {'package': '<包名>'}, '/unlock': {},
             '/shell': {'command': 'dumpsys window | grep mCurrentFocus', 'timeout': 10}},
    'text': 'Unicode，最多 512 字符；核对手机剪贴板后粘贴；会更新手机剪贴板；锁屏时用不了，输密码用 /key',
    'audio': 'sndcpy 1.1；需预先安装、授予录音与媒体采集权限；只捕获允许采音的应用，不含麦克风或通话',
    'launch': 'action/component/package 任选一个；可选 data URI',
    'batch': '串行、预先验证全部坐标及参数；失败返回从 0 开始的 failed_step 和已完成 results，不重放动作；断开后停止后续动作',
    'limits': {'body_bytes': 1048576, 'read_seconds': READ_TIMEOUT, 'clients': MAX_CLIENTS,
               'steps': 100, 'duration_seconds': 30, 'text_characters': 512},
    'requests': 'POST 必须为 application/json；浏览器请求限同源；密码见 auth，/health 不需要',
    'coordinates': 'wm size 的自然方向像素；网页按状态的 rotation 做逆旋转，API 不改手机旋转设置',
    'stop': 'am force-stop；系统界面、桌面和 DroidHub 自用的采音应用不能结束',
    'unlock': '设备锁着就点亮并输入配置里该设备的 pin，返回 was_locked；没配密码 400。输一次没解开就停用到服务重启。状态里 unlock 为 true 的设备才能用',
    'shell': 'adb shell 执行一段文本，返回 exit/stdout/stderr；最长 120 秒、输出最多 8 MiB；不排队，调用方自行避免与点屏冲突',
    'adb': '不对外提供 ADB；设备操作都走 HTTP',
    'lease': 'POST /lease?owner=名字 {"ttl":30,"priority":5} 占用一台设备（最长 300 秒，同一 owner 再调就是续期；'
             'priority 0–9 默认 5，比占用方高就直接接管，网页用 0），POST /release?owner=名字 释放；'
             '占用期间别人的输入（点、滑、按键、文字、启动/结束应用、shell、写文件）返回 423 和占用方，读取不受影响；'
             '输入请求带 ?owner= 表明身份；没人占用时不带也能用',
    'devices': '设备表在配置文件（/data/config.json）；网络设备经 droidhub-net 转发、由服务自己 adb connect；/phones 的 input 为 scrcpy 的设备经 scrcpy 控制通道注入触摸和按键，audio 为 false 的设备 /audio 返回 409',
    'video_session': '/phones.instance 标识服务启动；/stream?client=<1–64 位字母数字、下划线或连字符> 的存活订阅出现在 stream.clients；'
                     'POST /stream/stop {"clients":[...]} 结束这些订阅（之后到达的同名订阅返回 410）',
}


LEASE_MAX = 300
LEASE_PRIORITY = 5   # 默认优先级；网页用 0，任何程序都能接管它
OWNER = re.compile(r'[A-Za-z0-9_.:-]{1,64}')
UNGUARDED = object()   # 只读命令：不看占用


CLIENT = re.compile(r'[A-Za-z0-9_-]{1,64}')
STOPPED_KEEP = 120   # 已停订阅记多久：浏览器里还在排队的旧请求到得晚，到了也直接结束


class StreamStops:
    """网页主动停掉的画面订阅。WebKit 去掉 <img> 的 src 不会断开 MJPEG 连接，
    旧连接占满每个网站 6 条的上限后新画面要排队；由服务端按 client 结束这些连接。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}      # client → 正在推流的结束信号
        self.stopped = {}     # client → 停止时间

    def register(self, client):
        with self.lock:
            self._prune()
            if client in self.stopped:
                return None
            event = self.active[client] = threading.Event()
            return event

    def unregister(self, client, event):
        with self.lock:
            if self.active.get(client) is event:
                del self.active[client]

    def stop(self, clients):
        with self.lock:
            self._prune()
            for client in clients:
                self.stopped[client] = time.monotonic()
                if client in self.active:
                    self.active[client].set()

    def _prune(self):
        now = time.monotonic()
        for client in [c for c, t in self.stopped.items() if now - t > STOPPED_KEEP]:
            del self.stopped[client]


stream_stops = StreamStops()


class LeaseHeld(Exception):
    def __init__(self, owner, remaining):
        self.owner, self.remaining = owner, remaining
        super().__init__('设备正被 %s 使用（还剩 %d 秒）' % (owner, math.ceil(remaining)))


def owner_arg(value):
    if value is None:
        return None
    if not isinstance(value, str) or not OWNER.fullmatch(value):
        raise ValueError('owner 要是 1–64 位字母数字、下划线、点、冒号或连字符')
    return value


class StepFailure(Exception):
    def __init__(self, index, results, cause):
        self.index, self.results, self.cause = index, results, cause
        super().__init__(str(cause))


class Phone:
    """一台手机：一个常驻 Touch，所有动作串行；一路实时画面"""

    def __init__(self, serial, scrcpy_port):
        self.serial = serial
        config = DEVICES.get(serial, {})
        self.input = config.get('input', 'uhid')
        self.audio_enabled = config.get('audio', True)
        network = serial in NETWORK_SERIALS
        self.check_timeout = CHECK_TIMEOUT_NETWORK if network else CHECK_TIMEOUT
        self.retry_every = CONNECT_RETRY_NETWORK if network else CONNECT_RETRY_EVERY
        self.lock = threading.Lock()
        self.touch = None
        self.device = Device(serial, self.input)
        self.error = '未连接'
        self.since = None
        self.rotation = None
        self.awake = None         # 亮屏；每 5 秒的检查里读 dumpsys power
        self.lease = None         # (占用方, 到期的 monotonic 时间, 优先级)；只拦输入，不拦读取
        self.lease_lock = threading.Lock()
        self.pressed_at = None    # 网页实时拖动时手指按着，最后一次收到消息的时间
        self.failed = threading.Event()
        self.stop = threading.Event()
        self.last_check = None
        self.last_activity = time.monotonic()
        self.request_cancel = None
        self.retry_at = 0
        self.down_since = None    # 这次断开的起点（monotonic）；连上后清空
        self.unlocking = threading.Lock()
        self.unlock_disabled = False   # 输过一次密码没解开就不再输，免得连续输错触发设备锁定
        self.watch_thread = None
        self.stream = Stream(self, scrcpy_port)
        self.audio = Audio(serial, self.failure)

    def _adb(self, *args, timeout=10):
        cmd = ['adb'] + (['-s', self.serial] if self.serial else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _alive(self):
        # 快路径只检查常驻通道；每个 move 不再另起 adb 进程。
        # Touch 写失败时先 close() 置空 proc，再由上层标记 failed；两者之间也按已断开处理。
        t = self.touch
        return bool(not self.failed.is_set() and t and t.proc is not None and t.proc.poll() is None)

    def failure(self, error):
        self.error = str(error) or type(error).__name__
        if self.down_since is None:
            self.down_since = time.monotonic()
        self.failed.set()

    def holder(self):
        """当前占用 (占用方, 剩余秒数)，没人占用返回 None。"""
        with self.lease_lock:
            if self.lease and self.lease[1] > time.monotonic():
                return self.lease[0], self.lease[1] - time.monotonic()
            self.lease = None
            return None

    def acquire(self, owner, ttl, priority=LEASE_PRIORITY):
        """占用或续期；别人占着且优先级不比他高时抛 LeaseHeld，比他高就接管。"""
        with self.lease_lock:
            now = time.monotonic()
            held = self.lease if self.lease and self.lease[1] > now and self.lease[0] != owner else None
            if held and priority <= held[2]:
                raise LeaseHeld(held[0], held[1] - now)
            if held:
                log.info('%s 接管 %s 对 %s 的占用', owner, held[0], self.serial)
            self.lease = (owner, now + ttl, priority)
        return ttl

    def release(self, owner):
        with self.lease_lock:
            mine = bool(self.lease and self.lease[0] == owner)
            if mine:
                self.lease = None
            return mine

    def guard(self, owner):
        held = self.holder()
        if held and held[0] != owner:
            raise LeaseHeld(*held)

    def progressed(self):
        self.last_activity = time.monotonic()

    def cancelled(self):
        return self.stop.is_set() or bool(self.request_cancel and self.request_cancel())

    def pause(self, seconds):
        pause(seconds, self.cancelled, self.progressed)

    def _connect(self):
        if time.monotonic() < self.retry_at:
            return False
        previous, self.touch, self.pressed_at, self.rotation = self.touch, None, None, None
        for item in (self.device, previous):
            if item:
                try:
                    item.close()
                except Exception as e:
                    log.info('%s 清理连接失败：%s', self.serial, e)
        t = None
        try:
            # adb reconnect 会对 USB 设备执行 reset；这里只重建本手机的应用通道。
            t = ControlTouch(self.serial) if self.input == 'scrcpy' else Touch(self.serial)
            t.open()
        except Exception as e:
            if t:
                try:
                    t.close()
                except Exception:
                    pass
            if self.error != str(e):
                log.info('连接失败：%s', e)
            self.failure(e)
            self.retry_at = time.monotonic() + self.retry_every
            return False
        self.touch, self.error, self.since = t, None, time.time()
        self.retry_at = 0
        self.down_since = None
        self.failed.clear()
        self.progressed()
        log.info('已连接 %s，屏幕 %dx%d', self.serial, t.w, t.h)
        return True

    def ensure(self):
        if self._alive():
            return True
        if self.touch:
            log.info('%s 连接断开，重连：%s', self.serial, self.error or '输入通道进程已退出')
        return self._connect()

    def release_stuck(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.pressed_at and time.time() - self.pressed_at > RELEASE_AFTER:
                log.info('%s 网页按着没松手，自动松开', self.serial)
                self.pressed_at = None
                try:
                    self.touch.up()
                    self.progressed()
                except Exception as e:
                    if connection_error(e):
                        self.failure(e)
        finally:
            self.lock.release()

    def check(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            current = self.touch if self.ensure() and self._alive() else None
        finally:
            self.lock.release()
        # 只读探测不持输入锁，拖动中也能发现旋转；过时结果不能损坏已经重建的新通道。
        try:
            rotation = None
            if current:
                result = self._adb('shell', 'dumpsys input; dumpsys power | grep mWakefulness=',
                                   timeout=self.check_timeout)
                match = re.search(r'Viewport INTERNAL:[^\n]*\borientation=([0-3])\b', result.stdout)
                if result.returncode or not match:
                    raise ConnectionFailure('手机 adb 通道没有响应或读不到屏幕方向')
                rotation = int(match[1])
                awake = re.search(r'mWakefulness=(\w+)', result.stdout)
            if self.touch is current:
                self.last_check = time.time()
                if current and self._alive():
                    # scrcpy 注入按当前方向的画面坐标，需要跟着旋转；uhid 触摸屏不用这个值。
                    self.rotation = current.rotation = rotation
                    self.awake = awake[1] == 'Awake' if awake else None
                    self.progressed()
        except Exception as e:
            if self.touch is current:
                self.failure(e)

    def watch(self):
        last = -float('inf')
        while not self.stop.is_set():
            try:
                self.release_stuck()
                self.stream.expire()
                if time.monotonic() - last >= CHECK_EVERY:
                    last = time.monotonic()
                    self.check()
            except Exception as e:
                self.failure(e)
                log.info('%s 检查失败：%s', self.serial, e)
            self.stop.wait(.5)

    def _locked(self):
        out = self._adb('shell', 'dumpsys window | grep isKeyguardShowing=; dumpsys power | grep mWakefulness=',
                        timeout=self.check_timeout).stdout
        if 'isKeyguardShowing=' not in out:
            raise ConnectionFailure('读不到锁屏状态')
        return 'isKeyguardShowing=true' in out, 'mWakefulness=Awake' in out

    def unlock(self, owner=None):
        """锁屏显示着就点亮、弹出密码键盘、逐个按数字后回车；没锁返回 False。
        密码键盘这一层截图和画面都是全黑，/text 借剪贴板在锁屏下也用不了，所以用按键。"""
        if self.serial not in PINS:
            raise ValueError('这台设备没配锁屏密码')
        if not self.unlocking.acquire(timeout=20):
            raise RuntimeError('正在解锁')
        try:
            if self.unlock_disabled:
                raise RuntimeError('自动解锁已停用：上次输入密码没解开，核对 pins.env 后重启服务')
            locked, awake = self._locked()
            if not locked:
                return False
            def fn(p):
                if not awake:
                    p.device.key('POWER', [])
                    p.pause(.5)
                # 安全锁屏直接弹出密码键盘，不用模拟上滑
                p._adb('shell', 'wm dismiss-keyguard', timeout=5)
                p.pause(.8)
                for digit in PINS[p.serial]:
                    p.device.key(digit, [])
                p.device.key('ENTER', [])
            self.command(fn, owner=owner)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                self.pause(.5)
                try:
                    if not self._locked()[0]:
                        break
                except (ConnectionFailure, subprocess.SubprocessError, OSError):
                    pass   # 「默认 USB 配置」不是不传输数据的设备，解锁时 adbd 会重启
            else:
                # 连续输错会触发设备的锁定倒计时，没解开就不再自动输
                self.unlock_disabled = True
                log.info('%s 输入密码后仍锁着，停用自动解锁到服务重启', self.serial)
                raise RuntimeError('输入密码后仍锁着，已停用自动解锁；核对 pins.env 后重启服务')
            log.info('%s 已解锁', self.serial)
            return True
        finally:
            self.unlocking.release()

    def finger(self, kind, x=None, y=None, cancel=None, owner=None):
        """网页实时拖动：down / move / up"""
        def fn(t):
            if kind == 'up':
                t.up()
                self.pressed_at = None
            else:
                t.down(x, y)
                self.pressed_at = time.time()
        # 松手总是放行：占用换人时也不能把手指留在屏幕上
        self.do(fn, cancel, UNGUARDED if kind == 'up' else owner)

    def do(self, fn, cancel=None, owner=None):
        return self.command(lambda p: fn(p.touch), cancel, keep_gesture=True, owner=owner)

    def shot(self, box=None):
        # 截图只读 ADB；触屏未就绪时也可尝试，不排队，不触发重连。
        if self.stop.is_set():
            raise RuntimeError('服务正在停止')
        try:
            return self.device.shot(box)
        except Exception as e:
            if connection_error(e):
                self.failure(e)
            raise

    def read(self, fn):
        """只读查询（窗口、视图）：不排队、不要求触屏；连接错误照常记为失败，由检查线程恢复。"""
        if self.stop.is_set():
            raise RuntimeError('服务正在停止')
        try:
            return fn(self.device)
        except Exception as e:
            if connection_error(e):
                self.failure(e)
            raise

    def command(self, fn, cancel=None, *, require_touch=True, keep_gesture=False, owner=UNGUARDED):
        """原子命令只执行一次；文字已输入一部分时不能自动重放。
        owner 不是 UNGUARDED 时是输入：排到队后再核对占用，别人占着就不执行。"""
        def cancelled():
            return self.stop.is_set() or (cancel and cancel())
        while not self.lock.acquire(timeout=.25):
            if cancelled():
                raise RequestCancelled('请求已取消')
        try:
            if cancelled():
                raise RequestCancelled('请求已取消')
            if owner is not UNGUARDED:
                self.guard(owner)
            if require_touch and not self.ensure():
                raise RuntimeError('手机未连接：' + self.error)
            if cancelled():
                raise RequestCancelled('请求已取消')
            validate_step(fn, self.touch)
            self.request_cancel = cancel
            self.progressed()
            try:
                # 自动化接管前结束网页留下的接触点，长批次不能把旧手指一直按着。
                if not keep_gesture and self.pressed_at is not None:
                    try:
                        self.touch.up()
                    finally:
                        self.pressed_at = None
                result = fn(self)
                self.progressed()
                return result
            except Exception as e:
                if connection_error(e):
                    self.failure(e)
                raise
        finally:
            self.request_cancel = None
            self.lock.release()

    def run(self, steps, cancel=None, owner=None):
        def execute(phone):
            for step in steps:
                validate_step(step, phone.touch)
            results = []
            for i, step in enumerate(steps):
                try:
                    if self.cancelled():
                        raise RequestCancelled('请求已取消')
                    results.append(step(phone))
                    self.progressed()
                except Exception as e:
                    if connection_error(e):
                        self.failure(e)
                    raise StepFailure(i, results, e) from e
            return results
        execute.coordinates = tuple(xy for step in steps for xy in getattr(step, 'coordinates', ()))
        return self.command(execute, cancel, owner=owner)

    def status(self):
        t = self.touch
        alive = bool(t and self.error is None and not self.failed.is_set()
                     and t.proc is not None and t.proc.poll() is None)
        stale = time.monotonic() - self.last_activity > HEALTH_STALE_AFTER
        return {'connected': alive and not stale, 'serial': self.serial,
                'name': PHONE_NAMES.get(self.serial, self.serial), 'input': self.input,
                'audio': self.audio_enabled, 'unlock': self.serial in PINS,
                'size': [t.w, t.h] if t else None, 'density': getattr(t, 'density', None) if t else None,
                'since': self.since, 'rotation': self.rotation, 'awake': self.awake,
                'error': self.error or ('手机控制检查已超时' if alive and stale else None),
                'last_check': self.last_check, 'stream': self.stream.status(),
                'lease': (lambda h: {'owner': h[0], 'expires_in': round(h[1], 1)} if h else None)(self.holder())}


class Fleet:
    """设备表里的所有设备：不在 adb 列表里也一直显示为未连接；网络设备由这里发起 adb connect。"""

    def __init__(self):
        self.phones = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.heartbeat = None
        self.connect_at = {}
        self.connect_result = {}

    def get(self, serial=None):
        with self.lock:
            if serial:
                if serial in self.phones:
                    return self.phones[serial]
                matches = [p for p in self.phones.values() if PHONE_NAMES.get(p.serial) == serial]
                return matches[0] if len(matches) == 1 else None
            if DEFAULT_SERIAL in self.phones:
                return self.phones[DEFAULT_SERIAL]
            return self.phones[min(self.phones)] if self.phones else None

    def all(self):
        with self.lock:
            return [self.phones[k] for k in sorted(self.phones)]

    def _add(self, serial):
        if ALLOWED_SERIALS and serial not in ALLOWED_SERIALS:
            return
        with self.lock:
            if serial not in self.phones:
                self.phones[serial] = Phone(serial, SCRCPY_PORT + len(self.phones))
                log.info('发现手机 %s', serial)

    def _discover(self):
        for serial in ALLOWED_SERIALS:
            self._add(serial)
        out = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10).stdout
        states = dict(line.split('\t')[:2] for line in out.splitlines()[1:] if '\t' in line)
        for serial, state in states.items():
            if state == 'device':
                self._add(serial)
        # 发现只读状态；offline/unauthorized 由每台手机限速等待恢复，不重启 ADB server。
        for serial in NETWORK_SERIALS:
            state, phone = states.get(serial), self.phones.get(serial)
            if state == 'device' and phone and phone.down_since is not None \
                    and time.monotonic() - phone.down_since >= STALE_LINK_AFTER:
                state = 'offline'   # adb 还显示在线，但设备已经不响应：按断开处理
            if state != 'device':
                self._connect_network(serial, state)

    def _connect_network(self, serial, state):
        """网络设备没连上时限速发起 adb connect；只动这一条 TCP 连接，不碰 USB 和 ADB server。"""
        now = time.monotonic()
        if now < self.connect_at.get(serial, 0):
            return
        self.connect_at[serial] = now + CONNECT_RETRY_EVERY
        if state == 'unauthorized':
            return   # 等设备上点允许；重连不会改变授权
        if state == 'offline':
            # 断掉的 TCP 连接留在列表里时 connect 只回 already connected，先摘掉这一条。
            subprocess.run(['adb', 'disconnect', serial], capture_output=True, text=True, timeout=10)
        try:
            result = subprocess.run(['adb', 'connect', serial], capture_output=True, text=True, timeout=15)
            output = (result.stdout + result.stderr).strip() or str(result.returncode)
        except subprocess.TimeoutExpired:
            output = 'adb connect 15 秒没有返回'
        phone = self.phones.get(serial)
        if output.startswith('connected to') and phone:
            phone.retry_at = 0   # 连接刚恢复，下一次检查就重建通道，不等重试间隔
        # 设备长时间不在时每 30 秒重试一次，日志只记原因变化
        if output != self.connect_result.get(serial):
            log.info('连接 %s：%s', serial, output)
        self.connect_result[serial] = output

    def watch(self):
        while not self.stop.is_set():
            self.heartbeat = time.monotonic()
            try:
                self._discover()
            except Exception as e:
                log.info('查找手机失败：%s', e)
            for p in self.all():
                if not p.watch_thread or not p.watch_thread.is_alive():
                    p.watch_thread = threading.Thread(target=p.watch, name='phone-' + p.serial, daemon=True)
                    p.watch_thread.start()
            self.stop.wait(CHECK_EVERY)

    def close(self, timeout=20):
        deadline = time.monotonic() + timeout
        phones = self.all()
        self.stop.set()
        for p in phones:
            p.stop.set()
            p.stream.close()
            with p.audio.cond:
                p.audio.stop.set()
                if p.audio.sock:
                    try:
                        p.audio.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                p.audio.cond.notify_all()
        def cleanup(p):
            if p.lock.acquire(timeout=max(0, min(1, deadline - time.monotonic()))):
                try:
                    for item in (p.device, p.touch):
                        if item:
                            try:
                                item.close()
                            except Exception:
                                pass
                finally:
                    p.lock.release()
        # 各手机清理并行；预算包含清理及等待，卡住的客户端随网页退出由 init 回收。
        # 独立 ADB server 不属于网页容器，不会在这里被终止或释放 USB。
        cleaners = [threading.Thread(target=cleanup, args=(p,), daemon=True) for p in phones]
        for thread in cleaners:
            thread.start()
        for p in phones:
            for thread in (p.watch_thread, p.stream.thread, p.audio.thread):
                if thread:
                    thread.join(timeout=max(0, deadline - time.monotonic()))
        for thread in cleaners:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    def healthy(self):
        return ((self.heartbeat is None or time.monotonic() - self.heartbeat < 60)
                and any(p.status()['connected'] for p in self.all()))


fleet = Fleet()


class Stream:
    """每台共享一次投屏；停止、断流和子进程失败都会唤醒订阅者。"""

    def __init__(self, phone, port):
        self.phone, self.port = phone, port
        self.cond = threading.Condition()
        self.frame, self.seq, self.viewers = None, 0, 0
        self.clients = Counter()
        self.thread = self.sock = None
        self.stop = threading.Event()
        self.error = self.frame_at = None
        self.idle_since = None    # 最后一个人离开的时间；检查线程到点关掉投屏

    def status(self):
        with self.cond:
            age = time.monotonic() - self.frame_at if self.frame_at is not None else None
            state = ('error' if self.error else 'stopping' if self.thread and self.stop.is_set() else
                     'idle' if not self.thread else 'streaming' if self.frame is not None else 'starting')
            return {'state': state, 'viewers': self.viewers, 'error': self.error,
                    'clients': sorted(self.clients),
                    'frame_age': round(age, 2) if age is not None else None}

    def join(self, client=None):
        with self.cond:
            deadline = time.monotonic() + 10
            while (self.thread and self.stop.is_set()) or (not self.thread and self.viewers):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('画面正在停止，请稍后重试')
                self.cond.wait(remaining)
            self.viewers += 1
            self.idle_since = None
            if not self.thread:
                self.stop.clear()
                self.frame = self.frame_at = self.error = None
                self.thread = threading.Thread(target=self._run, daemon=True)
                try:
                    self.thread.start()
                except Exception:
                    self.thread = None
                    self.viewers -= 1
                    raise
            if client:
                self.clients[client] += 1

    def leave(self, client=None):
        with self.cond:
            if client and self.clients[client]:
                self.clients[client] -= 1
                if not self.clients[client]:
                    del self.clients[client]
            self.viewers = max(0, self.viewers - 1)
            if not self.viewers:
                if STREAM_LINGER > 0:
                    self.idle_since = time.monotonic()
                else:
                    self.close()
            self.cond.notify_all()

    def expire(self):
        """没人看满 STREAM_LINGER 秒就关掉投屏；由手机的检查线程每 0.5 秒调一次。"""
        with self.cond:
            if not self.viewers and self.idle_since is not None and \
                    time.monotonic() - self.idle_since >= STREAM_LINGER:
                self.idle_since = None
                self.close()

    def close(self):
        with self.cond:
            self.stop.set()
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.cond.notify_all()

    def wait(self, seq, timeout=10):
        with self.cond:
            self.cond.wait_for(lambda: (self.frame is not None and self.seq != seq)
                              or self.error or self.stop.is_set(), timeout)
            if self.error:
                raise RuntimeError(self.error)
            if self.stop.is_set():
                raise RuntimeError('画面已停止')
            if self.seq == seq or self.frame is None:
                return seq, None
            return self.seq, self.frame

    def latest(self, after=None, wait=0.0):
        """最新一帧 (seq, jpg, 帧龄秒)。画面静止时编码器不出新帧，最新一帧就是当前画面。
        after 给了时最多等 wait 秒，等到序号不同于 after 的帧；等不到也返回最新一帧。还没有首帧时最多等 FRAME_START 秒。"""
        with self.cond:
            seq, cached = self.seq, self.frame
        if cached is None:
            deadline = time.monotonic() + FRAME_START
            while cached is None:
                seq, cached = self.wait(seq, timeout=max(0, min(.25, deadline - time.monotonic())))
                if cached is None and time.monotonic() >= deadline:
                    raise RuntimeError('画面启动超时，请重新连接')
        if after is not None and seq == after and wait > 0:
            self.wait(seq, timeout=wait)
        with self.cond:
            if self.frame is None:
                raise RuntimeError(self.error or '画面已停止')
            return self.seq, self.frame, time.monotonic() - self.frame_at

    def _publish(self, jpg):
        with self.cond:
            self.frame, self.seq, self.frame_at = jpg, self.seq + 1, time.monotonic()
            self.cond.notify_all()

    def _run(self):
        log.info('%s 开始推画面', self.phone.serial)
        try:
            with self.cond:
                if self.viewers <= 0:
                    return
            t = self.phone.touch
            if not t:
                raise RuntimeError('手机未连接')
            self._session(t)
            if not self.stop.is_set():
                raise RuntimeError('画面已断开')
        except Exception as e:
            if not self.stop.is_set():
                if connection_error(e):
                    self.phone.failure(e)
                with self.cond:
                    self.error = str(e) or '画面中断'
                    self.frame = self.frame_at = None
                    self.cond.notify_all()
                log.info('%s 画面中断：%s', self.phone.serial, e)
        finally:
            with self.cond:
                self.thread = self.frame = self.frame_at = None
                self.cond.notify_all()
            log.info('%s 停止推画面', self.phone.serial)

    def _session(self, t):
        adb, server, ff, sock, feeder = t.adb, None, None, None, None
        forwarded = False
        try:
            run_command(adb + ['push', str(SCRCPY_JAR), SCRCPY_REMOTE], 8, self.stop)
            forwarded = True  # forward 也可能已经生效后才超时，仍需要清理。
            run_command(adb + ['forward', 'tcp:%d' % self.port, 'localabstract:scrcpy'], 3, self.stop)
            if self.stop.is_set():
                return
            server = subprocess.Popen(adb + ['shell', 'CLASSPATH=' + SCRCPY_REMOTE, 'app_process', '/',
                'com.genymobile.scrcpy.Server', SCRCPY_VERSION, 'tunnel_forward=true', 'audio=false',
                'control=false', 'cleanup=true', 'raw_stream=true',
                'max_size=%d' % min(STREAM_MAX_SIZE, max(t.w, t.h)),
                'video_bit_rate=%d' % STREAM_BITRATE, 'max_fps=%d' % STREAM_FPS],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline, first = time.monotonic() + 5, b''
            while time.monotonic() < deadline and not self.stop.is_set():
                try:
                    sock = socket.create_connection((adb_forward_host(), self.port), timeout=.3)
                    first = sock.recv(65536)
                    if first:
                        break
                except OSError:
                    pass
                if sock:
                    sock.close()
                    sock = None
                self.stop.wait(.1)
            if self.stop.is_set():
                return
            if not sock or not first:
                raise RuntimeError('scrcpy 服务端没起来')
            sock.settimeout(.5)
            with self.cond:
                self.sock = sock
            ff = subprocess.Popen(['ffmpeg', '-loglevel', 'error', '-flags', 'low_delay', '-probesize', '32',
                '-analyzeduration', '0', '-threads', '1', '-f', 'h264', '-i', 'pipe:0', '-fps_mode', 'passthrough',
                # 输入侧 threads 不限制 JPEG 编码器；自动帧线程会额外排队，滤镜也会按主机 CPU 数起线程。
                '-filter_threads', '1', '-f', 'image2pipe', '-c:v', 'mjpeg', '-threads:v', '1',
                '-q:v', str(STREAM_QUALITY), 'pipe:1'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            worker = threading.Thread(target=self._feed, args=(sock, ff, first), daemon=True)
            worker.start()
            feeder = worker
            buf, first_frame_deadline = b'', time.monotonic() + 12
            while not self.stop.is_set():
                if self.phone.failed.is_set():
                    raise ConnectionFailure('手机连接中断')
                if first_frame_deadline and time.monotonic() > first_frame_deadline:
                    raise RuntimeError('画面首帧超时，请重新连接')
                if not select.select([ff.stdout], [], [], .25)[0]:
                    continue
                chunk = ff.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 8 * 1024 * 1024:
                    raise RuntimeError('画面数据无效')
                while True:
                    end = buf.find(b'\xff\xd9')
                    if end < 0:
                        break
                    start = buf.find(b'\xff\xd8')
                    if 0 <= start < end:
                        self._publish(buf[start:end + 2])
                        # 安卓编码器在静止画面下可能停止产帧；用 adb 看门狗和流 EOF 判定断连。
                        first_frame_deadline = None
                    buf = buf[end + 2:]
        finally:
            with self.cond:
                self.sock = None
            if sock:
                sock.close()
            # 先杀解码器，唤醒可能阻塞在 stdin.write 的 feeder。
            for proc in (ff, server):
                if proc:
                    try:
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait(timeout=2)
                    except OSError:
                        pass
            if feeder:
                feeder.join(timeout=1)
            if ff:
                ff.stdout.close()
                if not feeder:
                    ff.stdin.close()
            if forwarded:
                try:
                    run_command(adb + ['forward', '--remove', 'tcp:%d' % self.port], 2)
                except Exception:
                    pass

    def _feed(self, sock, ff, first):
        try:
            ff.stdin.write(first)
            ff.stdin.flush()
            while not self.stop.is_set():
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                ff.stdin.write(data)
                ff.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                ff.stdin.close()
            except OSError:
                pass


def shot_box(query):
    """/shot 的区块参数 x、y、w、h（截图像素，当前方向）；都没给返回 None 截整屏。w、h 省略表示到边。"""
    if not any(k in query for k in ('x', 'y', 'w', 'h')):
        return None
    values = []
    for key in ('x', 'y', 'w', 'h'):
        raw = query.get(key, [None])[0]
        try:
            value = int(raw) if raw is not None else None
        except ValueError:
            value = -1
        if value is not None and not (0 <= value <= 100000) or key in ('w', 'h') and value == 0:
            raise ValueError('%s 要是正整数像素' % key if key in ('w', 'h') else '%s 要是非负整数像素' % key)
        values.append(value)
    x, y, w, h = values
    return x or 0, y or 0, w, h


def num(body, key, default=None, cast=int):
    v = body.get(key, default)
    if v is None:
        raise ValueError('缺少 ' + key)
    if isinstance(v, bool) or (isinstance(v, float) and not math.isfinite(v)):
        raise ValueError(key + ' 要是有限数值')
    return cast(v)


def validate_step(step, touch):
    for x, y in getattr(step, 'coordinates', ()):
        if not (0 <= x < touch.w and 0 <= y < touch.h):
            raise ValueError('坐标超出屏幕 %dx%d：(%s, %s)' % (touch.w, touch.h, x, y))


def pointer_action(fn, *coordinates):
    fn.coordinates = coordinates
    return fn


def action(body):
    """一个动作：{"tap": [x, y]} 这类，返回在手机上执行的函数"""
    kinds = {'tap', 'long', 'swipe', 'wait', 'text', 'key', 'launch', 'ui', 'prop', 'stop'}
    if len(kinds.intersection(body)) != 1:
        raise ValueError('每一步必须且只能包含一个动作')
    def duration(key, default):
        value = float(body.get(key, default))
        if not 0 <= value <= 30:
            raise ValueError(key + ' 要在 0–30 秒')
        return value
    if 'tap' in body:
        x, y = [num({'value': v}, 'value') for v in body['tap']]
        hold = duration('hold', 0.08)
        return pointer_action(lambda p: p.touch.tap(x, y, hold, cancel=p.cancelled, progress=p.progressed), (x, y))
    if 'long' in body:
        x, y = [num({'value': v}, 'value') for v in body['long']]
        secs = duration('secs', 1.0)
        return pointer_action(lambda p: p.touch.long_press(x, y, secs, cancel=p.cancelled, progress=p.progressed), (x, y))
    if 'swipe' in body:
        x1, y1, x2, y2 = [num({'value': v}, 'value') for v in body['swipe']]
        secs = duration('secs', 0.3)
        return pointer_action(lambda p: p.touch.swipe(x1, y1, x2, y2, secs, cancel=p.cancelled, progress=p.progressed),
                              (x1, y1), (x2, y2))
    if 'wait' in body:
        secs = float(body['wait'])
        if not 0 <= secs <= 30:
            raise ValueError('wait 要在 0–30 秒')
        return lambda p: p.pause(secs)
    if 'text' in body:
        text, replace = text_args(body)
        return lambda p: p.device.text(text, replace)
    if 'key' in body:
        key, modifiers = key_args(body)
        return lambda p: p.device.key(key, modifiers)
    if 'launch' in body:
        args = launch_args(body['launch'])
        return lambda p: p.device.launch(args)
    if 'ui' in body:
        if body['ui'] is not True:
            raise ValueError('ui 要是 true')
        return lambda p: p.device.ui()
    if 'prop' in body:
        name = prop_name(body['prop'])
        return lambda p: p.device.prop(name)
    if 'stop' in body:
        package = package_name(body['stop'], stop=True)
        return lambda p: p.device.stop(package)
    raise ValueError('不认识的动作：' + json.dumps(body, ensure_ascii=False))


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler, max_clients=MAX_CLIENTS):
        self.slots = threading.BoundedSemaphore(max_clients)
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'   # 长连接：拖动时每秒几十个请求，不用每次重新建连
    disable_nagle_algorithm = True  # 小响应和帧尾立即发送，避免与客户端延迟 ACK 叠加等待。
    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT)

    def _expire_header(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def handle_one_request(self):
        self.header_timer = threading.Timer(READ_TIMEOUT, self._expire_header)
        self.header_timer.daemon = True
        self.header_timer.start()
        try:
            super().handle_one_request()
        finally:
            self.header_timer.cancel()

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            self.header_timer.cancel()

    def send_response(self, code, message=None):
        self.connection_header = False
        super().send_response(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() == 'connection':
            self.connection_header = True
        super().send_header(keyword, value)

    def end_headers(self):
        # 回应后要关闭连接时告诉客户端，免得它在这条连接上接着发请求
        if self.close_connection and not self.connection_header:
            self.send_header('Connection', 'close')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

    def _allowed(self, protect=True):
        host = self.headers.get('Host', '')
        try:
            if not config.host_allowed(urlparse('//' + host).hostname or '', ALLOWED_HOSTS):
                raise ValueError('不允许的 Host；用域名访问时要把它加进配置的 hosts')
            origin = self.headers.get('Origin')
            # 同一 IP 其他端口的网页对浏览器是 same-site；<img>/<audio> 的 GET 不带 Origin，也要拒绝。
            if protect and ((origin and origin != 'http://' + host) or
                            self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site')):
                raise ValueError('不允许跨站访问手机接口')
        except ValueError as e:
            self.close_connection = True
            self._json(403, {'ok': False, 'detail': str(e)})
            return False
        if PASSWORD and urlparse(self.path).path not in ('/health', '/auth') and not self._authorized():
            self.close_connection = True
            # 浏览器打开页面时转到登录页；接口请求返回 401，不带 WWW-Authenticate，避免浏览器弹出密码框
            if self.command == 'GET' and 'text/html' in self.headers.get('Accept', ''):
                self.send_response(302)
                self.send_header('Location', '/auth?next=' + quote(self.path, safe=''))
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                self._json(401, {'ok': False, 'detail': '需要登录'})
            return False
        return True

    def _authorized(self):
        """登录 Cookie 或 HTTP Basic（用户名任意）。验证过的 Basic 请求头记下来，免得每个请求都算一遍 PBKDF2（约 0.1 秒）。"""
        session = config.cookie_value(self.headers.get('Cookie'), SESSION_COOKIE)
        if session and config.check_session(session, PASSWORD, 'droidhub'):
            return True
        header = self.headers.get('Authorization', '')
        if header in AUTHORIZED:
            return True
        if not header.startswith('Basic '):
            return False
        try:
            password = base64.b64decode(header[6:], validate=True).decode('utf-8').partition(':')[2]
        except ValueError:
            return False
        with LOGIN_LOCK:
            if not config.check_password(password, PASSWORD):
                time.sleep(1)
                return False
        if len(AUTHORIZED) < 64:
            AUTHORIZED.add(header)
        return True

    def _disconnected(self):
        try:
            return bool(select.select([self.connection], [], [], 0)[0]) and not self.connection.recv(
                1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _read_json(self):
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise ValueError('Content-Type 必须为 application/json')
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('不支持 Transfer-Encoding，请提供 Content-Length')
        n = int(self.headers.get('Content-Length') or 0)
        if not 0 <= n <= 1024 * 1024:
            raise ValueError('请求体大小无效，最多 1 MiB')
        remaining, parts, deadline = n, [], time.monotonic() + READ_TIMEOUT
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError('读取请求体超时')
            self.connection.settimeout(timeout)
            chunk = self.rfile.read1(min(remaining, 65536))
            if not chunk:
                raise ValueError('请求体不完整')
            parts.append(chunk)
            remaining -= len(chunk)
        self.connection.settimeout(READ_TIMEOUT)
        def invalid_constant(value):
            raise ValueError('不允许非有限数值：' + value)
        return json.loads(b''.join(parts) or b'{}', parse_constant=invalid_constant)

    def _send(self, code, content_type, data, cache=False):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if not cache:
            self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, 'application/json; charset=utf-8', json.dumps(obj, ensure_ascii=False).encode())

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _login(self, body):
        """网页登录：密码正确时发一年有效的 Cookie。"""
        password = body.get('password')
        if not isinstance(password, str):
            raise ValueError('需要 password')
        if not PASSWORD:
            return self._json(200, {'ok': True})
        with LOGIN_LOCK:
            if not config.check_password(password, PASSWORD):
                time.sleep(1)
                return self._json(401, {'ok': False, 'detail': '密码错误'})
        body = json.dumps({'ok': True}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Set-Cookie', '%s=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax' % (
            SESSION_COOKIE, config.session_token(PASSWORD, 'droidhub'), config.SESSION_SECONDS))
        self.end_headers()
        self.wfile.write(body)

    def _lease_held(self, e):
        self._json(423, {'ok': False, 'detail': str(e), 'owner': e.owner, 'expires_in': round(e.remaining, 1)})

    def _phone(self):
        """按 ?serial= 选手机；没有就回 404 并返回 None"""
        query = parse_qs(urlparse(self.path).query)
        serial = query.get('serial', query.get('phone', [None]))[0]
        p = fleet.get(serial)
        if not p:
            self._json(404, {'ok': False, 'detail': '没有这台手机：%s' % serial if serial else '没有接手机'})
        return p

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._allowed(protect=path not in ('/', '/index.html', '/auth')):
            return
        if path == '/auth':
            if not PASSWORD:
                return self._redirect('/')
            return self._send(200, 'text/html; charset=utf-8', LOGIN_PAGE.read_bytes(), cache=True)
        if path == '/api':
            return self._json(200, API)
        if path == '/phones':
            default = fleet.get()
            return self._json(200, {'instance': INSTANCE, 'default': default.serial if default else None,
                                    'phones': [p.status() for p in fleet.all()]})
        if path == '/health':
            ok = fleet.healthy()
            return self._json(200 if ok else 503, {'ok': ok, 'phones': [p.status() for p in fleet.all()]})
        if path == '/status':
            phone = self._phone()
            return phone and self._json(200, phone.status())
        if path in ('/audio/status', '/audio'):
            phone = self._phone()
            if phone and not phone.audio_enabled:
                return self._json(409, {'ok': False, 'detail': '这台设备没开采音'})
            if path == '/audio':
                return phone and self._audio(phone)
            return phone and self._json(200, dict(phone.audio.status(), serial=phone.serial))
        if path in ('/ui', '/prop'):
            phone = self._phone()
            if not phone:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                if path == '/prop':
                    name = prop_name(query.get('name', [None])[0])
                    value = phone.command(lambda p: p.device.prop(name), self._disconnected, require_touch=False)
                    return self._json(200, {'ok': True, 'serial': phone.serial, 'name': name, 'value': value})
                format = query.get('format', ['json'])[0]
                if format not in ('json', 'xml'):
                    raise ValueError('format 要是 json 或 xml')
                data = phone.command(lambda p: p.device.ui(format), self._disconnected, require_touch=False)
                if format == 'json':
                    return self._json(200, dict(data, ok=True, serial=phone.serial))
                return self._send(200, 'application/xml; charset=utf-8', data.encode('utf-8'))
            except ValueError as e:
                return self._json(400, {'ok': False, 'detail': str(e)})
            except Exception as e:
                return self._json(503, {'ok': False, 'detail': str(e)})
        if path in ('/windows', '/views'):
            phone = self._phone()
            if not phone:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                if path == '/windows':
                    data = phone.read(lambda d: d.windows())
                else:
                    package = query.get('package', [None])[0]
                    package = package and package_name(package)
                    data = phone.read(lambda d: d.views(package))
                return self._json(200, dict(data, ok=True, serial=phone.serial))
            except ValueError as e:
                return self._json(400, {'ok': False, 'detail': str(e)})
            except Exception as e:
                return self._json(503, {'ok': False, 'detail': str(e)})
        if path == '/logcat':
            phone = self._phone()
            return phone and self._logcat(phone)
        if path == '/file':
            phone = self._phone()
            return phone and self._pull(phone)
        if path in ('/', '/index.html'):
            return self._send(200, 'text/html; charset=utf-8', PAGE.read_bytes(), cache=True)
        if path == '/stream':
            phone = self._phone()
            return phone and self._stream(phone)
        if path == '/frame':
            phone = self._phone()
            return phone and self._frame(phone)
        if path == '/shot':
            phone = self._phone()
            if not phone:
                return
            try:
                png = phone.shot(shot_box(parse_qs(urlparse(self.path).query)))
            except ValueError as e:
                return self._json(400, {'ok': False, 'detail': str(e)})
            except Exception as e:
                return self._json(503, {'ok': False, 'detail': str(e)})
            return self._send(200, 'image/png', png)
        self._json(404, {'ok': False, 'detail': '没有这个接口'})

    def _frame(self, phone):
        """投屏的一帧 JPEG（长边 STREAM_MAX_SIZE）。投屏没开时先开，取完照常保留 STREAM_LINGER 秒，连续取帧不用冷启动。"""
        query = parse_qs(urlparse(self.path).query)
        try:
            after = query.get('after', [None])[0]
            after = int(after) if after is not None else None
            wait = float(query.get('wait', ['1'])[0])
            if not 0 <= wait <= FRAME_WAIT_MAX:
                raise ValueError
        except ValueError:
            return self._json(400, {'ok': False, 'detail': 'after 要是整数，wait 要在 0–%d 秒' % FRAME_WAIT_MAX})
        if not phone.touch or phone.error:
            return self._json(503, {'ok': False, 'detail': '手机未连接：' + (phone.error or '')})
        stream, joined = phone.stream, False
        try:
            stream.join()
            joined = True
            seq, jpg, age = stream.latest(after, wait)
        except RuntimeError as e:
            return self._json(503, {'ok': False, 'detail': str(e)})
        finally:
            if joined:
                stream.leave()
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(jpg)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Seq', str(seq))
        self.send_header('X-Frame-Age', '%.3f' % age)
        self.end_headers()
        self.wfile.write(jpg)

    def _stream(self, phone):
        client = parse_qs(urlparse(self.path).query).get('client', [None])[0]
        if client is not None and not CLIENT.fullmatch(client):
            return self._json(400, {'ok': False, 'detail': 'client 要是 1–64 位字母、数字、下划线或连字符'})
        if not phone.touch or phone.error:
            return self._json(503, {'ok': False, 'detail': '手机未连接：' + (phone.error or '')})
        stream, joined, started, seq = phone.stream, False, False, -1
        self.close_connection = True
        stopped = stream_stops.register(client) if client else threading.Event()
        who, t0, frames = '%s %s' % (self.client_address[0], client or '-'), time.monotonic(), 0
        if stopped is None:
            log.info('画面订阅 %s %s 到达时已被网页停掉', phone.serial, who)
            return self._json(410, {'ok': False, 'detail': '这个画面订阅已停止'})
        log.info('画面订阅 %s %s 开始', phone.serial, who)
        try:
            stream.join(client)
            joined = True
            deadline = time.monotonic() + 15
            while not self._disconnected() and not stopped.is_set():
                seq, jpg = stream.wait(seq, timeout=.25)
                if jpg is None:
                    if not started and time.monotonic() >= deadline:
                        raise RuntimeError('画面启动超时，请重新连接')
                    continue
                if not started:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
                    self.connection.settimeout(3)
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(PART_HEAD)
                    started = True
                    log.info('画面订阅 %s %s 首帧 %.2f 秒', phone.serial, who, time.monotonic() - t0)
                # 帧后面紧跟下一段的分隔符和段头一起发：WebKit 和 Chromium 都要读到下一段的段头
                # 才显示这一帧，画面静止不出新帧时最后一帧会一直不显示。段头因此不带 Content-Length
                self.wfile.write(jpg + b'\r\n' + PART_HEAD)
                self.wfile.flush()
                frames += 1
        except (RuntimeError, OSError) as e:
            if not started and not self._disconnected():
                try:
                    self._json(503, {'ok': False, 'detail': str(e)})
                except OSError:
                    pass
        finally:
            log.info('画面订阅 %s %s 结束：%s，%.1f 秒 %d 帧', phone.serial, who,
                     '网页停掉' if stopped.is_set() else '连接断开或出错', time.monotonic() - t0, frames)
            if client:
                stream_stops.unregister(client, stopped)
            if joined:
                stream.leave(client)

    def _audio(self, phone):
        if not phone.touch or phone.error:
            return self._json(503, {'ok': False, 'detail': '手机未连接：' + (phone.error or '')})
        audio, epoch, started = phone.audio, None, False
        self.close_connection = True
        try:
            epoch, seq = audio.join()
            deadline = time.monotonic() + 12
            while not self._disconnected():
                seq, data = audio.wait(epoch, seq, timeout=.25)
                if not data:
                    if not started and time.monotonic() >= deadline:
                        raise RuntimeError('声音启动超时，请重新开启')
                    continue
                if not started:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024)
                    self.connection.settimeout(3)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Audio-Sample-Rate', str(SAMPLE_RATE))
                    self.send_header('X-Audio-Channels', str(CHANNELS))
                    self.send_header('X-Audio-Format', FORMAT)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    started = True
                self.wfile.write(data)
                self.wfile.flush()
        except (RuntimeError, OSError) as error:
            if not started and not self._disconnected():
                try:
                    self._json(503, {'ok': False, 'detail': str(error)})
                except OSError:
                    pass
        finally:
            if epoch is not None:
                audio.leave(epoch)

    def _logcat(self, phone):
        query = parse_qs(urlparse(self.path).query)
        try:
            tags = query.get('tag', [])
            if len(tags) > 16 or any(not re.fullmatch(r'[\w.$-]{1,64}', t) for t in tags):
                raise ValueError('tag 最多 16 个，每个 1–64 位字母数字或 ._$-')
            level = query.get('level', ['V'])[0]
            fmt = query.get('format', ['time'])[0]
            tail, seconds = int(query.get('tail', ['1'])[0]), int(query.get('seconds', ['600'])[0])
            clear = query.get('clear', ['0'])[0]
            if level not in tuple('VDIWEF') or fmt not in ('raw', 'brief', 'time', 'threadtime', 'epoch') \
                    or not 1 <= tail <= 10000 or not 1 <= seconds <= 3600 or clear not in ('0', '1'):
                raise ValueError('level 为 VDIWEF 之一，format 为 raw/brief/time/threadtime/epoch，'
                                 'tail 1–10000，seconds 1–3600，clear 0/1')
        except ValueError as e:
            return self._json(400, {'ok': False, 'detail': str(e)})
        adb = ['adb', '-s', phone.serial]
        filters = ['-s'] + ['%s:%s' % (t, level) for t in tags] if tags else ['*:' + level]
        proc = None
        self.close_connection = True
        try:
            if clear == '1':
                subprocess.run(adb + ['logcat', '-c'], capture_output=True, timeout=10, check=True)
            # -T 从最近 tail 行开始，之后实时输出
            proc = subprocess.Popen(adb + ['logcat', '-v', fmt, '-T', str(tail)] + filters,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.connection.settimeout(3)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not self._disconnected() and not phone.stop.is_set():
                if not select.select([proc.stdout], [], [], .25)[0]:
                    continue
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, subprocess.SubprocessError) as e:
            if proc is None:
                try:
                    self._json(503, {'ok': False, 'detail': str(e)})
                except OSError:
                    pass
        finally:
            if proc:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=3)
                proc.stdout.close()

    def _file_path(self):
        value = parse_qs(urlparse(self.path).query).get('path', [''])[0]
        if not value.startswith('/') or value.endswith('/') or len(value) > 1024 or re.search(r'[\x00\n\r]', value):
            raise ValueError('path 要是手机上的绝对文件路径，最多 1024 字符')
        return value

    def _pull(self, phone):
        try:
            path = self._file_path()
        except ValueError as e:
            return self._json(400, {'ok': False, 'detail': str(e)})
        self.close_connection = True
        started = False
        try:
            with adb_sync.Sync(phone.serial, FILE_IDLE) as sync:
                mode, size = sync.stat(path)
                if not mode:
                    return self._json(404, {'ok': False, 'detail': '没有这个文件：' + path})
                if mode & 0o170000 != 0o100000:
                    return self._json(400, {'ok': False, 'detail': '不是普通文件：' + path})
                chunks = sync.pull(path)
                first = next(chunks, b'')
                started = True
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(size))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.connection.settimeout(FILE_IDLE)
                self.wfile.write(first)
                for chunk in chunks:
                    self.wfile.write(chunk)
        except (adb_sync.SyncError, OSError, ValueError) as e:
            # 已开始发送时只能断开连接，客户端按 Content-Length 发现不完整
            if not started:
                try:
                    self._json(503, {'ok': False, 'detail': str(e)})
                except OSError:
                    pass

    def do_PUT(self):
        path = urlparse(self.path).path
        if not self._allowed():
            return
        if path != '/file':
            return self._json(404, {'ok': False, 'detail': '没有这个接口'})
        self.close_connection = True
        try:
            target = self._file_path()
            mode = int(parse_qs(urlparse(self.path).query).get('mode', ['644'])[0], 8)
            if not 0 <= mode <= 0o777:
                raise ValueError('mode 要是 000–777 的八进制')
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('不支持 Transfer-Encoding，请提供 Content-Length')
            size = int(self.headers.get('Content-Length') or -1)
            if not 0 <= size <= FILE_MAX:
                raise ValueError('需要 Content-Length，最多 1 GiB')
        except ValueError as e:
            return self._json(400, {'ok': False, 'detail': str(e)})
        phone = self._phone()
        if not phone:
            return
        try:
            phone.guard(owner_arg(parse_qs(urlparse(self.path).query).get('owner', [None])[0]))
        except ValueError as e:
            return self._json(400, {'ok': False, 'detail': str(e)})
        except LeaseHeld as e:
            return self._lease_held(e)
        digest, t0 = hashlib.sha256(), time.monotonic()

        def body():
            remaining = size
            self.connection.settimeout(FILE_IDLE)
            while remaining:
                try:
                    chunk = self.rfile.read1(min(remaining, adb_sync.CHUNK))
                except OSError as e:
                    raise adb_sync.SourceError('读取请求体失败：%s' % e) from None
                if not chunk:
                    raise adb_sync.SourceError('请求体不完整')
                digest.update(chunk)
                remaining -= len(chunk)
                yield chunk
        try:
            with adb_sync.Sync(phone.serial, FILE_IDLE) as sync:
                sync.push(target, body(), mode)
        except adb_sync.SyncError as e:
            return self._json(400, {'ok': False, 'detail': '手机拒绝写入：%s' % e})
        except adb_sync.SourceError as e:
            try:
                return self._json(400, {'ok': False, 'detail': str(e)})
            except OSError:
                return
        except (OSError, ValueError) as e:
            try:
                return self._json(503, {'ok': False, 'detail': str(e)})
            except OSError:
                return
        self._json(200, {'ok': True, 'path': target, 'size': size, 'sha256': digest.hexdigest(),
                         'seconds': round(time.monotonic() - t0, 2)})

    def _shell(self, phone, body):
        command, timeout = body.get('command'), body.get('timeout', 10)
        if not isinstance(command, str) or not 1 <= len(command) <= 8192 or '\x00' in command:
            return self._json(400, {'ok': False, 'detail': 'command 要是 1–8192 字符的文本'})
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= SHELL_TIMEOUT:
            return self._json(400, {'ok': False, 'detail': 'timeout 要在 0–%d 秒' % SHELL_TIMEOUT})
        t0 = time.monotonic()
        proc = subprocess.Popen(['adb', '-s', phone.serial, 'shell', command],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = {proc.stdout: bytearray(), proc.stderr: bytearray()}
        open_pipes, truncated, timed_out = [proc.stdout, proc.stderr], False, False
        try:
            while open_pipes:
                remaining = t0 + timeout - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if self._disconnected():
                    return
                for pipe in select.select(open_pipes, [], [], min(.25, remaining))[0]:
                    chunk = pipe.read1(65536)
                    if not chunk:
                        open_pipes.remove(pipe)
                    elif len(output[proc.stdout]) + len(output[proc.stderr]) + len(chunk) > SHELL_OUTPUT:
                        truncated = True
                        open_pipes = []
                    else:
                        output[pipe].extend(chunk)
        finally:
            if proc.poll() is None and (timed_out or truncated or open_pipes):
                proc.kill()
            code = proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
        if timed_out:
            return self._json(504, {'ok': False, 'detail': '手机命令超时', 'stdout': output[proc.stdout].decode('utf-8', 'replace'),
                                    'stderr': output[proc.stderr].decode('utf-8', 'replace')})
        self._json(200, {'ok': True, 'exit': code, 'truncated': truncated, 'seconds': round(time.monotonic() - t0, 2),
                         'stdout': output[proc.stdout].decode('utf-8', 'replace'),
                         'stderr': output[proc.stderr].decode('utf-8', 'replace')})

    def do_POST(self):
        path = urlparse(self.path).path.strip('/')
        if not self._allowed():
            return
        try:
            body = self._read_json()
            if not isinstance(body, dict):
                raise ValueError('请求体要是 JSON 对象')
            if path == 'auth':
                return self._login(body)
            if path == 'stream/stop':
                clients = body.get('clients')
                if not isinstance(clients, list) or not 1 <= len(clients) <= 16 or \
                        any(not isinstance(c, str) or not CLIENT.fullmatch(c) for c in clients):
                    raise ValueError('clients 要是 1–16 个订阅标识')
                stream_stops.stop(clients)
                log.info('网页 %s 停掉画面订阅 %s', self.client_address[0], ','.join(clients))
                return self._json(200, {'ok': True})
            phone = self._phone()
            if not phone:
                return
            owner = owner_arg(parse_qs(urlparse(self.path).query).get('owner', [None])[0])
            if path == 'lease':
                ttl = body.get('ttl', 30)
                if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= LEASE_MAX:
                    raise ValueError('ttl 要在 0–%d 秒' % LEASE_MAX)
                priority = body.get('priority', LEASE_PRIORITY)
                if type(priority) is not int or not 0 <= priority <= 9:
                    raise ValueError('priority 要是 0–9 的整数')
                if not owner:
                    raise ValueError('缺少 owner')
                try:
                    phone.acquire(owner, ttl, priority)
                except LeaseHeld as e:
                    return self._json(409, {'ok': False, 'detail': str(e), 'owner': e.owner,
                                            'expires_in': round(e.remaining, 1)})
                return self._json(200, {'ok': True, 'owner': owner, 'expires_in': ttl})
            if path == 'release':
                if not owner:
                    raise ValueError('缺少 owner')
                return self._json(200, {'ok': True, 'released': phone.release(owner)})
            if path == 'touch':
                kind = body.get('type')
                if kind not in ('down', 'move', 'up'):
                    raise ValueError('type 要是 down / move / up')
                args = (kind,) if kind == 'up' else (kind, num(body, 'x'), num(body, 'y'))
                run = lambda: phone.finger(*args, cancel=self._disconnected, owner=owner)
            elif path == 'run':
                raw_steps = body.get('steps', [])
                if not isinstance(raw_steps, list) or len(raw_steps) > 100 or any(not isinstance(s, dict) for s in raw_steps):
                    raise ValueError('steps 要是最多 100 个动作对象的列表')
                steps = [action(s) for s in raw_steps]
                run = lambda: phone.run(steps, self._disconnected, owner)
            elif path == 'shell':
                phone.guard(owner)
                return self._shell(phone, body)
            elif path == 'unlock':
                run = lambda: phone.unlock(owner)
            elif path == 'stop':
                step = action({'stop': body.get('package')})
                run = lambda: phone.command(step, self._disconnected, require_touch=False, owner=owner)
            elif path in ('text', 'key', 'launch'):
                if path != 'launch' and path not in body:
                    raise ValueError('缺少 ' + path)
                step = action({'launch': body} if path == 'launch' else body)
                run = lambda: phone.command(step, self._disconnected, owner=owner)
            elif path in ('tap', 'long', 'swipe'):
                if path == 'swipe':
                    body = {'swipe': [num(body, k) for k in ('x1', 'y1', 'x2', 'y2')], 'secs': body.get('secs', 0.3)}
                else:
                    body = dict(body, **{path: [num(body, 'x'), num(body, 'y')]})
                step = action(body)
                run = lambda: phone.command(step, self._disconnected, owner=owner)
            else:
                return self._json(404, {'ok': False, 'detail': '没有这个接口'})
        except LeaseHeld as e:
            return self._lease_held(e)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as e:
            self.close_connection = True
            return self._json(400, {'ok': False, 'detail': str(e)})
        except TimeoutError:
            self.close_connection = True
            return self._json(408, {'ok': False, 'detail': '读取请求体超时'})
        t0 = time.time()
        try:
            result = run()
        except LeaseHeld as e:
            return self._lease_held(e)
        except StepFailure as e:
            return self._json(400 if isinstance(e.cause, ValueError) else 503,
                              {'ok': False, 'detail': str(e), 'failed_step': e.index, 'results': e.results})
        except ValueError as e:
            return self._json(400, {'ok': False, 'detail': str(e)})
        except Exception as e:
            log.info('%s 失败：%s', path, e)
            return self._json(503, {'ok': False, 'detail': str(e)})
        response = {'ok': True, 'seconds': round(time.time() - t0, 2)}
        if path == 'run':
            response['results'] = result
        elif path in ('text', 'key', 'launch', 'stop'):
            response['result'] = result
        elif path == 'unlock':
            response['was_locked'] = result
        self._json(200, response)


def watch_config(shutdown, stop):
    """配置文件变了且能读通就退出，由容器的 restart 拉起来按新配置运行；读不通就记日志、继续用旧配置。"""
    seen = config.mtime()
    while not stop.wait(CONFIG_CHECK_EVERY):
        current = config.mtime()
        if current == seen:
            continue
        seen = current
        try:
            config.load()
        except ValueError as e:
            log.info('配置文件改了但有错，继续用旧配置：%s', e)
            continue
        log.info('配置文件改了，重启生效')
        shutdown()
        return


def main():
    watcher = threading.Thread(target=fleet.watch, daemon=True)
    watcher.start()
    log.info('HTTP 端口 %d，设备 %d 台，%s', PORT, len(DEVICES), '网页有密码' if PASSWORD else '网页没设密码')
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    def shutdown(*args):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=watch_config, args=(shutdown, fleet.stop), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        fleet.close()
        server.server_close()
        watcher.join(timeout=2)


if __name__ == '__main__':
    main()
