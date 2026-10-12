#!/bin/sh
# droidhub            启动 HTTP 服务，使用独立的 ADB 服务，不启动或重启它
# droidhub forward    网络设备的 adb 转发（droidhub-net 容器）
# droidhub passwd     修改网页密码，直接回车表示取消密码
set -eu
case "${1:-}" in
  forward) exec python3 /app/forward.py ;;
  passwd) exec python3 /app/passwd.py ;;
  "") : "${ADB_SERVER_SOCKET:?independent ADB server required}"; exec python3 /app/app.py ;;
  *) echo "用法：droidhub [forward|passwd]" >&2; exit 2 ;;
esac
