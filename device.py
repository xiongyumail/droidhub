"""手机控制原语：UHID 键盘、scrcpy 控制通道、界面树、窗口/视图、Intent、结束应用和系统属性。"""
import json
import re
import shlex
import socket
import struct
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import dumps
from touch import READY_WAIT, Touch
from runtime import ConnectionFailure, adb_forward_host, capture_png, close_hid, write_pipe

# Report 1 是标准 USB 键盘；Report 2 是 Consumer Control（系统 Home/Back）。
KEYBOARD_DESCRIPTOR = [
    5, 1, 9, 6, 161, 1, 133, 1, 5, 7, 25, 224, 41, 231, 21, 0, 37, 1,
    117, 1, 149, 8, 129, 2, 149, 1, 117, 8, 129, 3, 149, 6, 117, 8,
    21, 0, 37, 101, 5, 7, 25, 0, 41, 101, 129, 0, 192,
    5, 12, 9, 1, 161, 1, 133, 2, 21, 0, 38, 255, 3, 25, 0, 42, 255, 3,
    117, 16, 149, 1, 129, 0, 192,
]
KEYS = {chr(65 + i): 4 + i for i in range(26)}
KEYS.update({str(i): 29 + i for i in range(1, 10)})
KEYS.update({'0': 39, 'ENTER': 40, 'ESC': 41, 'BACKSPACE': 42, 'TAB': 43,
             'SPACE': 44, 'CAPS_LOCK': 57, 'DELETE': 76, 'RIGHT': 79,
             'LEFT': 80, 'DOWN': 81, 'UP': 82, 'PAGE_UP': 75, 'PAGE_DOWN': 78})
CONSUMER_KEYS = {'HOME': 0x223, 'BACK': 0x224, 'POWER': 0x30, 'VOLUME_UP': 0xE9, 'VOLUME_DOWN': 0xEA,
                 'APP_SWITCH': 0x1A2}
MODIFIERS = {'ctrl': 1, 'shift': 2, 'alt': 4, 'meta': 8}
# 同一组按键的 Android keycode 和 metaState，给经 scrcpy 注入的设备用。
ANDROID_KEYS = {chr(65 + i): 29 + i for i in range(26)}
ANDROID_KEYS.update({str(i): 7 + i for i in range(10)})
ANDROID_KEYS.update({'ENTER': 66, 'ESC': 111, 'BACKSPACE': 67, 'TAB': 61, 'SPACE': 62, 'CAPS_LOCK': 115,
                     'DELETE': 112, 'RIGHT': 22, 'LEFT': 21, 'DOWN': 20, 'UP': 19, 'PAGE_UP': 92,
                     'PAGE_DOWN': 93, 'HOME': 3, 'BACK': 4, 'POWER': 26, 'VOLUME_UP': 24, 'VOLUME_DOWN': 25,
                     'APP_SWITCH': 187})
ANDROID_META = {'ctrl': 0x3000, 'shift': 0x41, 'alt': 0x12, 'meta': 0x30000}
INPUTS = ('uhid', 'scrcpy')
ROTATION = re.compile(r'Viewport INTERNAL:[^\n]*\borientation=([0-3])\b')


def text_args(body):
    text, replace = body.get('text'), body.get('replace', False)
    if not isinstance(text, str) or len(text) > 512:
        raise ValueError('text 要是最多 512 字符的字符串')
    if type(replace) is not bool:
        raise ValueError('replace 要是布尔值')
    if any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127 for c in text):
        raise ValueError('text 不能含换行、制表符以外的控制字符')
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('text 要是有效的 Unicode 字符串') from None
    return text, replace


def key_args(body):
    key, modifiers = body.get('key'), body.get('modifiers', [])
    if not isinstance(key, str) or key.upper() not in KEYS.keys() | CONSUMER_KEYS.keys():
        raise ValueError('不支持的 key；支持字母、数字、ENTER/BACKSPACE/TAB/ESC/CAPS_LOCK/方向键、'
                         'HOME/BACK/APP_SWITCH/POWER/VOLUME_UP/VOLUME_DOWN')
    if not isinstance(modifiers, list) or any(not isinstance(m, str) or m not in MODIFIERS for m in modifiers):
        raise ValueError('modifiers 要是 ctrl/shift/alt/meta 的列表')
    key = key.upper()
    if key in CONSUMER_KEYS and modifiers:
        raise ValueError('系统键不支持 modifiers')
    return key, modifiers


