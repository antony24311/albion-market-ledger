from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from tkinter import Tk, messagebox

from albion_tracker.db import Database
from albion_tracker.server import serve_in_thread


APP_NAME = "Albion Market Ledger"
APP_DIRECTORY = "AlbionMarketLedger"
APP_URL = "http://127.0.0.1:8765"
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
WAIT_TIMEOUT = 0x00000102


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
shell32.ShellExecuteExW.restype = wintypes.BOOL


def resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent


def app_data_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / APP_DIRECTORY
    path.mkdir(parents=True, exist_ok=True)
    return path


def show_error(title: str, message: str) -> None:
    root = Tk()
    root.withdraw()
    messagebox.showerror(title, message, parent=root)
    root.destroy()


def health_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{APP_URL}/api/health", timeout=1) as response:
            return response.status == 200 and json.loads(response.read()).get("ok") is True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def active_capture_device() -> str | None:
    command = (
        "$config=Get-NetIPConfiguration | Where-Object "
        "{$_.IPv4DefaultGateway -and $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up'} | "
        "Select-Object -First 1; if ($config) {$config.NetAdapter.InterfaceGuid.ToString()}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        guid = result.stdout.strip().strip("{}")
        if result.returncode == 0 and guid:
            return rf"\Device\NPF_{{{guid.upper()}}}"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def find_edge() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def edge_app_process_ids(profile: Path) -> list[int]:
    """Find a relaunched Edge App process that belongs to this application."""
    command = (
        "$profile=$env:ALBION_LEDGER_EDGE_PROFILE; "
        "$url=$env:ALBION_LEDGER_APP_URL; "
        "Get-CimInstance Win32_Process -Filter \"Name = 'msedge.exe'\" | "
        "Where-Object {$_.CommandLine -and $_.CommandLine.Contains($profile) -and "
        "$_.CommandLine.Contains(('--app=' + $url))} | "
        "ForEach-Object {$_.ProcessId}"
    )
    environment = os.environ.copy()
    environment["ALBION_LEDGER_EDGE_PROFILE"] = str(profile)
    environment["ALBION_LEDGER_APP_URL"] = APP_URL
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            return []
        return [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


class DesktopRuntime:
    def __init__(self) -> None:
        self.resources = resource_root()
        self.app_data = app_data_root()
        self.data_directory = self.app_data / "data"
        self.log_directory = self.app_data / "logs"
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self.server = None
        self.capture_handle: wintypes.HANDLE | None = None
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.runtime_log = open(self.log_directory / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = self.runtime_log
        sys.stderr = self.runtime_log

    def start_server(self) -> None:
        print(f"Starting tracker service at {APP_URL}.")
        if health_ready():
            print("Using the tracker service already listening on port 8765.")
            return
        database = Database(self.data_directory / "albion-purchases.sqlite3")
        self.server, _ = serve_in_thread(database, "127.0.0.1", 8765, self.resources / "web")
        for _ in range(30):
            if health_ready():
                print("Tracker service is ready on port 8765.")
                return
            time.sleep(0.1)
        raise RuntimeError("Tracker service did not become ready on port 8765.")

    def start_capture(self) -> None:
        capture_path = self.resources / "bin" / "albion-capture-windows-app-amd64.exe"
        if not capture_path.is_file():
            capture_path = self.resources / "bin" / "albion-capture-windows-amd64.exe"
        if not capture_path.is_file():
            raise RuntimeError(f"Packet capture executable is missing: {capture_path}")

        arguments = [
            "-api", APP_URL,
            "-spool", str(self.data_directory / "capture-spool.jsonl"),
            "-log", str(self.log_directory / "capture.log"),
        ]
        device = active_capture_device()
        if device:
            arguments.extend(["-devices", device])
        execution = ShellExecuteInfo()
        execution.cbSize = ctypes.sizeof(execution)
        execution.fMask = SEE_MASK_NOCLOSEPROCESS
        execution.lpVerb = "runas"
        execution.lpFile = str(capture_path)
        execution.lpParameters = subprocess.list2cmdline(arguments)
        execution.lpDirectory = str(self.app_data)
        execution.nShow = SW_HIDE
        if not shell32.ShellExecuteExW(ctypes.byref(execution)):
            error = ctypes.get_last_error()
            if error == ERROR_CANCELLED:
                raise RuntimeError("Administrator permission was cancelled; packet capture was not started.")
            raise ctypes.WinError(error)
        self.capture_handle = execution.hProcess
        result = kernel32.WaitForSingleObject(self.capture_handle, 800)
        if result != WAIT_TIMEOUT:
            kernel32.CloseHandle(self.capture_handle)
            self.capture_handle = None
            raise RuntimeError(
                "Packet capture could not start. Confirm that Npcap is installed in WinPcap-compatible mode.\n\n"
                f"Log: {self.log_directory / 'capture.log'}"
            )
        print("Packet capture is running.")

    def open_window(self) -> None:
        edge = find_edge()
        if not edge:
            raise RuntimeError("Microsoft Edge was not found. Install or repair Edge, then retry.")
        profile = self.app_data / "EdgeProfile"
        print(f"Opening desktop window at {APP_URL}.")
        process = subprocess.Popen(
            [
                str(edge),
                f"--app={APP_URL}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-session-crashed-bubble",
                "--window-size=1440,960",
                # Edge can otherwise relaunch itself to remove Windows compatibility
                # settings. Waiting on the original process would then stop our server
                # while the replacement window is still open.
                "--edge-skip-compat-layer-relaunch",
            ],
            creationflags=CREATE_NO_WINDOW,
        )
        time.sleep(1)
        if process.poll() is None:
            print(f"Desktop window is running (PID {process.pid}).")
            process.wait()
            print("Desktop window was closed.")
            return

        # If Edge reused or relaunched a profile process despite the flag above,
        # keep the tracker alive for that real App window instead of stopping it.
        process_ids: list[int] = []
        for _ in range(20):
            process_ids = edge_app_process_ids(profile)
            if process_ids:
                break
            time.sleep(0.25)
        if not process_ids:
            raise RuntimeError(
                "Microsoft Edge exited before the desktop window opened.\n\n"
                f"Open {APP_URL} manually and check {self.log_directory / 'app.log'}."
            )

        print(f"Edge relaunched or reused the desktop window (PID {process_ids[0]}).")
        while edge_app_process_ids(profile):
            time.sleep(2)
        print("Desktop window was closed.")

    def stop(self) -> None:
        if self.capture_handle:
            if kernel32.WaitForSingleObject(self.capture_handle, 0) == WAIT_TIMEOUT:
                kernel32.TerminateProcess(self.capture_handle, 0)
                kernel32.WaitForSingleObject(self.capture_handle, 4000)
            kernel32.CloseHandle(self.capture_handle)
            self.capture_handle = None
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.runtime_log:
            sys.stdout = self.original_stdout
            sys.stderr = self.original_stderr
            self.runtime_log.close()


def main() -> int:
    if "--verify-package" in sys.argv:
        resources = resource_root()
        required = [
            resources / "web" / "index.html",
            resources / "web" / "app.js",
            resources / "bin" / "albion-capture-windows-app-amd64.exe",
        ]
        return 0 if all(path.is_file() for path in required) else 2

    ctypes.set_last_error(0)
    mutex = kernel32.CreateMutexW(None, True, "Local\\AlbionMarketLedgerDesktopApp")
    if not mutex:
        show_error(APP_NAME, "Unable to create the application lock.")
        return 1
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        show_error(APP_NAME, "Albion Market Ledger is already running.")
        kernel32.CloseHandle(mutex)
        return 0

    runtime: DesktopRuntime | None = None
    try:
        runtime = DesktopRuntime()
        runtime.start_server()
        runtime.start_capture()
        runtime.open_window()
        return 0
    except Exception as error:
        show_error(APP_NAME, str(error))
        return 1
    finally:
        if runtime:
            runtime.stop()
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
