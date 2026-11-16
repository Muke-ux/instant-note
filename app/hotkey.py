"""即时备忘 · 全局 Alt+L 外置热键守护进程。

单独一个进程，专门负责一件事：
    用 RegisterHotKey 注册全局 Alt+L，按下就关显示器。

为什么要独立成一个程序？
    主程序是 tkinter，热键消息必须由窗口过程接收；ctypes 挂出来的
    窗口过程在 Python 里跑会崩（回调进入时不持有线程状态）。
    独立进程可以拥有自己干净的消息循环，跟 tkinter 彻底分开，
    主程序该干嘛干嘛，互不影响。

用法（一般不用手动开，主程序会自己拉起来）：
    python hotkey.py            常驻守热键
    python hotkey.py --off      关一次显示器然后退出
    python hotkey.py --quit     让正在跑的守护进程退出
"""

import sys
import os
import time
import subprocess
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_DESTROY = 0x0002
HOTKEY_ID = 0xB001
MOD_ALT = 0x0001
VK_L = 0x4C

# 关显示器的系统命令
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_OFF = 2
HWND_BROADCAST = 0xFFFF
SMTO_ABORTIFHUNG = 0x0002

MUTEX_NAME = "Local\\InstantNote_HotkeyDaemon_v1"
WINDOW_TITLE = "InstantNoteHotkeyDaemon"
WINDOW_CLASS = "InstantNoteHotkeyWnd"
# 退出旗标：主程序写这个文件，守护进程看到就自己退
QUIT_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".hotkey-quit")
# 防抖时间戳：跨进程共享，拦"两路同时触发"
_STAMP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      ".hotkey-last")
_daemon_started = False      # 本进程是不是"守热键的那一个"（不是关屏模式）
_last_off = 0.0              # 上次关屏时刻（进程内，备用）


# ---------------------------------------------------------------- 关显示器
def turn_off_monitor():
    """关掉显示器（不动系统电源状态，等于按了显示器开关）。"""
    try:
        res = wt.DWORD(0)
        user32.SendMessageTimeoutW(
            wt.HWND(HWND_BROADCAST), WM_SYSCOMMAND, SC_MONITORPOWER,
            MONITOR_OFF, SMTO_ABORTIFHUNG, 2000, ctypes.byref(res))
        return True
    except Exception:
        return False


def do_display_off():
    """关一次屏。

    【为什么以前会一闪一闪】
    早先在关屏之后又"补了两刀"（隔 0.35s、0.9s 各再关一次），
    想法是：Windows 把松开的 Alt 当唤醒源，屏会自己亮回去，
    多关几次能盖住。实际效果完全相反 —— 每次 SC_MONITORPOWER
    本身就是一次"唤醒 + 熄灭"，补刀补出的是黑-亮-黑的循环，
    人看着就是屏幕乱闪、没法操作。

    正确做法是只发一次关屏指令，然后就别管了。
    Alt 松开那一下如果真把屏唤醒，那是 Windows 的正常行为
    （按键即唤醒），不该跟它抢。

    防抖用**时间戳文件**而不是进程内变量：
    --off 模式每次都是一个新进程，进程内变量拦不住跨进程的重复调用
    （全局热键一次 + 主窗口 bind_all 一次 = 闪两次）。
    """
    try:
        now = time.time()
        last = 0.0
        if os.path.exists(_STAMP):
            try:
                last = float(open(_STAMP).read().strip() or 0)
            except Exception:
                last = 0.0
        if now - last < 1.2:
            return
        with open(_STAMP, "w") as f:
            f.write("%.3f" % now)
    except Exception:
        pass
    turn_off_monitor()


# ---------------------------------------------------------------- 单实例
def acquire_mutex():
    """只允许一个守护进程。返回 (句柄, 是否新建)。"""
    try:
        h = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        already = (kernel32.GetLastError() == 183)   # ERROR_ALREADY_EXISTS
        return h, (not already)
    except Exception:
        return None, True


