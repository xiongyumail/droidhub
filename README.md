# DroidHub

安卓设备网关。通过 adb 连接 USB 手机和网络设备（开启无线调试的平板、容器中的安卓虚拟机），对外提供 HTTP 接口和网页：

- 点击、长按、滑动、按键、输入文字
- 截图、实时画面、应用声音、界面树、logcat
- shell 命令、文件上传下载

调用方只使用 HTTP 接口，不持有 adb 密钥。

输入方式：

- USB 手机：用系统自带的 `/system/bin/hid` 创建虚拟触摸屏和键盘。不需要 root，也不需要 MIUI 的「USB 调试（安全设置）」。
  安卓检测到这个键盘后会默认隐藏屏幕输入法。需要在手机上用输入法时，到「设置 → 物理键盘」打开「显示虚拟键盘」。
- 网络设备：无法使用宿主机的 `/dev/uhid`，改用 scrcpy 控制通道。常驻一个进程，比每次调用 `input` 命令快约 0.5 秒。

## 快速开始

```sh
git clone https://github.com/xiongyumail/droidhub && cd droidhub
mkdir -p data && cp config.example.json data/config.json   # 按「配置」一节修改
docker compose pull && docker compose up -d
```

打开 `http://127.0.0.1:8791/`。首次连接设备时，在设备上允许 droidhub 的 adb 密钥，并勾选「一律允许」。

升级：`docker compose pull && docker compose up -d`。ADB 镜像按 adb 版本打标签，升级 droidhub 时 ADB 容器不重建。

## 配置

配置文件为 `data/config.json`，权限 600。修改后几秒内自动生效：网页和转发容器检测到变化后退出，由 Docker 重启。配置有错时记录日志，继续使用旧配置。

```json
{
  "default": "P1",
  "hosts": ["phone.example.lan"],
  "devices": [
    {"name": "P1", "serial": "9e24cca"},
    {"name": "VM", "address": "android:5555"},
    {"name": "Pad", "address": "192.168.1.50:5555", "pin": "1234"}
  ]
}
```

| 字段 | 说明 |
|---|---|
| `devices` | 设备列表。省略或为空时显示 adb 中所有在线设备 |
| `default` | 默认设备。省略时取序列号排序第一的设备 |
| `hosts` | 允许的域名。用 IP 或 localhost 访问时不需要列出 |
| `password` | 网页和接口的密码，用 `docker compose exec droidhub droidhub passwd` 设置。省略时不验证 |

设备字段：

| 字段 | 说明 |
|---|---|
| `name` | 必填。显示名称，接口中可用 `?phone=名称` 选择设备 |
| `serial` | USB 设备的 adb 序列号 |
| `address` | 网络设备的 `主机:端口`，与 `serial` 二选一 |
| `input` | `uhid` 或 `scrcpy`。默认 USB 设备用 uhid，网络设备用 scrcpy |
| `audio` | 是否允许采集声音，默认 true |
| `pin` | 锁屏数字密码，用于自动解锁 |

设置密码后：

- 浏览器打开网页时转到登录页，登录后一年内不用再输入。修改密码后需重新登录。
- 程序调用接口使用 HTTP Basic 认证，用户名任意。
- `/health` 不需要密码。

