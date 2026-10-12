import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config


class ConfigTests(unittest.TestCase):
    def test_usb_and_network_defaults(self):
        c = config.parse({'default': 'VM', 'devices': [
            {'name': 'P1', 'serial': '9e24cca'},
            {'name': 'VM', 'address': 'android:5555'},
            {'name': 'Pad', 'address': '192.168.1.50:5555', 'pin': '1234', 'audio': False}]})
        self.assertEqual(list(c['devices']), ['9e24cca', 'droidhub-net:5555', 'droidhub-net:5556'])
        self.assertEqual(c['devices']['9e24cca'], {'name': 'P1', 'input': 'uhid', 'audio': True, 'address': None, 'pin': None})
        self.assertEqual(c['devices']['droidhub-net:5555']['input'], 'scrcpy')
        self.assertTrue(c['devices']['droidhub-net:5555']['audio'])
        self.assertEqual(c['default'], 'droidhub-net:5555')
        self.assertEqual(config.forwards(c), [(5555, 'android', '5555'), (5556, '192.168.1.50', '5555')])

    def test_missing_file_means_no_restriction(self):
        c = config.load('/nonexistent/config.json')
        self.assertEqual((c['devices'], c['default'], c['password']), ({}, None, None))

    def test_bad_configs_are_rejected(self):
        for raw in ([], {'port': 1}, {'devices': [{'serial': 'x'}]},
                    {'devices': [{'name': 'a', 'serial': 'x'}, {'name': 'a', 'serial': 'y'}]},
                    {'devices': [{'name': 'a', 'serial': 'x', 'address': 'h:1'}]},
                    {'devices': [{'name': 'a', 'serial': 'x:1'}]},
                    {'devices': [{'name': 'a', 'address': 'h'}]},
                    {'devices': [{'name': 'a', 'address': 'h:70000'}]},
                    {'devices': [{'name': 'a', 'serial': 'x', 'input': 'adb'}]},
                    {'devices': [{'name': 'a', 'serial': 'x', 'audio': 'no'}]},
                    {'devices': [{'name': 'a', 'serial': 'x', 'pin': 1234}]},
                    {'devices': [{'name': 'a', 'serial': 'x', 'pin': '12'}]},
                    {'default': 'b', 'devices': [{'name': 'a', 'serial': 'x'}]},
                    {'password': 'plain'}, {'hosts': 'a'}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.parse(raw)

    def test_hosts_allow_ip_and_configured_names_only(self):
        for host in ('127.0.0.1', '192.168.1.2', '[::1]', 'localhost', 'hub.local'):
            self.assertTrue(config.host_allowed(host, {'hub.local'}), host)
        for host in ('foreign.example', '', 'hub.local.evil'):
            self.assertFalse(config.host_allowed(host, {'hub.local'}), host)

    def test_password_round_trip_and_file_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'config.json')
            with open(path, 'w') as f:
                json.dump({'devices': [{'name': 'P1', 'serial': 'x'}]}, f)
            config.set_password('秘密 pass', path)
            stored = config.load(path)['password']
            self.assertTrue(config.check_password('秘密 pass', stored))
            self.assertFalse(config.check_password('wrong', stored))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(len(config.load(path)['devices']), 1)
            config.set_password('', path)
            self.assertIsNone(config.load(path)['password'])


class PasswordHttpTests(unittest.TestCase):
    def setUp(self):
        import app
        import threading
        self.app = app
        self.server = app.HTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.patch = patch.object(app, 'PASSWORD', config.hash_password('pw'))
        self.patch.start()
        app.AUTHORIZED.clear()

    def tearDown(self):
        self.patch.stop()
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, password=None, method='GET', body=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = dict(headers or {})
        if password is not None:
            headers['Authorization'] = 'Basic ' + base64.b64encode(b'any:' + password.encode()).decode()
        if body is not None:
            body = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body, headers)
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status, r.headers

    def get(self, path, password=None):
        return self.request(path, password)

    def test_password_required_except_health(self):
        code, headers = self.get('/phones')
        self.assertEqual(code, 401)
        self.assertIsNone(headers['WWW-Authenticate'])
        self.assertEqual(self.get('/phones', 'bad')[0], 401)
        self.assertEqual(self.get('/phones', 'pw')[0], 200)
        self.assertEqual(self.get('/phones', 'pw')[0], 200)
        self.assertNotEqual(self.get('/health')[0], 401)

    def test_browser_redirected_to_login_page(self):
        code, headers = self.request('/?phone=P1', headers={'Accept': 'text/html'})
        self.assertEqual(code, 302)
        self.assertEqual(headers['Connection'], 'close')
        self.assertEqual(headers['Location'], '/auth?next=%2F%3Fphone%3DP1')
        self.assertEqual(self.get('/auth')[0], 200)

    def test_login_sets_long_lived_cookie(self):
        self.assertEqual(self.request('/auth', method='POST', body={'password': 'bad'})[0], 401)
        code, headers = self.request('/auth', method='POST', body={'password': 'pw'})
        self.assertEqual(code, 200)
        cookie = headers['Set-Cookie']
        self.assertIn('Max-Age=31536000', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        value = cookie.split(';')[0]
        self.assertEqual(self.request('/phones', headers={'Cookie': value})[0], 200)
        name, token = value.split('=')
        self.assertEqual(self.request('/phones', headers={'Cookie': name + '=' + token[:-1] + '0'})[0], 401)
        other = config.session_token(self.app.PASSWORD, 'tygbot')
        self.assertEqual(self.request('/phones', headers={'Cookie': name + '=' + other})[0], 401)

    def test_cookie_read_despite_malformed_neighbours(self):
        # 同一 IP 上其他服务的 Cookie 可能带空格、引号或 JSON
        token = config.session_token(self.app.PASSWORD, 'droidhub')
        for other in ('ui={"a":1}', 'theme=dark mode', 'a="'):
            cookie = '%s; droidhub_session=%s' % (other, token)
            self.assertEqual(self.request('/phones', headers={'Cookie': cookie})[0], 200, other)

    def test_failed_basic_auth_is_throttled(self):
        import time
        t0 = time.monotonic()
        self.assertEqual(self.get('/phones', 'bad')[0], 401)
        self.assertGreaterEqual(time.monotonic() - t0, 1)

    def test_password_change_invalidates_cookie(self):
        token = config.session_token(self.app.PASSWORD, 'droidhub')
        with patch.object(self.app, 'PASSWORD', config.hash_password('pw')):
            self.assertEqual(self.request('/phones', headers={'Cookie': 'droidhub_session=' + token})[0], 401)


class ForwardTests(unittest.TestCase):
    def test_forward_relays_bytes(self):
        import asyncio
        import forward

        async def run():
            async def echo(reader, writer):
                writer.write(b'pong:' + await reader.read(4))
                await writer.drain()
                writer.close()
            target = await asyncio.start_server(echo, '127.0.0.1', 0)
            port = target.sockets[0].getsockname()[1]
            relay = await asyncio.start_server(forward.handler('127.0.0.1', port), '127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', relay.sockets[0].getsockname()[1])
            writer.write(b'ping')
            await writer.drain()
            data = await asyncio.wait_for(reader.read(100), 5)
            writer.close()
            relay.close()
            target.close()
            return data
        self.assertEqual(asyncio.run(run()), b'pong:ping')


if __name__ == '__main__':
    unittest.main()