def daemon_running():
    """有没有守护进程在跑：试开一下互斥体就知道，不用找窗口。

    HWND_MESSAGE 窗口既不进 EnumWindows 也不进 FindWindow，
    所以按窗口找一定失败；互斥体才是可靠的"我还活着"标记。
    """
    try:
        h = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
        if not h:
            return False
        kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def stop_daemon():
    """让守护进程退出。

    不走"找窗口/找进程"那套：守护进程是 HWND_MESSAGE 窗口，
    EnumWindows 和 FindWindow 都看不见它；wmic 在新系统上也没了。
    改用文件信号——守护进程自己每 300ms 看一眼这个旗标文件，
    有就干净退出（顺便注销热键）。简单、跨版本、不会误杀别人。
    """
    try:
        with open(QUIT_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        # 最多等 2.5 秒让它自己走
        for _ in range(25):
            time.sleep(0.1)
            if not daemon_running():
                try:
                    os.remove(QUIT_FLAG)
                except Exception:
                    pass
                return 1
    except Exception:
        pass
    try:
        os.remove(QUIT_FLAG)
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- 守护进程
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", wt.DWORD), ("pt", wt.POINT)]


def run_daemon(on_hotkey=True):
    """建立消息窗口、注册热键、跑消息循环。阻塞，直到收到 WM_QUIT。"""
    global _daemon_started
    _daemon_started = True

    hinst = kernel32.GetModuleHandleW(None)
    cls_name = WINDOW_CLASS

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY and int(wparam) == HOTKEY_ID:
                # 在消息循环体里处理（见下面的分支），这里只交回系统
                pass
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # 关键：把回调对象存住，别让 GC 回收，否则回调地址会失效
    proc = WNDPROC(wndproc)

    wc = WNDCLASSW()
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = cls_name
    try:
        user32.RegisterClassW(ctypes.byref(wc))
    except Exception:
        pass

    hwnd = user32.CreateWindowExW(
        0, cls_name, WINDOW_TITLE, 0, 0, 0, 0, 0,
        ctypes.c_void_p(-3), None, hinst, None)      # HWND_MESSAGE
    if not hwnd:
        return 3

    registered = False
    if on_hotkey:
        user32.UnregisterHotKey(ctypes.c_void_p(hwnd), HOTKEY_ID)
        registered = bool(user32.RegisterHotKey(
            ctypes.c_void_p(hwnd), HOTKEY_ID, MOD_ALT, VK_L))
        if not registered:
            # 注册不上也别退出：可能是上一次没退干净，等一会儿再试一次
            time.sleep(0.6)
            user32.UnregisterHotKey(ctypes.c_void_p(hwnd), HOTKEY_ID)
            registered = bool(user32.RegisterHotKey(
                ctypes.c_void_p(hwnd), HOTKEY_ID, MOD_ALT, VK_L))
        if not registered:
            user32.DestroyWindow(ctypes.c_void_p(hwnd))
            return 2

    # 消息循环：热键在这里取出来就地处理，不进 wndproc。
    # 用 PeekMessage + 短 sleep 而不是阻塞的 GetMessage，
    # 这样每 300ms 能顺便看一眼退出旗标。
    try:
        os.remove(QUIT_FLAG)
    except Exception:
        pass
    m = MSG()
    while True:
        if os.path.exists(QUIT_FLAG):        # 主程序让我退
            break
        while user32.PeekMessageW(ctypes.byref(m), None, 0, 0, 1):   # PM_REMOVE
            if m.message == WM_QUIT:
                return 0
            if m.message == WM_HOTKEY and int(m.wParam) == HOTKEY_ID:
                do_display_off()
                continue
            user32.TranslateMessage(ctypes.byref(m))
            user32.DispatchMessageW(ctypes.byref(m))
        time.sleep(0.03)

    try:
        user32.UnregisterHotKey(ctypes.c_void_p(hwnd), HOTKEY_ID)
        user32.DestroyWindow(ctypes.c_void_p(hwnd))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- 入口
def main():
    args = sys.argv[1:]

    if "--quit" in args:
        if not daemon_running():
            print("没有在跑的热键守护进程")
            return 0
        n = stop_daemon()
        print("已结束热键守护进程 %d 个" % n)
        return 0

    if "--off" in args:
        do_display_off()
        return 0

    # 默认：守热键
    h, is_new = acquire_mutex()
    if not is_new:
        print("热键守护进程已经在跑了")
        return 0

    return run_daemon(on_hotkey=True)


if __name__ == "__main__":
    sys.exit(main())
