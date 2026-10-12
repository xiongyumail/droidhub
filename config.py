"""droidhub 配置文件，默认 /data/config.json（环境变量 DROIDHUB_CONFIG）。

{
  "default": "P1",
  "hosts": ["hub.local"],
  "password": "pbkdf2_sha256$200000$盐$哈希",
  "devices": [
    {"name": "P1", "serial": "9e24cca"},
    {"name": "VM", "address": "android:5555"},
    {"name": "Pad", "address": "192.168.1.50:5555", "pin": "1234", "audio": false}
  ]
}

- USB 设备填 serial，网络设备填 address（主机:端口）。
- 网络设备由 droidhub-net 按顺序从 5555 起转发，adb 序列号为 droidhub-net:端口。
- input 默认 USB 设备用 uhid，网络设备用 scrcpy；audio 默认 true。
- 没有配置文件时不限设备，显示 adb 中所有在线设备。
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time

PATH = os.environ.get('DROIDHUB_CONFIG', '/data/config.json')
FORWARD_HOST = 'droidhub-net'
FORWARD_BASE = 5555
INPUTS = ('uhid', 'scrcpy')
ITERATIONS = 200000
SESSION_SECONDS = 365 * 86400
NAME = re.compile(r'[^\s/?#&=]{1,32}')
SERIAL = re.compile(r'[A-Za-z0-9._:-]{1,64}')
ADDRESS = re.compile(r'[A-Za-z0-9.-]{1,253}:\d{1,5}')
PIN = re.compile(r'\d{4,16}')
FIELDS = {'default', 'hosts', 'password', 'devices'}
DEVICE_FIELDS = {'name', 'serial', 'address', 'input', 'audio', 'pin'}


def load(path=PATH):
    """读配置并校验；文件不存在时返回空配置。格式错误抛 ValueError。"""
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as e:
        raise ValueError(f'{path} 不是有效的 JSON：{e}') from None
    return parse(raw)


def parse(raw):
    if not isinstance(raw, dict):
        raise ValueError('配置要是 JSON 对象')
    extra = set(raw) - FIELDS
    if extra:
        raise ValueError('配置有不认识的字段：' + ', '.join(sorted(extra)))
    hosts = raw.get('hosts', [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
        raise ValueError('hosts 要是主机名列表')
    password = raw.get('password')
    if password is not None and not (isinstance(password, str) and password.startswith('pbkdf2_sha256$')):
        raise ValueError('password 要用 droidhub passwd 生成')
    devices, names, forward = {}, set(), FORWARD_BASE
    for item in raw.get('devices', []):
        if not isinstance(item, dict):
            raise ValueError('devices 的每一项要是对象')
        extra = set(item) - DEVICE_FIELDS
        name = item.get('name')
        if extra:
            raise ValueError(f'设备 {name} 有不认识的字段：' + ', '.join(sorted(extra)))
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise ValueError(f'设备名 {name!r} 无效或重复')
        names.add(name)
        if ('serial' in item) == ('address' in item):
            raise ValueError(f'设备 {name} 要写 serial（USB）或 address（网络）其中一个')
        if 'serial' in item:
            serial = item['serial']
            if not isinstance(serial, str) or not SERIAL.fullmatch(serial) or ':' in serial:
                raise ValueError(f'设备 {name} 的 serial 无效')
            address = None
        else:
            address = item['address']
            if not isinstance(address, str) or not ADDRESS.fullmatch(address) \
                    or not 0 < int(address.rsplit(':', 1)[1]) < 65536:
                raise ValueError(f'设备 {name} 的 address 要是「主机:端口」')
            serial = f'{FORWARD_HOST}:{forward}'
            forward += 1
        if serial in devices:
            raise ValueError(f'设备 {name} 的 serial 重复')
        entry = {'name': name, 'input': item.get('input', 'scrcpy' if address else 'uhid'),
                 'audio': item.get('audio', True), 'address': address, 'pin': item.get('pin')}
        if entry['input'] not in INPUTS:
            raise ValueError(f'设备 {name} 的 input 要是 uhid 或 scrcpy')
        if type(entry['audio']) is not bool:
            raise ValueError(f'设备 {name} 的 audio 要是布尔值')
        if entry['pin'] is not None and not (isinstance(entry['pin'], str) and PIN.fullmatch(entry['pin'])):
            raise ValueError(f'设备 {name} 的 pin 要是 4–16 位数字字符串')
        devices[serial] = entry
    default = raw.get('default')
    if default is not None and default not in names:
        raise ValueError(f'default 指向的设备 {default!r} 不在设备表里')
    default_serial = next((s for s, d in devices.items() if d['name'] == default), None)
    return {'devices': devices, 'default': default_serial, 'hosts': frozenset(hosts), 'password': password}


def forwards(config):
    """转发表：[(监听端口, 目标主机, 目标端口)]。"""
    return [(int(serial.rsplit(':', 1)[1]), *d['address'].rsplit(':', 1)) for serial, d in config['devices'].items()
            if d['address']]


def host_allowed(host, hosts):
    """请求的 Host 是 IP、localhost 或配置里的主机名才接受。
    DNS 重绑定只能借域名指到本机，IP 直连和 localhost 不受影响，所以只需列出用来访问的域名。"""
    if host == 'localhost' or host in hosts:
        return True
    try:
        ipaddress.ip_address(host.strip('[]'))
        return True
    except ValueError:
        return False


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return 'pbkdf2_sha256$%d$%s$%s' % (ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def check_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


def session_token(stored, service, now=None):
    """登录 Cookie 的值：到期时间加签名。签名密钥是保存的密码哈希，改密码后旧 Cookie 全部失效。"""
    expires = int((now or time.time()) + SESSION_SECONDS)
    return '%d.%s' % (expires, _session_sign(stored, service, expires))


def check_session(token, stored, service, now=None):
    try:
        expires, signature = token.split('.')
        expires = int(expires)
    except (AttributeError, ValueError):
        return False
    if expires < (now or time.time()):
        return False
    return hmac.compare_digest(signature, _session_sign(stored, service, expires))


def cookie_value(header, name):
    """从 Cookie 请求头取一项。逐项拆分：同一 IP 上其他服务的 Cookie 格式不规范时，不影响这一项。"""
    for part in (header or '').split(';'):
        key, _, value = part.strip().partition('=')
        if key == name:
            return value.strip('"')
    return None


def _session_sign(stored, service, expires):
    return hmac.new(stored.encode(), ('%s:%d' % (service, expires)).encode(), hashlib.sha256).hexdigest()


def set_password(password, path=PATH):
    """改网页密码（空字符串表示取消密码），先写临时文件再替换，权限 600。"""
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    if password:
        raw['password'] = hash_password(password)
    else:
        raw.pop('password', None)
    parse(raw)
    tmp = path + '.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def mtime(path=PATH):
    try:
        return os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return None
