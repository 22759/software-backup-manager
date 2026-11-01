# -*- coding: utf-8 -*-
"""
软件备份管理器 / Software Backup Manager
读取本机已装应用 -> 勾选 -> 从官网(winget 源)下载最新安装包 -> 导出一键重装脚本。
重装系统时，每个软件安装到「安装根目录/软件名」各自的子文件夹。

技术栈: Python + pywebview(Edge WebView2) + winget
"""
import os
import re
import sys
import json
import time
import threading
import subprocess
import http.server
import socketserver
import socket
import webbrowser
import base64
import shutil

WINDOW_TITLE = "软件备份管理器"

try:
    import ctypes
    from ctypes import wintypes
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False

try:
    import webview
except Exception:
    webview = None

try:
    import winreg
    _HAS_WINREG = True
except Exception:
    winreg = None
    _HAS_WINREG = False

try:
    import tkinter as tk
    from tkinter import filedialog
    _HAS_TK = True
except Exception:
    _HAS_TK = False

try:
    import ctypes
    def is_admin():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
except Exception:
    def is_admin():
        return False

# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # PyInstaller 打包成 exe 后：BASE 用 exe 所在目录（可写），web 在解压目录内
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
HERE = BASE
WEB_DIR = os.path.join(getattr(sys, "_MEIPASS", BASE), "web")
WINGET = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe")
if not os.path.exists(WINGET):
    WINGET = "winget"

DEFAULT_BACKUP = os.path.join(BASE, "Backups")
DEFAULT_INSTALL = os.path.join(BASE, "Install")

# 窗口最小尺寸（逻辑像素）
MIN_W, MIN_H = 880, 560

INSTALLER_EXTS = (".exe", ".msi", ".msix", ".msixbundle", ".appx",
                  ".appxbundle", ".zip", ".7z", ".cab")

# 压缩包类扩展名：无法直接执行安装，需解压/手动
ARCHIVE_EXTS = {".zip", ".7z", ".tar", ".gz", ".tgz", ".rar"}

# ----------------------------------------------------------------------------
# winget 安装器类型（对应 manifest 里的 InstallerType 字段）
# ----------------------------------------------------------------------------
# 同一个软件常常同时提供好几种安装器，例如 Arduino IDE 同时有
#   nullsoft(EXE) / wix(MSI) / zip(免安装)
# 不加筛选地 `winget download` 会把几个变体统统下一遍，白白多占几百 MB。
# 所以这里按「文件形态」分组，下载时按用户选择的优先级依次尝试，
# 命中第一个存在的类型就停手。
INSTALLER_TYPE_GROUPS = {
    "exe":  ["exe", "nullsoft", "inno", "burn"],
    "msi":  ["msi", "wix", "msp"],
    "zip":  ["zip"],
    "msix": ["msix", "appx"],
}

# 各类型的界面文案
INSTALLER_TYPE_LABEL = {
    "auto":    "自动 · 优先 EXE",
    "exe":     "EXE 安装程序",
    "nullsoft": "EXE 安装程序（NSIS）",
    "inno":    "EXE 安装程序（Inno Setup）",
    "burn":    "EXE 安装程序（Burn 引导）",
    "msi":     "MSI 安装包",
    "wix":     "MSI 安装包（WiX）",
    "msp":     "MSI 补丁包",
    "zip":     "ZIP 压缩包（免安装）",
    "msix":    "MSIX 应用包",
    "appx":    "APPX 应用包",
    "all":     "全部变体（不筛选，体积大）",
}

# 用户可选的档位 -> 尝试顺序（每档是一个组，组内按上面的顺序试）
INSTALLER_TYPE_MODES = {
    "auto": ["exe", "msi", "zip", "msix"],
    "exe":  ["exe", "msi", "zip", "msix"],
    "msi":  ["msi", "exe", "zip", "msix"],
    "zip":  ["zip", "exe", "msi", "msix"],
    "all":  [],
}


def _installer_candidates(mode: str):
    """
    根据用户选择算出「要依次尝试的安装器类型」列表。
    返回空列表 = 不加 --installer-type 筛选（把该包的所有变体都下下来）。
    """
    mode = (mode or "auto").strip().lower()
    groups = INSTALLER_TYPE_MODES.get(mode)
    if groups is None:
        groups = INSTALLER_TYPE_MODES["auto"]
    out = []
    for g in groups:
        for t in INSTALLER_TYPE_GROUPS.get(g, []):
            if t not in out:
                out.append(t)
    return out


# 同一进程内缓存「包 id -> 命中的安装器类型」，避免重复试探
_TYPE_CACHE = {}

# 启动子进程时禁止弹出黑色控制台窗口（CREATE_NO_WINDOW）。
# 本程序是 --windowed 打包的（自身没有控制台），而 winget.exe / msiexec /
# cmd.exe 都是控制台子系统程序，直接 Popen 会让它们各自开一个黑框闪一下。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _is_archive(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in ARCHIVE_EXTS


# ----------------------------------------------------------------------------
# 原生窗口操作（拖动 / 八向缩放 / 最大化）
#
# 为什么不用 pywebview 自带的 API？
#   - pywebview 6.x 的 Window 没有 start_drag() 方法（只有 move/resize），
#     之前调用会抛 AttributeError 被静默吞掉，导致「窗口推不动」。
#   - frameless=True 时 FormBorderStyle 被设成 None，Windows 自带的缩放边框
#     也没了，所以窗口大小同样没法调。
# 这里直接用 Win32 的 SetWindowPos / GetCursorPos 实现，行为最可控。
# ----------------------------------------------------------------------------

if _HAS_WIN32:
    _U32 = ctypes.windll.user32
    _SWP_NOSIZE = 0x0001
    _SWP_NOMOVE = 0x0002
    _SWP_NOZORDER = 0x0004
    _SWP_NOACTIVATE = 0x0010
    _SWP_FRAMECHANGED = 0x0020
    _GWL_STYLE = -16
    _GWL_EXSTYLE = -20
    _WS_THICKFRAME = 0x00040000
    _WS_CAPTION = 0x00C00000
    _WS_SYSMENU = 0x00080000
    _WS_MINIMIZEBOX = 0x00020000
    _WS_MAXIMIZEBOX = 0x00010000
    _WS_EX_APPWINDOW = 0x00040000
    _WS_EX_TOOLWINDOW = 0x00000080
    _MONITOR_DEFAULTTONEAREST = 2

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong),
                    ("rcMonitor", _RECT),
                    ("rcWork", _RECT),
                    ("dwFlags", ctypes.c_ulong)]

    # 显式声明指针类型参数，避免 64 位下句柄被截断
    _U32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    _U32.GetWindowRect.restype = ctypes.c_bool
    _U32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _U32.SetWindowPos.restype = ctypes.c_bool
    _U32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _U32.GetCursorPos.restype = ctypes.c_bool
    _U32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR,
                                     ctypes.POINTER(_MONITORINFO)]
    _U32.GetMonitorInfoW.restype = ctypes.c_bool
    _U32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _U32.MonitorFromWindow.restype = wintypes.HMONITOR


_DBG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dbg.txt")


def _DBG(*args):
    try:
        with open(_DBG_LOG, "a", encoding="utf-8") as f:
            f.write(" ".join(str(a) for a in args) + "\n")
    except Exception:
        pass


def _hwnd():
    """取当前窗口的原生句柄（HWND）。取不到返回 0。"""
    if not _HAS_WIN32 or webview is None:
        return 0
    try:
        w = webview.windows[0]
    except Exception:
        return 0
    native = getattr(w, "native", None)
    if native is None:
        return 0
    try:
        # winforms 后端：native 是 Form，Handle 是 IntPtr
        h = native.Handle
        return int(h.ToInt32()) if hasattr(h, "ToInt32") else int(h)
    except Exception:
        return 0


def _win_rect(hwnd):
    """返回窗口在屏幕上的物理像素矩形 (left, top, right, bottom)。"""
    if not hwnd:
        return None
    try:
        r = _RECT()
        if not _U32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _cursor_pos():
    """返回鼠标在屏幕上的物理像素坐标 (x, y)。"""
    if not _HAS_WIN32:
        return (0, 0)
    try:
        p = _POINT()
        if not _U32.GetCursorPos(ctypes.byref(p)):
            return (0, 0)
        return (int(p.x), int(p.y))
    except Exception:
        return (0, 0)


def _set_pos(hwnd, x, y, w=None, h=None):
    """移动/缩放窗口（物理像素）。"""
    if not hwnd:
        return False
    try:
        flags = _SWP_NOZORDER | _SWP_NOACTIVATE
        if w is None or h is None:
            flags |= _SWP_NOSIZE
        # 注意：SetWindowPos 已声明 argtypes，宽高必须是 int，
        # 传 None 会被 ctypes 判为类型错误（之前拖动失效就是栽在这）。
        _U32.SetWindowPos(hwnd, None, int(x), int(y),
                          0 if w is None else int(w),
                          0 if h is None else int(h), flags)
        return True
    except Exception:
        return False


def _dpi_scale(hwnd):
    """窗口所在显示器的 DPI 缩放比（96dpi = 1.0）。"""
    if not _HAS_WIN32 or not hwnd:
        return 1.0
    try:
        dpi = _U32.GetDpiForWindow(hwnd)
        return (dpi / 96.0) if dpi else 1.0
    except Exception:
        return 1.0