def prop_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name):
        raise ValueError('name 要是一个系统属性名')
    return name


PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+')
# 结束这些会关掉系统界面、桌面或采音/投屏自己
PROTECTED_PACKAGES = frozenset({'android', 'com.android.systemui', 'com.miui.home', 'com.android.launcher3',
                                'com.android.shell', 'com.rom1v.sndcpy'})


def package_name(value, stop=False):
    if not isinstance(value, str) or not PACKAGE.fullmatch(value) or len(value) > 255:
        raise ValueError('package 要是一个应用包名')
    if stop and value in PROTECTED_PACKAGES:
        raise ValueError('不能结束系统界面、桌面或 DroidHub 自己用的应用：' + value)
    return value


def launch_args(body):
    if not isinstance(body, dict):
        raise ValueError('launch 要是对象')
    chosen = [k for k in ('action', 'component', 'package') if k in body]
    if len(chosen) != 1:
        raise ValueError('action/component/package 必须且只能提供一个')
    key, value = chosen[0], body[chosen[0]]
    pattern = r'[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+' if key == 'component' else r'[A-Za-z0-9_.$]+'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(key + ' 格式无效')
    args = {'action': ['-a', value], 'component': ['-n', value], 'package': ['-p', value]}[key]
    if key == 'package':
        args += ['-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER']
    if 'data' in body:
        if not isinstance(body['data'], str) or '\x00' in body['data']:
            raise ValueError('data 要是 URI 字符串')
        args += ['-d', body['data']]
    return args


