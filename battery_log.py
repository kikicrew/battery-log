"""
배터리 사용 기록기 — 오늘 어떤 앱이 배터리를 썼는지 추정해서 보여줍니다.

필요: Windows 10/11, Python 3.9+ (기본 라이브러리만 사용, battery.ico는 같은 폴더에)
실행: py battery_log.py            창을 닫아도 트레이에서 계속 기록합니다
      py battery_log.py --hidden   트레이에 숨은 채로 시작 (윈도우 시작 시 자동 실행용)
확인: py battery_log.py --test

- 창이 떠 있으면 2초마다 잔량·전력(W)·남은 시간을 갱신합니다. 숨어 있으면 30초마다만 깨어납니다.
- 배터리로 쓰는 동안 30초마다 앱별 CPU 사용 시간을 읽고, 실제로 줄어든 잔량을 나눠 적습니다.
  평소 가장 느리게 줄던 속도만큼은 '화면·대기'로, 그보다 더 빨리 줄어든 몫은 그 사이 CPU를 쓴 앱들에게,
  덮어둔 동안 줄어든 몫은 '절전 중'으로 들어갑니다.
- 충전 중에는 프로세스를 훑지 않습니다.
- 배터리 수명(설계 용량 대비 완충 용량)과 충전 횟수를 보여주고, 윈도우 배터리 리포트를 열 수 있습니다.
- 기록은 같은 폴더의 battery_log.json에 날짜별로 최근 60일치가 쌓입니다.
"""
import ctypes
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import winreg
import xml.etree.ElementTree as ET
from ctypes import wintypes as wt

APP = "BatteryLog"
FROZEN = getattr(sys, "frozen", False)
# exe로 묶으면 __file__이 임시 폴더를 가리켜서 기록이 사라지므로 exe 옆에 저장
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
LOG_PATH = os.path.join(HERE, "battery_log.json")
ICON_PATH = os.path.join(getattr(sys, "_MEIPASS", HERE), "battery.ico")
REPORT_PATH = os.path.join(tempfile.gettempdir(), "BatteryLog-report.html")

SAMPLE_SEC = 30          # 기록 간격
LIVE_MS = 2000           # 창이 보일 때 화면 갱신 간격
SAVE_SEC = 120           # 디스크에 쓰는 최소 간격
KEEP_DAYS = 60
TOP_N = 6
IDLE, SLEEP = "화면·대기", "절전 중"
FONT, NUM_FONT = "Malgun Gothic", "Segoe UI"

# Material 3 색 (파란 씨앗색)
LIGHT = dict(bg="#F9F9FF", card="#EDEDF4", high="#E1E2E9", on="#191C20", var="#44474E",
             p="#415F91", on_p="#FFFFFF", pc="#D6E3FF", on_pc="#001B3E", track="#FFFFFF",
             outline="#74777F")
DARK = dict(bg="#111318", card="#1D2024", high="#33353A", on="#E2E2E9", var="#C4C6D0",
            p="#AAC7FF", on_p="#0A305F", pc="#284777", on_pc="#D6E3FF", track="#0A305F",
            outline="#8E9099")


# ---------- 윈도우에서 읽기 ----------
class SYSTEM_BATTERY_STATE(ctypes.Structure):
    _fields_ = [("AcOnLine", ctypes.c_ubyte), ("BatteryPresent", ctypes.c_ubyte),
                ("Charging", ctypes.c_ubyte), ("Discharging", ctypes.c_ubyte),
                ("Spare", ctypes.c_ubyte * 4), ("MaxCapacity", wt.DWORD),
                ("RemainingCapacity", wt.DWORD), ("Rate", wt.LONG),
                ("EstimatedTime", wt.DWORD), ("Alert1", wt.DWORD), ("Alert2", wt.DWORD)]


k32 = ctypes.WinDLL("kernel32")
u32 = ctypes.WinDLL("user32")
shell32 = ctypes.WinDLL("shell32")
ntdll = ctypes.WinDLL("ntdll")
powrprof = ctypes.WinDLL("powrprof")