# 拖动/缩放会话状态
_DRAG_STATE = {"thread": None, "mode": None, "hwnd": 0,
               "start_cursor": (0, 0), "start_rect": None}
_DRAG_LOCK = threading.Lock()

# 最大化状态（记录还原前的窗口矩形）
_MAX_STATE = {"maximized": False, "saved_rect": None}

_VK_LBUTTON = 0x01
_SW_RESTORE = 9
_SW_MAXIMIZE = 3


def _left_button_down():
    """左键是否仍按下（全局状态，鼠标移出窗口也有效）。"""
    if not _HAS_WIN32:
        return False
    try:
        return bool(_U32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000)
    except Exception:
        return False


def _is_zoomed(hwnd):
    try:
        return bool(_U32.IsZoomed(hwnd))
    except Exception:
        return False


def _work_area(hwnd):
    """
    返回当前窗口所在显示器的「工作区」矩形（物理像素，已排除任务栏）。
    无边框窗口直接用 ShowWindow(SW_MAXIMIZE) 会盖住任务栏，所以自己按工作区摆。
    """
    if not _HAS_WIN32 or not hwnd:
        return None
    try:
        mon = _U32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _U32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _all_work_areas():
    """枚举所有显示器的工作区矩形（物理像素）。"""
    out = []

    if not _HAS_WIN32:
        return out
    try:
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(_RECT), wintypes.LPARAM)

        def _cb(hmon, hdc, lprc, lparam):
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if _U32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork
                out.append((r.left, r.top, r.right, r.bottom))
            return True

        _U32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(_RECT), MONITORENUMPROC, wintypes.LPARAM]
        _U32.EnumDisplayMonitors.restype = ctypes.c_bool
        _U32.EnumDisplayMonitors(0, None, MONITORENUMPROC(_cb), 0)
    except Exception:
        pass
    return out


def _rect_visible(l, t, r, b, min_ratio=0.25):
    """
    判断矩形是否还落在某个显示器上（拔掉外接屏 / 改分辨率后不至于把窗口弄丢）。
    min_ratio：窗口与工作区交集面积至少要占窗口面积的该比例。
    """
    areas = _all_work_areas()
    if not areas:
        return True
    win_area = max(1, (r - l) * (b - t))
    best = 0
    for (al, at, ar, ab) in areas:
        iw = min(r, ar) - max(l, al)
        ih = min(b, ab) - max(t, at)
        if iw > 0 and ih > 0:
            best = max(best, iw * ih)
    return (best / win_area) >= min_ratio


