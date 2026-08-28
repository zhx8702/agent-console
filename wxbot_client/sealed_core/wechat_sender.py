"""WeChat 4.0 Windows client driver — hotkey + clipboard.

Sends messages by automating WeChat's desktop UI:
  find window -> foreground -> Ctrl+F search -> paste session -> Enter ->
  paste message -> Enter

Image sending: convert to BMP DIB -> clipboard CF_DIB -> Ctrl+V -> Enter.

No pixel coordinates. Safety checks before every keystroke.
"""
import ctypes
import io
import os
import time

import pyperclip
import win32api
import win32clipboard
import win32con
import win32gui

from sealed_core.runtime import require_capability

VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_ALT = 0x12
VK_DELETE = 0x2E
VK_A = 0x41
VK_V = 0x56
VK_F = 0x46
KEYEVENTF_KEYUP = 0x0002

_MOD_MAP = {"ctrl": VK_CONTROL, "shift": VK_SHIFT, "alt": VK_ALT}
_MAIN_KEY_MAP = {"f": VK_F, "a": VK_A, "v": VK_V,
                 "enter": VK_RETURN, "delete": VK_DELETE}

_WECHAT_NAMES = ("微信", "Weixin", "WeChat")

_last_session = None
_last_session_ts = 0.0
_SESSION_CACHE_TTL = 300


def _set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _find_main_hwnd():
    found = []

    def cb(h, _):
        cls = win32gui.GetClassName(h)
        name = win32gui.GetWindowText(h)
        if cls.startswith("Qt") and name in _WECHAT_NAMES:
            r = win32gui.GetWindowRect(h)
            w, height = r[2] - r[0], r[3] - r[1]
            if w < 200 or height < 200:
                return True
            found.append({
                "hwnd": h,
                "visible": bool(win32gui.IsWindowVisible(h)),
                "iconic": bool(win32gui.IsIconic(h)),
                "area": w * height,
            })
        return True

    win32gui.EnumWindows(cb, None)
    found.sort(key=lambda c: (not c["visible"], c["iconic"], -c["area"]))
    return found[0]["hwnd"] if found else None


def _show(hwnd):
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.15)
    else:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
        except Exception:
            pass

    try:
        user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass

    cur_tid = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        try:
            if user32.AttachThreadInput(cur_tid, fg_tid, True):
                attached = True
        except Exception:
            pass
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            try:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
            except Exception:
                pass
    time.sleep(0.15)


def _normalize(hwnd):
    if win32gui.IsIconic(hwnd) or not win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.2)


def _ensure_foreground(hwnd, max_tries=3):
    user32 = ctypes.windll.user32
    for _ in range(max_tries):
        if user32.GetForegroundWindow() == hwnd:
            return True
        _show(hwnd)
        if user32.GetForegroundWindow() == hwnd:
            return True

    fg = user32.GetForegroundWindow()
    if fg:
        try:
            fg_name = win32gui.GetWindowText(fg)
            fg_class = win32gui.GetClassName(fg)
        except Exception:
            fg_name, fg_class = "?", "?"
        print(f"[sender] foreground give-up: target=wechat({hwnd}), "
              f"actual=hwnd={fg} name={fg_name!r} class={fg_class!r}")
    else:
        print("[sender] foreground give-up: no foreground window")
    return False


def _is_wechat_foreground():
    fg = ctypes.windll.user32.GetForegroundWindow()
    if not fg:
        return False
    try:
        cls = win32gui.GetClassName(fg)
        name = win32gui.GetWindowText(fg)
    except Exception:
        return False
    return cls.startswith("Qt") and name in _WECHAT_NAMES


def _safety_check():
    if _is_wechat_foreground():
        return
    hwnd = _find_main_hwnd()
    if hwnd and _ensure_foreground(hwnd, max_tries=2):
        return
    raise RuntimeError("aborting input — WeChat is not foreground")


def _key(vk, up=False):
    win32api.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _press(vk):
    _safety_check()
    _key(vk, up=False)
    time.sleep(0.02)
    _key(vk, up=True)
    time.sleep(0.05)


def _send_hotkey(spec):
    _safety_check()
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    *mods, main = parts
    mod_vks = [_MOD_MAP[m] for m in mods]
    main_vk = _MAIN_KEY_MAP.get(main) or (
        ord(main.upper()) if len(main) == 1 and main.isalpha() else None
    )
    if main_vk is None:
        raise ValueError(f"bad hotkey spec: {spec!r}")

    for m in mod_vks:
        _key(m, up=False)
    time.sleep(0.02)
    _key(main_vk, up=False)
    time.sleep(0.03)
    _key(main_vk, up=True)
    for m in reversed(mod_vks):
        _key(m, up=True)
    time.sleep(0.05)


