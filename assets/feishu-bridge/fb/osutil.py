"""和操作系统打交道的零碎：Windows 一套、Mac 一套，其余模块只调这里，不自己判断平台。

Mac 部分是 2026-10 加的：密钥进钥匙串（见 credstore）、开机自启用 launchd、进程组代替 Job 对象、
伪终端代替隐藏控制台。
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
AGENT_LABEL = "feishu-bridge"  # Mac 开机代理（LaunchAgent）的名字


def _py_hint() -> str:
    """给人看的命令提示里怎么写 python：Windows 用 py；Mac 装的时候建了 .venv（Homebrew 的 Python 不让全局 pip），
    就用安装目录里的 .venv/bin/python3。"""
    if WINDOWS:
        return "py"
    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python3"
    return ".venv/bin/python3" if venv_py.exists() else "python3"


PY = _py_hint()


def app_support_dir() -> Path:
    """桌面应用放数据的地方：Windows %APPDATA%，Mac ~/Library/Application Support。"""
    if WINDOWS:
        return Path(os.environ.get("APPDATA", ""))
    if MAC:
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def hidden_console() -> dict:
    """起 claude 这类长时间进程用的 Popen 参数。

    Windows：给它一个隐藏的控制台，它再起的子进程就不会闪黑框。
    Mac：让它自成一个进程组（组号就是它的 pid），停的时候整组一起杀。
    """
    if WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return {"startupinfo": si, "creationflags": subprocess.CREATE_NEW_CONSOLE}
    return {"start_new_session": True}


def no_window() -> dict:
    """跑一下就退的小命令（claude --version 这类）：Windows 上别闪黑框。"""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def kill_tree(proc: subprocess.Popen) -> None:
    """连子进程一起杀。Windows 上优先用 Job 对象（见 executor），这里是兜底。"""
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, **no_window())
        return
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # hidden_console() 起的进程自成一组
    except (ProcessLookupError, PermissionError):
        pass


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")


def run_in_terminal(cmd: list[str], cwd: str | None, env: dict, timeout: float = 60) -> tuple[int, str]:
    """给命令一个终端再跑，返回 (退出码, 输出)。claude --desktop 发现输出被接管就拒绝执行，所以要这样跑。

    Windows：隐藏的新控制台，拿不到输出。Mac：伪终端（pty），输出能拿到，报错原话可以转给用户。
    """
    if WINDOWS:
        r = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, **hidden_console())
        return r.returncode, ""
    import pty
    import select
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True)
    finally:
        os.close(slave)
    out, deadline = bytearray(), time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                kill_tree(proc)
                break
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # 子进程那头关了（Linux 上是 EIO）
                    chunk = b""
                if not chunk:
                    break
                out += chunk
            elif proc.poll() is not None:
                break
    finally:
        os.close(master)
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        rc = proc.wait()
    return rc, _ANSI.sub("", out.decode("utf-8", "replace")).strip()


_LOCK = None


def _try_lock(key: str):
    """试着占住这个名字：占到返回句柄（Windows 互斥量 / Mac 锁文件），已被别的进程占着返回 None。"""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateMutexW(None, False, "Local\\" + key)
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            if h:
                k32.CloseHandle(h)
            return None
        return h
    import fcntl
    f = open(Path(tempfile.gettempdir()) / f"{key}-{os.getuid()}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _release(handle) -> None:
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle(handle)
    else:
        handle.close()


def single_instance(key: str) -> bool:
    """同一个用户同一时间只能跑一个。飞书长连接是集群模式，开两个的话每条消息只会随机推给其中一个。"""
    global _LOCK
    h = _try_lock(key)
    if h is None:
        return False
    _LOCK = h  # 一直占着，进程退出时自动释放
    return True


def wait_until_free(key: str, timeout: float = 30) -> bool:
    """等占着这个名字的进程退出。重启时用：先停旧的，等它真退出了再起新的，不然新的一看「已经有一个」就自己退了。"""
    deadline = time.monotonic() + timeout
    while True:
        h = _try_lock(key)
        if h is not None:
            _release(h)
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)


_AWAKE = None


def keep_awake() -> subprocess.Popen | None:
    """Mac：飞书桥在跑的时候，接着电源就别让电脑自己睡着（显示器照常熄屏），睡着了就收不到手机上的消息。

    用系统自带的 caffeinate -s -w <飞书桥的进程号>：飞书桥一退出它也跟着退，不留尾巴。-s 只在接电源时有效，
    笔记本拔了电源照常睡；合上盖子通常也还是会睡。Windows 上不管，靠电源设置。
    """
    global _AWAKE
    if not MAC or _AWAKE is not None:
        return _AWAKE
    try:
        _AWAKE = subprocess.Popen(["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    return _AWAKE


# ---------- Mac 开机自启（launchd） ----------
def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def login_shell_path() -> str:
    """用户登录 shell 里的 PATH。launchd 起的进程 PATH 只有 /usr/bin:/bin 这几个，找不到 claude、git、brew。"""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        r = subprocess.run([shell, "-lic", "echo __PATH__=$PATH"], capture_output=True, text=True, timeout=15)
        for line in reversed(r.stdout.splitlines()):
            if line.startswith("__PATH__="):
                return line[len("__PATH__="):].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    extra = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    return ":".join(dict.fromkeys(extra + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":")))


def launch_agent_plist(python: str, script: Path, base_dir: Path, data_dir: Path, path_env: str) -> bytes:
    """登录后自动启动；异常退出隔一分钟重启；正常退出（比如发现已经有一个在跑）就不再拉起。"""
    return plistlib.dumps({
        "Label": AGENT_LABEL,
        "ProgramArguments": [python, str(script)],
        "WorkingDirectory": str(base_dir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "EnvironmentVariables": {"PATH": path_env, "LANG": "en_US.UTF-8", "PYTHONUTF8": "1"},
        "StandardOutPath": str(data_dir / "launchd.out.log"),
        "StandardErrorPath": str(data_dir / "launchd.err.log"),
    })


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def gui_domain() -> str:
    return f"gui/{os.getuid()}"


def restart_hint() -> str:
    """改了配置之后怎么重启飞书桥（给人看的）。"""
    return f"{PY} bridge.py --restart"