`.env` 中的变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DROIDHUB_LISTEN` | 127.0.0.1 | 监听地址，局域网访问需改为本机 IP 或 0.0.0.0 |
| `DROIDHUB_PORT` | 8791 | 端口 |
| `DROIDHUB_VERSION` | latest | 镜像版本 |
| `DROIDHUB_ADB_RESTART` | unless-stopped | ADB 容器的重启策略 |

### 添加网络设备

1. 在设备上执行 `adb tcpip 5555` 或开启无线调试，并设置静态 IP
2. 在配置中添加一项 `address`
3. 在设备上允许 droidhub 的 adb 密钥

建议在开发者选项中把「默认 USB 配置」设为「不进行数据传输」（或执行 `svc usb setScreenUnlockedFunctions`）。否则每次锁屏和解锁时 adbd 会重启，连接会断开几秒。

## 容器

| 容器 | 作用 |
|---|---|
| `droidhub-adb` | 运行 adb 服务。只连接内部网络，没有宿主机端口 |
| `droidhub-net` | 网络设备的转发。按配置顺序从 5555 起每台设备一个端口，adb 序列号为 `droidhub-net:端口` |
| `droidhub-web` | HTTP 接口和网页，通过 `tcp:adb:5037` 使用 adb 服务 |

droidhub-adb：

- 挂载宿主机 `/dev/bus/usb`，只允许 USB 字符设备（主设备号 189）。设备重新插拔后自动识别，不需要重建容器。
- 只认领带 ADB 接口的设备，其他 USB 设备只读取描述符。
- adb 密钥在 `data/adb/`，首次启动自动生成。换机器时复制旧密钥，设备不需要重新授权。
- 使用官方 adb 37.0.1 和 libusb 后端（`ADB_LIBUSB=1`）。原生后端依赖宿主机 udev 消息，在容器中收不到，插拔后无法识别设备。
- 启动时安装 seccomp 过滤器，拒绝 `USBDEVFS_RESET`，防止重置 USB 设备。只支持 Linux x86_64，安装失败时拒绝启动。

设备在其他 compose 项目的网络中时，把 droidhub-net 也接入那个网络。

安全限制：

- 容器均非特权，`cap_drop: ALL`，根文件系统只读，不使用 swap。
- droidhub-web 没有 USB 设备和密钥，限制 1 GiB 内存、2 核、256 个进程。
- droidhub-adb 限制 256 MiB 内存、0.5 核、64 个进程。
- 服务不调用 `adb reconnect`，不重置 USB、不解绑驱动、不重启设备。

## 接口

除特别说明外，接口都可以加 `?serial=<序列号>` 或 `?phone=<名称>` 指定设备，不指定时使用默认设备。

POST 请求使用 `Content-Type: application/json`。成功返回 `{ok: true, seconds}`；参数错误返回 400，设备未连接或执行出错返回 503，都带 `detail`。坐标为屏幕像素（自然方向），超出屏幕时报错。

### 状态

| 接口 | 说明 |
|---|---|
| `GET /` | 网页 |
| `GET /api` | 接口说明 |
| `GET /phones` | 所有设备：`{default, instance, phones: [...]}` |
| `GET /status` | 一台设备的状态 |
| `GET /health` | 至少一台设备连通时返回 200，否则 503 |

设备状态字段：`connected`、`serial`、`name`、`input`、`audio`、`size`、`density`、`rotation`、`awake`、`since`、`error`、`last_check`、`lease`、`unlock`、`stream`。未确认的值为 null。

### 输入

| 接口 | 参数 | 说明 |
|---|---|---|
| `POST /tap` | `{"x":540,"y":600}`，可选 `hold` 秒 | 点击 |
| `POST /long` | `{"x":540,"y":1200,"secs":1.0}` | 长按 |
| `POST /swipe` | `{"x1":540,"y1":1800,"x2":540,"y2":700,"secs":0.4}` | 滑动 |
| `POST /touch` | `{"type":"down\|move\|up","x":540,"y":600}` | 实时触摸，按下 3 秒无消息自动松开 |
| `POST /key` | `{"key":"A","modifiers":["ctrl"]}` | 按键或组合键 |
| `POST /text` | `{"text":"P2","replace":true}` | 向当前焦点粘贴文字 |
| `POST /launch` | `action`、`component` 或 `package` 三选一，可选 `data` | 启动 Intent |
| `POST /stop` | `{"package":"com.example"}` | 强制停止应用 |
| `POST /unlock` | 无 | 用配置的 `pin` 解锁 |
| `POST /run` | `{"steps":[...]}` | 批量执行 |

`/key` 支持 A–Z、0–9、ENTER、ESC、BACKSPACE、DELETE、TAB、SPACE、CAPS_LOCK、方向键、PAGE_UP/PAGE_DOWN，以及 HOME、BACK、APP_SWITCH、POWER、VOLUME_UP、VOLUME_DOWN。修饰键可选 ctrl、shift、alt、meta。POWER 切换亮灭屏，只需点亮时先查看 `awake`。

`/text` 通过剪贴板粘贴，支持中文、换行和制表符，最多 512 字符。会覆盖设备剪贴板。锁屏时剪贴板不可用，输入密码请用 `/key`。

`/unlock` 在锁屏时点亮屏幕、输入密码并回车，返回 `was_locked`。解锁失败一次后停用，直到服务重启，避免连续输错触发设备锁定。网页在选中设备时自动调用。

`/run` 最多 100 步，串行执行，开始前校验全部参数。每步只能有一种动作：`tap`、`long`、`swipe`、`wait`（0–30 秒）、`key`、`text`、`launch`、`stop`、`ui`、`prop`。

```sh
curl 'http://<宿主机>:8791/run?phone=P1' \
  -H 'Content-Type: application/json' \
  -d '{"steps":[{"tap":[300,1448]},{"wait":1.5},{"text":"P2","replace":true},{"key":"ENTER"}]}'