def _paste(text):
    pyperclip.copy(text)
    time.sleep(0.08)
    _send_hotkey("ctrl+v")


def _paste_image(image_path):
    """Write image to clipboard as CF_DIB, then Ctrl+V."""
    from PIL import Image

    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, "BMP")
    dib = buf.getvalue()[14:]
    buf.close()

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
    finally:
        win32clipboard.CloseClipboard()

    time.sleep(0.15)
    _send_hotkey("ctrl+v")


def _open_chat(hwnd, session_name):
    _send_hotkey("ctrl+f")
    time.sleep(0.35)
    _send_hotkey("ctrl+a")
    _press(VK_DELETE)
    time.sleep(0.08)
    _paste(session_name)
    time.sleep(0.9)
    _press(VK_RETURN)
    time.sleep(1.0)


def _send_one(hwnd, text):
    _send_hotkey("ctrl+a")
    _press(VK_DELETE)
    time.sleep(0.08)
    _paste(text)
    time.sleep(0.25)
    _press(VK_RETURN)
    time.sleep(0.3)


def _send_image_one(hwnd, image_path):
    _send_hotkey("ctrl+a")
    _press(VK_DELETE)
    time.sleep(0.08)
    _paste_image(image_path)
    time.sleep(0.5)
    _press(VK_RETURN)
    time.sleep(0.5)


def _prep_window():
    _set_dpi_aware()
    hwnd = _find_main_hwnd()
    if not hwnd:
        raise RuntimeError("WeChat main window not found — is WeChat running?")
    _normalize(hwnd)
    if not _ensure_foreground(hwnd):
        raise RuntimeError("could not bring WeChat to foreground")
    return hwnd


def send(session_name, text):
    require_capability("send_message")
    hwnd = _prep_window()
    _open_chat(hwnd, session_name)
    _send_one(hwnd, text)


def send_image(session_name, image_path):
    require_capability("send_message")
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"image not found: {image_path}")
    hwnd = _prep_window()
    _open_chat(hwnd, session_name)
    _send_image_one(hwnd, image_path)


def send_video(session_name, video_path):
    """Send a video through the production sender implementation.

    The checked-in Python fallback deliberately does not emulate WeChat's
    video UI flow.  Production deployments use the updated compiled sender,
    which exports this function; failing closed here avoids sending a video as
    a misleading image.
    """

    require_capability("send_message")
    raise RuntimeError(
        "video sending requires a wxbot SDK sender with send_video support"
    )


def send_batch(tasks):
    require_capability("send_message_batch")
    global _last_session, _last_session_ts
    results = []

    by_sid = {}
    for t in tasks:
        by_sid.setdefault(t["session_id"], []).append(t)
    for msgs in by_sid.values():
        msgs.sort(key=lambda t: t.get("created_ts") or 0)
    ordered = sorted(by_sid.items(),
                     key=lambda kv: (kv[1][0].get("created_ts") or 0))

    for session_id, group in ordered:
        session_name = group[0]["session_name"]
        try:
            hwnd = _prep_window()
        except Exception as e:
            for t in group:
                results.append((t["id"], False, str(e)))
            continue

        cache_fresh = (
            _last_session == session_id
            and (time.time() - _last_session_ts) < _SESSION_CACHE_TTL
        )
        if not cache_fresh:
            try:
                _open_chat(hwnd, session_name)
            except Exception as e:
                for t in group:
                    results.append((t["id"], False, f"open_chat: {e}"))
                continue

        sent_any = False
        for t in group:
            try:
                image_path = t.get("image_path")
                if str(t.get("msg_type") or "").strip().lower() == "video":
                    is_remote_video = str(image_path or "").startswith(("http://", "https://"))
                    if not image_path or (not is_remote_video and not os.path.isfile(image_path)):
                        raise FileNotFoundError(f"video not found: {image_path}")
                    send_video(session_name, image_path)
                elif image_path and os.path.isfile(image_path):
                    _send_image_one(hwnd, image_path)
                else:
                    body = t["reply_text"]
                    is_group = (t.get("session_id") or "").endswith("@chatroom")
                    if is_group and t.get("mention_sender") and t.get("sender_name"):
                        body = f"@{t['sender_name']} {body}"
                    _send_one(hwnd, body)
                results.append((t["id"], True, None))
                sent_any = True
                time.sleep(0.3)
            except Exception as e:
                results.append((t["id"], False, str(e)))
        if sent_any:
            _last_session = session_id
            _last_session_ts = time.time()
    return results
