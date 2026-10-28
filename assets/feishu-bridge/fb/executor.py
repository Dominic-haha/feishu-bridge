"""跑一次 claude -p：组命令、清环境变量、读 stream-json、超时和停止。

Windows：任务进程树放进一个 Job 对象，停止、超时时一把全杀；飞书桥自己挂了，句柄一关系统也会把它们杀掉，
不会留下没人管的 claude 进程。
Mac：claude 自成一个进程组，停止、超时时整组杀；飞书桥被 launchd 停掉时会先停任务再退出（见 bridge.py）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import osutil
from .config import Config
from .osutil import WINDOWS

log = logging.getLogger("fb.executor")
MCP_SERVER = Path(__file__).resolve().parent / "mcp_server.py"

_SUBTYPE = {
    "error_max_turns": "超过最大轮数上限",
    "error_max_budget_usd": "超过单任务花费上限",
    "error_during_execution": "执行中出错",
}


@dataclass
class RunResult:
    ok: bool = False
    subtype: str = ""
    text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    exit_code: int | None = None
    error: str = ""
    error_kind: str = ""  # no_session / auth / limit
    stopped: bool = False
    timed_out: bool = False
    denials: int = 0


def python_exe(windowless: bool = False) -> str:
    """和当前进程同一套 Python 的 python.exe / pythonw.exe。"""
    p = Path(sys.executable).parent / ("pythonw.exe" if windowless else "python.exe")
    return str(p) if p.exists() else sys.executable


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s)) or (0,)


CLAUDE_EXE = "claude.exe" if WINDOWS else "claude"


def desktop_claudes(base: Path | None = None) -> list[Path]:
    """Claude 桌面版自带的 claude，新版本在前。Windows 在 %APPDATA%\\Claude\\claude-code\\<版本>\\<hash>\\ 下；
    Mac 包在 .app 里：~/Library/Application Support/Claude/claude-code/<版本>/<hash>/claude.app/Contents/MacOS/claude
    （10-27 实测）。"""
    base = base or osutil.app_support_dir() / "Claude" / "claude-code"
    pats = (f"*/*/{CLAUDE_EXE}", f"*/{CLAUDE_EXE}", "*/*/claude.app/Contents/MacOS/claude")
    found = [p for pat in pats for p in base.glob(pat) if p.is_file()]
    return sorted(found, key=lambda p: _ver(p.relative_to(base).parts[0]), reverse=True)


def find_claude(configured: str = "") -> list[str]:
    """claude 的启动命令。没配置就用 Claude 桌面版自带的最新版，其次 PATH 上的，Mac 上再看几个常见安装位置。"""
    if configured:
        p = Path(configured)
        if not p.exists():
            raise FileNotFoundError(f"config.toml 里配置的 claude 不存在：{p}")
        return [python_exe(), str(p)] if p.suffix.lower() == ".py" else [str(p)]
    found = desktop_claudes()
    if found:
        return [str(found[0])]
    w = shutil.which(CLAUDE_EXE)
    if w:
        return [w]
    if not WINDOWS:  # launchd 起的进程 PATH 很短，直接看原生安装器、Homebrew 的位置
        for p in (Path.home() / ".local" / "bin" / "claude", Path("/opt/homebrew/bin/claude"),
                  Path("/usr/local/bin/claude"), Path.home() / ".claude" / "local" / "claude"):
            if p.is_file():
                return [str(p)]
    raise FileNotFoundError(f"找不到 {CLAUDE_EXE}：装 Claude 桌面版，或在 config.toml 的 [claude] path 写明路径")


def _classify(text: str) -> str:
    t = (text or "").lower()
    if "no conversation found" in t:
        return "no_session"
    if any(k in t for k in ("not logged in", "invalid api key", "please run /login", "oauth token",
                            "authentication_failed", "oauth_org_not_allowed")):
        return "auth"
    if any(k in t for k in ("usage limit", "rate limit", "rate_limit")):
        return "limit"
    return ""


def clean_env(cfg: Config, token: str | None) -> dict:
    """给 claude 子进程的环境变量。"""
    # 去掉从桌面版或终端继承来的 CLAUDE*/ANTHROPIC*（嵌套检测、别的认证、网关地址），只用飞书桥自己的 token
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    git_bash = cfg.git_bash_path or os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")
    if git_bash:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = git_bash
    env["MT_FEISHU_NOTIFY_DISABLE"] = "1"  # 你的 Stop hook：远程任务的结果由飞书桥发，别再重复通知
    env["MCP_TOOL_TIMEOUT"] = str(int((cfg.approval_timeout_seconds + 120) * 1000))
    env["NO_COLOR"] = "1"
    return env


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def rule_path(p: Path) -> str:
    """本机路径 → Claude 权限规则里的绝对路径写法（规则里单个 / 开头表示「相对 settings 文件」，绝对路径要 //）：
    Windows D:\\工作 → //d/工作；Mac /Users/me/work → //Users/me/work"""
    s = str(p).replace("\\", "/").rstrip("/")
    if len(s) > 1 and s[1] == ":":
        return "//" + s[0].lower() + s[2:]
    return "/" + s if s.startswith("/") and not s.startswith("//") else s


