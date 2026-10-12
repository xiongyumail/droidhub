"""解析 dumpsys 的窗口和视图树：不依赖 uiautomator，视频、动画页面也能立即读到。"""
import re

WINDOW_HEADER = re.compile(r'Window #\d+ Window\{\S+ \S+ ([^}]*)\}')
# 安卓 11 起写 ` frame=`，安卓 10 写 `mFrame=`
FRAME = (re.compile(r' frame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]'),
         re.compile(r' mFrame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]'))
FOCUS = re.compile(r'mCurrentFocus=Window\{\S+ \S+ ([^}]*)\}')
FOCUSED_APP = re.compile(r'mFocusedApp=.*?ActivityRecord\{\S+ \S+ ([^\s}]+)')
RESUMED = re.compile(r'(?:topResumedActivity=|mResumedActivity: )ActivityRecord\{\S+ \S+ ([^\s}]+)')
VIEW_NODE = re.compile(r'^(\s*)([\w.$]+)\{\w+ (\S{9}) (\S{8}) (-?\d+),(-?\d+)-(-?\d+),(-?\d+)(?: #\w+ ([\w:./]+))?')


def frame_of(block):
    for pattern in FRAME:
        m = pattern.search(block)
        if m:
            return list(map(int, m.groups()))
    return None


def windows(dump, focus_dump='', activities=''):
    """`dumpsys window windows` 的各窗口：名称、所属包、屏幕位置、是否显示；另附焦点和前台界面。"""
    items = []
    for block in dump.split('  Window #')[1:]:
        m = WINDOW_HEADER.match('Window #' + block)
        if not m:
            continue
        package = re.search(r' package=(\S+)', block)
        visibility = re.search(r'mViewVisibility=(0x[0-9a-f]+)', block)
        surface = re.search(r'mHasSurface=(true|false)', block)
        items.append({'name': m.group(1), 'package': package.group(1) if package else None,
                      'frame': frame_of(block),
                      'visible': bool(visibility and visibility.group(1) == '0x0'
                                      and (surface is None or surface.group(1) == 'true'))})
    focus = FOCUS.search(focus_dump or dump)
    focused_app = FOCUSED_APP.search(focus_dump or dump)
    resumed = RESUMED.search(activities)
    focus_name = focus.group(1) if focus else None
    return {'focus': focus_name,
            'focus_package': focus_name.split('/')[0] if focus_name and '/' in focus_name else None,
            'focused_app': focused_app.group(1) if focused_app else None,
            'resumed': resumed.group(1) if resumed else None, 'windows': items}


def full_component(component):
    package, _, name = component.partition('/')
    return f'{package}/{package}{name}' if name.startswith('.') else component


def top_activity_dump(dump, package=None):
    """`dumpsys activity top` 会列出各任务的界面；取最后（最上层）一个，指定包时取该包最上层的。"""
    marker = f'ACTIVITY {package}/' if package else 'ACTIVITY '
    i = dump.rfind(marker)
    if i < 0:
        return None, ''
    return full_component(dump[i:].split()[1]), dump[i:]


def views(top_dump, windows_dump='', package=None):
    """可见视图及屏幕绝对坐标。视图树里的坐标相对父视图，逐层累加；居中小窗口（插屏广告之类）
    的坐标从窗口左上角算，按根视图尺寸对上同名窗口的位置再加偏移。
    hierarchy 为 False 表示应用主线程没在 dumpsys 的期限（约 5 秒）内交出视图树，通常是卡死。"""
    activity, segment = top_activity_dump(top_dump, package)
    if not activity:
        return {'activity': None, 'hierarchy': False, 'views': []}
    frames = [w['frame'] for w in windows(windows_dump)['windows'] if w['frame'] and w['name'] == activity]
    result, stack, in_tree, hierarchy = [], [], False, False
    ox = oy = 0
    for line in segment.splitlines():
        if 'View Hierarchy:' in line:
            in_tree, hierarchy, stack = True, True, []
            continue
        m = in_tree and VIEW_NODE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        left, top, right, bottom = map(int, m.group(5, 6, 7, 8))
        if not stack:
            ox, oy = next(((f[0], f[1]) for f in frames if (f[2] - f[0], f[3] - f[1]) == (right - left, bottom - top)),
                          (0, 0))
        px, py, parent_visible = stack[-1][1:] if stack else (ox, oy, True)
        flags = m.group(3)
        visible = parent_visible and flags[0] == 'V'
        stack.append((indent, px + left, py + top, visible))
        if visible and right > left and bottom > top:
            result.append({'id': m.group(9) or '', 'class': m.group(2),
                           'bounds': [px + left, py + top, px + right, py + bottom],
                           'clickable': flags[6] == 'C'})
    return {'activity': activity, 'hierarchy': hierarchy, 'views': result}