class Keyboard:
    def __init__(self, serial):
        self.adb = ['adb', '-s', serial]
        self.proc = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def open(self):
        self.close()
        self.proc = subprocess.Popen(self.adb + ['shell', 'hid', '-'], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        try:
            self._send({'id': 2, 'command': 'register', 'name': 'droidhub keyboard',
                        'vid': 0x1209, 'pid': 0x0002, 'descriptor': KEYBOARD_DESCRIPTOR})
            time.sleep(READY_WAIT)
            if not self.alive():
                raise ConnectionFailure('UHID 键盘注册失败')
        except Exception:
            self.close()
            raise

    def close(self):
        proc, self.proc = self.proc, None
        if proc:
            close_hid(proc)

    def _send(self, obj):
        if not self.alive():
            raise ConnectionFailure('UHID 键盘已断开')
        try:
            write_pipe(self.proc.stdin, (json.dumps(obj) + '\n').encode())
        except ConnectionFailure:
            self.close()
            raise

    def stroke(self, usage, modifiers=0, consumer=False):
        report = [2, usage & 255, usage >> 8] if consumer else [1, modifiers, 0, usage, 0, 0, 0, 0, 0]
        release = [2, 0, 0] if consumer else [1] + [0] * 8
        modified = bool(modifiers) and not consumer
        held = [1, modifiers, 0, 0, 0, 0, 0, 0, 0]
        try:
            if modified:
                self._send({'id': 2, 'command': 'report', 'report': held})
                time.sleep(0.01)
            self._send({'id': 2, 'command': 'report', 'report': report})
            time.sleep(0.025)
        finally:
            try:
                # 普通键先松开，让输入法处理 key-up 时仍能看到修饰键。
                if modified and self.proc is not None:
                    self._send({'id': 2, 'command': 'report', 'report': held})
                    time.sleep(0.01)
            finally:
                if self.proc is not None:
                    self._send({'id': 2, 'command': 'report', 'report': release})
        time.sleep(0.025)

    def key(self, key, modifiers):
        if key in CONSUMER_KEYS:
            self.stroke(CONSUMER_KEYS[key], consumer=True)
        else:
            self.stroke(KEYS[key], sum(MODIFIERS[m] for m in set(modifiers)))
        return {'key': key}


class Clipboard:
    """scrcpy 2.4 的独立控制通道。USB 手机只用它同步剪贴板（MIUI 限制注入）；
    输入走 scrcpy 的设备（虚拟机等没有可用 UHID 的）也经它注入按键和触摸。"""
    remote = '/data/local/tmp/droidhub-clipboard-server.jar'

    def __init__(self, serial):
        self.adb = ['adb', '-s', serial]
        self.proc = self.sock = self.port = None
        self.sequence = 0

    def alive(self):
        return self.sock is not None and self.proc is not None and self.proc.poll() is None

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
        if self.proc:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            self.proc = None
        if self.port:
            try:
                subprocess.run(self.adb + ['forward', '--remove', 'tcp:%d' % self.port],
                               capture_output=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.port = None

    def open(self):
        self.close()
        try:
            jar = Path(__file__).with_name('scrcpy-server-v2.4')
            subprocess.run(self.adb + ['push', str(jar), self.remote], capture_output=True, timeout=10, check=True)
            scid = uuid.uuid4().int & 0x7fffffff
            forward = subprocess.run(self.adb + ['forward', 'tcp:0', 'localabstract:scrcpy_%08x' % scid],
                                     capture_output=True, text=True, timeout=5, check=True)
            self.port = int(forward.stdout.strip())
            self.proc = subprocess.Popen(self.adb + [
                'shell', 'CLASSPATH=' + self.remote, 'app_process', '/', 'com.genymobile.scrcpy.Server', '2.4',
                'scid=%08x' % scid, 'tunnel_forward=true', 'video=false', 'audio=false', 'control=true',
                'send_device_meta=false', 'send_dummy_byte=true', 'clipboard_autosync=false',
                'power_on=false', 'cleanup=false', 'log_level=warn'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.proc.poll() is None:
                connection = None
                try:
                    connection = socket.create_connection((adb_forward_host(), self.port), timeout=.3)
                    if connection.recv(1) == b'\0':
                        connection.settimeout(3)
                        self.sock = connection
                        return
                except OSError:
                    pass
                if connection:
                    connection.close()
                time.sleep(.05)
            raise RuntimeError('手机剪贴板服务连接失败')
        except Exception:
            self.close()
            raise

    def _recv(self, count):
        data = bytearray()
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise RuntimeError('手机剪贴板通道已断开')
            data.extend(chunk)
        return bytes(data)

    def _message(self):
        kind = self._recv(1)[0]
        if kind == 1:
            return kind, struct.unpack('>Q', self._recv(8))[0]
        if kind == 0:
            length = struct.unpack('>I', self._recv(4))[0]
            if length > 262139:
                raise RuntimeError('手机剪贴板响应过大')
            return kind, self._recv(length)
        raise RuntimeError('手机剪贴板协议响应无效')

    def set(self, text):
        if not self.alive():
            self.open()
        payload = text.encode('utf-8')
        self.sequence += 1
        try:
            # paste=false：粘贴由 UHID Ctrl+V 完成，避免 MIUI 注入权限限制。
            self.sock.sendall(struct.pack('>BQBI', 9, self.sequence, 0, len(payload)) + payload)
            if self._message() != (1, self.sequence):
                raise RuntimeError('手机剪贴板确认不匹配')
            # scrcpy 的 ACK 只确认消息已处理；回读核对后才能删除或粘贴。
            self.sock.sendall(bytes([8, 0]))
            if self._message() != (0, payload):
                raise RuntimeError('手机剪贴板写入未生效')
        except Exception:
            self.close()
            raise

    def inject(self, packet):
        if not self.alive():
            raise ConnectionFailure('scrcpy 控制通道已断开')
        try:
            self.sock.sendall(packet)
        except OSError as e:
            self.close()
            raise ConnectionFailure('scrcpy 控制通道已断开：%s' % e) from None

    def keycode(self, code, meta=0):
        for action in (0, 1):
            self.inject(struct.pack('>BBiii', 0, action, code, 0, meta))


class ControlKeyboard:
    """接口同 Keyboard，按键经设备的 scrcpy 控制通道注入，和剪贴板共用一路。"""

    def __init__(self, control):
        self.control = control

    def alive(self):
        return self.control.alive()

    def open(self):
        self.control.open()

    def close(self):
        self.control.close()

    def key(self, key, modifiers):
        self.control.keycode(ANDROID_KEYS[key], sum(ANDROID_META[m] for m in set(modifiers)))
        time.sleep(0.025)
        return {'key': key}


class ControlTouch(Touch):
    """接口同 Touch，触摸经 scrcpy 控制通道注入；给容器里的 Android 这类 UHID 用不了的设备。

    坐标仍是 wm size 的自然方向，按 rotation 换成 scrcpy 的当前方向画面坐标；
    scrcpy 画面宽高取 8 的倍数，尺寸对不上的事件服务端会丢掉。"""
    DOWN, UP, MOVE = 0, 1, 2
    FINGER = -2

    def __init__(self, serial):
        super().__init__(serial)
        self.control = Clipboard(serial)
        self.rotation = 0
        self.pressed = False

    def open(self):
        self.close()
        out = subprocess.run(self.adb + ['shell', 'dumpsys', 'input'], capture_output=True, text=True,
                             errors='replace', timeout=5).stdout
        match = ROTATION.search(out)
        self.rotation = int(match[1]) if match else 0
        self.control.open()
        self.proc = self.control.proc

    def close(self):
        self.proc, self.pos, self.pressed = None, None, False
        self.control.close()

    def _report(self, down, x, y):
        self.validate(x, y)
        w, h, r = self.w, self.h, self.rotation
        if r == 1:
            x, y, w, h = y, w - 1 - x, h, w
        elif r == 2:
            x, y = w - 1 - x, h - 1 - y
        elif r == 3:
            x, y, w, h = h - 1 - y, x, h, w
        vw, vh = w & ~7, h & ~7
        action = (self.MOVE if self.pressed else self.DOWN) if down else self.UP
        try:
            self.control.inject(struct.pack('>BBqiiHHHii', 2, action, self.FINGER, round(x * vw / w),
                                            round(y * vh / h), vw, vh, 0xffff if down else 0, 0, 0))
        except ConnectionFailure:
            self.close()
            raise
        self.pressed = down


class Device:
    def __init__(self, serial, input='uhid'):
        self.adb = ['adb', '-s', serial]
        self.clipboard = Clipboard(serial)
        self.keyboard = ControlKeyboard(self.clipboard) if input == 'scrcpy' else Keyboard(serial)
        self.ui_path = '/data/local/tmp/droidhub-ui-' + uuid.uuid4().hex + '.xml'

    def close(self):
        try:
            self.keyboard.close()
        finally:
            self.clipboard.close()

    def shell(self, args, timeout=10):
        try:
            result = subprocess.run(self.adb + ['shell', shlex.join(args)], capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ConnectionFailure('手机命令超时') from None
        if result.returncode:
            if any(s in (result.stderr + result.stdout).lower() for s in ('device offline', 'not found', 'error: closed')):
                raise ConnectionFailure((result.stderr or result.stdout).strip())
            raise RuntimeError((result.stderr or result.stdout).strip() or '手机命令失败')
        return (result.stdout + '\n' + (result.stderr or '')).strip()

    def sh(self, command, timeout=10):
        """执行一段手机 shell 文本（可含管道），错误处理同 shell()。"""
        try:
            result = subprocess.run(self.adb + ['shell', command], capture_output=True, text=True,
                                    errors='replace', timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ConnectionFailure('手机命令超时') from None
        if result.returncode and any(s in (result.stderr + result.stdout).lower()
                                     for s in ('device offline', 'not found', 'error: closed')):
            raise ConnectionFailure((result.stderr or result.stdout).strip())
        return result.stdout

    def windows(self):
        out = self.sh('dumpsys window windows; echo ---DROIDHUB-FOCUS---; '
                      'dumpsys window | grep -E "mCurrentFocus=|mFocusedApp="; echo ---DROIDHUB-ACTIVITY---; '
                      'dumpsys activity activities | grep -E "topResumedActivity=|mResumedActivity:"', timeout=15)
        windows, _, rest = out.partition('---DROIDHUB-FOCUS---')
        focus, _, activities = rest.partition('---DROIDHUB-ACTIVITY---')
        return dumps.windows(windows, focus, activities)

    def views(self, package=None):
        out = self.sh('dumpsys activity top; echo ---DROIDHUB-WINDOWS---; dumpsys window windows', timeout=15)
        top, _, windows = out.partition('---DROIDHUB-WINDOWS---')
        return dumps.views(top, windows, package)

    def stop(self, package):
        self.shell(['am', 'force-stop', package_name(package, stop=True)])
        return {'package': package}

    def prop(self, name):
        return self.shell(['getprop', prop_name(name)], timeout=3)

    def shot(self, box=None):
        return capture_png(self.adb, box)

    def launch(self, args):
        if '-p' in args:
            # Launcher 常不含 CATEGORY_DEFAULT，隐式 am start -p 会解析失败。
            resolved = self.shell(['cmd', 'package', 'resolve-activity', '--brief'] + args)
            components = [line.strip() for line in resolved.splitlines()
                          if re.fullmatch(r'[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+', line.strip())]
            package = args[args.index('-p') + 1]
            if len(components) != 1 or components[0].split('/')[0] != package:
                raise RuntimeError('找不到应用入口：' + package)
            args = ['-n', components[0]] + (args[args.index('-d'):args.index('-d') + 2] if '-d' in args else [])
        output = self.shell(['am', 'start', '-W'] + args, timeout=15)
        if 'Error:' in output or 'Exception' in output or 'Status: timeout' in output:
            raise RuntimeError(output)
        return {'output': output}

    def ui(self, format='json'):
        # 手机端也限时，避免 HTTP 超时后 uiautomator 仍挂在手机上。
        output = self.shell(['timeout', '8', 'uiautomator', 'dump', '--compressed', self.ui_path], timeout=10)
        if 'UI hierchary dumped to:' not in output and 'UI hierarchy dumped to:' not in output:
            raise RuntimeError(output or '抓取界面失败')
        try:
            xml = self.shell(['cat', self.ui_path])
        finally:
            try:
                self.shell(['rm', '-f', self.ui_path])
            except RuntimeError:
                pass
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            raise RuntimeError('界面 XML 无效') from None
        if format == 'xml':
            return xml
        nodes = []
        def walk(node, parent=None):
            if node.tag != 'node':
                for child in node:
                    walk(child, parent)
                return
            a = node.attrib
            index = len(nodes)
            item = {'index': index, 'parent': parent, 'text': a.get('text', ''),
                    'description': a.get('content-desc', ''), 'resource_id': a.get('resource-id', ''),
                    'class': a.get('class', ''), 'bounds': list(map(int, re.findall(r'-?\d+', a.get('bounds', ''))))}
            item.update({k: a.get(k) == 'true' for k in ('clickable', 'long-clickable', 'focused',
                         'focusable', 'enabled', 'selected', 'checked', 'scrollable', 'password')})
            nodes.append(item)
            for child in node:
                walk(child, index)
        walk(root)
        return {'rotation': int(root.attrib.get('rotation', 0)), 'nodes': nodes}

    def key(self, key, modifiers):
        if not self.keyboard.alive():
            self.keyboard.open()
        try:
            return self.keyboard.key(key, modifiers)
        except (BrokenPipeError, RuntimeError):
            self.keyboard.close()
            raise

    def text(self, text, replace):
        if not text and not replace:
            return {'characters': 0}
        if not self.keyboard.alive():
            self.keyboard.open()
        try:
            if text:
                self.clipboard.set(text)
            if replace:
                self.keyboard.key('A', ['ctrl'])
            if text:
                self.keyboard.key('V', ['ctrl'])
            elif replace:
                self.keyboard.key('BACKSPACE', [])
            return {'characters': len(text)}
        except Exception:
            self.close()
            raise