def render_runtime_files(cfg: Config) -> tuple[Path, Path]:
    """把 bridge-settings.json、remote-mode.md 模板里的路径占位符换成这台电脑的实际路径，写进数据目录。

    这样飞书桥装在哪、工作目录在哪，禁区规则和给 Claude 的说明都对得上，不用每台电脑手改。
    """
    subs = {
        "{{WORKDIR_RULE}}": rule_path(cfg.workdir),
        "{{BRIDGE_DIR_RULE}}": rule_path(cfg.base_dir),
        "{{BRIDGE_DIR}}": str(cfg.base_dir),
        "{{SEND_FILE_ROOTS}}": "、".join(f"`{p}`" for p in cfg.send_file_roots),
    }
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for src in (cfg.settings_file, cfg.system_prompt_file):
        text = src.read_text(encoding="utf-8")
        for k, v in subs.items():
            text = text.replace(k, json.dumps(v, ensure_ascii=False)[1:-1] if src.suffix == ".json" else v)
        dst = cfg.data_dir / src.name
        if not dst.exists() or dst.read_text(encoding="utf-8") != text:
            dst.write_text(text, encoding="utf-8")
        out.append(dst)
    return out[0], out[1]


# ---------- Windows Job 对象（Mac 上不加载，改用进程组） ----------
if WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