def _center_on_primary(l, t, w, h):
    """把窗口居中放到主显示器工作区，返回物理像素矩形。"""
    areas = _all_work_areas()
    if not areas:
        return (l, t, w, h)
    al, at, ar, ab = areas[0]
    x = al + max(0, ((ar - al) - w) // 2)
    y = at + max(0, ((ab - at) - h) // 2)
    return (x, y, w, h)


def _restore_normal(hwnd):
    """从最大化还原为普通窗口。"""
    try:
        _U32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.06)
        return True
    except Exception:
        return False


def _drag_loop(mode, hwnd, anchor, start_rect):
    """
    拖动/缩放主循环：在 Python 线程里以 ~60fps 轮询光标位置并改窗口。
    放在后端而不是让 JS 每帧调一次 API，是为了避免 IPC 往返造成的拖动卡顿。

    anchor = 按下鼠标那一刻的光标物理坐标（由前端在 mousedown 时算出），
    用它做基准就不会出现「窗口先跳一下再跟手」的抖动。
    """
    l, t, r, b = start_rect
    scale = _dpi_scale(hwnd)
    min_w = int(MIN_W * scale)
    min_h = int(MIN_H * scale)
    ax, ay = anchor
    try:
        while _left_button_down():
            cx, cy = _cursor_pos()
            dx = int(cx) - int(ax)
            dy = int(cy) - int(ay)
            if mode == "move":
                _set_pos(hwnd, l + dx, t + dy)
            else:
                nl, nt, nr, nb = l, t, r, b
                if "w" in mode:
                    nl = l + dx
                if "e" in mode:
                    nr = r + dx
                if "n" in mode:
                    nt = t + dy
                if "s" in mode:
                    nb = b + dy
                # 最小尺寸约束
                if nr - nl < min_w:
                    if "w" in mode:
                        nl = nr - min_w
                    else:
                        nr = nl + min_w
                if nb - nt < min_h:
                    if "n" in mode:
                        nt = nb - min_h
                    else:
                        nb = nt + min_h
                _set_pos(hwnd, nl, nt, nr - nl, nb - nt)
            time.sleep(0.016)
    finally:
        with _DRAG_LOCK:
            _DRAG_STATE.update({"thread": None, "mode": None, "hwnd": 0,
                                "start_cursor": (0, 0), "start_rect": None})


def _begin_window_drag(mode, grab_x=None, grab_y=None):
    """
    开始一次窗口拖动或缩放。
    mode: 'move' | 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'
    grab_x/grab_y: mousedown 时光标相对窗口内容区的 CSS 坐标（可选）。
    真正的动作由后端线程轮询光标完成，前端只需在 mousedown 时调一次。
    """
    hwnd = _hwnd()
    if not hwnd:
        return {"ok": False, "error": "取不到窗口句柄"}

    with _DRAG_LOCK:
        old = _DRAG_STATE.get("thread")
        if old and old.is_alive():
            return {"ok": False, "error": "已有拖动在进行"}

    # 最大化状态下先还原成普通窗口，否则拖不动 / 缩不了
    if _is_zoomed(hwnd) or _MAX_STATE.get("maximized"):
        saved = _MAX_STATE.get("saved_rect")
        if saved:
            _set_pos(hwnd, saved[0], saved[1], saved[2], saved[3])
        else:
            _restore_normal(hwnd)
        _MAX_STATE["maximized"] = False
        time.sleep(0.05)

    rect = _win_rect(hwnd)
    if not rect:
        return {"ok": False, "error": "取不到窗口位置"}

    scale = _dpi_scale(hwnd)
    # 锚点 = 按下鼠标那一刻的光标屏幕坐标。
    # 无边框窗口的内容区就是整个窗口矩形，所以 client 坐标 + 窗口原点 = 屏幕坐标。
    if grab_x is None or grab_y is None:
        anchor = _cursor_pos()
    else:
        anchor = (int(rect[0] + float(grab_x) * scale),
                  int(rect[1] + float(grab_y) * scale))

    with _DRAG_LOCK:
        _DRAG_STATE.update({"mode": mode, "hwnd": hwnd,
                            "start_cursor": anchor, "start_rect": rect})
        th = threading.Thread(target=_drag_loop,
                              args=(mode, hwnd, anchor, rect), daemon=True)
        _DRAG_STATE["thread"] = th
    th.start()
    return {"ok": True}


def _end_window_drag():
    """强制结束拖动（前端 mouseup / 失焦时的兜底）。"""
    with _DRAG_LOCK:
        _DRAG_STATE.update({"mode": None, "hwnd": 0,
                            "start_cursor": (0, 0), "start_rect": None})
    return {"ok": True}


def _is_maximized():
    return bool(_MAX_STATE.get("maximized"))


def _toggle_maximize():
    """
    最大化 / 还原。
    无边框窗口用 ShowWindow(SW_MAXIMIZE) 会盖住任务栏，所以手动按工作区摆放，
    并记住还原前的位置。
    """
    hwnd = _hwnd()
    if not hwnd or not _HAS_WIN32:
        return False
    try:
        if _MAX_STATE.get("maximized"):
            saved = _MAX_STATE.get("saved_rect")
            if saved:
                _set_pos(hwnd, saved[0], saved[1], saved[2], saved[3])
            else:
                _U32.ShowWindow(hwnd, _SW_RESTORE)
            _MAX_STATE["maximized"] = False
        else:
            rect = _win_rect(hwnd)
            if rect:
                _MAX_STATE["saved_rect"] = (rect[0], rect[1],
                                            rect[2] - rect[0], rect[3] - rect[1])
            wa = _work_area(hwnd)
            if wa:
                _set_pos(hwnd, wa[0], wa[1], wa[2] - wa[0], wa[3] - wa[1])
            else:
                _U32.ShowWindow(hwnd, _SW_MAXIMIZE)
            _MAX_STATE["maximized"] = True
        return True
    except Exception:
        return False


def _is_zip(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".zip"


def _find_7z():
    """在常见位置找 7z 可执行文件，找不到返回 None。"""
    cand = [shutil.which("7z"), shutil.which("7za")]
    for c in cand:
        if c:
            return c
    for root in (r"C:\Program Files\7-Zip", r"C:\Program Files (x86)\7-Zip"):
        p = os.path.join(root, "7z.exe")
        if os.path.exists(p):
            return p
    return None

STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "phase": "",
    "total": 0,
    "done": 0,
    "current": "",
    "current_id": "",
    "log": [],
    "results": {},   # id -> {status, detail}
}

# 全局设置（由前端「总设置」面板写入）：flat=True 时所有软件直接装进大文件夹（不建子目录）
GLOBAL = {"flat": False}

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def sanitize(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", name or "").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > 60:
        s = s[:60].rstrip()
    return s or "app"


def dir_key(s: str) -> str:
    """用于文件夹名的稳定安全标识（保留点号，其余替换成下划线）。"""
    s = re.sub(r"[^0-9A-Za-z.]+", "_", s or "").strip("_")
    return s or "app"


def encode_image(path: str) -> str:
    """把本地图片读成 data URI，供前端直接当背景图使用。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "bmp": "image/bmp", "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "image/png")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{data}"


SETTINGS_FILE = os.path.join(BASE, "settings.json")
LINKS_FILE = os.path.join(BASE, "install_links.json")
# 已下载安装包索引：{winget_id: {path, version, installer_type, saved_at, size}}
# 有了它，「一键安装 / 导出重装脚本」就能优先用本地已下载的包（可离线），
# 而不是再去 winget 在线装一遍。
BACKUP_INDEX_FILE = os.path.join(BASE, "backup_index.json")


def load_backup_index() -> dict:
    """已下载安装包索引。"""
    try:
        with open(BACKUP_INDEX_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_backup_index(obj: dict) -> bool:
    try:
        with open(BACKUP_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(obj or {}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def record_backup_installer(pid: str, path: str, version: str = "",
                             installer_type: str = "", name: str = ""):
    """把下载好的安装包记进索引，供后续安装 / 导出脚本优先使用。"""
    if not pid or not path:
        return
    try:
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            return
        idx = load_backup_index()
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0
        idx[pid] = {
            "path": abs_path,
            "name": name or os.path.basename(abs_path),
            "version": version or "",
            "installer_type": installer_type or "",
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "size": size,
        }
        save_backup_index(idx)
    except Exception:
        pass


def find_backup_installer(pid: str):
    """按 winget id 找一个已下载且仍存在的安装包。返回 dict 或 None。"""
    if not pid:
        return None
    rec = load_backup_index().get(pid)
    if not rec:
        return None
    p = rec.get("path") or ""
    if p and os.path.isfile(p):
        rec["path"] = p
        return rec
    return None


def _silent_args_for(installer_type: str, installer_abs: str, loc: str):
    """
    根据 winget 的 InstallerType 推断静默安装命令。
    这正好用上了「安装器格式」那套分组信息 —— 下载时记住了类型，
    安装时就能用对应的静默参数，而不是一律 /S（对 Inno / MSI 全都不对）。
    """
    t = (installer_type or "").lower()
    ext = os.path.splitext(installer_abs)[1].lower()
    if ext == ".msi" or t in ("msi", "wix", "msp"):
        return f'msiexec /i "{installer_abs}" /qn /norestart INSTALLDIR="{loc}"'
    if t == "inno":
        return f'"{installer_abs}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
    if t == "burn":
        return f'"{installer_abs}" /quiet /norestart'
    # nullsoft / exe / 未知 → NSIS 风格
    return f'"{installer_abs}" /S'


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(obj: dict) -> bool:
    """
    保存设置。采用【合并】而非覆盖 —— 前端 persistSettings 只提交它管理的字段，
    若整体覆盖会把后端自己维护的 windowRect（窗口位置/大小记忆）抹掉。
    """
    try:
        cur = load_settings()
        if not isinstance(cur, dict):
            cur = {}
        if isinstance(obj, dict):
            cur.update(obj)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def load_links() -> dict:
    """已链接的本地安装包：{winget_id: {path, args}}。"""
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_links(obj: dict) -> bool:
    try:
        with open(LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(obj or {}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


SELECTION_FILE = os.path.join(BASE, "selection.json")


def load_selection() -> list:
    """勾选记忆：返回上次勾选的 winget id 列表。"""
    try:
        with open(SELECTION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def save_selection(ids: list) -> bool:
    try:
        with open(SELECTION_FILE, "w", encoding="utf-8") as f:
            json.dump(list(ids or []), f, ensure_ascii=False)
        return True
    except Exception:
        return False


def file_exists(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except Exception:
        return False


def get_system_theme() -> str:
    """
    读取 Windows 系统主题（深色/浅色）。
    WebView2 的 prefers-color-scheme 在 pywebview 里不可靠（常常固定返回 light），
    所以直接读注册表 AppsUseLightTheme：0 = 深色，1 = 浅色。
    """
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return "light" if int(val) else "dark"
    except Exception:
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                val, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
                return "light" if int(val) else "dark"
        except Exception:
            return "dark"


def save_text_file(path: str, content: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        return True
    except Exception:
        return False


def _run_local_installer(cmd: str, log_fn=None, timeout=900):
    """运行一个本地安装包命令（msiexec 或 setup.exe），shell=True。返回 (rc, out)。"""
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        if log_fn:
            log_fn(f"[错误] 无法启动安装程序: {e}")
        return -1, ""
    out = []
    if proc.stdout:
        for line in proc.stdout:
            line = line.rstrip("\n").rstrip("\r")
            if line.strip():
                out.append(line)
                if log_fn:
                    log_fn(line)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        if log_fn:
            log_fn("[错误] 安装执行超时")
        return -2, ""
    return proc.returncode, "\n".join(out)


def _stage_linked_installer(src: str, backup_root: str, d: str):
    """把链接的安装包放进 Backups/<d>/，返回其相对 backup_root 的路径。
    若源已在 backup_root 内则直接复用（不重复复制）。"""
    backup_root = os.path.abspath(backup_root)
    abs_src = os.path.abspath(src)
    try:
        rel = os.path.relpath(abs_src, backup_root)
        if not rel.startswith(".."):
            return rel
    except Exception:
        pass
    dst_dir = os.path.join(backup_root, d)
    os.makedirs(dst_dir, exist_ok=True)
    base = os.path.basename(src)
    dst = os.path.join(dst_dir, base)
    if os.path.abspath(dst) != abs_src:
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            _log(f"  ⚠ 复制安装包失败: {e}")
            return os.path.relpath(abs_src, backup_root)
    return os.path.relpath(dst, backup_root)


def _resolve_local_cmd(template: str, installer_abs: str, loc: str) -> str:
    """把模板里的 {path}/{loc} 占位符替换成实际路径。"""
    return (template or "").replace("{path}", installer_abs).replace("{loc}", loc)



def _log(msg: str):
    with STATE_LOCK:
        STATE["log"].append(msg)
        if len(STATE["log"]) > 400:
            STATE["log"] = STATE["log"][-400:]


def _run_winget(args, log_fn=None, timeout=600):
    """运行 winget，逐行回传输出，返回 returncode。"""
    cmd = [WINGET] + args
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        if log_fn:
            log_fn(f"[错误] 无法启动 winget: {e}")
        return -1, ""

    out_lines = []
    if proc.stdout:
        for line in proc.stdout:
            line = line.rstrip("\n").rstrip("\r")
            if line.strip():
                out_lines.append(line)
                if log_fn:
                    log_fn(line)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        if log_fn:
            log_fn("[错误] winget 执行超时")
        return -2, ""
    return proc.returncode, "\n".join(out_lines)


# ----------------------------------------------------------------------------
# 读取已安装应用
# ----------------------------------------------------------------------------

# 注册表里能拿到的额外信息：安装时间 / 安装目录 / 发布者 / 真实版本号。
# winget list 只给 Name/Id/Version/Available/Source，没有安装日期，
# 所以这里单独扫一遍 Uninstall 键，再按名字合并回去。
_REG_UNINSTALL_ROOTS = [
    (winreg.HKEY_LOCAL_MACHINE if _HAS_WINREG else None,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE if _HAS_WINREG else None,
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER if _HAS_WINREG else None,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

_REG_META_CACHE = {"data": None, "ts": 0}


def _dir_from_uninstall(uninstall: str) -> str:
    """
    从 UninstallString 反推安装目录：
      "D:\\Programs\\Arduino IDE\\Uninstall Arduino IDE.exe" /currentuser
      -> "D:\\Programs\\Arduino IDE"
    """
    s = (uninstall or "").strip()
    if not s:
        return ""
    # 去掉开关参数
    s = re.split(r"\s+/", s, maxsplit=1)[0].strip().strip('"')
    if not s:
        return ""
    # 去掉末尾的卸载程序文件名
    base = os.path.basename(s)
    low = base.lower()
    if low.startswith("uninstall") or low in ("uninst.exe", "uninstaller.exe"):
        parent = os.path.dirname(s)
        return parent if parent else ""
    # 有些直接指向安装目录下的 msi / exe
    if low.endswith(".msi"):
        parent = os.path.dirname(s)
        return parent if parent else ""
    return ""


def _norm_key(s: str) -> str:
    """把软件名归一化成可比较的 key：去空格、去版本号后缀、大小写无关。"""
    s = (s or "").strip().lower()
    s = re.sub(r"\s+v?\d+(\.\d+)*$", "", s)      # "Arduino IDE 2.3.10" -> "arduino ide"
    s = re.sub(r"\s*\(\s*\d+\s*[- ]?bit\s*\)\s*$", "", s)   # "(64-bit)"
    s = re.sub(r"[\s\-_·．\.、]+", "", s)
    return s


def _norm_key_full(s: str) -> str:
    """保留版本号的完整 key，用于优先精确匹配。"""
    s = (s or "").strip().lower()
    s = re.sub(r"[\s\-_·．\.、]+", "", s)
    return s


def _parse_install_date(raw):
    """
    注册表 InstallDate 有两种格式：
      - "20261011"   8 位，YYYYMMDD
      - "1778900884" 10 位，Unix 时间戳
    返回 ("YYYY-MM-DD", Unix 时间戳)，解析不出来返回 (None, None)。
    ⚠ 两种格式必须统一换算成 Unix 时间戳，否则排序时量纲不一致会排乱。
    """
    s = str(raw or "").strip()
    if not s:
        return (None, None)
    try:
        if len(s) == 8 and s.isdigit():
            y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:8])
            if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                ts = int(time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1)))
                return (f"{y:04d}-{mo:02d}-{d:02d}", ts)
        if len(s) >= 9 and s.isdigit():
            ts = int(s)
            if 946684800 < ts < 4102444800:      # 2000-01-01 ~ 2100-01-01
                t = time.localtime(ts)
                return (time.strftime("%Y-%m-%d", t), ts)
    except Exception:
        pass
    return (None, None)


def _scan_registry_meta(force: bool = False):
    """
    扫描注册表 Uninstall 键，收集每个软件的：
        安装日期 / 安装时间戳 / 安装目录 / 发布者 / 精确版本号
    返回 {"exact": {完整key: meta}, "loose": {去版本key: meta}}
    结果缓存 60 秒，避免每次刷新都重扫。
    """
    if not _HAS_WINREG:
        return {"exact": {}, "loose": {}}

    now = time.time()
    if (not force and _REG_META_CACHE["data"] is not None
            and now - _REG_META_CACHE["ts"] < 60):
        return _REG_META_CACHE["data"]

    exact, loose, by_guid = {}, {}, {}
    for hk, path in _REG_UNINSTALL_ROOTS:
        if hk is None:
            continue
        try:
            root = winreg.OpenKey(hk, path)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(root)[0]
        except OSError:
            continue
        for i in range(count):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                continue
            try:
                k = winreg.OpenKey(root, sub)
            except OSError:
                continue

            def _q(name, k=k):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return ""

            display = str(_q("DisplayName") or "").strip()
            if not display:
                continue
            # 跳过更新器/卸载器之类的噪声条目
            if str(_q("SystemComponent") or "") == "1":
                continue

            date_str, date_ts = _parse_install_date(_q("InstallDate"))
            location = str(_q("InstallLocation") or "").strip()
            publisher = str(_q("Publisher") or "").strip()
            rversion = str(_q("DisplayVersion") or "").strip()

            # 有些条目没写 InstallLocation，但 UninstallString 里带着安装目录
            if not location:
                location = _dir_from_uninstall(str(_q("UninstallString") or ""))

            # 有些条目没写 InstallDate，但有安装目录 —— 用目录的创建时间兜底
            if not date_str and location and os.path.isdir(location):
                try:
                    st = os.stat(location)
                    ct = getattr(st, "st_birthtime", st.st_ctime)
                    t = time.localtime(ct)
                    date_str = time.strftime("%Y-%m-%d", t)
                    date_ts = int(ct)
                except Exception:
                    pass

            meta = {"installed_at": date_str, "installed_ts": date_ts,
                    "location": location, "publisher": publisher,
                    "reg_version": rversion}

            # 注册表子键名就是 ARP 的产品代码，winget 的 ARP id 里带着它
            # （形如 ARP\Machine\X64\{GUID}），这是最可靠的匹配方式。
            guid = sub.split("_")[0].strip().upper()
            if guid.startswith("{") and guid not in by_guid:
                by_guid[guid] = meta

            fk = _norm_key_full(display)
            lk = _norm_key(display)
            if fk and fk not in exact:
                exact[fk] = meta
            if lk and lk not in loose:
                loose[lk] = meta

    data = {"exact": exact, "loose": loose, "by_guid": by_guid}
    _REG_META_CACHE["data"] = data
    _REG_META_CACHE["ts"] = now
    return data


def _match_registry_meta(name: str, meta_db, wid: str = ""):
    """按 id / 名字在注册表元数据里找对应条目。"""
    if not meta_db:
        return None
    # 1) ARP id 里带产品代码（GUID），这是最可靠的匹配方式。
    #    注意 winget 偶尔会把 GUID 末位截断（如 ...19D6 vs 注册表 ...19D6B），
    #    所以用「前缀包含」而不是全等。
    if wid:
        m = re.search(r"\{[0-9A-Fa-f-]+\}", wid or "")
        if m:
            g = m.group(0).upper()
            for bg, meta in meta_db.get("by_guid", {}).items():
                if bg == g or bg.startswith(g) or g.startswith(bg):
                    return meta
    if not name:
        return None
    # 2) 名字精确匹配
    fk = _norm_key_full(name)
    lk = _norm_key(name)
    if fk in meta_db["exact"]:
        return meta_db["exact"][fk]
    if lk in meta_db["loose"]:
        return meta_db["loose"][lk]
    # 3) 再试一次：winget 名字带版本、注册表不带（或反过来）
    fk2 = _norm_key_full(_norm_key(name))
    if fk2 in meta_db["exact"]:
        return meta_db["exact"][fk2]
    if fk2 in meta_db["loose"]:
        return meta_db["loose"][fk2]
    return None


def _msix_install_dir(wid: str) -> str:
    """
    MSIX 应用的 winget id 形如
      MSIX\\Microsoft.AV1VideoExtension_2.0.35.0_x64__8wekyb3d8bbwe
    对应 WindowsApps 下的目录名（去掉 "MSIX\\" 前缀）。
    WindowsApps 目录本身不能列，但可以直接 stat 已知的完整路径。
    """
    if not wid or not wid.upper().startswith("MSIX\\"):
        return ""
    pkg = wid.split("\\", 1)[1]
    if not pkg:
        return ""
    for root in (r"C:\Program Files\WindowsApps",
                 os.path.expandvars(r"%LOCALAPPDATA%\Packages")):
        p = os.path.join(root, pkg)
        if os.path.isdir(p):
            return p
    return ""


def _dir_birth_date(path: str):
    """取目录的创建时间，返回 (YYYY-MM-DD, 时间戳)；拿不到返回 (None, None)。"""
    if not path or not os.path.isdir(path):
        return (None, None)
    try:
        st = os.stat(path)
        ct = getattr(st, "st_birthtime", st.st_ctime)
        return (time.strftime("%Y-%m-%d", time.localtime(ct)), int(ct))
    except Exception:
        return (None, None)


def _parse_winget_list(text: str):
    lines = text.splitlines()
    header_idx = None
    header = ""
    for i, line in enumerate(lines):
        low = line.lower()
        if "name" in low and "id" in low and "version" in low and "source" in low:
            header_idx = i
            header = line
            break
    if header_idx is None:
        return []

    try:
        id_pos = header.index("Id")
        ver_pos = header.index("Version")
        avail_pos = header.index("Available")
        src_pos = header.index("Source")
    except ValueError:
        return []

    apps = {}
    for line in lines[header_idx + 1:]:
        if not line.strip() or set(line.strip()) <= set("- "):
            continue
        if len(line) < id_pos + 2:
            continue
        name = line[:id_pos].strip()
        pid = line[id_pos:ver_pos].strip()
        version = line[ver_pos:avail_pos].strip()
        available = line[avail_pos:src_pos].strip()
        source = line[src_pos:].strip()
        if not pid or not name:
            continue
        is_winget = (source.lower() == "winget")
        # 去重：优先保留 winget 源
        if pid in apps:
            if is_winget and not apps[pid]["winget"]:
                apps[pid] = {"name": name, "id": pid, "version": version,
                             "available": available, "source": source, "winget": is_winget}
            continue
        apps[pid] = {"name": name, "id": pid, "version": version,
                     "available": available, "source": source, "winget": is_winget}

    result = list(apps.values())
    result.sort(key=lambda a: a["name"].lower())
    return result


def _enrich_with_registry(apps):
    """把注册表里的安装时间 / 安装目录 / 发布者合并到应用列表上。"""
    meta_db = _scan_registry_meta()
    for a in apps:
        wid = a.get("id", "")
        m = _match_registry_meta(a.get("name", ""), meta_db, wid)
        if m:
            a["installed_at"] = m.get("installed_at") or ""
            a["installed_ts"] = m.get("installed_ts") or 0
            a["install_location"] = m.get("location") or ""
            a["publisher"] = m.get("publisher") or ""
            # 注册表版本比 winget 显示的更精确时用它（winget 对非 winget 源常显示 Unknown）
            rv = m.get("reg_version") or ""
            if rv and (not a.get("version") or a.get("version") == "Unknown"):
                a["version"] = rv
            continue

        # 注册表里没有的，试试 MSIX 包目录的创建时间
        a["installed_at"] = ""
        a["installed_ts"] = 0
        a["install_location"] = ""
        a["publisher"] = ""
        msix_dir = _msix_install_dir(wid)
        if msix_dir:
            d, ts = _dir_birth_date(msix_dir)
            a["installed_at"] = d or ""
            a["installed_ts"] = ts or 0
            a["install_location"] = msix_dir
    return apps


def get_installed_apps():
    """调用 winget list 并解析。"""
    _log("[信息] 正在读取已安装应用，请稍候（首次可能较慢）...")
    rc, out = _run_winget(
        ["list", "--accept-source-agreements", "--disable-interactivity"],
        log_fn=_log, timeout=180,
    )
    if isinstance(rc, tuple):
        rc = rc[0]
    apps = _parse_winget_list(out if isinstance(out, str) else "")
    apps = _enrich_with_registry(apps)
    dated = sum(1 for a in apps if a.get("installed_at"))
    _log(f"[信息] winget list 返回码: {rc}")
    _log(f"[完成] 共发现 {len(apps)} 个应用，其中 {sum(1 for a in apps if a['winget'])} 个支持自动下载/安装，"
         f"{dated} 个能读到安装时间。")
    return apps


# ----------------------------------------------------------------------------
# 下载安装包
# ----------------------------------------------------------------------------

def _find_installer(directory: str):
    files = []
    try:
        for f in os.listdir(directory):
            fp = os.path.join(directory, f)
            if os.path.isfile(fp) and f.lower().endswith(INSTALLER_EXTS):
                files.append(fp)
    except Exception:
        pass
    files.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return files[0] if files else None


def _is_no_such_installer(out: str) -> bool:
    """winget 输出里表示「该包没有这种安装器类型」的信号。"""
    low = (out or "").lower()
    return ("no applicable installer" in low
            or "no installer found" in low
            or "installer type" in low and "not found" in low)


def _download_one(pid: str, out_dir: str, mode: str, log_fn=None):
    """
    下载单个软件的安装包，自动在多种安装器类型之间挑选。
    返回 (rc, out, installer_path, used_type)
    used_type 为空字符串表示「未筛选类型，下载了全部变体」。
    """
    base = ["download", "--id", pid, "-e", "-d", out_dir,
            "--accept-source-agreements", "--disable-interactivity"]
    cands = _installer_candidates(mode)

    # ---- 不筛选类型：保持原来的行为，一次下完所有变体 ----
    if not cands:
        rc, out = _run_winget(base, log_fn=log_fn, timeout=600)
        return rc, out, _find_installer(out_dir), ""

    # ---- 有缓存：这个包上次就是用这种类型下的，直接用 ----
    cached = _TYPE_CACHE.get(pid)
    if cached and cached in cands:
        cands = [cached] + [c for c in cands if c != cached]

    last_out = ""
    for t in cands:
        label = INSTALLER_TYPE_LABEL.get(t, t)
        if log_fn:
            log_fn(f"  ↳ 尝试安装器类型: {t}（{label}）")
        rc, out = _run_winget(base + ["--installer-type", t],
                              log_fn=log_fn, timeout=600)
        last_out = out or ""

        if rc == 0:
            ins = _find_installer(out_dir)
            if ins:
                _TYPE_CACHE[pid] = t
                return rc, out, ins, t
            # 命令成功但没产出文件（例如已是最新版本被跳过），换下一种类型
            if log_fn:
                log_fn(f"  ↳ {label} 没有产出文件，换下一种")
            continue

        if _is_no_such_installer(last_out):
            if log_fn:
                log_fn(f"  ↳ 该软件没有「{label}」，自动换下一种")
            continue

        # 需要指定架构的包
        if "architecture" in last_out.lower() or "multiple" in last_out.lower():
            if log_fn:
                log_fn("  ↳ 尝试指定架构 x64 重试...")
            rc2, out2 = _run_winget(base + ["--installer-type", t, "-a", "x64"],
                                     log_fn=log_fn, timeout=600)
            if rc2 == 0:
                ins = _find_installer(out_dir)
                if ins:
                    _TYPE_CACHE[pid] = t
                    return rc2, out2, ins, t
                last_out = out2 or last_out
                continue
            last_out = out2 or last_out
            if _is_no_such_installer(last_out):
                continue

        # 网络 / 校验等其它错误：换一种类型再试一次（不同类型可能走不同 CDN）
        if log_fn:
            log_fn(f"  ↳ {label} 下载失败，换下一种类型再试")

    # 全部类型都试过仍失败：用户若是「全部变体」档位，就退回老行为兜底
    if (mode or "auto").strip().lower() == "all":
        if log_fn:
            log_fn("  ↳ 退回下载全部变体")
        rc, out = _run_winget(base, log_fn=log_fn, timeout=600)
        return rc, out, _find_installer(out_dir), ""

    return 1, last_out, None, ""


def download_apps(payload, backup_root, installer_type="auto"):
    """
    payload: [{name,id,version,source,winget,local?}]
    backup_root: 安装包保存根目录
    installer_type: 安装器类型档位 auto/exe/msi/zip/all
    若 app.local 存在（已链接本地安装包），优先使用它、不再去 winget 下载。
    """
    apps = [a for a in payload if a.get("winget") or a.get("local")]
    with STATE_LOCK:
        STATE["running"] = True
        STATE["phase"] = "download"
        STATE["total"] = len(apps)
        STATE["done"] = 0
        STATE["current"] = ""
        STATE["current_id"] = ""
        STATE["results"] = {}

    os.makedirs(backup_root, exist_ok=True)
    manifest = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backup_root": backup_root,
                "installer_type": installer_type or "auto",
                "apps": []}

    for idx, app in enumerate(apps, 1):
        pid = app["id"]
        name = app["name"]
        d = dir_key(name)
        out_dir = os.path.join(backup_root, d)
        os.makedirs(out_dir, exist_ok=True)
        with STATE_LOCK:
            STATE["current"] = f"({idx}/{len(apps)}) {name}"
            STATE["current_id"] = pid
        _log(f"▼ 处理 [{idx}/{len(apps)}] {name}  ({pid})")

        local = app.get("local")
        if local and local.get("path"):
            # 已链接本地安装包：不再下载，直接归入备份目录
            rel = _stage_linked_installer(local["path"], backup_root, d)
            installer = os.path.join(backup_root, rel)
            status, detail, used_type = "完成", rel, ""
            _log(f"  ✔ 已使用本地安装包: {rel}")
        else:
            rc, out, installer, used_type = _download_one(
                pid, out_dir, installer_type, log_fn=_log)
            status, detail = "失败", ""
            if rc == 0:
                if installer:
                    status = "完成"
                    detail = os.path.relpath(installer, backup_root)
                    _log(f"  ✔ 已保存: {detail}"
                         + (f"（{INSTALLER_TYPE_LABEL.get(used_type, used_type)}）"
                            if used_type else ""))
                else:
                    status = "完成(无文件)"
                    detail = "未识别到安装包文件"
                    _log("  ⚠ 命令成功但未找到安装包文件")
            else:
                detail = "下载失败，详见日志"
                _log(f"  ✘ 下载失败 (rc={rc})")

        with STATE_LOCK:
            STATE["results"][pid] = {"status": status, "detail": detail}
            STATE["done"] = idx
        manifest["apps"].append({
            "name": name, "id": pid, "version": app.get("version", ""),
            "dir": d, "installer": detail if installer else "",
            "installer_type": used_type,
        })
        # 记进索引：之后「一键安装 / 导出重装脚本」会优先用这个本地包
        if installer and os.path.isfile(installer):
            record_backup_installer(pid, installer, app.get("version", ""),
                                    used_type, name)

    _write_manifest(backup_root, manifest)
    with STATE_LOCK:
        STATE["running"] = False
        STATE["current"] = "下载完成"
    _log("[完成] 全部下载任务结束。")
    return manifest


# ----------------------------------------------------------------------------
# 一键安装（本机 / 重装后）
# ----------------------------------------------------------------------------

def _install_archive(src: str, loc: str, log_fn=None):
    """把压缩包解压/复制到目标文件夹。返回 (status, detail)。"""
    src_abs = os.path.abspath(src)
    ext = os.path.splitext(src_abs)[1].lower()
    try:
        if ext == ".zip":
            import zipfile
            with zipfile.ZipFile(src_abs, "r") as z:
                z.extractall(loc)
            if log_fn:
                log_fn(f"  ✔ 已解压 zip 到: {loc}")
            return "完成", f"已解压到 {loc}"
        if ext in (".tar", ".tgz", ".tar.gz"):
            import tarfile
            with tarfile.open(src_abs, "r:*") as t:
                t.extractall(loc)
            if log_fn:
                log_fn(f"  ✔ 已解压 tar 到: {loc}")
            return "完成", f"已解压到 {loc}"
        # 其它格式：尝试 7z
        seven = _find_7z()
        if seven:
            rc, _ = _run_local_installer(
                f'"{seven}" x "{src_abs}" -o"{loc}" -y', log_fn=log_fn, timeout=600)
            if rc == 0:
                return "完成", f"已解压到 {loc}"
        # 兜底：复制并提示手动解压
        shutil.copy2(src_abs, os.path.join(loc, os.path.basename(src_abs)))
        if log_fn:
            log_fn(f"  ⚠ 无法自动解压 {ext}，已复制压缩包到: {loc}（需手动解压）")
        return "完成", "已复制，压缩包需手动解压"
    except Exception as e:
        if log_fn:
            log_fn(f"  ✘ 解压失败: {e}")
        return "失败", f"解压失败: {e}"


def _winget_type_arg(mode: str):
    """给 winget install 用的 --installer-type 参数（只有精确类型才能加）。"""
    m = (mode or "auto").strip().lower()
    if m == "msi":
        return ["--installer-type", "msi"]
    return []


def install_apps(payload, install_root, installer_type="auto"):
    apps = [a for a in payload if a.get("winget") or a.get("local")]
    with STATE_LOCK:
        STATE["running"] = True
        STATE["phase"] = "install"
        STATE["total"] = len(apps)
        STATE["done"] = 0
        STATE["current"] = ""
        STATE["current_id"] = ""
        STATE["results"] = {}

    os.makedirs(install_root, exist_ok=True)
    for idx, app in enumerate(apps, 1):
        pid = app["id"]
        name = app["name"]
        # 统一用 dir_key 生成子目录名，保证「安装」与「导出脚本」指向同一文件夹
        if GLOBAL["flat"]:
            loc = install_root
        else:
            loc = os.path.join(install_root, dir_key(name))
        os.makedirs(loc, exist_ok=True)
        with STATE_LOCK:
            STATE["current"] = f"({idx}/{len(apps)}) {name}"
            STATE["current_id"] = pid
        _log(f"▼ 安装 [{idx}/{len(apps)}] {name}  ->  {loc}")

        local = app.get("local")
        inst_path = (local or {}).get("path")

        # 没手动链接本地包时，看看「下载所选安装包」是不是已经下过了。
        # 有就直接跑本地文件（离线、快），没有才去 winget 在线装。
        bk = None
        if not inst_path:
            bk = find_backup_installer(pid)

        if inst_path and _is_archive(os.path.abspath(inst_path)):
            status, detail = _install_archive(inst_path, loc, log_fn=_log)
        elif inst_path:
            installer_abs = os.path.abspath(inst_path)
            resolved = _resolve_local_cmd(local.get("args", ""), installer_abs, loc)
            _log(f"  ▶ 运行本地安装包: {resolved}")
            rc, _ = _run_local_installer(resolved, log_fn=_log, timeout=900)
            status = "完成" if rc == 0 else "失败"
            detail = "已安装" if rc == 0 else "安装失败，详见日志"
        elif bk:
            installer_abs = bk["path"]
            if _is_archive(installer_abs):
                status, detail = _install_archive(installer_abs, loc, log_fn=_log)
            else:
                resolved = _silent_args_for(bk.get("installer_type", ""),
                                            installer_abs, loc)
                _log(f"  ▶ 使用已下载的安装包（离线）: {os.path.basename(installer_abs)}")
                _log(f"  ▶ {resolved}")
                rc, _ = _run_local_installer(resolved, log_fn=_log, timeout=900)
                status = "完成" if rc == 0 else "失败"
                detail = ("已安装（用本地已下载的安装包）" if rc == 0
                          else "安装失败，详见日志")
        else:
            args = ["install", "-e", "--id", pid, "-h",
                    "--accept-package-agreements", "--accept-source-agreements",
                    "--disable-interactivity", "-l", loc]
            args += _winget_type_arg(installer_type)
            rc, _ = _run_winget(args, log_fn=_log, timeout=900)
            status = "完成" if rc == 0 else "失败"
            detail = "已安装" if rc == 0 else "安装失败，详见日志"

        if status == "完成":
            _log(f"  ✔ {name} {detail}")
        else:
            _log(f"  ✘ {name} {detail}")

        with STATE_LOCK:
            STATE["results"][pid] = {"status": status, "detail": detail}
            STATE["done"] = idx

    with STATE_LOCK:
        STATE["running"] = False
        STATE["current"] = "安装完成"
    _log("[完成] 全部安装任务结束。")
    return True


# ----------------------------------------------------------------------------
# 导出清单 + 重装脚本
# ----------------------------------------------------------------------------

def _write_manifest(backup_root, manifest):
    try:
        with open(os.path.join(backup_root, "manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"[警告] 写入 manifest.json 失败: {e}")


def export_reinstall_script(payload, backup_root, install_root, installer_type="auto"):
    apps = [a for a in payload if a.get("winget") or a.get("local")]
    manifest = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backup_root": backup_root, "install_root": install_root,
                "installer_type": installer_type or "auto", "apps": []}
    os.makedirs(backup_root, exist_ok=True)

    lines = []
    lines.append("@echo off")
    lines.append("chcp 65001 >nul")
    lines.append("setlocal EnableDelayedExpansion")
    lines.append('set "ROOT=' + install_root.replace("/", "\\") + '"')
    lines.append('set "BACKUP=' + backup_root.replace("/", "\\") + '"')
    lines.append("echo ============================================")
    lines.append("echo    软件一键重装脚本 (由 软件备份管理器 生成)")
    lines.append("echo    安装目录: %ROOT%")
    lines.append("echo    建议：右键本文件 -> 以管理员身份运行")
    lines.append("echo ============================================")
    lines.append("echo.")

    # 统一的大文件夹下是否给每个软件建独立子目录
    flat = GLOBAL["flat"]

    # 把应用分成三类：
    #   local_apps  —— 用户手动链接了本地安装包
    #   backup_apps —— 「下载所选安装包」已经下过了（离线安装，脚本里直接跑本地文件）
    #   winget_apps —— 啥都没有，只能走 winget 在线装
    local_apps, backup_apps, winget_apps = [], [], []
    for a in apps:
        if a.get("local") and a["local"].get("path"):
            local_apps.append(a)
            continue
        bk = find_backup_installer(a.get("id", ""))
        if bk:
            a["_backup"] = bk
            backup_apps.append(a)
        elif a.get("winget"):
            winget_apps.append(a)

    for app in winget_apps:
        d = "" if flat else dir_key(app["name"])
        label = dir_key(app["id"])
        lines.append(f'call :install "{app["name"]}" "{d}" "{app["id"]}" {label}')

    # 2) 已链接本地安装包的应用：把安装包复制进备份目录（可离线），直接写死命令
    for app in local_apps:
        d = "" if flat else dir_key(app["name"])
        loc = install_root if flat else os.path.join(install_root, d)
        rel = _stage_linked_installer(app["local"]["path"], backup_root, d or "root")
        installer_abs = os.path.join(os.path.abspath(backup_root), rel)
        lines.append("echo.")
        if _is_zip(installer_abs):
            # zip：用系统自带的 Expand-Archive 自动解压到子文件夹
            target = "%ROOT%" if not d else f"%ROOT%\\{d}"
            lines.append(f'echo [解压] {app["name"]}  (zip 自动解压到 {target})')
            lines.append(f"powershell -NoProfile -Command \"Expand-Archive -Path '{installer_abs}' -DestinationPath '{loc}' -Force\"")
            lines.append("if errorlevel 1 (")
            lines.append(f'  echo [失败] {app["name"]} 解压失败，可手动解压 {installer_abs}')
            lines.append(") else (")
            lines.append(f'  echo [成功] {app["name"]}')
            lines.append(")")
        elif _is_archive(installer_abs):
            # 其它压缩包：只复制，提示手动解压
            lines.append(f'echo [归档] {app["name"]} 为压缩包，已复制到 {installer_abs}，请手动解压安装。')
        else:
            template = app["local"].get("args", "")
            resolved = _resolve_local_cmd(template, installer_abs, loc)
            lines.append(f'echo [安装] {app["name"]}  (本地安装包: {os.path.basename(installer_abs)})')
            lines.append(resolved)
            lines.append("if errorlevel 1 (")
            lines.append(f'  echo [失败] {app["name"]} 安装失败，可手动运行 {installer_abs}')
            lines.append(") else (")
            lines.append(f'  echo [成功] {app["name"]}')
            lines.append(")")

    # 3) 已下载过安装包的：直接用备份目录里的文件（离线，不需要 winget 源）
    for app in backup_apps:
        bk = app["_backup"]
        d = "" if flat else dir_key(app["name"])
        loc = install_root if flat else os.path.join(install_root, d)
        # 把下载好的包规整到备份目录的对应子文件夹里，脚本里写相对路径更稳
        rel = _stage_linked_installer(bk["path"], backup_root, d or "root")
        installer_abs = os.path.join(os.path.abspath(backup_root), rel)
        # 脚本里用 %BACKUP% 相对路径，备份目录挪走了也不影响
        rel_win = rel.replace("/", "\\")
        lines.append("echo.")
        if _is_zip(installer_abs):
            target = "%ROOT%" if not d else f"%ROOT%\\{d}"
            lines.append(f'echo [解压] {app["name"]}  (zip 自动解压到 {target})')
            lines.append(f"powershell -NoProfile -Command \"Expand-Archive -Path '%BACKUP%\\{rel_win}' -DestinationPath '{loc}' -Force\"")
            lines.append("if errorlevel 1 (")
            lines.append(f'  echo [失败] {app["name"]} 解压失败，可手动解压 %BACKUP%\\{rel_win}')
            lines.append(") else (")
            lines.append(f'  echo [成功] {app["name"]}')
            lines.append(")")
        elif _is_archive(installer_abs):
            lines.append(f'echo [归档] {app["name"]} 为压缩包，已复制到 %BACKUP%\\{rel_win}，请手动解压安装。')
        else:
            itype = bk.get("installer_type", "")
            # 按下载时记住的安装器类型给对应的静默参数
            if (itype or "").lower() in ("msi", "wix", "msp") or \
                    installer_abs.lower().endswith(".msi"):
                cmd = (f'msiexec /i "%BACKUP%\\{rel_win}" /qn /norestart '
                       f'INSTALLDIR="{loc}"')
            elif (itype or "").lower() == "inno":
                cmd = f'"%BACKUP%\\{rel_win}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
            elif (itype or "").lower() == "burn":
                cmd = f'"%BACKUP%\\{rel_win}" /quiet /norestart'
            else:
                cmd = f'"%BACKUP%\\{rel_win}" /S'
            lines.append(f'echo [安装] {app["name"]}  (用已下载的安装包，离线)')
            lines.append(cmd)
            lines.append("if errorlevel 1 (")
            lines.append(f'  echo [失败] {app["name"]} 安装失败，可手动运行 %BACKUP%\\{rel_win}')
            lines.append(") else (")
            lines.append(f'  echo [成功] {app["name"]}')
            lines.append(")")

    lines.append("echo.")
    lines.append("echo 重装脚本执行完毕。失败项见上方 [失败] 标记。")
    lines.append("echo 若需离线安装，可手动运行 %BACKUP% 下对应软件文件夹中的安装包。")
    lines.append("pause")
    lines.append("goto :eof")

    # :install 子程序只在还有 winget 在线安装项时才生成（否则是死代码）
    if winget_apps:
        lines.append("")
        lines.append(":install")
        lines.append('set "NAME=%~1"')
        lines.append('set "DIR=%~2"')
        lines.append('set "ID=%~3"')
        lines.append('set "LABEL=%~4"')
        lines.append('echo.')
        lines.append('echo [安装] %NAME%  (id=%ID%, 目录=%DIR%)')
        # 用户指定了精确安装器类型时，脚本里也锁死同一种，保证与下载的一致
        type_arg = _winget_type_arg(installer_type)
        type_flag = (" --installer-type " + type_arg[1]) if type_arg else ""
        if type_arg:
            lines.append('echo [提示] %NAME% 锁定安装器类型: ' + type_arg[1])
        lines.append('if "%DIR%"=="" (')
        lines.append('  winget install -e --id %ID% -h --accept-package-agreements --accept-source-agreements --disable-interactivity' + type_flag + ' -l "%ROOT%"')
        lines.append(") else (")
        lines.append('  winget install -e --id %ID% -h --accept-package-agreements --accept-source-agreements --disable-interactivity' + type_flag + ' -l "%ROOT%\\%DIR%"')
        lines.append(")")
        lines.append("if errorlevel 1 (")
        lines.append('  echo [失败] %NAME% 安装失败，请手动处理。')
        lines.append(") else (")
        lines.append('  echo [成功] %NAME%')
        lines.append(")")
        lines.append("goto :eof")

    bat_path = os.path.join(backup_root, "重装全部.bat")
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    for app in winget_apps:
        manifest["apps"].append({
            "name": app["name"], "id": app["id"],
            "version": app.get("version", ""), "dir": "" if flat else dir_key(app["name"]),
            "source": "winget",
        })
    for app in backup_apps:
        d = "" if flat else dir_key(app["name"])
        bk = app["_backup"]
        rel = _stage_linked_installer(bk["path"], backup_root, d or "root")
        manifest["apps"].append({
            "name": app["name"], "id": app.get("id", ""),
            "version": app.get("version", ""), "dir": d,
            "source": "backup", "local": rel,
            "installer_type": bk.get("installer_type", ""),
        })
    for app in local_apps:
        d = "" if flat else dir_key(app["name"])
        rel = _stage_linked_installer(app["local"]["path"], backup_root, d or "root")
        manifest["apps"].append({
            "name": app["name"], "id": app.get("id", ""),
            "version": app.get("version", ""), "dir": d,
            "source": "local", "local": rel,
        })
    _write_manifest(backup_root, manifest)
    _log(f"[导出] 重装脚本已生成: {bat_path}")
    _log(f"[导出] 内含 {len(winget_apps)} 个 winget 在线安装 + "
         f"{len(backup_apps)} 个用已下载的安装包（离线）+ "
         f"{len(local_apps)} 个手动链接的本地安装包，"
         f"将安装到 {install_root}" + ("" if flat else "\\<软件名>\\"))
    return bat_path


# ----------------------------------------------------------------------------
# pywebview API
# ----------------------------------------------------------------------------

class Api:
    def get_defaults(self):
        s = load_settings()
        return {
            "backup": s.get("backupDir") or DEFAULT_BACKUP,
            "install": s.get("installDir") or DEFAULT_INSTALL,
            "winget": WINGET,
            "admin": is_admin(),
            "installerType": s.get("installerType") or "auto",
        }

    # ---------- 全局设置 / 提权 ----------
    def apply_global(self, obj):
        """前端「总设置」面板写入：{flat: bool}。"""
        try:
            if isinstance(obj, dict) and "flat" in obj:
                GLOBAL["flat"] = bool(obj["flat"])
        except Exception:
            pass
        return True

    def elevate(self):
        """以管理员身份重启本程序（UAC 提权），原窗口随后关闭。"""
        try:
            import ctypes
            if getattr(sys, "frozen", False):
                exe = sys.executable
                params = ""
                cwd = os.path.dirname(os.path.abspath(sys.executable))
            else:
                # 用 pythonw.exe（无控制台版本），否则提权后又会闪一个黑框
                exe = sys.executable
                base, ext = os.path.splitext(exe)
                if os.path.basename(exe).lower().startswith("python"):
                    w = base + "w.exe"
                    if os.path.exists(w):
                        exe = w
                params = f'"{os.path.abspath(__file__)}"'
                cwd = HERE
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", exe, params, cwd, 1)
            return {"ok": ret > 32}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 外观 / 背景 ----------
    def get_settings(self):
        return load_settings()

    def get_system_theme(self):
        """系统当前深/浅色主题（跟随系统模式用）。"""
        return get_system_theme()

    def save_settings(self, obj):
        try:
            return save_settings(obj or {})
        except Exception:
            return False

    def encode_image(self, path):
        if not path:
            return ""
        try:
            return encode_image(path)
        except Exception:
            return ""

    def pick_image(self):
        if not _HAS_TK:
            return {"path": "", "dataUri": "", "error": "无 tkinter"}
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="选择背景图片",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp *.gif"),
                           ("所有文件", "*.*")])
            root.destroy()
            if not path:
                return {"path": "", "dataUri": ""}
            return {"path": path, "dataUri": encode_image(path)}
        except Exception as e:
            return {"path": "", "dataUri": "", "error": str(e)}

    def get_apps(self):
        return get_installed_apps()

    # ---------- 本地安装包链接 ----------
    def get_links(self):
        return load_links()

    def save_links(self, obj):
        try:
            return save_links(obj or {})
        except Exception:
            return False

    def pick_installer(self):
        if not _HAS_TK:
            return {"path": "", "error": "无 tkinter，无法选择文件"}
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="选择已有的安装包（将固定使用此版本）",
                filetypes=[("安装包", "*.exe *.msi *.msix *.msixbundle *.appx *.appxbundle *.cab"),
                           ("所有文件", "*.*")])
            root.destroy()
            if not path:
                return {"path": ""}
            return {"path": path, "name": os.path.basename(path),
                    "size": os.path.getsize(path)}
        except Exception as e:
            return {"path": "", "error": str(e)}

    def pick_folder(self, title="选择文件夹"):
        if not _HAS_TK:
            return ""
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title=title or "选择文件夹")
            root.destroy()
            return path or ""
        except Exception as e:
            return ""

    # ---------- 勾选记忆 ----------
    def load_selection(self):
        return load_selection()

    def save_selection(self, ids):
        try:
            return save_selection(ids or [])
        except Exception:
            return False

    # ---------- 批量链接：多选安装包 ----------
    def pick_installers(self):
        if not _HAS_TK:
            return []
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            files = filedialog.askopenfilenames(
                title="选择多个安装包（可批量链接到对应软件）",
                filetypes=[("安装包", "*.exe *.msi *.msix *.msixbundle *.appx *.appxbundle *.cab *.zip *.7z *.tar *.gz"),
                           ("所有文件", "*.*")])
            root.destroy()
            out = []
            for f in files:
                if f:
                    out.append({"path": f, "name": os.path.basename(f),
                                "size": os.path.getsize(f)})
            return out
        except Exception as e:
            return []

    # ---------- 失效链接检测 / 导出清单 ----------
    def file_exists(self, path):
        return file_exists(path)

    def check_links(self, paths):
        """批量检测本地安装包是否还存在（失效链接提示用）。"""
        out = {}
        for p in (paths or []):
            if p:
                out[p] = file_exists(p)
        return out

    def get_backup_index(self):
        """已下载安装包索引（供前端显示「已备份」徽标）。"""
        idx = load_backup_index()
        out = {}
        for pid, rec in (idx or {}).items():
            if not isinstance(rec, dict):
                continue
            p = rec.get("path") or ""
            out[pid] = {
                "path": p,
                "name": rec.get("name") or os.path.basename(p),
                "version": rec.get("version") or "",
                "installer_type": rec.get("installer_type") or "",
                "saved_at": rec.get("saved_at") or "",
                "size": rec.get("size") or 0,
                "exists": bool(p) and os.path.isfile(p),
            }
        return out

    def clear_backup_index(self):
        """清空已下载安装包索引。"""
        try:
            save_backup_index({})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pick_save_file(self, title="保存清单"):
        if not _HAS_TK:
            return {"path": ""}
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.asksaveasfilename(
                title=title or "保存清单",
                defaultextension=".txt",
                filetypes=[("文本清单", "*.txt"), ("CSV 表格", "*.csv"),
                           ("所有文件", "*.*")])
            root.destroy()
            return {"path": path or ""}
        except Exception as e:
            return {"path": ""}

    def save_text_file(self, path, content):
        try:
            return {"ok": save_text_file(path, content), "path": path}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def start_download(self, payload, backup_root, installer_type="auto"):
        if STATE["running"]:
            return {"ok": False, "msg": "已有任务在运行"}
        t = threading.Thread(target=download_apps,
                             args=(payload, backup_root, installer_type),
                             daemon=True)
        t.start()
        return {"ok": True}

    def start_install(self, payload, install_root, installer_type="auto"):
        if STATE["running"]:
            return {"ok": False, "msg": "已有任务在运行"}
        t = threading.Thread(target=install_apps,
                             args=(payload, install_root, installer_type),
                             daemon=True)
        t.start()
        return {"ok": True}

    def export_script(self, payload, backup_root, install_root, installer_type="auto"):
        try:
            p = export_reinstall_script(payload, backup_root, install_root,
                                        installer_type)
            return {"ok": True, "path": p}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_installer_types(self):
        """给前端渲染「安装器格式」下拉框。"""
        return {
            "modes": [
                {"value": "auto", "label": INSTALLER_TYPE_LABEL["auto"]},
                {"value": "msi",  "label": INSTALLER_TYPE_LABEL["msi"]},
                {"value": "zip",  "label": INSTALLER_TYPE_LABEL["zip"]},
                {"value": "all",  "label": INSTALLER_TYPE_LABEL["all"]},
            ],
            "current": (load_settings().get("installerType") or "auto"),
        }

    def get_progress(self):
        with STATE_LOCK:
            return {
                "running": STATE["running"],
                "phase": STATE["phase"],
                "total": STATE["total"],
                "done": STATE["done"],
                "current": STATE["current"],
                "current_id": STATE["current_id"],
                "log": STATE["log"][-120:],
                "results": STATE["results"],
            }

    def open_folder(self, path):
        try:
            os.startfile(path)
        except Exception:
            try:
                webbrowser.open(path)
            except Exception:
                pass
        return True

    def dir_exists(self, path):
        try:
            return os.path.isdir(path)
        except Exception:
            return False

    # ---------- 窗口控制 ----------
    def get_window_rect(self):
        """返回当前窗口的屏幕矩形（物理像素）。"""
        hwnd = _hwnd()
        r = _win_rect(hwnd)
        if not r:
            return {"rect": None}
        return {"rect": [r[0], r[1], r[2] - r[0], r[3] - r[1]],
                "dpi": _dpi_scale(hwnd)}

    def save_window_rect(self, rect):
        """记住窗口位置与大小（逻辑像素），下次启动时恢复。"""
        try:
            if not rect:
                return False
            s = load_settings()
            s["windowRect"] = [int(v) for v in rect]
            return save_settings(s)
        except Exception:
            return False

    def start_drag(self):
        """兼容旧接口：真正的拖动由前端 begin_drag 驱动。"""
        return self.begin_drag("move")

    def begin_drag(self, mode="move", grab_x=None, grab_y=None):
        """
        开始拖动/缩放窗口。
        mode: move | nw | n | ne | e | se | s | sw | w
        grab_x/grab_y: mousedown 时光标相对内容区的坐标，用于消除起手抖动。
        后端起线程轮询光标，鼠标左键松开即结束，前端无需每帧回调。
        """
        try:
            return _begin_window_drag(mode or "move", grab_x, grab_y)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def end_drag(self):
        try:
            return _end_window_drag()
        except Exception:
            return {"ok": False}

    def toggle_maximize(self):
        try:
            return {"ok": _toggle_maximize()}
        except Exception:
            return {"ok": False}

    def is_maximized(self):
        try:
            return {"maximized": _is_maximized()}
        except Exception:
            return {"maximized": False}

    def minimize(self):
        if webview is not None and webview.windows:
            webview.windows[0].minimize()

    def close_window(self):
        if webview is not None and webview.windows:
            webview.windows[0].destroy()


# ----------------------------------------------------------------------------
# 本地静态服务器（避免 file:// 的模块/CORS 限制）
# ----------------------------------------------------------------------------

class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB_DIR, **k)

    def log_message(self, *a, **k):
        pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    settings = load_settings()
    transparent = bool(settings.get("desktop_transparent", False))
    GLOBAL["flat"] = bool(settings.get("flat", False))

    port = _free_port()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    api = Api()

    # 恢复上次窗口位置与大小。
    # settings 里存的是【逻辑像素】（物理像素 / DPI 缩放比），
    # 这样换到不同缩放的显示器上也不会变形。
    wr = settings.get("windowRect")
    init_w, init_h, init_x, init_y = 1160, 800, None, None
    want_rect = None
    if isinstance(wr, list) and len(wr) == 4:
        try:
            # 存储顺序：[x, y, width, height]
            lx, ly, lw, lh = [int(v) for v in wr]
            init_w = max(MIN_W, min(lw, 4000))
            init_h = max(MIN_H, min(lh, 3000))
            init_x, init_y = lx, ly
            want_rect = (lx, ly, init_w, init_h)
        except Exception:
            pass

    webview.create_window(
        WINDOW_TITLE,
        url=f"http://127.0.0.1:{port}/",
        js_api=api,
        width=init_w, height=init_h,
        x=init_x, y=init_y,
        min_size=(MIN_W, MIN_H),
        frameless=True,
        easy_drag=False,          # 拖动自己实现（pywebview 6.x 没有 start_drag）
        on_top=False,
        background_color="#08090e",
        transparent=transparent,
    )

    # 窗口创建后：把缩放/最大化/位置记忆接上
    def _after_start():
        hwnd = 0
        for _ in range(60):
            hwnd = _hwnd()
            if hwnd:
                break
            time.sleep(0.1)



        # --- 精确还原窗口矩形 ---
        # 两个坑：
        #  1) pywebview 把 width/height 交给 WinForms 后会被系统边框吃掉一圈
        #     （1308x802 实际变成 1292x763），按传入值还原永远差一截；
        #  2) WinForms 首次显示时还会再做一次自动缩放/布局，把刚设好的矩形改掉。
        # 所以这里用 SetWindowPos 摆位，并且「设完 → 等 → 回读 → 不一致就再设」，
        # 最多纠 3 次，确保最终矩形和记录的一模一样。
        if hwnd and want_rect:
            try:
                time.sleep(1.0)          # 等 pywebview 完成首次布局
                sc = _dpi_scale(hwnd) or 1.0
                lx, ly, lw, lh = want_rect
                px, py = int(lx * sc), int(ly * sc)
                pw = max(int(MIN_W * sc), int(lw * sc))
                ph = max(int(MIN_H * sc), int(lh * sc))
                # 拔掉外接屏 / 改分辨率后，原位置可能已经不在任何屏幕上了
                if not _rect_visible(px, py, px + pw, py + ph):
                    px, py, pw, ph = _center_on_primary(px, py, pw, ph)
                for i in range(3):
                    _set_pos(hwnd, px, py, pw, ph)
                    time.sleep(0.35)
                    r = _win_rect(hwnd)
                    if not r:
                        break
                    if (r[0], r[1], r[2] - r[0], r[3] - r[1]) == (px, py, pw, ph):
                        break
                    _DBG("rect mismatch", i, r, "want", (px, py, pw, ph))
            except Exception:
                pass

        # 监听窗口移动/缩放，节流后写回设置
        last = [0.0]

        def _watch():
            while True:
                time.sleep(1.0)
                hwnd = _hwnd()
                if not hwnd:
                    continue
                if _MAX_STATE.get("maximized"):
                    continue
                r = _win_rect(hwnd)
                if not r:
                    continue
                now = time.time()
                if now - last[0] < 1.5:
                    continue
                last[0] = now
                try:
                    s = load_settings()
                    sc = _dpi_scale(hwnd) or 1.0
                    # 存成逻辑像素，换显示器/改缩放后也不会变形
                    new = [int(r[0] / sc), int(r[1] / sc),
                           int((r[2] - r[0]) / sc), int((r[3] - r[1]) / sc)]
                    if s.get("windowRect") != new:
                        s["windowRect"] = new
                        save_settings(s)
                except Exception:
                    pass

        threading.Thread(target=_watch, daemon=True).start()

    threading.Thread(target=_after_start, daemon=True).start()

    try:
        webview.start()
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
