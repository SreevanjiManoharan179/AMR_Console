"""Close one managed main window per owned RViz process, then await its exit."""
import ctypes
import ctypes.util
from pathlib import Path
import re
import subprocess
import time


def _live_rviz(owned):
    result = set()
    # refresh checks the recorded PID/start-tick identity before returning it.
    for record in owned.refresh():
        try:
            if Path(f'/proc/{record["pid"]}/comm').read_text().strip() == 'rviz2':
                result.add(record['pid'])
        except OSError:
            pass
    return result


def main_rviz_windows(live_pids):
    clients = subprocess.check_output(['xprop', '-root', '_NET_CLIENT_LIST'],
                                      text=True, timeout=5)
    windows = {}
    for window in dict.fromkeys(re.findall(r'0x[0-9a-fA-F]+', clients)):
        prop = subprocess.run(['xprop', '-id', window, '_NET_WM_PID',
                               '_NET_WM_WINDOW_TYPE', 'WM_TRANSIENT_FOR'],
                              capture_output=True, text=True, timeout=5)
        pid = re.search(r'^_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)', prop.stdout, re.M)
        if not pid or int(pid.group(1)) not in live_pids:
            continue
        kinds = re.search(r'^_NET_WM_WINDOW_TYPE\(ATOM\)\s*=\s*(.*)', prop.stdout, re.M)
        if not kinds or kinds.group(1).strip() != '_NET_WM_WINDOW_TYPE_NORMAL':
            continue
        transient = re.search(r'^WM_TRANSIENT_FOR.*?(0x[0-9a-fA-F]+)', prop.stdout, re.M)
        if transient and int(transient.group(1), 16) != 0:
            continue
        value = int(pid.group(1))
        if value not in windows.values():
            windows[window] = value
    return windows


def _send_close(windows):
    lib = ctypes.CDLL(ctypes.util.find_library('X11'))
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    class Data(ctypes.Union):
        _fields_ = [('l', ctypes.c_long * 5)]
    class ClientMessage(ctypes.Structure):
        _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong),
                    ('send_event', ctypes.c_int), ('display', ctypes.c_void_p),
                    ('window', ctypes.c_ulong), ('message_type', ctypes.c_ulong),
                    ('format', ctypes.c_int), ('data', Data)]
    class Event(ctypes.Union):
        _fields_ = [('client', ClientMessage), ('pad', ctypes.c_long * 24)]
    lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                              ctypes.c_long, ctypes.POINTER(Event)]
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = lib.XOpenDisplay(None)
    if not display:
        raise RuntimeError('Cannot open X11 display for RViz close')
    try:
        for window in windows:
            event = Event()
            event.client.type = 33
            event.client.display = display
            event.client.window = int(window, 16)
            event.client.message_type = lib.XInternAtom(display, b'_NET_CLOSE_WINDOW', 0)
            event.client.format = 32
            event.client.data.l[1] = 2
            lib.XSendEvent(display, lib.XDefaultRootWindow(display), 0,
                           (1 << 20) | (1 << 19), ctypes.byref(event))
        lib.XFlush(display)
    finally:
        lib.XCloseDisplay(display)


def close_rviz(owned, timeout=30):
    start = time.monotonic()
    live = _live_rviz(owned)
    windows = main_rviz_windows(live) if live else {}
    # Revalidate ownership immediately before sending the UI request.
    current = _live_rviz(owned)
    windows = {window: pid for window, pid in windows.items() if pid in current}
    if windows:
        print('Closing RViz; if it asks to save display changes, choose Discard. '
              f'Waiting up to {timeout:g}s before stack shutdown.', flush=True)
        _send_close(windows)
        while _live_rviz(owned) and time.monotonic() - start < timeout:
            time.sleep(.1)
    pending = sorted(_live_rviz(owned))
    return {'owned_rviz_close_requests': list(windows),
            'requested_pids': sorted(windows.values()),
            'pending_rviz_pids': pending, 'complete': not pending,
            'wait_seconds': round(time.monotonic() - start, 3)}