```

成功时返回逐步 `results`。中途出错时停止，返回 `{ok:false, detail, failed_step, results}`，`failed_step` 从 0 开始。失败的一步可能已部分执行，重试前请先检查设备状态。服务不会自动重放。

### 读取

| 接口 | 说明 |
|---|---|
| `GET /shot` | 当前屏幕 PNG，约 1 秒。`?x=&y=&w=&h=` 只截一块（截图像素；省略的边到屏幕边），约 0.4 秒 |
| `GET /frame` | 投屏的一帧 JPEG，长边 1600，有损。`?after=<X-Frame-Seq>&wait=1` 等到比上次新的帧再返回 |
| `GET /stream` | 实时画面，MJPEG，长边 1600，最高 60 帧 |
| `GET /audio` | 应用声音，原始 PCM（s16le、48000 Hz、双声道） |
| `GET /audio/status` | 采音状态，不会启动采集 |
| `GET /ui` | 界面控件树 JSON；`?format=xml` 返回原始 XML。界面需静止，通常约 2 秒 |
| `GET /views?package=<包名>` | 最上层界面的视图 id、类名和坐标，不依赖 uiautomator，适合有动画的页面 |
| `GET /windows` | 窗口列表和焦点，约 0.3 秒 |
| `GET /prop?name=<属性>` | 读取系统属性 |
| `GET /logcat` | logcat 文本流，参数 `tag`（可多个）、`level`、`format`、`tail`、`seconds`、`clear` |

读取接口不需要触摸通道就绪。adb 无响应时返回 503。

区块截图时手机照样抓整屏（约 0.2 秒），只把覆盖的行传回来，列在服务端裁，区块高矮对耗时影响不大。区块超出屏幕返回 400。

`/frame` 取的是 `/stream` 同一路投屏：投屏没开时先开（首帧约 1 秒），之后保留 30 秒，连续取帧只需几十毫秒。响应头 `X-Frame-Seq` 是帧序号，`X-Frame-Age` 是帧龄（秒）。画面静止时编码器不出新帧，最新一帧就是当前画面，`after` 等不到新帧也会在 `wait` 秒后返回最新一帧。画面宽高取 8 的倍数，换成截图坐标时宽、高要分别按比例换算。Android 11 及以下，scrcpy 2.4 以安全模式创建投屏，设置了 FLAG_SECURE 的界面也会出现在画面中（`/shot` 截不到这类界面）。

### shell 和文件

| 接口 | 说明 |
|---|---|
| `POST /shell` | `{"command":"...","timeout":10}`，返回 `{exit, stdout, stderr, truncated}`。超时最多 120 秒，输出最多 8 MiB |
| `GET /file?path=<路径>` | 下载文件，最大 4 GiB |
| `PUT /file?path=<路径>&mode=644` | 上传文件，最多 1 GiB，返回 `{path, size, sha256, seconds}`。自动创建上级目录，中途断开时删除未完成的文件 |

安装应用：先 `PUT /file` 上传，再用 `/shell` 执行 `pm install`。ADB 不对外开放，以上接口覆盖 adb 的常见用法。

`/shell` 和 `/file` 不排队，调用方需避免与正在执行的动作冲突。

## 设备占用

同一设备同一时间只应有一个调用方操作。

- `POST /lease?owner=<名字>` `{"ttl":30}`：占用，最长 300 秒，同一 owner 再次调用即续期，到期自动释放
- `POST /release?owner=<名字>`：释放
- 可带 `priority`（0–9，默认 5）：高于当前占用方时直接接管，否则返回 409 和 `{owner, expires_in}`

占用期间，其他调用方的输入请求返回 423。输入请求用 `?owner=` 表明身份。读取接口不受占用影响，网页仍可查看画面。无人占用时不需要先占用。

网页以 `web` 身份、优先级 0 操作，任何程序都可以直接接管。

## 声音

网页默认关闭声音，点「声音关」开启。只采集当前选中的设备。`audio: false` 的设备不采集。

- Android 11 及以上：使用 scrcpy 自带的采音，设备上不需要安装应用。锁屏时无法采集。
- Android 10：需预先安装 `sndcpy-v1.1.apk`，并授予录音和媒体采集权限：
  ```sh
  pm grant com.rom1v.sndcpy android.permission.RECORD_AUDIO
  appops set com.rom1v.sndcpy PROJECT_MEDIA allow
  ```
  MIUI 上还需把 `RECORD_AUDIO` app-op 设为 `allow`。

只能采集允许被采集的应用声音（媒体、游戏），不包括麦克风、通话、铃声和通知。设备本地照常出声。每个订阅约 1.5 Mbps。

## 实现说明

- 每 5 秒检查一次设备连接。断开后只重建该设备的输入通道；失败后 USB 设备等 30 秒、网络设备等 3 秒再试。
- 网络设备的 TCP 连接断网后可能长时间显示在线，检查连续失败 20 秒即按断开处理。
- 同一设备的动作串行执行，不同设备互不阻塞。
- 实时画面：设备上运行 scrcpy 服务端输出 H.264，ffmpeg 转为 MJPEG，有人观看时才推送。最后一个观看者离开后保留 30 秒，便于来回切换。
- 截图：读取 `screencap` 原始帧，在服务端压缩为 PNG。比设备端 `screencap -p` 快一倍以上。
- 安全：Host 只接受 IP、localhost 和配置中的 `hosts`，防止 DNS 重绑定。浏览器跨站 POST 返回 403。请求体最多 1 MiB，最多 64 个并发连接。

### 虚拟触摸屏描述符

单指触摸屏，由 hid-multitouch 驱动接管，包含 Tip Switch、In Range、Contact Identifier、X/Y（0..32767）和 Contact Count。

- 必须有 Contact Identifier，否则 Android 会把它识别为触控板。
- 不包含 Contact Count Maximum 特性报告，否则驱动会等待超时。
- `dumpsys input` 中设备 Classes 应为 `0x80000014`（TOUCH | TOUCH_MT | EXTERNAL）。
- Android 10 的 hid 命令输入是连续的 JSON 对象，不能有外层数组括号。

## 验证

`python3 validate.py`：运行单元测试和本地浏览器测试，不连接真实设备。浏览器测试需要 Playwright 和 Chromium，可用 `PLAYWRIGHT_MODULE` 和 `DROIDHUB_CHROMIUM` 指定位置。

对运行中的服务只读检查：`DROIDHUB_URL=http://<地址>:8791 node e2e/validate_ui.mjs`，触摸请求会被拦截。

## 许可

Apache-2.0，见 `LICENSE`。附带的第三方文件保留各自许可：

| 文件 | 来源 | 许可 | SHA256 |
|---|---|---|---|
| `scrcpy-server-v2.4` | [scrcpy v2.4](https://github.com/Genymobile/scrcpy/releases/tag/v2.4) | Apache-2.0 | `93c272b7438605c055e127f7444064ed78fa9ca49f81156777fd201e79ce7ba3` |
| `sndcpy-v1.1.apk` | [sndcpy v1.1](https://github.com/rom1v/sndcpy/releases/tag/v1.1) | MIT，见 `sndcpy-LICENSE.txt` | `4216b69b647aeec7cbe9ab7d991f406eb27ebc7f76e0d1fadd4c291d23eff03c` |