k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
k32.GetCurrentProcess.restype = wt.HANDLE
k32.SetProcessWorkingSetSize.argtypes = [wt.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
ntdll.NtQuerySystemInformation.argtypes = [wt.ULONG, ctypes.c_void_p, wt.ULONG,
                                           ctypes.POINTER(wt.ULONG)]


def battery():
    """(잔량 %, 충전기 연결, 전력 W: +충전 / -방전, 다 쓰거나 다 찰 때까지 남은 초).
    배터리가 없으면 잔량이 None."""
    s = SYSTEM_BATTERY_STATE()
    if (powrprof.CallNtPowerInformation(5, None, 0, ctypes.byref(s), ctypes.sizeof(s))
            or not s.BatteryPresent or not s.MaxCapacity):
        return None, True, 0.0, None
    rate = s.Rate if abs(s.Rate) < 1_000_000 else 0       # 0x80000000 = 모름
    left = s.RemainingCapacity if rate < 0 else s.MaxCapacity - s.RemainingCapacity
    secs = left / abs(rate) * 3600 if rate else None
    return min(100.0, 100 * s.RemainingCapacity / s.MaxCapacity), bool(s.AcOnLine), rate / 1000, secs


_need = wt.ULONG(1 << 18)                                 # 지난번에 필요했던 버퍼 크기


def cpu_times():
    """{(pid, 시작 시각): (앱 이름, 누적 CPU 초)}. 시스템 호출 한 번으로 전체를 읽습니다."""
    for _ in range(4):
        size = _need.value + 65536
        buf = ctypes.create_string_buffer(size)
        status = ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(_need)) & 0xFFFFFFFF
        if status == 0:
            break
        if status != 0xC0000004:                          # 버퍼 부족이 아닌 오류
            return {}
    else:
        return {}
    base, off, out = ctypes.addressof(buf), 0, {}
    while True:
        # SYSTEM_PROCESS_INFORMATION (64비트): 다음 항목, 시작·유저·커널 시간, 이름, pid
        nxt, created, user, kernel, nlen, _, name_at, pid = struct.unpack_from(
            "<I28xqqqHH4xQ8xQ", buf, off)
        if pid:                                           # pid 0은 '쉬는 시간'이라 제외
            name = buf[name_at - base:name_at - base + nlen].decode("utf-16-le", "replace")
            out[(pid, created)] = (name.removesuffix(".exe"), (user + kernel) / 1e7)
        if not nxt:
            return out
        off += nxt


# ---------- 배터리 수명 / 리포트 ----------
def powercfg_report(*args):
    """윈도우 배터리 리포트를 만듭니다 (관리자 권한 필요 없음, 1초쯤 걸림)."""
    subprocess.run(["powercfg", "/batteryreport", *args], check=True, capture_output=True,
                   timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)


def parse_health(xml_bytes):
    """배터리 리포트 XML → (설계 용량 mWh, 완충 용량 mWh, 충전 횟수 또는 None). 배터리가 없으면 None."""
    root = ET.fromstring(xml_bytes)
    design = full = cycles = 0
    for b in root.iterfind(".//{*}Batteries/{*}Battery"):  # 배터리가 여러 개면 합침
        design += int(b.findtext("{*}DesignCapacity") or 0)
        full += int(b.findtext("{*}FullChargeCapacity") or 0)
        cycles = max(cycles, int(b.findtext("{*}CycleCount") or 0))
    return (design, full, cycles or None) if design else None


def battery_health():
    path = os.path.join(tempfile.gettempdir(), "BatteryLog-report.xml")
    powercfg_report("/xml", "/output", path)
    with open(path, "rb") as f:
        return parse_health(f.read())


def describe_health(h):
    """(큰 글씨, 작은 글씨)."""
    if not h:
        return "배터리 수명", "정보를 읽을 수 없어요"
    design, full, cycles = h
    sub = f"완충 {full / 1000:.1f} Wh / 설계 {design / 1000:.1f} Wh"
    return f"배터리 수명 {min(100, 100 * full / design):.0f}%", sub + (f" · 충전 {cycles}회" if cycles else "")


# ---------- 시작 프로그램 등록 ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_cmd():
    if FROZEN:
        return f'"{sys.executable}" --hidden'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}" --hidden'


def get_autostart(name=APP):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, name)[0] == autostart_cmd()
    except OSError:
        return False


