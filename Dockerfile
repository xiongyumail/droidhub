FROM python:3.10.14-slim-bookworm AS adb-client

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 固定官方版本并校验 ZIP，保证重建结果一致
ARG ADB_VERSION=37.0.1
ARG ADB_SHA256=d230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1
RUN python3 -c "import io,urllib.request,zipfile; \
import hashlib; b=urllib.request.urlopen('https://dl.google.com/android/repository/platform-tools_r${ADB_VERSION}-linux.zip',timeout=120).read(); \
assert hashlib.sha256(b).hexdigest()=='${ADB_SHA256}', 'adb ZIP checksum mismatch'; \
z=zipfile.ZipFile(io.BytesIO(b)); \
open('/usr/local/bin/adb','wb').write(z.read('platform-tools/adb'))" \
 && chmod +x /usr/local/bin/adb && adb version | head -1

FROM adb-client AS adb
ENV ADB_LIBUSB=1 ADB_MDNS_AUTO_CONNECT=none
COPY adb_health.py usb_guard.py /app/
EXPOSE 5037
ENTRYPOINT ["python3", "/app/usb_guard.py"]

FROM adb-client AS web
# ffmpeg 把 scrcpy 输出的 H.264 转为 MJPEG，只在网页镜像中安装
# 国内构建时传 --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s#deb.debian.org#$APT_MIRROR#g" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY runtime.py touch.py dumps.py device.py audio.py adb_sync.py config.py forward.py passwd.py app.py index.html login.html scrcpy-server-v2.4 sndcpy-v1.1.apk sndcpy-LICENSE.txt /app/
COPY --chmod=755 entry.sh /usr/local/bin/droidhub
EXPOSE 8791
ENTRYPOINT ["droidhub"]
