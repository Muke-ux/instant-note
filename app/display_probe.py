# -*- coding: utf-8 -*-
"""
显示器开关探测。

三种手段同时记录：
  1. GetLastInputInfo 空闲时间
  2. GetSystemPowerStatus.SystemStatusFlag
  3. RegisterPowerSettingNotification + GUID_CONSOLE_DISPLAY_STATE（权威信号）

日志实时落盘，进程被杀也有证据。
用法：python display_probe.py 30
"""

import ctypes
import ctypes.wintypes as wt
import time
import sys
import os

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "display_probe.log")

# ---------------- Win32 定义
WM_POWERBROADCAST = 0x0218
PBT_POWERSETTINGCHANGE = 0x8013
DEVICE_NOTIFY_WINDOW_HANDLE = 0

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)

# DefWindowProcW 用无符号指针宽度接参数，避免负值被当成超长整数
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD),
                ("Data2", wt.WORD),
                ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


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


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", GUID),
                ("DataLength", wt.DWORD),
                ("Data", ctypes.c_ubyte * 16)]


# GUID_CONSOLE_DISPLAY_STATE = {6FE69556-704A-47A0-8F24-C28D936FDA47}
GUID_CONSOLE_DISPLAY_STATE = GUID(
    0x6FE69556, 0x704A, 0x47A0,
    (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))


# ---------------- 日志
_fh = open(LOG, "w", encoding="utf-8", buffering=1)
_t0 = time.time()

STATE = {"display": "?", "events": []}


def log(msg):
    _fh.write(f"{time.time() - _t0:7.2f}s  {msg}\n")
    _fh.flush()


# ---------------- 窗口过程
@WNDPROC
def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE:
        try:
            pbs = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
            raw = bytes(pbs.Data[:pbs.DataLength])
            if raw:
                val = int.from_bytes(raw[:4], "little")
                name = {0: "关闭", 1: "点亮", 2: "变暗"}.get(val, str(val))
                STATE["display"] = name
                STATE["events"].append((time.time() - _t0, name))
                log(f"*** 电源通知：显示器 → {name}")
        except Exception as e:
            log(f"power broadcast err {e}")
    if msg == 0x0002:  # WM_DESTROY
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def create_window():
    hInst = kernel32.GetModuleHandleW(None)
    cls = "DisplayProbeWnd"
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hInst
    wc.lpszClassName = cls
    user32.RegisterClassW(ctypes.byref(wc))
    # HWND_MESSAGE = -3
    hwnd = user32.CreateWindowExW(0, cls, cls, 0, 0, 0, 0, 0,
                                  ctypes.c_void_p(-3), None, hInst, None)
    return hwnd


def register_power(hwnd):
    try:
        h = user32.RegisterPowerSettingNotification(
            hwnd, ctypes.byref(GUID_CONSOLE_DISPLAY_STATE),
            DEVICE_NOTIFY_WINDOW_HANDLE)
        log(f"RegisterPowerSettingNotification → {h}")
        return bool(h)
    except Exception as e:
        log(f"RegisterPowerSettingNotification 不可用: {e}")
        return False


# ---------------- 采样
class SPS(ctypes.Structure):
    _fields_ = [("ACLineStatus", wt.BYTE),
                ("BatteryFlag", wt.BYTE),
                ("BatteryLifePercent", wt.BYTE),
                ("SystemStatusFlag", wt.BYTE),
                ("BatteryLifeTime", wt.DWORD),
                ("BatteryFullLifeTime", wt.DWORD)]


def idle():
    class LI(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]
    li = LI()
    li.cbSize = ctypes.sizeof(li)
    if user32.GetLastInputInfo(ctypes.byref(li)):
        return ((kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0
    return -1


def power():
    sps = SPS()
    if kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        return sps.ACLineStatus, sps.SystemStatusFlag
    return None, None


def pump(msgs=50):
    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                    ("wParam", WPARAM), ("lParam", LPARAM),
                    ("time", wt.DWORD), ("pt", wt.POINT)]
    m = MSG()
    n = 0
    while n < msgs and user32.PeekMessageW(ctypes.byref(m), None, 0, 0, 1):
        user32.TranslateMessage(ctypes.byref(m))
        user32.DispatchMessageW(ctypes.byref(m))
        n += 1
    return n


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    log("=== 启动 ===")

    hwnd = create_window()
    log(f"消息窗口 hwnd={hwnd}")
    has_power = register_power(hwnd) if hwnd else False

    last = None
    while time.time() - _t0 < dur:
        pump()
        ac, flag = power()
        line = (f"idle={idle():7.1f}s  AC={ac}  SysStatusFlag=0x{flag:02x}"
                f"  bit0={'ON' if (flag or 0) & 1 else 'off'}  display={STATE['display']}")
        if line != last:
            log(line)
            last = line
        time.sleep(0.5)

    log("=== 结束 ===")
    log(f"电源通知可用={has_power}  收到的显示器事件={STATE['events']}")
    _fh.close()


if __name__ == "__main__":
    main()
