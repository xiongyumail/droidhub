"""droidhub passwd：改网页密码；从终端读两遍，或从标准输入读一行。直接回车表示取消密码。"""
import getpass
import sys

import config

if sys.stdin.isatty():
    password = getpass.getpass('新密码（直接回车取消密码）：')
    if password and getpass.getpass('再输一遍：') != password:
        sys.exit('两次不一致，没改')
else:
    password = sys.stdin.readline().rstrip('\n')
config.set_password(password)
print(('已设密码' if password else '已取消密码') + '，几秒后自动生效')