class _Job:
    def __init__(self) -> None:
        self.h = _k32.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError(ctypes.get_last_error(), "CreateJobObject 失败")
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, "SetInformationJobObject 失败")

    def assign(self, proc: subprocess.Popen) -> None:
        if not _k32.AssignProcessToJobObject(self.h, int(proc._handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject 失败")

    def kill(self) -> None:
        _k32.TerminateJobObject(self.h, 1)

    def close(self) -> None:
        if self.h:
            _k32.CloseHandle(self.h)
            self.h = None


# ---------- 执行 ----------
class Runner:
    def __init__(self, cfg: Config, *, broker_url: str, broker_token: str,
                 token_getter: Callable[[], str | None] | None = None,
                 claude_cmd: list[str] | None = None) -> None:
        self.cfg = cfg
        self.broker_url = broker_url
        self.broker_token = broker_token
        self.token_getter = token_getter
        self._claude_cmd = claude_cmd
        self._proc: subprocess.Popen | None = None
        self._job: _Job | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def claude_cmd(self) -> list[str]:
        return self._claude_cmd or find_claude(self.cfg.claude_path)

    def stop(self) -> bool:
        """停掉正在跑的任务；没有在跑的返回 False。"""
        with self._lock:
            proc, job = self._proc, self._job
            if proc is None:
                return False
            self._stop.set()
        self._kill(proc, job)
        return True

    @staticmethod
    def _kill(proc: subprocess.Popen, job: _Job | None) -> None:
        if job is not None and job.h:
            job.kill()
            return
        osutil.kill_tree(proc)

    def env(self) -> dict:
        return clean_env(self.cfg, self.token_getter() if self.token_getter else None)

    def write_mcp_config(self, run_dir: Path, task_id: int) -> Path:
        env = {"FB_BROKER_URL": self.broker_url, "FB_BROKER_TOKEN": self.broker_token,
               "FB_TASK_ID": str(task_id), "FB_APPROVE_WAIT": str(int(self.cfg.approval_timeout_seconds + 60))}
        for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",  # Windows
                  "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"):  # Mac
            if os.environ.get(k):
                env[k] = os.environ[k]
        servers = {"bridge": {"type": "stdio", "command": python_exe(), "args": ["-I", str(MCP_SERVER)],
                              "env": env}}
        servers.update(self._extra_servers())
        p = run_dir / "mcp.json"
        p.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=1), encoding="utf-8")
        return p

    def _extra_servers(self) -> dict:
        if not self.cfg.extra_mcp_servers:
            return {}
        try:
            user = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8")).get("mcpServers") or {}
        except (OSError, ValueError):
            user = {}
        out = {}
        for name in self.cfg.extra_mcp_servers:
            if name != "bridge" and name in user:
                out[name] = user[name]
            elif name != "bridge":
                log.warning("extra_mcp_servers 里的 %s 在 ~/.claude.json 里找不到", name)
        return out

    def build_cmd(self, task: dict, mcp_path: Path, resume: bool) -> list[str]:
        c = self.cfg
        settings, prompt = render_runtime_files(c)
        cmd = self.claude_cmd() + [
            "-p", "--output-format", "stream-json", "--verbose",
            "--setting-sources", c.setting_sources,
            "--settings", str(settings),
            "--strict-mcp-config", "--mcp-config", str(mcp_path),
            "--permission-mode", "default",
            "--permission-prompt-tool", "mcp__bridge__approve",
            "--max-turns", str(c.max_turns),
            "--append-system-prompt-file", str(prompt),
        ]
        if c.inbox_dir.is_dir():  # 手机发来的附件：接着别的目录的会话时也能直接读
            cmd += ["--add-dir", str(c.inbox_dir)]
        if c.max_budget_usd > 0:
            cmd += ["--max-budget-usd", f"{c.max_budget_usd:g}"]
        if c.model:
            cmd += ["--model", c.model]
        cmd += ["--resume", task["session_id"]] if resume else ["--session-id", task["session_id"]]
        return cmd

    def run(self, task: dict, on_event: Callable[[str, object], None]) -> RunResult:
        """阻塞跑完一个任务。on_event(kind, data)：init / tool / text / note / result。"""
        run_dir = self.cfg.runs_dir / str(task["id"])
        run_dir.mkdir(parents=True, exist_ok=True)
        mcp = self.write_mcp_config(run_dir, task["id"])
        self._stop.clear()
        try:
            res = self._run_once(task, run_dir, mcp, bool(task.get("resume")), on_event)
            if task.get("resume") and res.error_kind == "no_session" and not res.stopped:
                on_event("note", "原会话找不到了，改成新开一个会话")
                task = {**task, "session_id": str(uuid.uuid4()), "resume": 0}
                res = self._run_once(task, run_dir, mcp, False, on_event)
            return res
        finally:
            try:
                mcp.unlink()
            except OSError:
                pass

    def _run_once(self, task: dict, run_dir: Path, mcp: Path, resume: bool,
                  on_event: Callable[[str, object], None]) -> RunResult:
        res = RunResult(session_id=task.get("session_id", ""))
        cmd = self.build_cmd(task, mcp, resume)
        with open(run_dir / "stderr.log", "ab") as err_f, open(run_dir / "events.jsonl", "ab") as ev_f:
            ev_f.write(json.dumps({"bridge": "start", "at": time.time(), "resume": resume,
                                   "session_id": task.get("session_id")}).encode("utf-8") + b"\n")
            cwd = task.get("cwd") or ""
            if not (cwd and Path(cwd).is_dir()):  # 接着电脑上的会话时用它原来的目录；目录没了就退回默认
                cwd = str(self.cfg.workdir)
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=self.env(), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=err_f, **osutil.hidden_console())
            except OSError as e:
                res.error = f"启动 claude 失败：{e}"
                return res
            job: _Job | None = None
            if WINDOWS:
                try:
                    job = _Job()
                    job.assign(proc)
                except OSError as e:
                    log.warning("Job 对象不可用，停止时改用 taskkill：%s", e)
                    if job is not None:
                        job.close()
                    job = None
            with self._lock:
                self._proc, self._job = proc, job
            if self._stop.is_set():  # 刚启动就被停了
                self._kill(proc, job)
            threading.Thread(target=self._watch, args=(proc, job, res), daemon=True).start()
            try:
                proc.stdin.write(task["prompt"].encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
            for raw in proc.stdout:
                ev_f.write(raw if raw.endswith(b"\n") else raw + b"\n")
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    try:
                        self._handle(ev, res, on_event)
                    except Exception:
                        log.exception("处理事件出错")
            res.exit_code = proc.wait()
            proc.stdout.close()
            with self._lock:
                self._proc, self._job = None, None
            if job is not None:
                job.close()
        res.stopped = self._stop.is_set()
        if res.timed_out:
            res.ok = False
            res.error = f"超过 {self.cfg.timeout_minutes:g} 分钟，已强制停止"
        elif res.stopped and not res.subtype:
            res.error = "已停止"
        elif not res.subtype:  # 没等到 result 事件就退出了
            res.error = _tail(run_dir / "stderr.log", 2000).strip() or f"claude 异常退出（退出码 {res.exit_code}）"
        if not res.ok:
            res.error_kind = _classify(res.text + "\n" + res.error)
        return res

    def _watch(self, proc: subprocess.Popen, job: _Job | None, res: RunResult) -> None:
        deadline = time.time() + self.cfg.timeout_minutes * 60
        while proc.poll() is None:
            if time.time() > deadline:
                res.timed_out = True
                log.warning("任务超时，强制停止 pid=%s", proc.pid)
                self._kill(proc, job)
                return
            time.sleep(0.5)

    @staticmethod
    def _handle(ev: dict, res: RunResult, on_event: Callable[[str, object], None]) -> None:
        t = ev.get("type")
        if t == "system":
            st = ev.get("subtype")
            if st == "init":
                res.session_id = ev.get("session_id") or res.session_id
                for s in ev.get("mcp_servers") or []:
                    if s.get("name") == "bridge" and s.get("status") not in ("connected", "pending"):
                        on_event("note", "审批服务没连上，需要授权的操作都会被拒绝")
                on_event("init", ev)
            elif st == "api_retry":
                on_event("note", f"接口重试中（{ev.get('error', '')}，第 {ev.get('attempt')} 次）")
        elif t == "assistant":
            sub = bool(ev.get("parent_tool_use_id"))
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    on_event("tool", (b.get("name", ""), b.get("input") or {}, sub))
                elif b.get("type") == "text" and not sub and (b.get("text") or "").strip():
                    on_event("text", b["text"])
        elif t == "result":
            res.subtype = ev.get("subtype") or ""
            res.ok = res.subtype == "success" and not ev.get("is_error")
            res.text = ev.get("result") or ""
            res.session_id = ev.get("session_id") or res.session_id
            res.cost_usd = float(ev.get("total_cost_usd") or 0)
            res.num_turns = int(ev.get("num_turns") or 0)
            res.denials = len(ev.get("permission_denials") or [])
            if not res.ok:  # 注意：没登录时 subtype 也是 success，只是 is_error=true，原因在 result 里
                errs = ev.get("errors") or []
                res.error = ("；".join(str(e) for e in errs) or _SUBTYPE.get(res.subtype, "")
                             or res.text or "执行失败")
            on_event("result", ev)