def set_autostart(on, name=APP):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass


def is_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


# ---------- 기록 ----------
def today_key():
    return time.strftime("%Y-%m-%d")


def load_log():
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)
    except FileNotFoundError:
        log = {}
    except Exception:
        os.replace(LOG_PATH, LOG_PATH + ".bad")           # 깨진 기록은 버리지 않고 옆에 남겨둠
        log = {}
    if "days" not in log:                                 # 첫 버전은 날짜가 최상위에 있었음
        log = {"days": log}
    log.setdefault("settings", {})
    for old in sorted(log["days"])[:-KEEP_DAYS]:
        del log["days"][old]
    return log


def save_log(log):
    tmp = LOG_PATH + ".tmp"                               # 쓰다 꺼져도 기존 기록이 깨지지 않게
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LOG_PATH)


BASE_RISE, BASE_FALL = 0.01, 0.3   # 대기 소모 추정치가 따라가는 속도: 위로는 느리게, 아래로는 빠르게


def describe(level, plugged, watts, secs):
    """(상태 한마디, 자세한 한 줄) — 창과 트레이 풍선말이 같이 씁니다."""
    if level is None:
        return "배터리 없음", "배터리를 찾을 수 없어요"
    if not watts:
        return ("전원 연결됨", "충전하지 않는 중") if plugged else ("배터리 사용 중", "소모 전력 측정 중")
    h, m = divmod(int(secs) // 60, 60)
    t = f"{h}시간 {m}분" if h else f"{m}분"
    if watts > 0:
        return "충전 중", f"{watts:.1f} W로 충전 · 완충까지 약 {t}"
    return "배터리 사용 중", f"{-watts:.1f} W 사용 · 약 {t} 남음"


class Recorder:
    def __init__(self):
        self.log = load_log()
        self.days = self.log["days"]
        self.version = 0                  # 기록이 바뀔 때마다 +1 (화면을 다시 그릴지 판단)
        self.dirty = False
        self.saved_at = 0.0
        self.last_time = None
        self.prev_cpu = None
        self._reset(None)

    def _reset(self, level):
        self.last_level, self.acc, self.span = level, {}, 0.0

    def _split(self, drop):
        """구간에 실제로 줄어든 잔량(%)을 나눔: 대기 소모만큼은 화면·대기, 더 줄어든 만큼은 CPU 쓴 앱들.
        순간 전력(W)을 찍는 대신 잔량 차이를 써서, 측정 사이에 치솟은 소모도 빠지지 않습니다."""
        # shortcut: 대기 소모 = 최근 가장 낮았던 소모 속도(%/초) 근처, 화면 밝기를 올린 몫은 앱에 섞임
        s = self.log["settings"]
        span = max(self.span, 1.0)
        rate, base = drop / span, s.get("base_rate")
        if base is None:
            base = rate
        else:
            base += (rate - base) * (BASE_FALL if rate < base else BASE_RISE)
        s["base_rate"] = base
        used = sum(self.acc.values())
        idle = min(drop, base * span) if used else drop
        out = {app: (drop - idle) * t / used for app, t in self.acc.items()}
        out[IDLE] = idle
        return out

    def _add(self, parts):
        day = self.days.setdefault(today_key(), {})
        for app, v in parts.items():
            if v > 0:
                day[app] = day.get(app, 0) + v
        self.version += 1
        self.dirty = True

    def flush(self, now=None, force=False):
        now = time.time() if now is None else now
        if self.dirty and (force or now - self.saved_at >= SAVE_SEC):
            save_log(self.log)
            self.dirty, self.saved_at = False, now

    def sample(self, level, plugged, now):
        """now는 절전 시간도 세는 벽시계(time.time)."""
        gap = now - self.last_time if self.last_time is not None else 0
        self.last_time = now
        if level is None or plugged:                      # 충전 중엔 프로세스도 안 훑음
            self._reset(None)
            self.prev_cpu = None
        else:
            cpu = cpu_times()
            if self.last_level is None or self.prev_cpu is None or level > self.last_level:
                self._reset(level)                        # 배터리로 넘어온 첫 측정
            elif gap > SAMPLE_SEC * 3:                    # 절전에서 깨어남
                self._add({SLEEP: self.last_level - level})
                self._reset(level)
            else:
                for key, (name, t) in cpu.items():
                    d = t - self.prev_cpu.get(key, (name, 0.0))[1]
                    if d > 0:
                        self.acc[name] = self.acc.get(name, 0) + d
                self.span += gap
                if level < self.last_level:
                    self._add(self._split(self.last_level - level))
                    self._reset(level)
            self.prev_cpu = cpu
        self.flush(now)


# ---------- 트레이 아이콘 ----------
TRAY_CLASS = "BatteryLogTray"
WM_TRAY = 0x8001                                          # lParam: 마우스 메시지, 1 = 창 열기, 2 = 종료
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD] + [ctypes.c_int] * 4 + [
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.DefWindowProcW.restype = ctypes.c_ssize_t
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u32.LoadImageW.restype = wt.HANDLE
u32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
u32.LoadIconW.restype = wt.HICON
u32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
u32.FindWindowW.restype = wt.HWND
u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
u32.GetParent.restype = wt.HWND
u32.GetParent.argtypes = [wt.HWND]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


class Tray:
    """알림 영역 아이콘. 숨은 창 하나가 클릭·전원 변화·윈도우 종료 알림을 받습니다.

    tkinter 메시지 루프 안에서 다른 창의 콜백이 Tcl을 건드리면 죽을 수 있어서
    자기 스레드에서 따로 메시지를 받습니다. on_event(이름)는 그 스레드에서 불립니다.
    """

    def __init__(self, icon_path, on_event):
        self.on_event = on_event
        self._tip = None
        self._proc = WNDPROC(self._wndproc)               # 가비지 컬렉션되지 않게 잡아둠
        self._ready = threading.Event()
        threading.Thread(target=self._run, args=(icon_path,), daemon=True).start()
        self._ready.wait()

    def _run(self, icon_path):
        inst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=inst, lpszClassName=TRAY_CLASS)
        u32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = u32.CreateWindowExW(0, TRAY_CLASS, APP, 0, 0, 0, 0, 0, None, None, inst, None)
        size = u32.GetSystemMetrics(49)                   # 작은 아이콘 크기
        icon = u32.LoadImageW(None, icon_path, 1, size, size, 0x10) or u32.LoadIconW(None, 32512)
        self.nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                   uFlags=7, uCallbackMessage=WM_TRAY, hIcon=icon, szTip=APP)
        shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid))
        self._ready.set()
        msg = wt.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wp, lp):
        what = None
        if msg == WM_TRAY:
            what = {0x202: "toggle", 0x205: "toggle", 1: "show", 2: "quit"}.get(lp)
        elif msg == 0x218 and wp in (0x7, 0xA, 0x12):     # 절전 복귀, 전원 상태 변경
            what = "power"
        elif msg == 0x16 and wp:                          # 윈도우 종료·로그오프
            what = "end"
        if what:
            try:
                self.on_event(what)
            except Exception:
                pass
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def tip(self, text):
        if text != self._tip:
            self._tip = self.nid.szTip = text[:127]
            shell32.Shell_NotifyIconW(1, ctypes.byref(self.nid))

    def remove(self):
        shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))


