# -*- coding: utf-8 -*-
"""v3：熄屏后计时到阈值才弹 —— 判断逻辑测试（不启动 GUI）"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class Fake:
    def __init__(self, unlock_on=True, display_ok=True, delay=0):
        self.settings = {"overlayOnUnlock": unlock_on,
                         "idleAwaySeconds": 150}
        self.display_ok = display_ok
        self.delay = delay
        self._display_off_at = None
        self._pending_pop = False
        self._away_while_off = False
        self._was_locked = False
        self._away = False
        self.pops = []
        self.now = 1000.0

    def monitor(self, on):
        """与 _on_display_change 一致：只记时间，不决定弹"""
        self.now += 0.01
        if on:
            self._display_off_at = None
            self._away_while_off = False
        else:
            if self._display_off_at is None:
                self._display_off_at = self.now
                self._away_while_off = False

    def tick(self, locked=False, idle=99.0):
        """与 App.tick 一致"""
        want = self.settings["overlayOnUnlock"]
        now = self.now
        monitor_off = self._display_off_at is not None
        off_dur = (now - self._display_off_at) if monitor_off else 0.0

        should_pop = False
        threshold = self.delay

        if monitor_off and not self._away_while_off and off_dur >= threshold:
            self._away_while_off = True
            self._pending_pop = True

        if self._pending_pop:
            self._pending_pop = False
            if want:
                should_pop = True

        if not self.display_ok and not locked:
            away = max(20, int(self.settings.get("idleAwaySeconds") or 150))
            if idle >= away:
                self._away = True
                if want:
                    should_pop = True
            elif self._away and idle < 2.0:
                self._away = False

        if (not should_pop) and self._was_locked and not locked:
            if want:
                should_pop = True
        self._was_locked = locked

        if should_pop:
            self.pops.append("弹")

    def ahead(self, sec):
        self.now += sec


def run(name, script, expect, **kw):
    f = Fake(**kw)
    for s in script:
        k = s[0]
        if k == "off":
            f.monitor(False)
        elif k == "on":
            f.monitor(True)
        elif k == "wait":
            f.ahead(s[1])
        elif k == "tick":
            f.tick(locked=s[1] if len(s) > 1 else False,
                   idle=s[2] if len(s) > 2 else 99.0)
    ok = f.pops == expect
    print(f"{'PASS' if ok else 'FAIL'}  {name:40s} pops={f.pops} 期望={expect}")
    return ok


ok = True

# ---- 延迟 0：熄屏即弹
ok &= run("延迟0：熄屏就弹", [("off",), ("tick",)], ["弹"], delay=0)

# ---- 延迟 30：熄屏 10 秒不弹，31 秒才弹
ok &= run("延迟30：熄屏10秒不弹",
          [("off",), ("wait", 10), ("tick",)], [], delay=30)
ok &= run("延迟30：熄屏31秒弹",
          [("off",), ("wait", 31), ("tick",)], ["弹"], delay=30)

# ---- 延迟 60：反复 tick 也不会提前弹
ok &= run("延迟60：中途多轮tick都不弹",
          [("off",), ("wait", 20), ("tick",), ("wait", 20), ("tick",),
           ("wait", 15), ("tick",)], [], delay=60)
ok &= run("延迟60：熬到61秒弹",
          [("off",), ("wait", 61), ("tick",)], ["弹"], delay=60)

# ---- 延迟很长（不限上限）：3600 秒
ok &= run("延迟3600：3599秒不弹",
          [("off",), ("wait", 3599), ("tick",)], [], delay=3600)
ok &= run("延迟3600：3601秒弹",
          [("off",), ("wait", 3601), ("tick",)], ["弹"], delay=3600)

# ---- 熄屏中途亮回来 → 重新计时，不该弹
ok &= run("熄屏5秒就亮，不弹",
          [("off",), ("wait", 5), ("on",), ("tick",)], [], delay=30)

# ---- 亮了再熄 → 重新计满才弹
ok &= run("亮后再熄，重新计时",
          [("off",), ("wait", 5), ("on",), ("tick",),
           ("off",), ("wait", 5), ("tick",), ("wait", 30), ("tick",)],
          ["弹"], delay=30)

# ---- 弹过一次，不会重复弹
ok &= run("只弹一次",
          [("off",), ("wait", 31), ("tick",), ("tick",), ("tick",)],
          ["弹"], delay=30)

# ---- 开关关闭不弹
ok &= run("开关关闭不弹",
          [("off",), ("wait", 100), ("tick",)], [],
          delay=10, unlock_on=False)

# ---- 通知不可用，靠空闲兜底
ok &= run("通知不可用，空闲兜底",
          [("wait", 200), ("tick", False, 200.0)], ["弹"], display_ok=False)

print()
print("ALL-PASS" if ok else "SOME-FAILED")
