#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
即时备忘 —— 轻量备忘提醒器
纯标准库实现，无第三方依赖。

三个窗口：
  1. 主窗   —— 写备忘、勾完成
  2. 全屏   —— 开机/解锁/熄屏回来/定时弹出
  3. 浮窗   —— 常驻桌面小卡片，可勾可记，可贴边隐藏

用法：
  main.py              正常启动
  main.py --autostart  开机自启模式
"""

import json
import os
import sys
import time
import subprocess
import ctypes
import ctypes.wintypes as wt
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox
from datetime import datetime


# ================================================================ DPI
# 必须在创建任何窗口之前声明 DPI 感知，否则 Windows 会把窗口位图拉伸 → 糊
def _enable_dpi_awareness():
    try:
        # Per-Monitor V2（Win10 1703+）
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()


APP_NAME = "即时备忘"
APP_ID = "InstantNote"

# 单实例用的全局互斥量名（所有本程序的窗口共享同一个）
MUTEX_NAME = "Local\\InstantNote_SingleInstance_v1"
_mutex_handle = None          # 必须留住，句柄一回收互斥量就没了


def acquire_single_instance(name=None):
    """抢单实例锁。

    返回 True = 我是第一个，正常启动；
    返回 False = 已经有一个在跑，本进程该退出。
    name 只在测试时用，正常启动传 None 走默认的那个。
    """
    global _mutex_handle
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_wchar_p]
        k32.CreateMutexW.restype = ctypes.c_void_p
        _mutex_handle = k32.CreateMutexW(None, False, name or MUTEX_NAME)
        if not _mutex_handle:
            return True          # 建不出来就当没有，别把人挡在外面
        return k32.GetLastError() != 183   # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True


def wake_existing():
    """把已经在跑的那个窗口显示到最前。

    返回 True = 真找到了一个活着的窗口并唤起；
    返回 False = 没找到（可能是僵尸残留的窗口，或压根没在跑）。
    """
    try:
        u32 = ctypes.windll.user32
        u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u32.FindWindowW.restype = ctypes.c_void_p
        u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u32.IsWindowVisible.restype = ctypes.c_bool
        u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_ulong)]

        for title in (APP_NAME, "备忘浮窗"):
            hwnd = u32.FindWindowW(None, title)
            if not hwnd:
                continue

            # 关键：确认这个窗口背后真的还有个活进程。
            # 见过的情况：进程被强杀后窗口句柄还挂在系统里，
            # FindWindow 照样找得到，去唤醒它等于什么都没唤醒，
            # 用户看到的就是"双击了没反应"。
            pid = ctypes.c_ulong(0)
            try:
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            except Exception:
                pid.value = 0

            if pid.value:
                # 进程还在不在？OpenProcess 失败说明已经没了
                k32 = ctypes.windll.kernel32
                k32.OpenProcess.restype = ctypes.c_void_p
                h = k32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED
                if not h:
                    continue                      # 僵尸窗口，跳过
                k32.CloseHandle(ctypes.c_void_p(h))

            SW_SHOW, SW_RESTORE = 5, 9
            u32.ShowWindow(hwnd, SW_RESTORE)
            u32.ShowWindow(hwnd, SW_SHOW)
            u32.SetForegroundWindow(hwnd)
            u32.BringWindowToTop(hwnd)
            return True
    except Exception:
        pass
    return False


def turn_off_monitor():
    """立刻关显示器（相当于按显示器的电源键）。

    用 SendMessageTimeoutW 而不是 PostMessage：
    WM_SYSCOMMAND 得让系统真的处理完，超时给 2 秒，别把界面卡住。
    """
    try:
        u32 = ctypes.windll.user32
        HWND_BROADCAST = 0xFFFF
        WM_SYSCOMMAND = 0x0112
        SC_MONITORPOWER = 0xF170
        MONITOR_OFF = 2
        SMTO_ABORTIFHUNG = 0x0002
        res = ctypes.c_ulong(0)
        u32.SendMessageTimeoutW(ctypes.c_void_p(HWND_BROADCAST),
                                WM_SYSCOMMAND,
                                SC_MONITORPOWER,
                                MONITOR_OFF,
                                SMTO_ABORTIFHUNG,
                                2000,
                                ctypes.byref(res))
        return True
    except Exception as e:
        print(f"[关屏] 失败: {e}")
        return False


# 熄屏后多少秒弹全屏。默认 0 = 熄屏即弹；用户可以改任意秒数。
DEFAULT_MONITOR_AWAY = 0


def monitor_away_threshold(state):
    """熄屏后延迟多少秒才弹。

    不做上下限夹取 —— 用户要多少给多少（至少 0）。
    """
    try:
        v = int(state["settings"].get("monitorAwaySeconds", DEFAULT_MONITOR_AWAY))
    except Exception:
        v = DEFAULT_MONITOR_AWAY
    return max(0, v)


# ---------------------------------------------------------------- 数据目录
# 数据固定放在程序旁边的 app\data —— 绿色版跟着文件夹走，
# 整个目录拷到别处，备忘也跟着过去。
#
# 【为什么不再到处找】
# 以前按 %APPDATA% → ~/.instant-note → 程序目录 的顺序挑第一个能写的，
# 结果程序到底读哪儿取决于运行时环境（sys.argv[0] 是谁、APPDATA 是否
# 可写……），跟人以为的位置对不上，表现就是"我明明改了文件，界面没变"、
# "数据一会儿在一会儿没"。现在只认一处，没有歧义。
def _program_dir():
    """程序根目录（app 的上一级）。

    正常启动时 sys.argv[0] 是 ...\\即时备忘-绿色版\\app\\main.py，
    取 dirname 两次就是 ...\\即时备忘-绿色版。
    直接用 python 交互式 import 时退化到本文件的目录。
    """
    try:
        here = os.path.dirname(os.path.abspath(sys.argv[0]))
        if os.path.basename(here).lower() == "app":
            return os.path.dirname(here)
        return here
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def _pick_data_dir():
    """数据目录：固定为 程序目录\\app\\data，不挑、不随机。"""
    root = _program_dir()
    # 源码目录直接跑（pyapp/main.py）时，落到 pyapp/data，方便调试
    if os.path.basename(root).lower() == "pyapp":
        d = os.path.join(root, "data")
    else:
        d = os.path.join(root, "app", "data")

    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return d
    except Exception:
        # 程序目录不可写（比如放在 Program Files）才退到用户目录，
        # 且同样用固定名字，绝不随机。
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "InstantNote", "data")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d


DATA_DIR = _pick_data_dir()
STATE_FILE = os.path.join(DATA_DIR, "state.json")


def _migrate_old_state():
    """第一次用新路径时，把散落在旧位置的数据搬过来。

    以前数据可能在 %APPDATA%\\InstantNote 或 ~/.instant-note，
    换成固定 app\\data 之后，老用户第一次启动会看到一个空列表 ——
    所以这里做一次性迁移：新位置没有 state.json、而旧位置有时，复制过来。
    只复制，不删旧的，留个后路。
    """
    try:
        if os.path.exists(STATE_FILE):
            return None                      # 新位置已有，不用管
        olds = [
            os.path.join(os.environ.get("APPDATA", ""), APP_ID, "state.json"),
            os.path.join(os.path.expanduser("~"), ".instant-note", "state.json"),
        ]
        for old in olds:
            if not old or not os.path.exists(old):
                continue
            try:
                with open(old, "rb") as f:
                    blob = f.read()
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(STATE_FILE, "wb") as f:
                    f.write(blob)
                return old
            except Exception:
                continue
    except Exception:
        pass
    return None


_migrated_from = _migrate_old_state()

# 启动自检：把实际用的数据目录写进日志文件。
# pythonw.exe 没有控制台，print 是看不见的，必须落盘。
try:
    import io as _io
    _log = _io.open(os.path.join(DATA_DIR, "datadir.log"), "a", encoding="utf-8")
    _log.write("%s  DATA_DIR=%s  存在=%s%s\n" % (
        datetime.now().strftime("%m-%d %H:%M:%S"), DATA_DIR,
        os.path.exists(STATE_FILE),
        ("  已从 %s 迁移" % _migrated_from) if _migrated_from else ""))
    _log.close()
except Exception:
    pass

DEFAULT_STATE = {
    "version": 1,
    "notes": [],
    "settings": {
        "launchAtLogin": True,
        "overlayOnBoot": True,
        "overlayOnUnlock": True,
        "overlayEveryMinutes": 30,
        "idleAwaySeconds": 150,
        "monitorAwaySeconds": 0,
        "theme": "dark",
        "regionTheme": {"main": "dark", "widget": "dark", "overlay": "dark"},
        "widget": True,
        "widgetPos": None,
        "widgetEdgeHide": True,
        "showDone": False,
        # 浮窗 / 全屏提醒各自只看哪个类，"" = 全部
        "widgetCat": "",
        "overlayCat": "",
        # Alt+L 一键关显示器（默认关，面板里可以开）
        "hotkeyDisplayOff": False,
    },
    # 分类表：[{"cat": "工作", "subs": ["周报", "会议"]}, ...]
    "categories": [],
}

DEFAULT_CAT = "未分类"
DEFAULT_SUB = ""

# ---------------------------------------------------------------- 配色
# 两套主题，用户在设置里切换。切换后重建窗口，所有控件重新取色。
THEMES = {
    "dark": {
        "BG":         "#0f1015",
        "BG_SOFT":    "#16181f",
        "BG_CARD":    "#1c1f28",
        "LINE":       "#2a2e3a",
        "TEXT":       "#e8eaf0",
        "TEXT_DIM":   "#8b93a7",
        "TEXT_FAINT": "#5c6478",
    },
    "light": {
        "BG":         "#ffffff",
        "BG_SOFT":    "#f5f6f8",
        "BG_CARD":    "#eceef2",
        "LINE":       "#dcdfe6",
        "TEXT":       "#1a1d24",
        "TEXT_DIM":   "#5a6274",
        "TEXT_FAINT": "#8c94a6",
    },
}
# 强调色和语义色两套主题共用（浅底上也看得清）
ACCENT     = "#ffb545"
ACCENT_HI  = "#ffc463"
ACCENT_DARK_ON_LIGHT = "#c47a00"   # 浅色主题下用更深的琥珀，保证对比度
DANGER     = "#ff5f56"
DANGER_LIGHT = "#d92d20"
OK_GREEN   = "#4ade80"
OK_GREEN_LIGHT = "#16a34a"
ON_ACCENT  = "#1a1200"   # 强调色按钮上的文字（琥珀底配深字，两套主题通用）

# ---------------------------------------------------------------- 分区配色
# 主界面 / 浮窗 / 全屏提醒 三个窗口各自独立选黑或白。
# 每个窗口类在 build() 开头调 palette_for('main'|'widget'|'overlay')，
# 把该区域的颜色绑到 self.BG / self.TEXT ... 上，之后整个类都用 self.XXX。
REGIONS = ("main", "widget", "overlay")


def palette_for(key):
    """取某个窗口区域的配色字典"""
    return THEMES[palette_key(key)]


def palette_key(key):
    """从 state 里读某区域选的是黑还是白"""
    try:
        return _SETTINGS_REF.get("regionTheme", {}).get(key, "dark")
    except Exception:
        return "dark"


_SETTINGS_REF = {}


class RegionPalette:
    """给一个窗口区域装上自己的配色。

    各窗口类继承它，在 build() 第一行调 self.set_palette()，
    之后类里所有 BG / TEXT / ACCENT 都指到 self.XXX 上，
    三个窗口就能各选各的黑白，互不干扰。
    """

    # 默认值（深色），set_palette 会覆盖
    BG = TEXT = BG_SOFT = BG_CARD = LINE = "#000000"
    TEXT_DIM = TEXT_FAINT = ACCENT = ACCENT_HI = DANGER = OK_GREEN = "#000000"
    ON_ACCENT = "#1a1200"
    THEME_KIND = "dark"

    def set_palette(self, key):
        global _SETTINGS_REF
        try:
            _SETTINGS_REF = self.app.state["settings"]
        except Exception:
            pass
        kind = palette_key(key)
        t = THEMES.get(kind, THEMES["dark"])

        self.THEME_KIND = kind
        self.BG         = t["BG"]
        self.BG_SOFT    = t["BG_SOFT"]
        self.BG_CARD    = t["BG_CARD"]
        self.LINE       = t["LINE"]
        self.TEXT       = t["TEXT"]
        self.TEXT_DIM   = t["TEXT_DIM"]
        self.TEXT_FAINT = t["TEXT_FAINT"]

        if kind == "light":
            # 浅色主题：强调色要够深才压得住白底，这时按钮上必须用白字
            self.ACCENT     = ACCENT_DARK_ON_LIGHT
            self.ACCENT_HI  = "#a86a00"
            self.DANGER     = DANGER_LIGHT
            self.OK_GREEN   = OK_GREEN_LIGHT
            self.ON_ACCENT  = "#ffffff"
        else:
            self.ACCENT     = "#ffb545"
            self.ACCENT_HI  = "#ffc463"
            self.DANGER     = "#ff5f56"
            self.OK_GREEN   = "#4ade80"
            self.ON_ACCENT  = "#1a1200"
        return kind

# 模块级默认色（只在类外零散用到时兜底，实际配色看各窗口的 set_palette）
THEME = "dark"
BG         = THEMES["dark"]["BG"]
BG_SOFT    = THEMES["dark"]["BG_SOFT"]
BG_CARD    = THEMES["dark"]["BG_CARD"]
LINE       = THEMES["dark"]["LINE"]
TEXT       = THEMES["dark"]["TEXT"]
TEXT_DIM   = THEMES["dark"]["TEXT_DIM"]
TEXT_FAINT = THEMES["dark"]["TEXT_FAINT"]


UI_FONT = "Microsoft YaHei UI"

# 运行时确定缩放比（96dpi = 1.0）
SCALE = 1.0


def S(v):
    """按 DPI 缩放一个逻辑像素值"""
    return max(1, int(round(v * SCALE)))


def F(px, weight="normal"):
    """字体：用负数字号 = 像素高度，保证和 S() 同步缩放"""
    if weight == "normal":
        return (UI_FONT, -S(px))
    return (UI_FONT, -S(px), weight)


# ---------------------------------------------------------------- 状态存取
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        st = json.loads(json.dumps(DEFAULT_STATE))
        st.update(data)
        st["settings"].update(data.get("settings", {}))
        if not isinstance(st.get("notes"), list):
            st["notes"] = []
        return st
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("[保存失败]", e)


_id_counter = [0]


def new_id():
    _id_counter[0] += 1
    return f"{int(time.time()*1000):x}{_id_counter[0]:x}{os.urandom(3).hex()}"


def pending_notes(state):
    """未完成的备忘，按手动顺序排。

    排序键：(置顶优先, 手动序号, 创建时间)。
    手动序号是拖动/上下移时写进去的，没有就退回按创建时间。
    """
    out = [n for n in state["notes"] if not n.get("done") and n.get("text", "").strip()]
    out.sort(key=lambda n: (0 if n.get("pinned") else 1,
                            n.get("order", n.get("createdAt", 0)),
                            n.get("createdAt", 0)))
    return out


# ---------------------------------------------------------------- 分类
def get_cats(state):
    """读分类表，顺便把备忘里出现过但没登记的类补进去。"""
    cats = state.setdefault("categories", [])
    seen = {}
    for c in cats:
        seen[c.get("cat", "")] = c
    changed = False
    for n in state.get("notes", []):
        cat = (n.get("cat") or "").strip() or DEFAULT_CAT
        sub = (n.get("sub") or "").strip()
        if cat not in seen:
            c = {"cat": cat, "subs": []}
            cats.append(c)
            seen[cat] = c
            changed = True
        if sub and sub not in seen[cat]["subs"]:
            seen[cat]["subs"].append(sub)
            changed = True
    if changed:
        save_state(state)
    return cats


def add_category(state, cat, sub=""):
    """加一个类；sub 给了就同时记一个标签。已存在就不重复加。"""
    cat = (cat or "").strip()
    sub = (sub or "").strip()
    if not cat:
        return
    cats = get_cats(state)
    for c in cats:
        if c["cat"] == cat:
            if sub and sub not in c["subs"]:
                c["subs"].append(sub)
                save_state(state)
            return
    cats.append({"cat": cat, "subs": [sub] if sub else []})
    save_state(state)


def note_label(n):
    """备忘的分类标签。"""
    cat = (n.get("cat") or "").strip() or DEFAULT_CAT
    sub = (n.get("sub") or "").strip()
    return f"{cat}/{sub}" if sub else cat


def note_matches(n, pairs):
    """pairs 为空表示全都要；否则命中任一个 (cat, sub) 就行。"""
    if not pairs:
        return True
    cat = (n.get("cat") or "").strip() or DEFAULT_CAT
    sub = (n.get("sub") or "").strip()
    for pc, ps in pairs:
        if cat == pc and (ps == "" or sub == ps):
            return True
    return False


# ---------------------------------------------------------------- Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def set_topmost(hwnd, on=True):
    try:
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            wt.HWND(hwnd), wt.HWND(HWND_TOPMOST if on else HWND_NOTOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def get_idle_seconds():
    """系统空闲秒数：没键盘鼠标动作多久了。熄屏时也会一直累加。"""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]
        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if user32.GetLastInputInfo(ctypes.byref(li)):
            millis = (kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF
            return millis / 1000.0
    except Exception:
        pass
    return 0.0


def is_session_locked():
    """锁屏检测：锁屏后打不开输入桌面。"""
    try:
        hdesk = user32.OpenInputDesktop(0, False, 0x0001)  # DF_ALLOWOTHERACCOUNTHOOK
        if hdesk:
            user32.CloseDesktop(hdesk)
            return False
        return True
    except Exception:
        return False


def is_monitor_on():
    """显示器是否亮着。

    关显示器（power off）时 Windows 会把显示器判定为关闭，但很多机器
    只是断电信号，API 仍返回开。所以这里只作为辅助信号，主判据是空闲时间。
    返回 True / False / None(判断不了)
    """
    try:
        # SM_CMONITORS = 80
        n = user32.GetSystemMetrics(80)
        return bool(n and n > 0)
    except Exception:
        return None


def get_display_session_flag():
    """SYSTEM_POWER_STATUS.SystemStatusFlag bit0 是否表示显示器关闭。

    实测（拯救者本机，150% 缩放，Win10 19044）：用 SC_MONITORPOWER 关屏时
    这个位**始终是 0x00**，抓不到。所以它只能当补充，不能当主判据。
    真正可靠的是下面的 RegisterPowerSettingNotification。
    """
    try:
        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", wt.BYTE),
                        ("BatteryFlag", wt.BYTE),
                        ("BatteryLifePercent", wt.BYTE),
                        ("SystemStatusFlag", wt.BYTE),
                        ("BatteryLifeTime", wt.DWORD),
                        ("BatteryFullLifeTime", wt.DWORD)]
        sps = SPS()
        if kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            return bool(sps.SystemStatusFlag & 0x01)
    except Exception:
        pass
    return None


# ================================================================ 显示器状态监听
# GUID_CONSOLE_DISPLAY_STATE = {6FE69556-704A-47A0-8F24-C28D936FDA47}
# 显示器真正关掉/点亮时，Windows 会向注册了这条 GUID 的窗口发 WM_POWERBROADCAST，
# 数据 4 字节：0=关闭 1=点亮 2=变暗。这是唯一可靠的「关显示器」信号。
WM_POWERBROADCAST = 0x0218
PBT_POWERSETTINGCHANGE = 0x8013
GUID_CONSOLE_DISPLAY_STATE = (0x6FE69556, 0x704A, 0x47A0,
                              (0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))

# 全局热键：RegisterHotKey 注册后，不管焦点在哪个程序都收得到。
# 消息进 WM_HOTKEY，由隐藏窗口的 wndproc 接。
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xB001          # 随便一个本进程内唯一的 id
MOD_ALT = 0x0001
VK_L = 0x4C                 # 字母 L 的虚拟键码

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT,
                             ctypes.c_size_t, ctypes.c_ssize_t)
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class _PBS(ctypes.Structure):
    _fields_ = [("PowerSetting", _GUID), ("DataLength", wt.DWORD),
                ("Data", ctypes.c_ubyte * 16)]


class DisplayWatcher:
    """监听显示器开关。回调 display_on(bool)。

    只用一个隐藏消息窗口，不占任务栏，不需要消息循环——主线程的
    tkinter mainloop 会顺带 PumpMessage，但那个窗口属于本进程，
    需要 pythonw 下也能收到，所以这里用独立的 PeekMessage 泵。
    """

    def __init__(self, on_change=None):
        self.on_change = on_change
        self.hwnd = None
        self._proc = None
        self._reg = None        # 电源通知注册句柄，注销时要用
        self._state = None      # True=亮, False=灭, None=未知

    @property
    def state(self):
        return self._state

    def start(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            cls = f"InstantNoteDisplayWatcher"

            self._proc = WNDPROC(self._wndproc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = cls
            user32.RegisterClassW(ctypes.byref(wc))

            # HWND_MESSAGE = -3
            self.hwnd = user32.CreateWindowExW(
                0, cls, cls, 0, 0, 0, 0, 0,
                ctypes.c_void_p(-3), None, hinst, None)
            if not self.hwnd:
                return False

            g = _GUID()
            g.Data1, g.Data2, g.Data3 = GUID_CONSOLE_DISPLAY_STATE[0], \
                GUID_CONSOLE_DISPLAY_STATE[1], GUID_CONSOLE_DISPLAY_STATE[2]
            for i, b in enumerate(GUID_CONSOLE_DISPLAY_STATE[3]):
                g.Data4[i] = b

            h = user32.RegisterPowerSettingNotification(
                self.hwnd, ctypes.byref(g), 0)
            self._reg = h            # 注销时要用
            return bool(h)
        except Exception as e:
            print("[DisplayWatcher] 启动失败", e)
            return False

    def stop(self):
        """注销电源通知、销毁消息窗口。退出前调，别让系统再往这里发消息。"""
        try:
            if self._reg:
                user32.UnregisterPowerSettingNotification(self._reg)
                self._reg = None
        except Exception:
            pass
        try:
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE:
                pbs = ctypes.cast(
                    lparam, ctypes.POINTER(_PBS)).contents
                raw = bytes(pbs.Data[:pbs.DataLength])
                if len(raw) >= 4:
                    val = int.from_bytes(raw[:4], "little")
                    on = (val != 0)
                    self._state = on
                    if self.on_change:
                        try:
                            self.on_change(on)
                        except Exception:
                            pass
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pump(self):
        """把属于本隐藏窗口的电源广播消息派发掉。

        tkinter 的 mainloop 只泵自己的窗口，这个隐藏窗口的消息
        得自己取，否则 _wndproc 永远收不到 WM_POWERBROADCAST。

        注意必须传 hwnd 限定范围：不传的话会把 tkinter 自己的
        消息一起吞掉，之后 tkinter.update() 会崩。
        """
        if not self.hwnd:
            return

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                        ("time", wt.DWORD), ("pt", wt.POINT)]

        m = MSG()
        # 每轮最多取 8 条，别把一帧的时间全花在这儿
        for _ in range(8):
            got = user32.PeekMessageW(ctypes.byref(m), ctypes.c_void_p(self.hwnd),
                                      0, 0, 1)      # PM_REMOVE
            if not got:
                break
            user32.TranslateMessage(ctypes.byref(m))
            user32.DispatchMessageW(ctypes.byref(m))


# ================================================================ 全局热键
class HotkeyDaemon:
    """管理外置的 Alt+L 守护进程（hotkey.py）。

    为什么单独拆一个程序？
    主程序是 tkinter，全局热键的消息必须由窗口过程接；ctypes 挂的
    窗口过程在 Python 里跑会崩（回调进入时没有线程状态）。拆出去以后
    它有自己的消息循环，跟 tkinter 彻底分开，谁也不影响谁。

    主程序只负责：
      - 设置开着  -> 把它拉起来
      - 设置关掉  -> 让它退出
      - 主面板按钮 -> 调它关一次屏（--off）
    """

    def __init__(self):
        self.proc = None        # 常驻守护进程的 Popen
        self.enabled = False

    # ---- 找可执行文件
    @staticmethod
    def _app_dir():
        return os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def _script():
        return os.path.join(HotkeyDaemon._app_dir(), "hotkey.py")

    @staticmethod
    def _runner():
        """优先用 pythonw.exe（不弹黑框），没有就用当前解释器。"""
        exe = sys.executable or ""
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pw):
            return pw
        return exe

    # ---- 起停
    def start(self):
        """拉起常驻守护进程（已经在跑就什么都不做）。"""
        if not os.path.isfile(self._script()):
            print("[热键] 找不到 hotkey.py，跳过")
            return False
        try:
            flags = 0
            if os.name == "nt":
                flags = 0x00000008 | 0x08000000     # DETACHED_PROCESS | CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                [self._runner(), self._script()],
                cwd=self._app_dir(), creationflags=flags,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.enabled = True
            print("[热键] Alt+L 守护进程已启动")
            return True
        except Exception as e:
            print("[热键] 启动守护进程失败", e)
            return False

    def ensure(self):
        """确认守护进程活着；已经有一个在跑（比如上次没退干净）也算成功。"""
        if self._alive():
            self.enabled = True
            return True
        return self.start()

    def _alive(self):
        p = self.proc
        return bool(p and p.poll() is None)

    def stop(self):
        """让守护进程退出，并释放 Alt+L。"""
        try:
            if os.path.isfile(self._script()):
                flags = 0
                if os.name == "nt":
                    flags = 0x08000000              # CREATE_NO_WINDOW
                subprocess.run([self._runner(), self._script(), "--quit"],
                               cwd=self._app_dir(), creationflags=flags,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None
        self.enabled = False
        print("[热键] Alt+L 守护进程已停止")

    def off_once(self):
        """调守护进程关一次显示器（面板按钮走这条）。"""
        if not os.path.isfile(self._script()):
            return False
        try:
            flags = 0
            if os.name == "nt":
                flags = 0x08000000
            subprocess.Popen([self._runner(), self._script(), "--off"],
                             cwd=self._app_dir(), creationflags=flags,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            print("[热键] 关屏失败", e)
            return False


# ================================================================ 主窗口
class MainWindow(RegionPalette):
    def __init__(self, app):
        self.app = app
        self.win = None
        self._visible = False
        self._base_h = None

    def build(self):
        self.set_palette('main')
        if self.win and self.win.winfo_exists():
            return self.win

        w = tk.Toplevel(self.app.root)
        self.win = w
        w.title(APP_NAME)

        sw = self.app.sw
        sh = self.app.sh
        ww = min(S(620), max(S(480), int(sw * 0.33)))
        wh = min(S(820), max(S(560), int(sh * 0.66)))
        wx = sw - ww - max(S(60), int(sw * 0.04))
        wy = max(S(40), int((sh - wh) * 0.24))
        w.geometry(f"{ww}x{wh}+{wx}+{wy}")
        w.configure(bg=self.BG)
        w.minsize(S(420), S(480))
        w.protocol("WM_DELETE_WINDOW", self.hide)
        w.withdraw()
        self._base_h = wh       # 不展开设置时的默认高度

        # ---- 顶栏
        bar = tk.Frame(w, bg=self.BG, height=S(44))
        bar.pack(fill="x")
        bar.pack_propagate(False)

        dot = tk.Canvas(bar, width=S(10), height=S(10), bg=self.BG, highlightthickness=0)
        dot.create_oval(S(1), S(1), S(9), S(9), fill=self.ACCENT, outline="")
        dot.pack(side="left", padx=(S(16), S(6)))
        tk.Label(bar, text=APP_NAME, bg=self.BG, fg=self.TEXT_DIM,
                 font=F(13)).pack(side="left")

        # 关闭靠系统标题栏的 ×，这里不再画一个

        btn_remind = tk.Label(bar, text="提醒", bg=self.BG, fg=self.TEXT_DIM, font=F(12),
                              padx=S(12), cursor="hand2")
        btn_remind.pack(side="right")
        btn_remind.bind("<Button-1>", lambda e: self.app.show_overlay())
        btn_remind.bind("<Enter>", lambda e: btn_remind.configure(fg=self.TEXT, bg=self.BG_CARD))
        btn_remind.bind("<Leave>", lambda e: btn_remind.configure(fg=self.TEXT_DIM, bg=self.BG))

        tk.Frame(w, bg=self.LINE, height=1).pack(fill="x")

        # ---- 输入区
        self.input_wrap = tk.Frame(w, bg=self.BG_SOFT, highlightbackground=self.LINE,
                                   highlightthickness=1)
        self.input_wrap.pack(fill="x", padx=S(16), pady=(S(16), 0))

        self.input = tk.Text(self.input_wrap, height=2, bg=self.BG_SOFT, fg=self.TEXT,
                             insertbackground=self.ACCENT, font=F(13),
                             relief="flat", wrap="word",
                             padx=S(10), pady=S(8),
                             borderwidth=0, highlightthickness=0)
        self.input.pack(fill="x")
        self.input.bind("<Key-Return>", self._on_enter)

        # ---- 分类行：只选「类」
        catrow = tk.Frame(self.input_wrap, bg=self.BG_SOFT)
        catrow.pack(fill="x", padx=S(10), pady=(0, S(4)))

        self.cat_var = tk.StringVar(value=DEFAULT_CAT)

        # 先摆「新建类」贴右边
        newcat = tk.Label(catrow, text="＋ 新建类", bg=self.BG_CARD, fg=self.ACCENT,
                          font=F(11), cursor="hand2", padx=S(10), pady=S(1))
        newcat.pack(side="right", padx=(S(6), 0))
        newcat.bind("<Button-1>", lambda e: self._new_category())

        # 下拉框宽度：按输入区实际宽度减去「新建类」和间距来定，
        # 这样左边跟输入框齐、右边到按钮为止。
        self.cat_box = self._combo(
            catrow, self.cat_var, S(200),
            items=lambda: self._cat_names(with_default=True),
            on_pick=self._pick_cat, deletable=True)
        self.cat_box.pack_configure(side="left")
        self._sync_cat_width = lambda: self._fit_cat_box()

        # 窗口尺寸一变就重新算
        self.input_wrap.bind("<Configure>", lambda e: self._fit_cat_box())

        foot = tk.Frame(self.input_wrap, bg=self.BG_SOFT)
        foot.pack(fill="x", padx=S(10), pady=(0, S(8)))

        self.pin_var = tk.BooleanVar(value=False)
        pin = tk.Checkbutton(foot, text="置顶", variable=self.pin_var,
                             bg=self.BG_SOFT, fg=self.TEXT_DIM, selectcolor=self.BG_CARD,
                             activebackground=self.BG_SOFT, activeforeground=self.TEXT,
                             font=F(12), relief="flat", borderwidth=0,
                             highlightthickness=0)
        pin.pack(side="left")

        add = tk.Label(foot, text="添加", bg=self.ACCENT, fg=self.ON_ACCENT, font=F(12, "bold"),
                       padx=S(18), pady=S(4), cursor="hand2")
        add.pack(side="right")
        add.bind("<Button-1>", lambda e: self.add_note())

        # ---- 列表头
        head = tk.Frame(w, bg=self.BG)
        head.pack(fill="x", padx=S(18), pady=(S(14), S(6)))
        self.count_lbl = tk.Label(head, text="", bg=self.BG, fg=self.TEXT_FAINT, font=F(12))
        self.count_lbl.pack(side="left")

        self.showdone_var = tk.BooleanVar(
            value=self.app.state["settings"].get("showDone", False))
        cb = tk.Checkbutton(head, text="显示已完成", variable=self.showdone_var,
                            bg=self.BG, fg=self.TEXT_FAINT, selectcolor=self.BG_CARD,
                            activebackground=self.BG, activeforeground=self.TEXT,
                            font=F(12), relief="flat", borderwidth=0,
                            highlightthickness=0,
                            command=self.on_toggle_showdone)
        cb.pack(side="right")

        # ---- 列表
        listwrap = tk.Frame(w, bg=self.BG)
        listwrap.pack(fill="both", expand=True, padx=S(16), pady=(0, S(8)))

        self.canvas = tk.Canvas(listwrap, bg=self.BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(listwrap, orient="vertical", command=self.canvas.yview,
                          bg=self.BG_CARD, troughcolor=self.BG, borderwidth=0,
                          highlightthickness=0, width=S(8))
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.listframe = tk.Frame(self.canvas, bg=self.BG)
        self._win_id = self.canvas.create_window((0, 0), window=self.listframe,
                                                 anchor="nw")
        self.listframe.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # ---- 设置区（可折叠，默认收起，把空间让给列表）
        tk.Frame(w, bg=self.LINE, height=1).pack(fill="x", side="bottom")

        self.set_body = tk.Frame(w, bg=self.BG_SOFT)
        self._set_open = False

        self.set_toggle = tk.Label(
            w, text="▸ 设置", bg=self.BG_SOFT, fg=self.TEXT_FAINT, font=F(12),
            anchor="w", padx=S(16), pady=S(7), cursor="hand2")
        self.set_toggle.pack(fill="x", side="bottom")
        self.set_toggle.bind("<Button-1>", lambda e: self.toggle_settings())
        self.set_toggle.bind("<Enter>", lambda e: self.set_toggle.configure(fg=self.TEXT))
        self.set_toggle.bind("<Leave>",
                             lambda e: self.set_toggle.configure(fg=self.TEXT_FAINT))

        row1 = tk.Frame(self.set_body, bg=self.BG_SOFT)
        row1.pack(fill="x", padx=S(16), pady=S(3))
        self.s_boot = self._switch(row1, "启动时全屏提醒", "overlayOnBoot")
        self.s_unlock = self._switch(row1, "熄屏回来提醒", "overlayOnUnlock")

        row2 = tk.Frame(self.set_body, bg=self.BG_SOFT)
        row2.pack(fill="x", padx=S(16), pady=S(3))
        self.s_widget = self._switch(row2, "桌面浮窗", "widget")
        self.s_edge = self._switch(row2, "贴边隐藏", "widgetEdgeHide")

        # Alt+L 关显示器：单独的开关，默认关
        row2b = tk.Frame(self.set_body, bg=self.BG_SOFT)
        row2b.pack(fill="x", padx=S(16), pady=S(3))
        self.s_hotkey = self._switch(row2b, "Alt+L 一键关显示器", "hotkeyDisplayOff")
        tk.Label(row2b, text="（关掉后 Alt+L 不响应）", bg=self.BG_SOFT,
                 fg=self.TEXT_FAINT, font=F(11)).pack(side="left")

        row3 = tk.Frame(self.set_body, bg=self.BG_SOFT)
        row3.pack(fill="x", padx=S(16), pady=(S(4), S(4)))
        tk.Label(row3, text="每隔", bg=self.BG_SOFT, fg=self.TEXT_DIM,
                 font=F(12)).pack(side="left")
        self.every_var = tk.StringVar(
            value=str(self.app.state["settings"].get("overlayEveryMinutes", 30)))
        ent = tk.Entry(row3, textvariable=self.every_var, width=4, bg=self.BG_CARD,
                       fg=self.TEXT, insertbackground=self.ACCENT, relief="flat",
                       font=F(12), justify="center")
        ent.pack(side="left", padx=S(6))
        ent.bind("<FocusOut>", lambda e: self.on_every_change())
        ent.bind("<Return>", lambda e: self.on_every_change())
        tk.Label(row3, text="分钟（0 = 不提醒）", bg=self.BG_SOFT, fg=self.TEXT_DIM,
                 font=F(12)).pack(side="left")

        # 熄屏后多少秒弹全屏
        row3b = tk.Frame(self.set_body, bg=self.BG_SOFT)
        row3b.pack(fill="x", padx=S(16), pady=(0, S(10)))
        tk.Label(row3b, text="熄屏后", bg=self.BG_SOFT, fg=self.TEXT_DIM,
                 font=F(12)).pack(side="left")
        self.away_var = tk.StringVar(
            value=str(monitor_away_threshold(self.app.state)))
        ent2 = tk.Entry(row3b, textvariable=self.away_var, width=5, bg=self.BG_CARD,
                        fg=self.TEXT, insertbackground=self.ACCENT, relief="flat",
                        font=F(12), justify="center")
        ent2.pack(side="left", padx=S(6))
        ent2.bind("<FocusOut>", lambda e: self.on_away_change())
        ent2.bind("<Return>", lambda e: self.on_away_change())
        tk.Label(row3b, text="秒弹全屏（0 = 立刻）", bg=self.BG_SOFT, fg=self.TEXT_DIM,
                 font=F(12)).pack(side="left")

        # 配色：三个窗口各选各的黑白
        self._theme_chips = {}
        for region, title in (("main", "主界面"),
                              ("widget", "浮窗"),
                              ("overlay", "全屏提醒")):
            row = tk.Frame(self.set_body, bg=self.BG_SOFT)
            row.pack(fill="x", padx=S(16), pady=(0, S(4)))
            tk.Label(row, text=title, bg=self.BG_SOFT, fg=self.TEXT_DIM,
                     font=F(11), width=5, anchor="w").pack(side="left")
            cur = self.app.region_theme(region)
            for label, key in (("黑", "dark"), ("白", "light")):
                on = (cur == key)
                chip = tk.Label(
                    row, text=label, font=F(11),
                    bg=(self.ACCENT if on else self.BG_CARD),
                    fg=(self.ON_ACCENT if on else self.TEXT_DIM),
                    padx=S(13), pady=S(1), cursor="hand2")
                chip.pack(side="left", padx=S(3))
                chip.bind("<Button-1>",
                          lambda e, r=region, k=key: self.app.set_region_theme(r, k))
                self._theme_chips[(region, key)] = chip

        # 显示哪个类：浮窗 / 全屏提醒 各一个（下拉）
        if not hasattr(self, "_disp_vars"):
            self._disp_vars = {}

        def cat_row(title, key, getter, setter):
            row = tk.Frame(self.set_body, bg=self.BG_SOFT)
            row.pack(fill="x", padx=S(16), pady=(0, S(6)))
            tk.Label(row, text=title, bg=self.BG_SOFT, fg=self.TEXT_DIM,
                     font=F(11), width=9, anchor="w").pack(side="left")
            var = tk.StringVar(value=(getter() or "全部"))
            self._disp_vars[key] = var
            box = self._combo(
                row, var, S(200),
                items=lambda: ["全部"] + self._cat_names(),
                on_pick=lambda n, st=setter: st("" if n == "全部" else n))
            if not hasattr(self, "_disp_boxes"):
                self._disp_boxes = {}
            self._disp_boxes[key] = box

        cat_row("浮窗显示", "widget", self.app.widget_cat, self.app.set_widget_cat)
        cat_row("全屏显示", "overlay", self.app.overlay_cat, self.app.set_overlay_cat)

        # 底部一行：退出按钮（数据目录不再显示，省得占地方）
        row4 = tk.Frame(self.set_body, bg=self.BG_SOFT)
        row4.pack(fill="x", padx=S(16), pady=(0, S(8)))

        quitbtn = tk.Label(row4, text="Exit", bg=self.BG_CARD,
                           fg=self.DANGER, font=F(11, "bold"), cursor="hand2",
                           padx=S(16), pady=S(2))
        quitbtn.pack(side="right")
        quitbtn.bind("<Button-1>", lambda e: self.app.quit_app())
        quitbtn.bind("<Enter>", lambda e: quitbtn.configure(
            bg=self.DANGER, fg="#ffffff"))
        quitbtn.bind("<Leave>", lambda e: quitbtn.configure(
            bg=self.BG_CARD, fg=self.DANGER))

        tk.Label(row4, text=f"数据：{DATA_DIR}", bg=self.BG_SOFT, fg=self.TEXT_FAINT,
                 font=F(10), anchor="w", justify="left",
                 wraplength=S(300)).pack(side="left", pady=(S(4), 0))

        self.refresh()
        self._refresh_cats(keep=False)
        # 窗口尺寸定下来了，再对一次宽度（<Configure> 早于真实布局）
        self.win.after(80, self._fit_cat_box)
        return w

    def open_data_dir(self):
        try:
            os.startfile(DATA_DIR)
        except Exception:
            pass

    # ---- 分类控件
    def _combo(self, parent, var, width_px, items=None, on_pick=None,
               deletable=False, stretch=False):
        """分类下拉（自绘浮层）。

        不用 tk.Menu —— 菜单一行只能整行点击，做不到「名字后面单独挂个 ✕」。
        这里用 Toplevel 铺一排「名字…✕」，名字和 ✕ 各自独立可点。
        """
        box = tk.Frame(parent, bg=self.BG_CARD,
                       highlightbackground=self.LINE, highlightthickness=1)
        box.pack(side="left", padx=(0, S(6)))
        if stretch:
            # 宽度交给外部的 fill/expand：高度用固定行高兜住，
            # 不能让 pack_propagate(False) 兜 —— 它连宽度也一起锁死
            box.configure(height=S(26))
        else:
            box.pack_propagate(False)
            box.configure(width=width_px, height=S(26))

        lb = tk.Label(box, textvariable=var, bg=self.BG_CARD, fg=self.TEXT,
                      font=F(11), anchor="w", cursor="hand2", padx=S(8))
        lb.pack(side="left", fill="both", expand=True)

        arrow = tk.Label(box, text="▾", bg=self.BG_CARD, fg=self.TEXT_DIM,
                         font=F(10), cursor="hand2", padx=S(5))
        arrow.pack(side="right")

        box.menu = None          # 兼容旧代码里的 box.menu 引用
        box.label = lb
        box.var = var            # 选完之后要写回这个变量
        box.popup = None
        box.width_px = width_px
        box.items_fn = items
        box.on_pick = on_pick
        box.deletable = deletable

        def pop(e=None):
            self._open_combo(box)
            return "break"

        for w in (lb, arrow, box):
            w.bind("<Button-1>", pop)
        return box

    # ---------- 自绘下拉浮层 ----------
    def _close_combo(self, box=None):
        pw = getattr(self, "_combo_pop", None)
        if pw is not None:
            try:
                pw.destroy()
            except Exception:
                pass
        self._combo_pop = None

    def _open_combo(self, box):
        self._close_combo()
        try:
            items = box.items_fn() if callable(box.items_fn) else list(box.items_fn or [])
        except Exception:
            items = []
        if not items:
            return

        pw = tk.Toplevel(self.win)
        pw.overrideredirect(True)
        pw.configure(bg=self.LINE)          # 1px 边框是底色露出来的
        pw.attributes("-topmost", True)
        self._combo_pop = pw

        inner = tk.Frame(pw, bg=self.BG_CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        for name in items:
            row = tk.Frame(inner, bg=self.BG_CARD)
            row.pack(fill="x")
            del_btn = None

            txt = tk.Label(row, text=name, bg=self.BG_CARD, fg=self.TEXT,
                           font=F(11), anchor="w", padx=S(8), pady=S(3),
                           cursor="hand2")
            txt.pack(side="left", fill="x", expand=True)

            if box.deletable and name != DEFAULT_CAT:
                del_btn = tk.Label(row, text="✕", bg=self.BG_CARD,
                                   fg=self.TEXT_FAINT, font=F(10),
                                   padx=S(7), cursor="hand2")
                del_btn.pack(side="right")

            def pick(e, n=name):
                self._close_combo()
                # 先写回这个下拉自己的变量，否则设置区那两个永远是旧值
                try:
                    box.var.set(n)
                except Exception:
                    pass
                if callable(box.on_pick):
                    box.on_pick(n)
                return "break"

            def delit(e, n=name):
                self._close_combo()
                self._delete_category(n)
                return "break"

            txt.bind("<Button-1>", pick)
            row.bind("<Button-1>", pick)

            if del_btn is not None:
                del_btn.bind("<Button-1>", delit)
                del_btn.bind("<Enter>", lambda e, w=del_btn:
                             w.configure(fg=self.DANGER))
                del_btn.bind("<Leave>", lambda e, w=del_btn:
                             w.configure(fg=self.TEXT_FAINT))
                del_btn.bind("<Button-1>", delit)

            # 悬停高亮
            def on_enter(e, r=row, t=txt):
                r.configure(bg=self.BG_SOFT)
                t.configure(bg=self.BG_SOFT)
            def on_leave(e, r=row, t=txt):
                r.configure(bg=self.BG_CARD)
                t.configure(bg=self.BG_CARD)
            row.bind("<Enter>", on_enter)
            row.bind("<Leave>", on_leave)

        # 摆到下拉框正下方
        pw.update_idletasks()
        x = box.winfo_rootx()
        y = box.winfo_rooty() + box.winfo_height()
        pw.geometry(f"+{x}+{y}")
        pw.bind("<FocusOut>", lambda e: self._close_combo())
        pw.focus_set()

    def _cat_names(self, with_default=False):
        """当前所有类名。with_default 时把「未分类」垫在最前。"""
        names = [c["cat"] for c in get_cats(self.app.state)]
        if with_default and DEFAULT_CAT not in names:
            names.insert(0, DEFAULT_CAT)
        return names

    def _refresh_cats(self, keep=True):
        """选中项还在就保留，不在就退回未分类。选项是现读的，不用重建。"""
        names = self._cat_names(with_default=True)
        cur = self.cat_var.get() if keep else DEFAULT_CAT
        if cur not in names:
            cur = DEFAULT_CAT if DEFAULT_CAT in names else names[0]
        self.cat_var.set(cur)

    def _pick_cat(self, name):
        self.cat_var.set(name)
        self.refresh()              # 换了类就换一份清单

    def _combo_pick(self, box, var, name):
        """下拉框选中：更新文字，再叫它自己的回调。"""
        var.set(name)
        cb = getattr(box, "on_pick", None)
        if cb:
            try:
                cb(name)
            except Exception as e:
                print("[下拉]", e)

    def _fit_cat_box(self):
        """让分类下拉的宽度跟输入框对齐。

        输入框里有 padx=S(10) 的内边距，标题那一行定位在外框上；
        所以这里按输入框的实际宽度算，扣掉内边距和「新建类」。
        """
        try:
            if not self.cat_box.winfo_exists():
                return
            row = self.cat_box.master
            row.update_idletasks()
            avail = row.winfo_width()
            if avail <= 1:
                # 还没布局，退回用输入区宽度去算
                avail = self.input_wrap.winfo_width() - 2 * S(10)
            if avail <= 1:
                return
            # 「新建类」占了多宽
            used = 0
            for c in row.winfo_children():
                if c is not self.cat_box:
                    used += c.winfo_width() + S(6)
            w = avail - used
            if w < S(120):
                w = S(120)
            if abs(w - self.cat_box.winfo_width()) >= 2:
                self.cat_box.configure(width=w)
        except Exception:
            pass

    def _delete_category(self, name):
        """删掉一个类：连它下面的备忘一起删。"""
        if name == DEFAULT_CAT:
            return
        try:
            n = sum(1 for x in self.app.state["notes"]
                    if (x.get("cat") or DEFAULT_CAT) == name)
            msg = f"删除「{name}」?"
            if n:
                msg += f"\n它下面的 {n} 条备忘也会一起删掉。"
            if not messagebox.askokcancel("删除类", msg):
                return

            self.app.state["notes"] = [
                x for x in self.app.state["notes"]
                if (x.get("cat") or DEFAULT_CAT) != name]
            self.app.state["categories"] = [
                c for c in self.app.state["categories"]
                if c.get("cat") != name]

            # 正在看的类被删了，退回未分类
            if self.cat_var.get() == name:
                self.cat_var.set(DEFAULT_CAT)
            for key, getter, setter in (
                    ("widget", self.app.widget_cat, self.app.set_widget_cat),
                    ("overlay", self.app.overlay_cat, self.app.set_overlay_cat)):
                if getter() == name:
                    setter("")
                    v = self._disp_vars.get(key)
                    if v is not None:
                        v.set("全部")

            save_state(self.app.state)
            self._refresh_cats()
            self.refresh()
            self.app.widget_refresh()
        except Exception as e:
            print("[删类]", e)
            import traceback
            traceback.print_exc()

    def _fill_disp_menu(self, key):
        """保留为兼容入口 —— 新下拉每次点开都现读，不需要预填。"""
        return

    def _new_category(self):
        """新建一个类。"""
        top = tk.Toplevel(self.win)
        top.title("新建类")
        top.configure(bg=self.BG)
        top.transient(self.win)
        top.resizable(False, False)

        tk.Label(top, text="类名", bg=self.BG, fg=self.TEXT_DIM,
                 font=F(12)).grid(row=0, column=0, padx=(S(16), S(6)),
                                  pady=(S(16), S(12)), sticky="e")
        cat_e = tk.Entry(top, bg=self.BG_CARD, fg=self.TEXT,
                         insertbackground=self.ACCENT,
                         relief="flat", font=F(12), width=18)
        cat_e.grid(row=0, column=1, padx=(0, S(16)), pady=(S(16), S(12)))

        def ok(ev=None):
            name = cat_e.get().strip()
            if not name:
                return "break"
            # 先把窗关掉，再刷新界面。
            # 反过来的话 refresh 一起错，窗就一直卡着不关。
            try:
                top.grab_release()
            except Exception:
                pass
            top.destroy()
            try:
                add_category(self.app.state, name)
                self.cat_var.set(name)
                self._refresh_cats()
                self.refresh()          # 新建完直接切过去，清单跟着换
                # 设置里的两个显示下拉也跟着更新，免得起手动重开设置
                for k in list(getattr(self, "_disp_boxes", {})):
                    self._fill_disp_menu(k)
                self.app.widget_refresh()
            except Exception as e:
                print("[新建类]", e)
                import traceback
                traceback.print_exc()
            return "break"

        cat_e.bind("<Return>", ok)

        btns = tk.Frame(top, bg=self.BG)
        btns.grid(row=1, column=0, columnspan=2, pady=(0, S(14)))
        yes = tk.Label(btns, text="确定", bg=self.ACCENT, fg=self.ON_ACCENT,
                       font=F(11, "bold"), padx=S(18), pady=S(3), cursor="hand2")
        yes.pack(side="left", padx=S(6))
        yes.bind("<Button-1>", ok)
        no = tk.Label(btns, text="取消", bg=self.BG_CARD, fg=self.TEXT_DIM,
                      font=F(11), padx=S(18), pady=S(3), cursor="hand2")
        no.pack(side="left", padx=S(6))
        def cancel(ev=None):
            try:
                top.grab_release()
            except Exception:
                pass
            top.destroy()
            return "break"

        no.bind("<Button-1>", cancel)
        top.bind("<Escape>", cancel)

        cat_e.focus_set()

    def _pick_cat_disp(self, title, opts, setter, val):
        """点了某个类的显示按钮：存盘 + 原地换色。"""
        setter(val)
        for _, v in opts:
            chip = self._cat_chips.get((title, v))
            if chip is None or not chip.winfo_exists():
                continue
            on = (v == val)
            chip.configure(bg=(self.ACCENT if on else self.BG_CARD),
                           fg=(self.ON_ACCENT if on else self.TEXT_DIM))

    def _sync_disp_menus(self):
        """把设置里两个分类下拉刷成最新。设置区是启动时建的，
        那时多半一个类都没有，展开前必须重新拉一遍。"""
        for k in list(getattr(self, "_disp_boxes", {})):
            try:
                self._fill_disp_menu(k)
            except Exception:
                pass

    def toggle_settings(self):
        if self._set_open:
            self.set_body.pack_forget()
            self.set_toggle.configure(text="▸ 设置")
            self._set_open = False
        else:
            self._sync_disp_menus()      # 展开前先拉一遍最新分类
            self.set_body.pack(fill="x", side="bottom")
            self.set_toggle.configure(text="▾ 设置")
            self._set_open = True
        self._fit_height()

    def _fit_height(self):
        """按内容调整窗口高度。

        设置区展开后行数会变多，固定高度会把底下的按钮挤没。
        这里量一下设置区实际需要多少，不够就把窗口撑高，
        但不超过屏幕可用高度。
        """
        try:
            w = self.win
            if not w or not w.winfo_exists():
                return
            w.update_idletasks()
            base = getattr(self, "_base_h", None) or w.winfo_height()

            if self._set_open:
                # 设置区被压掉多少，就把窗口加高多少。
                # 不能拿 reqheight 跟窗口高度比 —— 设置区是 side=bottom，
                # 别的控件会先抢空间，req 过了不代表真放得下。
                req = self.set_body.winfo_reqheight()
                got = self.set_body.winfo_height()
                short = max(0, req - got)
                # 列表区也要留够，别把列表挤没
                target = w.winfo_height() + short + S(8)
            else:
                target = base

            # 别超过屏幕，留出任务栏和边距
            target = min(target, self.app.sh - S(160))
            cur_w = w.winfo_width()
            w.geometry(f"{cur_w}x{target}")
        except Exception:
            pass

    def _switch(self, parent, label, key):
        var = tk.BooleanVar(value=bool(self.app.state["settings"].get(key)))
        cb = tk.Checkbutton(parent, text=label, variable=var, bg=self.BG_SOFT,
                            fg=self.TEXT_DIM, selectcolor=self.BG_CARD,
                            activebackground=self.BG_SOFT, activeforeground=self.TEXT,
                            font=F(12), relief="flat", borderwidth=0,
                            highlightthickness=0,
                            command=lambda: self._on_switch(key, var))
        cb.pack(side="left", padx=(0, S(20)))
        return var

    def _on_switch(self, key, var):
        self.app.state["settings"][key] = bool(var.get())
        save_state(self.app.state)
        if key == "widget":
            self.app.set_widget(bool(var.get()))
        elif key == "widgetEdgeHide":
            self.app.widget.set_edge_hide(bool(var.get()))
        elif key == "hotkeyDisplayOff":
            # 开关一并控制全局热键的注册，关掉后系统层面也不占用 Alt+L
            self.app.apply_hotkey_state()

    def on_every_change(self):
        try:
            v = int(self.every_var.get())
        except Exception:
            v = 30
        v = max(0, min(240, v))
        self.every_var.set(str(v))
        self.app.state["settings"]["overlayEveryMinutes"] = v
        save_state(self.app.state)
        self.app._next_remind = None

    def on_away_change(self):
        """熄屏延迟改了：写回设置；正在关屏的那次按新值重算"""
        try:
            v = int(self.away_var.get())
        except Exception:
            v = DEFAULT_MONITOR_AWAY
        v = max(0, v)          # 不限上限，用户要多久给多久
        self.away_var.set(str(v))
        self.app.state["settings"]["monitorAwaySeconds"] = v
        save_state(self.app.state)
        # 已经关屏了就重新计一次，按新延迟判断
        if self.app._display_off_at is not None:
            self.app._away_while_off = False

    def sync_theme_chips(self):
        """把三行黑白色块的高亮状态刷成当前设置。

        改浮窗/全屏的配色时主界面不重建，这几个色块得手动同步，
        否则要关掉重开主界面才看得到新选中状态。
        """
        if not getattr(self, "_theme_chips", None):
            return
        for (region, key), chip in self._theme_chips.items():
            try:
                if not chip.winfo_exists():
                    continue
                on = (self.app.region_theme(region) == key)
                chip.configure(
                    bg=(self.ACCENT if on else self.BG_CARD),
                    fg=(self.ON_ACCENT if on else self.TEXT_DIM))
            except Exception:
                pass

    def on_toggle_showdone(self):
        self.app.state["settings"]["showDone"] = bool(self.showdone_var.get())
        save_state(self.app.state)
        self.refresh()

    def _on_wheel(self, e):
        try:
            if self.canvas.winfo_exists():
                self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except Exception:
            pass

    def _on_frame_configure(self, e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, e):
        self.canvas.itemconfig(self._win_id, width=e.width)

    def _on_enter(self, e):
        if e.state & 0x0001:      # Shift+Enter 换行
            return None
        self.add_note()
        return "break"

    def add_note(self):
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        cat = self.cat_var.get().strip() or DEFAULT_CAT
        add_category(self.app.state, cat)
        self.app.state["notes"].append({
            "id": new_id(), "text": text, "done": False,
            "pinned": bool(self.pin_var.get()),
            "cat": cat, "sub": "",
            "createdAt": int(time.time() * 1000),
        })
        self.input.delete("1.0", "end")
        self.pin_var.set(False)
        save_state(self.app.state)
        self.refresh()
        self.app.widget_refresh()

    def toggle_done(self, nid):
        for n in self.app.state["notes"]:
            if n["id"] == nid:
                n["done"] = not n.get("done")
                n["doneAt"] = int(time.time() * 1000) if n["done"] else None
                break
        save_state(self.app.state)
        self.refresh()
        self.app.widget_refresh()

    def toggle_pin(self, nid):
        for n in self.app.state["notes"]:
            if n["id"] == nid:
                n["pinned"] = not n.get("pinned")
                break
        save_state(self.app.state)
        self.refresh()
        self.app.widget_refresh()

    def delete_note(self, nid):
        self.app.state["notes"] = [n for n in self.app.state["notes"]
                                   if n["id"] != nid]
        save_state(self.app.state)
        self.refresh()
        self.app.widget_refresh()

    def move_note(self, nid, delta):
        """把一条待办往上（-1）或往下（+1）挪一格。

        先把本类当前可见顺序整体写成 order，
        再交换相邻两条的 order —— 没有 order 的老数据也能正常工作。
        """
        try:
            cur = self.cat_var.get().strip() or DEFAULT_CAT
            mine = [n for n in self.app.state["notes"]
                    if (n.get("cat") or DEFAULT_CAT) == cur
                    and not n.get("done")]
            mine.sort(key=lambda n: (0 if n.get("pinned") else 1,
                                     n.get("order", n.get("createdAt", 0)),
                                     n.get("createdAt", 0)))
            ids = [n["id"] for n in mine]
            if nid not in ids:
                return
            i = ids.index(nid)
            j = i + delta
            if j < 0 or j >= len(mine):
                return
            for k, n in enumerate(mine):
                n["order"] = k
            mine[i]["order"], mine[j]["order"] = j, i
            save_state(self.app.state)
            self.refresh()
            self.app.widget_refresh()
        except Exception as e:
            print("[上移下移]", e)

    def refresh(self):
        if not self.win or not self.win.winfo_exists():
            return
        for child in self.listframe.winfo_children():
            child.destroy()

        show_done = self.showdone_var.get()
        # 主界面只看当前这个类的待办 —— 每个类是独立清单
        cur = self.cat_var.get().strip() or DEFAULT_CAT
        mine = [n for n in self.app.state["notes"]
                if (n.get("cat") or DEFAULT_CAT) == cur]
        notes = [n for n in mine if show_done or not n.get("done")]
        notes.sort(key=lambda n: (0 if n.get("pinned") else 1,
                                  n.get("order", n.get("createdAt", 0)),
                                  n.get("createdAt", 0)))

        alive = [n for n in mine if not n.get("done")]
        self.count_lbl.configure(text=f"{len(alive)} 条 · {cur}")

        if not notes:
            tk.Label(self.listframe,
                     text=f"「{cur}」还没有备忘\n\n在上面写一条，它就会出现在桌面浮窗里",
                     bg=self.BG, fg=self.TEXT_FAINT, font=F(12),
                     justify="center").pack(pady=S(40))
            return

        for n in notes:
            self._make_row(n)

    def _make_row(self, note):
        done = bool(note.get("done"))
        pinned = bool(note.get("pinned"))

        row = tk.Frame(self.listframe, bg=self.BG_SOFT,
                       highlightbackground=self.ACCENT if pinned else self.LINE,
                       highlightthickness=1)
        row.pack(fill="x", pady=S(3))

        inner = tk.Frame(row, bg=self.BG_SOFT)
        inner.pack(fill="x", padx=S(10), pady=S(8))

        box = tk.Label(inner, text="✓" if done else "", width=2,
                       bg=self.OK_GREEN if done else self.BG_SOFT,
                       fg=self.ON_ACCENT if done else self.TEXT_FAINT,
                       font=F(12, "bold"), cursor="hand2",
                       relief="solid", borderwidth=1)
        box.pack(side="left", anchor="n", pady=(S(2), 0))
        box.bind("<Button-1>", lambda e, i=note["id"]: self.toggle_done(i))

        txt = tk.Label(inner, text=note["text"],
                       bg=self.BG_SOFT, fg=self.TEXT_FAINT if done else self.TEXT,
                       font=F(13), justify="left", anchor="w",
                       wraplength=S(380))
        txt.pack(side="left", fill="x", expand=True, padx=(S(8), S(6)))
        if done:
            f = tkfont.Font(font=F(13))
            f.configure(overstrike=True)
            txt.configure(font=f)

        # 分类小标签，方便一眼看是哪一类
        tag = note_label(note)
        if tag and tag != DEFAULT_CAT:
            t = tk.Label(inner, text=tag, bg=self.BG_CARD, fg=self.TEXT_DIM,
                         font=F(9), padx=S(5), pady=S(0))
            t.pack(side="left", padx=(0, S(6)))

        acts = tk.Frame(inner, bg=self.BG_SOFT)
        acts.pack(side="right")

        # 上下移：调整待办顺序，顺序会存进 state 里
        up = tk.Label(acts, text="▲", bg=self.BG_SOFT, fg=self.TEXT_FAINT,
                      font=F(9), cursor="hand2", padx=S(3))
        up.pack(side="left")
        up.bind("<Button-1>", lambda e, i=note["id"]: self.move_note(i, -1))
        up.bind("<Enter>", lambda e: up.configure(fg=self.ACCENT))
        up.bind("<Leave>", lambda e: up.configure(fg=self.TEXT_FAINT))

        dn = tk.Label(acts, text="▼", bg=self.BG_SOFT, fg=self.TEXT_FAINT,
                      font=F(9), cursor="hand2", padx=S(3))
        dn.pack(side="left")
        dn.bind("<Button-1>", lambda e, i=note["id"]: self.move_note(i, 1))
        dn.bind("<Enter>", lambda e: dn.configure(fg=self.ACCENT))
        dn.bind("<Leave>", lambda e: dn.configure(fg=self.TEXT_FAINT))

        pin = tk.Label(acts, text="★", bg=self.BG_SOFT,
                       fg=self.ACCENT if pinned else self.TEXT_FAINT,
                       font=F(12), cursor="hand2", padx=S(3))
        pin.pack(side="left")
        pin.bind("<Button-1>", lambda e, i=note["id"]: self.toggle_pin(i))

        dele = tk.Label(acts, text="✕", bg=self.BG_SOFT, fg=self.TEXT_FAINT,
                        font=F(12), cursor="hand2", padx=S(3))
        dele.pack(side="left")
        dele.bind("<Button-1>", lambda e, i=note["id"]: self.delete_note(i))
        dele.bind("<Enter>", lambda e: dele.configure(fg=self.DANGER))
        dele.bind("<Leave>", lambda e: dele.configure(fg=self.TEXT_FAINT))

    def show(self):
        w = self.build()
        w.deiconify()
        w.lift()
        w.attributes("-topmost", True)
        w.after(S(120), lambda: w.attributes("-topmost", False))
        w.focus_force()
        self.input.focus_set()
        self.refresh()
        self._visible = True

    def hide(self):
        if self.win and self.win.winfo_exists():
            self.win.withdraw()
        self._visible = False

    def toggle(self):
        if (self._visible and self.win and self.win.winfo_exists()
                and self.win.winfo_viewable()):
            self.hide()
        else:
            self.show()


# ================================================================ 全屏提醒
class OverlayWindow(RegionPalette):
    def __init__(self, app):
        self.app = app
        self.win = None

    def build(self):
        self.set_palette('overlay')
        if self.win and self.win.winfo_exists():
            return self.win

        w = tk.Toplevel(self.app.root)
        self.win = w
        w.title("今日备忘")
        sw, sh = self.app.sw, self.app.sh
        w.geometry(f"{sw}x{sh}+0+0")
        w.configure(bg=self.BG)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.withdraw()

        w.bind("<Escape>", lambda e: self.hide())
        w.bind("<Return>", lambda e: self.hide())

        body = tk.Frame(w, bg=self.BG)
        body.pack(fill="both", expand=True, padx=S(90), pady=S(60))

        # 顶部：日期
        top = tk.Frame(body, bg=self.BG)
        top.pack(fill="x")

        left = tk.Frame(top, bg=self.BG)
        left.pack(side="left", anchor="w")

        now = datetime.now()
        tk.Label(left, text=str(now.day), bg=self.BG, fg=self.ACCENT,
                 font=F(96)).pack(side="left")

        meta = tk.Frame(left, bg=self.BG)
        meta.pack(side="left", padx=(S(18), 0), pady=(S(14), 0))
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        tk.Label(meta, text=weekdays[now.weekday()], bg=self.BG, fg=self.TEXT,
                 font=F(24)).pack(anchor="w")
        tk.Label(meta, text=f"{now.year}年{now.month}月", bg=self.BG,
                 fg=self.TEXT_FAINT, font=F(12)).pack(anchor="w", pady=(S(2), 0))

        self.greet = tk.Label(top, text=self._greet(now.hour), bg=self.BG,
                              fg=self.TEXT_DIM, font=F(16))
        self.greet.pack(side="right", anchor="e", pady=(S(20), 0))

        tk.Frame(body, bg=self.BG_CARD, height=1).pack(fill="x", pady=(S(26), 0))

        self.stat = tk.Label(body, text="", bg=self.BG, fg=self.TEXT_FAINT,
                             font=F(12), anchor="w")
        self.stat.pack(fill="x", pady=(S(20), S(12)))

        # ---- 底部按钮区：先 pack 到底部，占好位置
        # 以前是列表先 expand、按钮后 pack，条目一多列表就把按钮
        # 挤出屏幕。现在按钮先占住底部，列表只在剩下的空间里滚。
        foot = tk.Frame(body, bg=self.BG)
        foot.pack(fill="x", side="bottom", pady=(S(20), 0))

        def mkbtn(parent, text, primary, cmd):
            b = tk.Label(parent, text=text,
                         bg=self.ACCENT if primary else self.BG_SOFT,
                         fg=self.ON_ACCENT if primary else self.TEXT_DIM,
                         font=F(13, "bold" if primary else "normal"),
                         padx=S(28), pady=S(9), cursor="hand2")
            b.bind("<Button-1>", lambda e: cmd())
            b.bind("<Enter>", lambda e: b.configure(
                bg=self.ACCENT_HI if primary else self.BG_CARD,
                fg=self.ON_ACCENT if primary else self.TEXT))
            b.bind("<Leave>", lambda e: b.configure(
                bg=self.ACCENT if primary else self.BG_SOFT,
                fg=self.ON_ACCENT if primary else self.TEXT_DIM))
            return b

        mkbtn(foot, "知道了", True, self.hide).pack(side="right")
        mkbtn(foot, "10 分钟后再提醒", False,
              lambda: self.app.snooze(10)).pack(side="right", padx=(0, S(12)))
        mkbtn(foot, "打开编辑", False,
              lambda: (self.hide(), self.app.show_main())
              ).pack(side="right", padx=(0, S(12)))

        # ---- 列表区：可滚动，高度吃满剩余空间
        listwrap = tk.Frame(body, bg=self.BG)
        listwrap.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(listwrap, bg=self.BG,
                                highlightthickness=0, bd=0)
        self.sb = tk.Scrollbar(listwrap, orient="vertical",
                               command=self.canvas.yview,
                               bg=self.BG_CARD, troughcolor=self.BG,
                               borderwidth=0, highlightthickness=0,
                               width=S(10))
        self.canvas.configure(yscrollcommand=self._on_scroll_set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.listframe = tk.Frame(self.canvas, bg=self.BG)
        self._win_id = self.canvas.create_window(
            (0, 0), window=self.listframe, anchor="nw")
        self.listframe.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        return w

    def _on_scroll_set(self, first, last):
        """内容装得下就藏滚动条。"""
        try:
            need = not (float(first) <= 0.0 and float(last) >= 1.0)
            if need:
                if not self.sb.winfo_ismapped():
                    self.sb.pack(side="right", fill="y")
            else:
                if self.sb.winfo_ismapped():
                    self.sb.pack_forget()
                self.canvas.yview_moveto(0)
        except Exception:
            pass
        self.sb.set(first, last)

    def _on_frame_configure(self, e=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, e):
        try:
            self.canvas.itemconfig(self._win_id, width=e.width)
        except Exception:
            pass

    def _on_wheel(self, e):
        try:
            if not (self.win and self.win.winfo_exists()):
                return
            if self.canvas.bbox("all") is None:
                return
            self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except Exception:
            pass

    @staticmethod
    def _greet(h):
        if h < 5:
            return "这么晚还没睡，明天的事别忘了"
        if h < 9:
            return "早上好，先看一眼今天要做什么"
        if h < 12:
            return "上午过半，该推的推进一下"
        if h < 14:
            return "中午了，下午的活儿记好了吗"
        if h < 18:
            return "下午好，别让事情拖到晚上"
        if h < 22:
            return "晚上了，今天还剩这些没做"
        return "快收工了，交代一下明天"

    def refresh(self):
        if not self.win or not self.win.winfo_exists():
            return
        for c in self.listframe.winfo_children():
            c.destroy()

        self.greet.configure(text=self._greet(datetime.now().hour))

        notes = pending_notes(self.app.state)
        oc = self.app.overlay_cat()
        if oc:
            notes = [n for n in notes if (n.get("cat") or DEFAULT_CAT) == oc]
        label = f"「{oc}」" if oc else ""
        self.stat.configure(
            text=f"{label}还有 {len(notes)} 件事等着你" if notes else "")

        if not notes:
            empty = f"「{oc}」今天没有未完成的事" if oc else "今天没有未完成的事"
            tk.Label(self.listframe, text=empty,
                     bg=self.BG, fg=self.TEXT_DIM, font=F(27)).pack(pady=S(80))
            tk.Label(self.listframe, text="去干点正事吧，或者再写两条",
                     bg=self.BG, fg=self.TEXT_FAINT, font=F(12)).pack()
            return

        for n in notes:
            row = tk.Frame(self.listframe, bg=self.BG_CARD,
                           highlightbackground=self.LINE, highlightthickness=1)
            row.pack(fill="x", pady=S(5))

            inner = tk.Frame(row, bg=self.BG_CARD)
            inner.pack(fill="x", padx=S(22), pady=S(16))

            box = tk.Label(inner, text="", width=2, bg=self.BG_CARD, fg=self.TEXT_FAINT,
                           font=F(13), cursor="hand2",
                           relief="solid", borderwidth=1)
            box.pack(side="left", anchor="n", pady=(S(4), 0))

            txt = tk.Label(inner, text=n["text"], bg=self.BG_CARD, fg=self.TEXT,
                           font=F(21), justify="left", anchor="w",
                           wraplength=self._wrap_width())
            txt.pack(side="left", fill="x", expand=True, padx=(S(16), 0))

            def do_done(e, nid=n["id"], bx=box, tx=txt):
                bx.configure(text="✓", bg=self.OK_GREEN, fg=self.ON_ACCENT)
                tx.configure(fg=self.TEXT_FAINT)
                for x in self.app.state["notes"]:
                    if x["id"] == nid:
                        x["done"] = True
                        x["doneAt"] = int(time.time() * 1000)
                        break
                save_state(self.app.state)
                self.win.after(S(380), self.refresh)
                self.app.widget_refresh()

            box.bind("<Button-1>", do_done)
            txt.bind("<Button-1>", do_done)

        # 列表重建了，滚动区重算并回到顶部
        self.listframe.update_idletasks()
        self._on_frame_configure()
        try:
            self.canvas.yview_moveto(0)
        except Exception:
            pass

    def _wrap_width(self):
        """按 Canvas 实际宽度算换行宽度。"""
        try:
            cw = self.canvas.winfo_width()
            if cw > 40:
                return max(S(300), cw - S(90))
        except Exception:
            pass
        return S(1000)

    def _fill_cat_chips(self, bar, cur, on_pick, small=False):
        """在 bar 里画一排分类标签：全部 + 各类，单选。

        cur 是当前选中的类（"" = 全部）。点一个就只显示那一类。
        """
        for c in bar.winfo_children():
            c.destroy()

        cats = get_cats(self.app.state)
        names = [""] + [c["cat"] for c in cats]
        fs = F(9) if small else F(10)
        px = S(6) if small else S(9)

        for name in names:
            active = (name == cur)
            lb = tk.Label(bar, text=(name or "全部"), font=fs, cursor="hand2",
                          bg=(self.ACCENT if active else self.BG_CARD),
                          fg=(self.ON_ACCENT if active else self.TEXT_DIM),
                          padx=px, pady=S(1))
            lb.pack(side="left", padx=(0, S(3)))
            lb.bind("<Button-1>", lambda e, n=name: on_pick(n))

    def _draw_filterbar(self):
        """在浮窗顶部画一排分类小标签：全部 + 各个类。"""
        bar = getattr(self, "filterbar", None)
        if bar is None or not bar.winfo_exists():
            return
        for c in bar.winfo_children():
            c.destroy()

        self._fill_cat_chips(bar, self.app.widget_cat(),
                             lambda c: self.app.set_widget_cat(c),
                             small=True)

    def show(self):
        w = self.build()
        w.deiconify()
        w.attributes("-topmost", True)
        w.lift()
        w.update_idletasks()
        try:
            hwnd = int(w.wm_frame(), 16)
            set_topmost(hwnd, True)
        except Exception:
            pass
        w.focus_force()
        self.refresh()
        # 全屏铺满，滚轮直接绑窗口
        try:
            w.bind("<MouseWheel>", self._on_wheel)
        except Exception:
            pass

    def hide(self):
        if self.win and self.win.winfo_exists():
            self.win.withdraw()


# ================================================================ 桌面浮窗
class WidgetWindow(RegionPalette):
    """常驻浮窗，支持贴边自动隐藏"""

    EDGE_SNAP = 24      # 距边缘多少像素内算贴边
    STRIP = 7           # 收起来时露出的宽度

    def __init__(self, app):
        self.app = app
        self.win = None
        self._collapsed = False
        self._side = None          # 'left' / 'right' / 'top'
        self._full_w = 300
        self._full_h = 240
        self._leave_job = None

    def _clamp_pos(self, x, y):
        """把窗口坐标夹回屏幕内。

        上次保存的位置可能是别的分辨率下拖的，或者干脆拖到了边外，
        直接拿来用就会有一半窗口看不见 —— 表现就是"启动时错位"。
        留一点边距，别贴着边缘。
        """
        w = self._full_w
        h = self._full_h
        margin = 8
        max_x = max(0, self.app.sw - w - margin)
        max_y = max(0, self.app.sh - h - margin)
        x = max(margin, min(int(x), max_x))
        y = max(0, min(int(y), max_y))
        return x, y

    def build(self):
        self.set_palette('widget')
        if self.win and self.win.winfo_exists():
            return self.win

        w = tk.Toplevel(self.app.root)
        self.win = w
        w.title("备忘浮窗")
        w.overrideredirect(True)
        w.configure(bg=self.BG)
        w.attributes("-topmost", True)

        self._full_w = S(300)
        self._full_h = S(240)

        pos = self.app.state["settings"].get("widgetPos")
        if pos:
            # 位置是上次存下来的，但屏幕分辨率/缩放可能变过，
            # 也可能上次拖到了边缘外 —— 这里统一夹回可视区，
            # 否则启动时窗口会有一半在屏幕外，看着就是"错位"。
            x, y = self._clamp_pos(int(pos["x"]), int(pos["y"]))
            w.geometry(f"{self._full_w}x{self._full_h}+{x}+{y}")
        else:
            w.geometry(f"{self._full_w}x{self._full_h}"
                       f"+{self.app.sw - self._full_w - S(30)}"
                       f"+{self.app.sh - self._full_h - S(190)}")

        # 外框
        self.border = tk.Frame(w, bg=self.ACCENT)
        self.border.pack(fill="both", expand=True, padx=1, pady=1)

        card = tk.Frame(self.border, bg=self.BG_SOFT)
        card.pack(fill="both", expand=True)
        self.card = card

        # ---- 标题栏（可拖动）
        head = tk.Frame(card, bg=self.BG_SOFT, height=S(34))
        head.pack(fill="x")
        head.pack_propagate(False)
        self.head = head
        self._bind_drag(head)

        led = tk.Canvas(head, width=S(9), height=S(9), bg=self.BG_SOFT,
                        highlightthickness=0)
        led.create_oval(S(1), S(1), S(8), S(8), fill=self.ACCENT, outline="")
        led.pack(side="left", padx=(S(10), S(6)))
        self._bind_drag(led)

        title = tk.Label(head, text="备忘", bg=self.BG_SOFT, fg=self.TEXT_DIM, font=F(11))
        title.pack(side="left")
        self._bind_drag(title)

        # 切回主程序（醒目按钮，不是小图标）
        mainbtn = tk.Label(head, text="主窗", bg=self.BG_CARD, fg=self.ACCENT,
                           font=F(11), cursor="hand2", padx=S(7), pady=S(2))
        mainbtn.pack(side="right", padx=(0, S(4)))
        mainbtn.bind("<Button-1>", lambda e: self.app.show_main())
        mainbtn.bind("<Enter>", lambda e: mainbtn.configure(bg=self.ACCENT, fg=self.ON_ACCENT))
        mainbtn.bind("<Leave>", lambda e: mainbtn.configure(bg=self.BG_CARD, fg=self.ACCENT))

        # 一键关屏（跟 Alt+L 同一套逻辑，走外置守护进程）
        offbtn = tk.Label(head, text="关屏", bg=self.BG_CARD, fg=self.TEXT_DIM,
                          font=F(11), cursor="hand2", padx=S(7), pady=S(2))
        offbtn.pack(side="right", padx=(0, S(4)))
        offbtn.bind("<Button-1>", lambda e: self.app.click_display_off())
        offbtn.bind("<Enter>", lambda e: offbtn.configure(bg=self.ACCENT, fg=self.ON_ACCENT))
        offbtn.bind("<Leave>", lambda e: offbtn.configure(bg=self.BG_CARD, fg=self.TEXT_DIM))

        # 关闭靠系统标题栏的 ×，这里不再画一个

        # 条数徽标
        self.badge = tk.Label(head, text="0", bg=self.ACCENT, fg=self.ON_ACCENT,
                              font=F(10, "bold"), padx=S(6))
        self.badge.pack(side="right", padx=S(4))

        tk.Frame(card, bg=self.LINE, height=1).pack(fill="x")

        # ---- 分类筛选条：点一下只看那一类，再点恢复全部
        self.filterbar = tk.Frame(card, bg=self.BG_SOFT)
        self.filterbar.pack(fill="x", padx=S(6), pady=(S(4), 0))

        # ---- 列表（可滚动）
        # 以前直接往 card 里 pack，条目一多就被下面的输入框挤掉，
        # 长条目换行后更明显。改成 Canvas 装，条目再多也能滚。
        listwrap = tk.Frame(card, bg=self.BG_SOFT)
        listwrap.pack(fill="both", expand=True, padx=S(6), pady=S(6))

        self.canvas = tk.Canvas(listwrap, bg=self.BG_SOFT,
                                highlightthickness=0, bd=0)
        self.sb = tk.Scrollbar(listwrap, orient="vertical",
                               command=self.canvas.yview,
                               bg=self.BG_CARD, troughcolor=self.BG_SOFT,
                               borderwidth=0, highlightthickness=0,
                               width=S(7))
        self.canvas.configure(yscrollcommand=self._on_scroll_set)
        # 浮窗窄，滚动条一隐一现会让内容宽度来回跳，干脆常驻；
        # 有条在，也等于告诉用户"下面还有"。
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.listframe = tk.Frame(self.canvas, bg=self.BG_SOFT)
        self._win_id = self.canvas.create_window(
            (0, 0), window=self.listframe, anchor="nw")
        self.listframe.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # ---- 快速添加
        tk.Frame(card, bg=self.LINE, height=1).pack(fill="x")
        addrow = tk.Frame(card, bg=self.BG_SOFT, height=S(32))
        addrow.pack(fill="x")
        addrow.pack_propagate(False)

        self.quick = tk.Entry(addrow, bg=self.BG_SOFT, fg=self.TEXT,
                              insertbackground=self.ACCENT, relief="flat",
                              font=F(12), borderwidth=0, highlightthickness=0)
        self.quick.pack(side="left", fill="x", expand=True, padx=(S(10), S(4)))
        self.quick.bind("<Return>", lambda e: self.quick_add())
        self._placeholder_on()
        self.quick.bind("<FocusIn>", self._placeholder_off)
        self.quick.bind("<FocusOut>", self._placeholder_on)

        plus = tk.Label(addrow, text="+", bg=self.BG_CARD, fg=self.ACCENT,
                        font=F(15, "bold"), width=2, cursor="hand2")
        plus.pack(side="right", padx=(0, S(8)))
        plus.bind("<Button-1>", lambda e: self.quick_add())

        # ---- 贴边隐藏
        w.bind("<Enter>", self._on_mouse_enter)
        w.bind("<Leave>", self._on_mouse_leave)

        w.protocol("WM_DELETE_WINDOW", self.hide)
        return w

    # ---------- 贴边逻辑
    def _nearest_edge(self):
        """窗口离哪条边最近（只对左右、上边），超出阈值返回 None"""
        w = self.win
        x, y = w.winfo_x(), w.winfo_y()
        ww = w.winfo_width()
        sw, sh = self.app.sw, self.app.sh
        # 贴边阈值放宽到 40 逻辑像素，手指一甩就能吸上
        snap = S(40)
        cands = []
        if x <= snap:
            cands.append(("left", x))
        if x + ww >= sw - snap:
            cands.append(("right", sw - (x + ww)))
        if y <= snap:
            cands.append(("top", y))
        if not cands:
            return None
        return min(cands, key=lambda c: c[1])[0]

    def _try_collapse(self):
        if not self.app.state["settings"].get("widgetEdgeHide", True):
            return
        if self._collapsed or not self.win or not self.win.winfo_exists():
            return
        side = self._nearest_edge()
        if not side:
            return

        self._side = side
        x, y = self.win.winfo_x(), self.win.winfo_y()
        wh = self.win.winfo_height()
        sw = self.app.sw

        strip = S(6)
        try:
            if side == "left":
                self.win.geometry(f"{strip}x{wh}+0+{y}")
            elif side == "right":
                self.win.geometry(f"{strip}x{wh}+{sw - strip}+{y}")
            else:  # top
                ww = self.win.winfo_width()
                self.win.geometry(f"{ww}x{strip}+{x}+0")
            self._collapsed = True
            # 收起时给个细微提示色
            self.border.configure(bg=self.TEXT_DIM)
        except Exception:
            pass

    def _expand(self):
        if not self._collapsed or not self.win or not self.win.winfo_exists():
            return
        x, y = self.win.winfo_x(), self.win.winfo_y()
        sw = self.app.sw
        w_, h_ = self._full_w, self._full_h
        try:
            if self._side == "left":
                geo = f"{w_}x{h_}+0+{y}"
            elif self._side == "right":
                geo = f"{w_}x{h_}+{sw - w_}+{y}"
            elif self._side == "top":
                geo = f"{w_}x{h_}+{x}+0"
            else:
                geo = f"{w_}x{h_}+{x}+{y}"
            self.win.geometry(geo)
            self._collapsed = False
            self.border.configure(bg=self.ACCENT)
        except Exception:
            pass

    def _on_mouse_enter(self, e=None):
        if self._leave_job:
            try:
                self.win.after_cancel(self._leave_job)
            except Exception:
                pass
            self._leave_job = None
        if self._collapsed:
            self._expand()

    def _on_mouse_leave(self, e=None):
        if self._collapsed:
            return
        if self.app.state["settings"].get("widgetEdgeHide", True):
            if self._leave_job:
                try:
                    self.win.after_cancel(self._leave_job)
                except Exception:
                    pass
            self._leave_job = self.win.after(S(700), self._try_collapse)

    def set_edge_hide(self, on):
        if not on and self._collapsed:
            self._expand()

    # ---------- 拖拽
    def _bind_drag(self, widget):
        widget.bind("<Button-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)

    def _drag_start(self, e):
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        x = e.x_root - self._dx
        y = e.y_root - self._dy
        self.win.geometry(f"+{x}+{y}")

    def _drag_end(self, e):
        try:
            self.app.state["settings"]["widgetPos"] = {
                "x": self.win.winfo_x(), "y": self.win.winfo_y()}
            save_state(self.app.state)
        except Exception:
            pass
        self._try_collapse()

    # ---------- 快捷输入
    def _placeholder_on(self, e=None):
        if not self.quick.get():
            self.quick.insert(0, "快速记一条…")
            self.quick.configure(fg=self.TEXT_FAINT)

    def _placeholder_off(self, e=None):
        if self.quick.get() == "快速记一条…":
            self.quick.delete(0, "end")
            self.quick.configure(fg=self.TEXT)

    def quick_add(self):
        v = self.quick.get().strip()
        if not v or v == "快速记一条…":
            return
        self.app.state["notes"].append({
            "id": new_id(), "text": v, "done": False,
            "pinned": False, "createdAt": int(time.time() * 1000),
        })
        save_state(self.app.state)
        self.quick.delete(0, "end")
        self._placeholder_on()
        self.refresh()
        self.app.main_refresh()

    # ---------- 渲染
    def _fill_cat_chips(self, bar, cur, on_pick, small=False):
        """在 bar 里画一排分类标签：全部 + 各类，单选。"""
        for c in bar.winfo_children():
            c.destroy()

        cats = get_cats(self.app.state)
        names = [""] + [c["cat"] for c in cats]
        fs = F(9) if small else F(10)
        px = S(6) if small else S(9)

        for name in names:
            active = (name == cur)
            lb = tk.Label(bar, text=(name or "全部"), font=fs, cursor="hand2",
                          bg=(self.ACCENT if active else self.BG_CARD),
                          fg=(self.ON_ACCENT if active else self.TEXT_DIM),
                          padx=px, pady=S(1))
            lb.pack(side="left", padx=(0, S(3)))
            lb.bind("<Button-1>", lambda e, n=name: on_pick(n))

    def _draw_filterbar(self):
        """浮窗顶部的分类条：全部 + 各类，单选。"""
        bar = getattr(self, "filterbar", None)
        if bar is None or not bar.winfo_exists():
            return
        self._fill_cat_chips(bar, self.app.widget_cat(),
                             self.app.set_widget_cat, small=True)

    def refresh(self):
        if not self.win or not self.win.winfo_exists():
            return
        for c in self.listframe.winfo_children():
            c.destroy()

        notes = pending_notes(self.app.state)
        self.badge.configure(text=str(len(notes)),
                             bg=self.ACCENT if notes else self.LINE,
                             fg=self.ON_ACCENT if notes else self.TEXT_FAINT)

        # ---- 筛选条：全部 + 各个类
        self._draw_filterbar()

        # 只看选中的那一个类
        cur_cat = self.app.widget_cat()
        if cur_cat:
            notes = [n for n in notes
                     if (n.get("cat") or DEFAULT_CAT) == cur_cat]

        if not notes:
            tip = f"「{cur_cat}」暂无备忘" if cur_cat else "暂无备忘"
            tk.Label(self.listframe, text=tip, bg=self.BG_SOFT, fg=self.TEXT_FAINT,
                     font=F(12)).pack(expand=True, pady=S(30))
            return

        for n in notes:
            row = tk.Frame(self.listframe, bg=self.BG_SOFT)
            row.pack(fill="x", pady=S(1))
            box = tk.Label(row, text="✓", width=2, bg=self.BG_SOFT, fg=self.TEXT_FAINT,
                           font=F(10), cursor="hand2",
                           relief="solid", borderwidth=1)
            box.pack(side="left", anchor="n", pady=(S(2), 0))

            # wraplength 按实际可用宽度算，不能写死 200：
            # 写死的话长条目换行后行数比预估多，底部就被切掉。
            wrap = self._wrap_width()
            txt = tk.Label(row, text=n["text"], bg=self.BG_SOFT,
                           fg=self.ACCENT if n.get("pinned") else self.TEXT,
                           font=F(12), justify="left", anchor="w",
                           wraplength=wrap)
            txt.pack(side="left", fill="x", expand=True, padx=(S(7), 0))

            # 分类小标签，贴在右边
            tagtxt = note_label(n)
            if tagtxt and tagtxt != DEFAULT_CAT:
                tk.Label(row, text=tagtxt, bg=self.BG_CARD, fg=self.TEXT_DIM,
                         font=F(8), padx=S(4)).pack(side="right",
                                                    anchor="n", padx=S(3),
                                                    pady=(S(4), 0))

            def do_done(e, nid=n["id"]):
                for x in self.app.state["notes"]:
                    if x["id"] == nid:
                        x["done"] = True
                        x["doneAt"] = int(time.time() * 1000)
                        break
                save_state(self.app.state)
                self.refresh()
                self.app.main_refresh()

            box.bind("<Button-1>", do_done)
            txt.bind("<Button-1>", do_done)
            txt.bind("<Double-Button-1>", lambda e: self.app.show_main())

        # 条目变了，滚动区跟着重算，并回到顶部
        self.listframe.update_idletasks()
        self._on_frame_configure()
        try:
            self.canvas.yview_moveto(0)
        except Exception:
            pass

    def _wrap_width(self):
        """算出文字标签能用的换行宽度（像素）。

        拿 Canvas 实际宽度减去：勾选框、右边距、分类标签的余量。
        量不到就退回一个保守值。
        """
        try:
            cw = self.canvas.winfo_width()
            if cw > 20:
                return max(S(120), cw - S(46))
        except Exception:
            pass
        return S(200)

    def _on_scroll_set(self, first, last):
        """浮窗的滚动条常驻，这里只把位置同步给它。

        早先试过"装得下就自动隐藏"，但浮窗宽度只有 300 出头，
        滚动条一进一出会让标签换行宽度跟着变，内容高度又变，
        反而来回抖。常驻最省心。
        """
        sb = getattr(self, "sb", None)
        if sb is None:
            return
        try:
            sb.set(first, last)
        except Exception:
            pass

    def _on_frame_configure(self, e=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, e):
        # 让内部 Frame 始终跟 Canvas 一样宽，wraplength 才准
        try:
            self.canvas.itemconfig(self._win_id, width=e.width)
        except Exception:
            pass

    def _on_wheel(self, e):
        """只在鼠标停在浮窗上时才滚，别抢别处的滚轮。"""
        try:
            if not (self.win and self.win.winfo_exists()):
                return
            if self._collapsed:
                return
            px, py = self.win.winfo_pointerxy()
            rx, ry = self.win.winfo_rootx(), self.win.winfo_rooty()
            if not (rx <= px <= rx + self.win.winfo_width()
                    and ry <= py <= ry + self.win.winfo_height()):
                return
            if self.canvas.bbox("all") is None:
                return
            self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except Exception:
            pass

    def _fit_height(self):
        """按列表内容把浮窗高度撑到刚好。

        以前高度写死 S(240)，8 条备忘根本装不下，底下几条被切掉，
        看上去就像"错位"。现在列表装进 Canvas 了，winfo_reqheight
        不再反映内容（Canvas 请求高度是固定的），得按内容实测。

        撑到「内容刚好装下」和「屏幕最多能放」两者取小：
        条目不多就紧贴内容，多了就停在屏幕高度上让滚动条接手。
        """
        try:
            w = self.win
            if not w or not w.winfo_exists() or self._collapsed:
                return
            w.update_idletasks()

            # 除列表外的固定部分（标题栏 + 筛选条 + 输入框 + 各道分隔线）
            chrome = w.winfo_height() - self.canvas.winfo_height()
            if chrome < 0:
                chrome = 0

            # 列表内容实际需要的高度
            bbox = self.canvas.bbox("all")
            content = (bbox[3] - bbox[1]) if bbox else 0
            content += S(14)          # 上下内边距

            need = chrome + content
            # 别超过屏幕，留出任务栏
            need = min(need, self.app.sh - S(80))
            if need < S(120):
                need = S(120)
            if abs(need - w.winfo_height()) < 2:
                return
            x, y = w.winfo_x(), w.winfo_y()
            # 底边如果会超出屏幕，整块往上挪，别让它飘出去
            if y + need > self.app.sh:
                y = max(0, self.app.sh - need)
            w.geometry(f"{w.winfo_width()}x{need}+{x}+{y}")
        except Exception:
            pass

    def show(self):
        w = self.build()
        w.deiconify()
        w.attributes("-topmost", True)
        w.lift()
        w.update_idletasks()
        try:
            hwnd = int(w.wm_frame(), 16)
            set_topmost(hwnd, True)
        except Exception:
            pass
        self.refresh()
        # 内容排完再定高度，顺序不能反
        self._fit_height()
        # 滚轮绑在窗口上（不用 bind_all，免得抢别的窗口）
        try:
            w.bind("<MouseWheel>", self._on_wheel)
        except Exception:
            pass

    def hide(self):
        if self.win and self.win.winfo_exists():
            try:
                if not self._collapsed:
                    self.app.state["settings"]["widgetPos"] = {
                        "x": self.win.winfo_x(), "y": self.win.winfo_y()}
            except Exception:
                pass
            self.win.withdraw()
        self._collapsed = False
        self.app.state["settings"]["widget"] = False
        save_state(self.app.state)
        self.app.main_sync_settings()


# ================================================================ 应用
class App:
    def __init__(self):
        global SCALE
        self.state = load_state()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)

        # 创建 root 之后才能拿到真实 DPI
        try:
            SCALE = self.root.winfo_fpixels("1i") / 96.0
        except Exception:
            SCALE = 1.0
        if SCALE < 0.5 or SCALE > 5:
            SCALE = 1.0

        self.sw = self.root.winfo_screenwidth()
        self.sh = self.root.winfo_screenheight()

        self.main = MainWindow(self)
        self.overlay = OverlayWindow(self)
        self.widget = WidgetWindow(self)

        self._snooze_job = None
        self._quitting = False
        self._was_locked = False
        self._last_unlock_pop = 0
        self._away = False
        self._away_while_off = False   # 关屏期间是否真的没人动（防止关屏瞬间误判）
        self._display_confirmed_off = False   # 是否真的收到过"显示器关闭"
        self._next_remind = None
        self._display_off_at = None    # 显示器关闭的时刻
        self._pending_pop = False      # 等显示器亮了再弹
        self._manual_off_until = 0     # 手动关屏的保护窗口截止时刻

        # 显示器开关监听（唯一可靠的熄屏信号）
        self.display = DisplayWatcher(on_change=self._on_display_change)
        self._display_ok = self.display.start()
        print(f"[显示器监听] {'已启动' if self._display_ok else '不可用，退回空闲时间判断'}")

        # 全局 Alt+L：交给外置守护进程（hotkey.py）
        self.hotkey = HotkeyDaemon()
        # 开机自启也走这里：设置开着就把守护进程带起来，关着就保证它别在
        self.apply_hotkey_state()

        # 根窗口上也挂一份（本程序窗口有焦点时走这条，双保险）
        self.bind_hotkey(self.root)

    # ---- 转发
    def show_main(self):
        self.main.show()

    def main_refresh(self):
        if self.main.win and self.main.win.winfo_exists():
            self.main.refresh()

    def main_sync_settings(self):
        if self.main.win and self.main.win.winfo_exists():
            try:
                self.main.s_widget.set(bool(self.state["settings"].get("widget")))
            except Exception:
                pass

    def widget_refresh(self):
        if self.widget.win and self.widget.win.winfo_exists():
            self.widget.refresh()

    def set_widget(self, on):
        self.state["settings"]["widget"] = bool(on)
        save_state(self.state)
        if on:
            self.widget.show()
        else:
            if self.widget.win and self.widget.win.winfo_exists():
                self.widget.win.withdraw()

    # ---- Alt+L 关显示器
    def _only_power_off(self):
        """熄灭显示器（Alt+L / 面板「关屏」按钮走这里）。

        两个要注意的点：

        1) **不能**把 _display_off_at 置空。
           关屏之后系统照常发 GUID_CONSOLE_DISPLAY_STATE，
           tick() 要靠 _display_off_at 起算"熄屏多久了"，
           清掉的话计时永远起不来，全屏提醒就等于废了。

        2) 冷静期取 3 秒，不长不短：
           - 够长：盖住"Alt 松开被当成唤醒 → 屏亮 → 又灭"的那一下，
             这段时间不弹，闪屏就断了；
           - 够短：3 秒之后正常熄屏提醒就该接管。以前设 30 秒，
             用户按完 Alt+L 后半小时内系统再熄屏都不弹，
             等于把提醒功能关掉了。
        """
        now = time.time()
        if now - getattr(self, "_last_off_call", 0) < 1.2:
            return
        self._last_off_call = now
        self._manual_off_until = now + 3.0
        # 不碰 _display_off_at：熄屏信号由 _on_display_change 正常写入，
        # 只是 tick() 在冷静期内不拿它来弹窗。
        if self.hotkey:
            self.hotkey.off_once()

    def _do_display_off(self):
        """按 Alt+L 关一次屏（面板按钮也走这里）。"""
        if not self.state["settings"].get("hotkeyDisplayOff"):
            return
        self._only_power_off()

    def _is_manual_off(self):
        """这次熄屏是不是"用户按 Alt+L 造成的"。

        只在很窄的条件下返回 True，别的都用正常熄屏提醒逻辑：

          1) 30 秒冷静期内：是刚按完 Alt+L 的那一下，
             系统还可能把 Alt 松开当唤醒、屏黑了一下又亮，
             这期间不弹，避免闪。
          2) 冷静期外，一律 False —— 让熄屏提醒正常工作。

        【之前错在哪】
        上一版在冷静期之后还加了一条"屏黑着 + idle<120 就继续算手动"，
        看似贴心，实则把正常场景全吞了：
          - 你手动关显示器去看书，人还在电脑前（idle 小）
          - 或者系统设了 5 分钟自动熄屏，你人还坐着
        这两种都会命中"屏黑 + idle 小"，于是永远不弹全屏提醒。
        用户看到的就是"熄屏后没有全屏提醒了"。
        """
        now = time.time()
        return now < getattr(self, "_manual_off_until", 0)

    def on_hotkey_display_off(self, event=None):
        """Alt+L：直接关显示器。

        开关在面板里，关着就什么都不做（但要把事件吃掉，
        免得 Alt+L 漏到别的程序里触发别的功能）。

        要点：关屏前先立个"这次是我自己关的"标记，
        否则 tick() 会把它当成"你离开电脑了"，到点弹全屏，
        全屏一建出来屏幕又亮了 —— 就成了"关一会儿又亮"。
        """
        if not self.state["settings"].get("hotkeyDisplayOff"):
            return "break"
        self._do_display_off()
        return "break"

    def hotkey_enabled(self):
        return bool(self.state["settings"].get("hotkeyDisplayOff"))

    def click_display_off(self):
        """面板上的"关屏"按钮。

        不跟 Alt+L 的开关绑：就算把全局热键关了，按钮也该能用。
        """
        self._only_power_off()

    def apply_hotkey_state(self):
        """按设置拉起 / 停掉外置的 Alt+L 守护进程。

        开机自启时也会走一遍这个判断：设置开着就把守护进程带起来，
        关着就保证它不在后台占着 Alt+L。
        """
        if not self.hotkey:
            return
        try:
            if self.hotkey_enabled():
                self.hotkey.ensure()
            else:
                self.hotkey.stop()
        except Exception as e:
            print("[热键] 同步开关失败", e)

    def bind_hotkey(self, widget):
        """给窗口挂上 Alt+L（本程序有焦点时走这条）。

        注意两点，都踩过：
        1) bind_all 是**全局**绑定，不是"这个窗口专属"。每调一次就多
           一份处理函数，三个窗口各调一次 = 按一下触发三下，正好是
           "屏幕一闪一闪"的来源之一。所以只绑一次，绑过就跳过。
        2) 键名 <Alt-l> 和 <Alt-L> 都要绑，但底层是同一个事件，
           重复绑会重复触发；用一个标志位挡住。
        """
        if getattr(self, "_hotkey_bound", False):
            return
        try:
            widget.bind_all("<Alt-l>", self.on_hotkey_display_off)
            self._hotkey_bound = True
        except Exception as e:
            print(f"[快捷键] 绑定失败: {e}")

    def region_theme(self, region):
        return self.state["settings"].get("regionTheme", {}).get(region, "dark")

    # ---- 分类筛选（浮窗 / 全屏提醒 各一个）
    def widget_cat(self):
        return self.state["settings"].get("widgetCat") or ""

    def overlay_cat(self):
        return self.state["settings"].get("overlayCat") or ""

    def set_widget_cat(self, cat):
        self.state["settings"]["widgetCat"] = cat or ""
        save_state(self.state)
        self.widget_refresh()

    def set_overlay_cat(self, cat):
        self.state["settings"]["overlayCat"] = cat or ""
        save_state(self.state)

    def set_region_theme(self, region, key):
        """给某个窗口区域换黑/白，只重建那一个窗口。

        颜色是建控件时读的，没法事后重染，所以重建。
        其余两个窗口不动，位置和状态都保持。
        """
        try:
            rt = self.state["settings"].setdefault("regionTheme", {})
            if rt.get(region) == key:
                return
            rt[region] = key
            save_state(self.state)

            if region == "main":
                self._rebuild_main()
            elif region == "widget":
                self._rebuild_widget()
            elif region == "overlay":
                self._rebuild_overlay()

            # 主界面没重建时，那三行色块得立刻跟着变，
            # 否则要看新选中状态还得关掉重开主界面
            try:
                if self.main.win and self.main.win.winfo_exists():
                    self.main.sync_theme_chips()
            except Exception:
                pass

            print(f"[配色] {region} → {key}")
        except Exception as e:
            print("[set_region_theme]", e)
            import traceback
            traceback.print_exc()

    # ---- 三个窗口各自重建
    def _rebuild_main(self):
        shown = bool(self.main.win and self.main.win.winfo_exists()
                     and self.main.win.state() != "withdrawn")
        pos = None
        if self.main.win and self.main.win.winfo_exists():
            try:
                pos = (self.main.win.winfo_x(), self.main.win.winfo_y())
            except Exception:
                pos = None
        try:
            if self.main.win and self.main.win.winfo_exists():
                self.main.win.destroy()
        except Exception:
            pass
        self.main.win = None
        self.main = MainWindow(self)
        if shown:
            self.main.show()
            if pos:
                try:
                    self.main.win.geometry(f"+{pos[0]}+{pos[1]}")
                except Exception:
                    pass
            try:
                if not self.main._set_open:
                    self.main.toggle_settings()
            except Exception:
                pass

    def _rebuild_widget(self):
        shown = bool(self.widget.win and self.widget.win.winfo_exists()
                     and self.widget.win.state() != "withdrawn")
        pos = None
        if self.widget.win and self.widget.win.winfo_exists():
            try:
                pos = (self.widget.win.winfo_x(), self.widget.win.winfo_y())
            except Exception:
                pos = None
        try:
            if self.widget.win and self.widget.win.winfo_exists():
                self.widget.win.destroy()
        except Exception:
            pass
        self.widget.win = None
        self.widget = WidgetWindow(self)
        if shown:
            self.widget.show()
            if pos:
                try:
                    self.widget.win.geometry(f"+{pos[0]}+{pos[1]}")
                except Exception:
                    pass

    def _rebuild_overlay(self):
        shown = self._overlay_visible()
        try:
            if self.overlay.win and self.overlay.win.winfo_exists():
                self.overlay.win.destroy()
        except Exception:
            pass
        self.overlay.win = None
        self.overlay = OverlayWindow(self)
        if shown:
            self.overlay.show()

    def quit_app(self):
        """彻底退出：关掉所有窗口、注销电源通知、结束进程。

        光销毁窗口不够 —— 消息窗口的监听还在，根窗口也可能有
        待跑的 after 回调。所以先标记退出、取消定时器、
        注销显示器监听，最后 os._exit 保证进程真的没了。
        """
        print("[退出] 用户点了退出")
        self._quitting = True
        try:
            save_state(self.state)
        except Exception:
            pass

        # 停掉主循环里的定时回调
        try:
            self.root.after_cancel(self.tick)
        except Exception:
            pass
        for job in (getattr(self, "_snooze_job", None),
                    getattr(self, "_next_remind", None)):
            try:
                if job:
                    self.root.after_cancel(job)
            except Exception:
                pass

        # 注销显示器电源通知，别让系统再往这个进程发消息
        try:
            self.display.stop()
        except Exception:
            pass

        # 顺手把外置的 Alt+L 守护进程也收掉，别留个孤儿占着热键
        try:
            if self.hotkey:
                self.hotkey.stop()
        except Exception:
            pass

        # 关窗口（浮窗、全屏、主窗）
        for holder in (self.widget, self.overlay, self.main):
            try:
                if holder.win and holder.win.winfo_exists():
                    holder.win.destroy()
            except Exception:
                pass
            holder.win = None

        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

        # 根窗口销毁后有零星回调可能还在跑，直接退进程最干净
        sys.stdout.flush() if sys.stdout else None
        os._exit(0)

    def set_theme(self, key):
        """三个区域一起换（留作快捷入口）"""
        for r in ("main", "widget", "overlay"):
            self.set_region_theme(r, key)

    # ---- 全屏提醒
    def show_overlay(self):
        self.overlay.show()

    def hide_overlay(self):
        self.overlay.hide()

    def snooze(self, minutes):
        self.hide_overlay()
        if self._snooze_job:
            try:
                self.root.after_cancel(self._snooze_job)
            except Exception:
                pass
        self._snooze_job = self.root.after(minutes * 60 * 1000, self.show_overlay)

    def _overlay_visible(self):
        ov = self.overlay.win
        return bool(ov and ov.winfo_exists() and ov.winfo_viewable())

    def _pop(self, why):
        """弹全屏，已显示就不重复弹"""
        if self._overlay_visible():
            return
        print(f"[提醒] {why}")
        self.show_overlay()

    def _on_display_change(self, on):
        """显示器开关回调。

        v2：检测到熄屏就立刻弹全屏提醒（不等回来）。

        这里仍然只做记录 + 标记要不要弹，真正的弹窗交给主循环，
        因为回调可能在消息泵里被调用，直接操作窗口不安全。
        """
        try:
            now = time.time()
            if on:
                off_at = self._display_off_at
                if off_at is not None:
                    self._last_off_duration = now - off_at
                self._display_off_at = None
                self._away_while_off = False
            else:
                # 熄屏统一从这一刻开始计时，手动关屏也一样。
                # "手动关屏不弹"这件事交给 tick() 里那个几秒的
                # 保护窗口去处理，不能在这里把计时抹掉。
                if self._display_off_at is None:
                    self._display_off_at = now
                    self._away_while_off = False
                    # 只记时间，弹不弹由主循环按阈值决定
                self._display_confirmed_off = True
        except Exception as e:
            print("[display]", e)

    # ---- 主循环
    def tick(self):
        if getattr(self, "_quitting", False):
            return          # 正在退出，别再排下一轮
        try:
            # 先把电源广播消息派发掉，_on_display_change 才会被调用
            if self._display_ok:
                self.display.pump()

            locked = is_session_locked()
            idle = get_idle_seconds()
            want = self.state["settings"].get("overlayOnUnlock")

            now = time.time()
            monitor_off = self._display_off_at is not None
            off_dur = (now - self._display_off_at) if monitor_off else 0.0

            # ---- v3：熄屏后计时，到阈值才弹 ----
            #
            # 信号来源：显示器关闭时 Windows 发 GUID_CONSOLE_DISPLAY_STATE 广播。
            # 关屏那一刻开始计时，熬够阈值就铺全屏。
            # 阈值 0 = 熄屏即弹；调到 60 = 熄屏 1 分钟后才弹。
            # 关屏期间屏是黑的，弹在那儿也不打扰人，屏一亮就在眼前。
            #
            # 例外：用户自己按 Alt+L 关的屏不弹 —— 那时人就在电脑前，
            # 弹全屏反而把屏幕唤醒，观感就是"关一会儿又亮了"。
            #
            # 【闪屏的根】
            # 早先"手动关屏"只护 2.5 秒，过后如果屏还黑着、阈值又是 0，
            # tick 就铺全屏 -> 全屏创建把屏幕点亮 -> 收到"屏已亮"清掉计时
            # -> 再熄屏又弹 -> 又亮…… 于是屏幕黑一下亮一下，停不下来。
            #
            # 解决办法是给手动关屏一个 3 秒冷静期：这一下（以及它引起的
            # "亮-灭"抖动）不弹。3 秒之后就交还给正常熄屏提醒，
            # 否则会反过来把提醒功能吞掉。
            should_pop = False
            threshold = monitor_away_threshold(self.state)

            if self._is_manual_off():
                # 刚按完 Alt+L 的 3 秒：不弹，也不累积"人去楼空"状态
                self._away_while_off = False
                self._pending_pop = False
            elif monitor_off and not self._away_while_off and off_dur >= threshold:
                self._away_while_off = True
                self._pending_pop = True

            if self._pending_pop:
                self._pending_pop = False
                if want:
                    should_pop = True

            # ---- 兜底：显示器通知不可用时，用空闲时间判断 ----
            if not self._display_ok and not locked:
                away_secs = max(20, int(self.state["settings"].get(
                    "idleAwaySeconds") or 150))
                if idle >= away_secs:
                    self._away = True
                    if want:
                        should_pop = True
                elif self._away and idle < 2.0:
                    self._away = False

            # ---- 锁屏也顺手弹一次（原来就有）----
            if (not should_pop) and self._was_locked and not locked:
                if want:
                    should_pop = True
            self._was_locked = locked

            # ---- 关屏太久（>10 分钟）清状态，避免醒来时重复弹 ----
            if monitor_off and off_dur >= 600:
                self._display_off_at = None
                self._away_while_off = False

            if should_pop:
                self.root.after(S(300), lambda: self._pop("熄屏"))

            # ---- 定时提醒
            mins = int(self.state["settings"].get("overlayEveryMinutes") or 0)
            if mins > 0 and not locked:
                if self._next_remind is None:
                    self._next_remind = time.time() + mins * 60
                elif time.time() >= self._next_remind:
                    self._next_remind = time.time() + mins * 60
                    if pending_notes(self.state):
                        self._pop("定时")
        except Exception as e:
            print("[tick]", e)
        finally:
            self.root.after(600, self.tick)

    def run(self, autostart=False):
        # 开机自启的路径要最短：
        #   先弹全屏（用户最想看的），浮窗排它后面建，别互相拖。
        # 手动打开就照旧，只开浮窗。
        boot = autostart or looks_like_boot()
        want_overlay = boot and self.state["settings"].get("overlayOnBoot")
        want_widget = bool(self.state["settings"].get("widget"))

        if want_overlay:
            self.root.after(30, self.show_overlay)
            if want_widget:
                # 全屏铺满之后再挂浮窗，两件事不抢同一次事件循环
                self.root.after(S(450), self.widget.show)
        elif want_widget:
            self.root.after(30 if boot else S(300), self.widget.show)

        # 计时器不抢头几秒
        self.root.after(S(1200), self.tick)
        self.root.mainloop()


def looks_like_boot(within_seconds=240):
    """这次启动像不像"开机自启"。

    注册表那种启动方式拿不到命令行参数，所以按系统开机时长判断：
    开机 4 分钟内起来的，认定是自启。手动双击一般都在开机很久之后。
    """
    try:
        k32 = ctypes.windll.kernel32
        up = k32.GetTickCount64() / 1000.0
        return up <= within_seconds
    except Exception:
        return False


def main():
    autostart = "--autostart" in sys.argv

    # 只能有一个在跑：开机自启尤其要挡住重复启动
    if not acquire_single_instance():
        # 有锁，但得确认背后真有活窗口。见 wake_existing 的注释：
        # 进程被强杀后互斥量和窗口句柄都可能残留，这时如果直接退出，
        # 用户双击就是"没反应"。所以——
        #   有活的窗口 -> 唤起来，本进程退出（正常单实例行为）
        #   全是僵尸   -> 本进程接管，正常启动
        if wake_existing():
            print("[即时备忘] 已经有一个在运行了")
            sys.exit(0)
        print("[即时备忘] 检测到残留实例，本进程接管启动")
        # 僵尸的互斥量句柄已经没人持有，系统会标记为 abandoned，
        # 但名字还被占着。这里不去硬删它（可能误伤），
        # 直接往下走 —— 后面的窗口创建不依赖这把锁。

    try:
        app = App()
        app.run(autostart=autostart)
    except Exception:
        import traceback
        logdir = os.path.dirname(os.path.abspath(sys.argv[0]))
        try:
            with open(os.path.join(logdir, "崩溃日志.txt"), "a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"SCALE={SCALE}\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