# ---------- 화면 ----------
def rpts(x1, y1, x2, y2, r):
    """둥근 사각형을 smooth 다각형으로 그리기 위한 점들."""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    return (x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
            x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1)


class App:
    W, H = 420, 732

    def __init__(self, root):
        self.root, self.rec = root, Recorder()
        self.s = root.winfo_fpixels("1i") / 96            # 화면 배율
        self.c = c = DARK if is_dark() else LIGHT
        self.day = None                                   # None = 오늘을 따라감
        self.head_sig = self.rows_sig = self.plugged = self.job = None
        self.health = None                                # 처음 창을 열 때 읽음 (작업 스레드가 채움)
        self.last_sample = 0.0
        self.auto = get_autostart()

        root.title("배터리 사용 기록기")
        root.geometry(f"{round(self.W * self.s)}x{round(self.H * self.s)}")
        root.resizable(False, False)
        root.configure(bg=c["bg"])
        try:
            root.iconbitmap(ICON_PATH)
        except tk.TclError:
            pass
        if c is DARK:                                     # 제목 표시줄도 어둡게
            root.update_idletasks()
            ctypes.WinDLL("dwmapi").DwmSetWindowAttribute(
                wt.HWND(u32.GetParent(root.winfo_id())), 20, ctypes.byref(ctypes.c_int(1)), 4)
        self.cv = tk.Canvas(root, bg=c["bg"], highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.build()

        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.bind("<Escape>", lambda e: self.hide())
        root.bind("<Left>", lambda e: self.step_day(-1))
        root.bind("<Right>", lambda e: self.step_day(1))
        root.bind("<Map>", lambda e: e.widget is root and self.poke())   # 다시 보이면 바로 갱신
        self.tray = Tray(ICON_PATH, self.on_tray)
        self.tick()

    # ----- 그리기 도구 (좌표는 100% 배율 기준, 여기서 화면 배율을 곱함) -----
    def P(self, *v):
        return [x * self.s for x in v]

    def font(self, size, bold=False, family=FONT):
        return (family, -round(size * self.s), "bold" if bold else "normal")

    def card(self, x1, y1, x2, y2, r, **kw):
        return self.cv.create_polygon(rpts(*self.P(x1, y1, x2, y2, r)), smooth=True, outline="", **kw)

    def pill_xy(self, x1, y1, x2, y2):
        r, y = (y2 - y1) / 2, (y1 + y2) / 2
        return self.P(x1 + r, y, max(x2 - r, x1 + r + 0.5), y)

    def pill(self, x1, y1, x2, y2, fill, **kw):
        """양 끝이 반원인 알약: 끝이 둥근 굵은 선 하나로 그립니다."""
        return self.cv.create_line(*self.pill_xy(x1, y1, x2, y2), width=(y2 - y1) * self.s,
                                   capstyle="round", fill=fill, **kw)

    def text(self, x, y, **kw):
        return self.cv.create_text(x * self.s, y * self.s, **kw)

    def button(self, x1, y1, x2, y2, label, cmd, size=13, color="high", ink="on"):
        tag = f"btn{x1}_{y1}"
        shapes = (self.pill(x1, y1, x2, y2, self.c[color], tags=tag),
                  self.card(x1, y1, x2, y2, 10, fill=self.c[color], state="hidden", tags=tag))
        self.text((x1 + x2) / 2, (y1 + y2) / 2, text=label, fill=self.c[ink],
                  font=self.font(size, True), tags=tag)

        def morph(pressed):               # 누르는 동안 알약이 둥근 사각형으로 바뀜 (shape morph)
            self.cv.itemconfig(shapes[0], state="hidden" if pressed else "normal")
            self.cv.itemconfig(shapes[1], state="normal" if pressed else "hidden")
        self.cv.tag_bind(tag, "<ButtonPress-1>", lambda e: morph(True))
        self.cv.tag_bind(tag, "<ButtonRelease-1>", lambda e: (morph(False), cmd()))

    def build(self):
        c = self.c
        self.text(24, 36, text="배터리", anchor="w", fill=c["on"], font=self.font(24, True))
        self.button(340, 22, 404, 50, "종료", self.quit, 12)

        self.card(16, 64, 404, 232, 40, fill=c["pc"])                       # 큰 카드
        self.t_pct = self.text(36, 116, anchor="w", fill=c["on_pc"],
                               font=self.font(54, True, NUM_FONT))
        self.pill(264, 100, 384, 132, c["p"])                               # 상태 칩
        self.t_state = self.text(324, 116, fill=c["on_p"], font=self.font(12, True))
        self.t_detail = self.text(36, 170, anchor="w", fill=c["on_pc"], font=self.font(13))
        self.pill(36, 194, 384, 210, c["track"])
        self.bar = self.pill(36, 194, 52, 210, c["p"])

        self.button(16, 242, 48, 274, "‹", lambda: self.step_day(-1), 16)   # 날짜 넘기기
        self.t_day = self.text(104, 258, fill=c["on"], font=self.font(15, True))
        self.button(160, 242, 192, 274, "›", lambda: self.step_day(1), 16)
        self.t_total = self.text(404, 258, anchor="e", fill=c["var"], font=self.font(12))

        self.card(16, 284, 404, 548, 40, fill=c["card"])                    # 앱 목록 카드

        self.card(16, 560, 404, 628, 34, fill=c["card"])                    # 배터리 수명 카드
        self.t_htitle = self.text(36, 582, anchor="w", fill=c["on"], font=self.font(14, True))
        self.t_hsub = self.text(36, 607, anchor="w", fill=c["var"], font=self.font(11))
        self.button(312, 578, 388, 610, "리포트", self.open_report, 12, "p", "on_p")

        self.pill(16, 640, 404, 692, c["card"], tags="auto")                # 자동 실행 스위치
        self.text(40, 666, text="윈도우 시작 시 자동 실행", anchor="w", fill=c["on"],
                  font=self.font(13), tags="auto")
        self.cv.tag_bind("auto", "<ButtonRelease-1>", lambda e: self.set_auto(not self.auto))
        self.draw_switch()

        self.text(210, 712, text="창을 닫아도 트레이에서 계속 기록해요 · 앱별 수치는 추정치",
                  fill=c["var"], font=self.font(10))

    def draw_switch(self):
        c, on = self.c, self.auto
        self.cv.delete("sw")
        self.pill(336, 650, 388, 682, c["p"] if on else c["outline"], tags=("sw", "auto"))
        if not on:                                        # 꺼짐: 테두리만 남기고 속을 비움
            self.pill(338, 652, 386, 680, c["high"], tags=("sw", "auto"))
        x, r = (372, 12) if on else (352, 8)
        self.cv.create_oval(*self.P(x - r, 666 - r, x + r, 666 + r), outline="",
                            fill=c["on_p"] if on else c["outline"], tags=("sw", "auto"))

    def draw_rows(self):
        c, today = self.c, today_key()
        key = self.day or today
        sig = (key, self.rec.version)
        if sig == self.rows_sig:
            return
        self.rows_sig = sig
        day = self.rec.days.get(key, {})
        m, d = key.split("-")[1:]
        self.cv.itemconfig(self.t_day, text="오늘" if key == today else f"{int(m)}월 {int(d)}일")
        self.cv.itemconfig(self.t_total, text=f"기록된 사용량 {sum(day.values()):.1f}%")
        self.cv.delete("rows")
        if not day:
            self.text(210, 416, text="아직 기록이 없어요\n배터리로 쓰는 동안 자동으로 채워집니다",
                      fill=c["var"], font=self.font(12), justify="center", tags="rows")
            return
        ranked = sorted(day.items(), key=lambda kv: -kv[1])[:TOP_N]
        biggest = ranked[0][1]
        for i, (name, v) in enumerate(ranked):
            y = 296 + 40 * i
            name = name if len(name) <= 26 else name[:25] + "…"
            self.text(36, y + 12, text=name, anchor="w", fill=c["on"], font=self.font(13), tags="rows")
            self.text(384, y + 12, text=f"{v:.1f}%", anchor="e", fill=c["var"],
                      font=self.font(12, family=NUM_FONT), tags="rows")
            self.pill(36, y + 26, 384, y + 32, c["high"], tags="rows")
            self.pill(36, y + 26, 36 + max(6, 348 * v / biggest), y + 32, c["p"], tags="rows")

    def render(self, level, state, detail):
        health = self.health or ("배터리 수명", "읽는 중…")
        sig = (None if level is None else round(level), state, detail, health)
        if sig != self.head_sig:
            self.head_sig = sig
            self.cv.itemconfig(self.t_pct, text="--" if level is None else f"{round(level)}%")
            self.cv.itemconfig(self.t_state, text=state)
            self.cv.itemconfig(self.t_detail, text=detail)
            self.cv.coords(self.bar, *self.pill_xy(36, 194, 36 + max(16, 348 * (level or 0) / 100), 210))
            self.cv.itemconfig(self.t_htitle, text=health[0])
            self.cv.itemconfig(self.t_hsub, text=health[1])
        self.draw_rows()

    # ----- 동작 -----
    def tick(self):
        self.job = None
        shown = False
        try:
            level, plugged, watts, secs = battery()
            now = time.time()
            if now - self.last_sample >= SAMPLE_SEC - 1 or plugged != self.plugged:
                self.rec.sample(level, plugged, now)
                self.last_sample, self.plugged = now, plugged
            state, detail = describe(level, plugged, watts, secs)
            shown = self.root.state() == "normal"
            if shown:
                self.render(level, state, detail)
            self.tray.tip(detail if level is None else f"배터리 {round(level)}% · {detail}")
        except Exception as e:
            self.cv.itemconfig(self.t_detail, text=f"오류: {e}")
        self.job = self.root.after(LIVE_MS if shown else SAMPLE_SEC * 1000, self.tick)

    def poke(self):
        """전원 상태가 바뀌었거나 창이 다시 보일 때: 기다리지 않고 지금 한 번."""
        if self.job:
            self.root.after_cancel(self.job)
        self.tick()

    def step_day(self, d):
        today = today_key()
        keys = sorted(set(self.rec.days) | {today})
        i = min(len(keys) - 1, max(0, keys.index(self.day or today) + d))
        self.day = None if keys[i] == today else keys[i]
        self.draw_rows()

    # 아래 두 작업은 1초쯤 걸려서 따로 돌리고, 결과는 self.health에만 넣어 tick이 그리게 합니다.
    def load_health(self):
        try:
            self.health = describe_health(battery_health())
        except Exception:
            self.health = describe_health(None)

    def open_report(self):
        def work():
            try:
                powercfg_report("/output", REPORT_PATH)
                os.startfile(REPORT_PATH)
            except Exception as e:
                self.health = ("리포트를 만들지 못했어요", str(e)[:48])
        threading.Thread(target=work, daemon=True).start()

    def set_auto(self, on):
        try:
            set_autostart(on)
        except OSError as e:
            self.cv.itemconfig(self.t_detail, text=f"자동 실행 설정 실패: {e}")
        self.auto = get_autostart()
        self.draw_switch()

    def ask_autostart(self):
        from tkinter import messagebox
        self.set_auto(messagebox.askyesno(
            "배터리 사용 기록기",
            "윈도우를 시작할 때 자동으로 실행할까요?\n\n"
            "트레이에 숨어서 조용히 기록합니다.\n나중에 창 아래 스위치로 바꿀 수 있어요.",
            parent=self.root))

    def on_tray(self, what):
        """트레이 스레드에서 불립니다. 창을 건드리는 일은 after로 본 스레드에 넘깁니다."""
        if what == "end":
            self.rec.flush(force=True)                    # 윈도우가 꺼지는 중: 기다릴 시간이 없어 바로 저장
        else:
            self.root.after(0, {"toggle": self.toggle, "show": self.show,
                                "quit": self.quit, "power": self.poke}[what])

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self.health is None:
            self.health = ("배터리 수명", "읽는 중…")
            threading.Thread(target=self.load_health, daemon=True).start()
        settings = self.rec.log["settings"]
        if not settings.get("asked"):                     # 처음 창을 열 때 한 번만 물어봄
            settings["asked"] = self.rec.dirty = True
            self.root.after(400, self.ask_autostart)

    def hide(self):
        self.root.withdraw()
        self.rec.flush(force=True)
        k32.SetProcessWorkingSetSize(k32.GetCurrentProcess(), -1, -1)   # 숨는 김에 안 쓰는 메모리를 돌려줌

    def toggle(self):
        self.hide() if self.root.state() == "normal" else self.show()

    def quit(self):
        self.rec.flush(force=True)
        self.tray.remove()
        self.root.destroy()


# ---------- 자체 확인 ----------
def _selftest():
    global battery, cpu_times, LOG_PATH

    assert ctypes.sizeof(NOTIFYICONDATAW) == 976
    level = battery()[0]
    assert level is None or 0 <= level <= 100
    me = [t for (pid, _), (_, t) in cpu_times().items() if pid == os.getpid()]
    assert len(me) == 1 and abs(me[0] - time.process_time()) < 0.5, me
    assert all(pid for pid, _ in cpu_times())

    assert describe(None, True, 0, None)[0] == "배터리 없음"
    assert describe(80, False, -6.0, 3 * 3600 + 120) == ("배터리 사용 중", "6.0 W 사용 · 약 3시간 2분 남음")
    assert describe(80, True, 20.0, 900) == ("충전 중", "20.0 W로 충전 · 완충까지 약 15분")
    assert describe(100, True, 0, None)[0] == "전원 연결됨"

    sample = (b'<?xml version="1.0" encoding="utf-8"?>'
              b'<BatteryReport xmlns="http://schemas.microsoft.com/battery/2012"><Batteries><Battery>'
              b'<DesignCapacity>51480</DesignCapacity><FullChargeCapacity>46630</FullChargeCapacity>'
              b'<CycleCount>125</CycleCount></Battery></Batteries><RuntimeEstimates><DesignCapacity>'
              b'<Capacity>1</Capacity></DesignCapacity></RuntimeEstimates></BatteryReport>')
    assert parse_health(sample) == (51480, 46630, 125)
    assert parse_health(sample.replace(b"125", b"0")) == (51480, 46630, None)
    assert parse_health(b'<BatteryReport xmlns="x"><Batteries/></BatteryReport>') is None
    assert describe_health((51480, 46630, 125)) == (
        "배터리 수명 91%", "완충 46.6 Wh / 설계 51.5 Wh · 충전 125회")
    assert describe_health(None)[1] == "정보를 읽을 수 없어요"
    h = battery_health()                                  # 이 컴퓨터에서 실제로
    assert h is None or 0 < h[1] <= h[0] * 1.2, h

    set_autostart(True, "BatteryLogSelfTest")
    assert get_autostart("BatteryLogSelfTest")
    set_autostart(False, "BatteryLogSelfTest")
    set_autostart(False, "BatteryLogSelfTest")            # 없는 걸 지워도 조용해야 함
    assert not get_autostart("BatteryLogSelfTest")

    LOG_PATH = os.path.join(tempfile.mkdtemp(), "log.json")
    with open(LOG_PATH, "w", encoding="utf-8") as f:      # 첫 버전 형식의 기록
        json.dump({"2026-01-01": {"a": 1}}, f)
    assert load_log() == {"days": {"2026-01-01": {"a": 1}}, "settings": {}}
    os.remove(LOG_PATH)

    feed = [                          # (시각, 잔량 %, 충전기, a의 누적 CPU초)
        (0, 50, False, 0), (30, 50, False, 12), (60, 49, False, 24),   # 60초에 1% = 대기 소모로 잡힘
        (1000, 45, False, 24),                                         # 긴 공백 = 절전 4%
        (1030, 46, True, 30),                                          # 충전 중엔 훑지도 않음
        (1060, 46, False, 40), (1090, 45.5, False, 46),                # 대기 속도 그대로 = 전부 화면·대기
        (1120, 44.5, False, 52),                                       # 두 배로 빨리 줄어듦 = 넘친 몫이 a에게
    ]
    state, scans = {}, []
    cpu_times = lambda: scans.append(1) or {(1, 0): ("a", state["cpu"])}
    rec = Recorder()
    for t, lv, plug, state["cpu"] in feed:
        rec.sample(lv, plug, t)
        assert os.path.exists(LOG_PATH) == (t >= 1000)    # 2분이 지나기 전엔 디스크에 안 씀
    day = rec.days[today_key()]
    assert len(scans) == 7, scans
    assert abs(day["a"] - 0.495) < 1e-9, day              # 1% 중 대기 몫 0.505%를 뺀 나머지
    assert day[SLEEP] == 4 and abs(sum(day.values()) - 6.5) < 1e-9, day
    rec.flush(force=True)
    assert load_log() == rec.log
    print("ok")


def main():
    hidden = "--hidden" in sys.argv
    running = u32.FindWindowW(TRAY_CLASS, None)
    if running:                                           # 이미 실행 중이면 그 창을 띄우고 끝
        if not hidden:
            u32.PostMessageW(running, WM_TRAY, 0, 1)
        return
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)     # 고배율 화면에서 흐려지지 않게
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    if not hidden:
        app.show()
    root.mainloop()


if __name__ == "__main__":
    _selftest() if "--test" in sys.argv else main()
