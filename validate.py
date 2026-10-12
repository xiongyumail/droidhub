#!/usr/bin/env python3
"""完整回归：单元测试加本地浏览器用例，只用假手机，不连接真实设备。"""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
environment = dict(os.environ)
environment.pop('DROIDHUB_URL', None)
commands = [[sys.executable, '-B', '-m', 'unittest', 'discover', '-q', '-p', 'test_*.py']]
commands += [['node', str(root / 'e2e' / name)] for name in
             ('validate_ui.mjs', 'validate_audio.mjs', 'validate_recovery.mjs', 'validate_session.mjs',
              'validate_login.mjs')]
for command in commands:
    subprocess.run(command, cwd=root, env=environment, check=True, timeout=120)
