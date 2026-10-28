"""电脑上的 Claude 会话：列出来（桌面版侧边栏的 + 在手机上开的）、读最近几轮对话、在桌面版里打开。

桌面版每个会话有一份元数据 %APPDATA%\\Claude\\claude-code-sessions\\…\\local_*.json，里面有侧边栏标题、
工作目录、最后活动时间和 cliSessionId；对话记录在 ~/.claude/projects/<目录>/<cliSessionId>.jsonl，
claude -p --resume <cliSessionId> 就能接着聊。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import osutil

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass
class Session:
    sid: str            # Claude 会话 ID（记录文件名）
    title: str
    cwd: str
    last_active: float  # 秒
    source: str         # 电脑 / 手机

    def to_dict(self) -> dict:
        return asdict(self)


def valid_sid(sid: str) -> bool:
    return bool(_UUID.match(sid or ""))


def find_transcript(sid: str, root: Path) -> Path | None:
    if not valid_sid(sid):
        return None
    return next(iter(Path(root).glob(f"*/{sid}.jsonl")), None)


def desktop_sessions(base: Path) -> list[Session]:
    """桌面版侧边栏里没归档的会话。"""
    out = []
    for f in Path(base).rglob("local_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = d.get("cliSessionId") or ""
        if d.get("isArchived") or not valid_sid(sid):
            continue
        out.append(Session(sid=sid, title=(d.get("title") or "").strip() or "（未命名）",
                           cwd=d.get("cwd") or "",
                           last_active=float(d.get("lastActivityAt") or d.get("createdAt") or 0) / 1000,
                           source="电脑"))
    return out


def list_sessions(desktop_dir: Path, transcripts_dir: Path, bridge_rows: list[dict], limit: int = 10) -> list[Session]:
    """最近活动的会话。最后活动时间取元数据和记录文件修改时间里更晚的那个（手机上接着聊过也算）。"""
    merged: dict[str, Session] = {s.sid: s for s in desktop_sessions(desktop_dir)}
    for r in bridge_rows:
        sid = r.get("sid") or ""
        if valid_sid(sid) and sid not in merged:
            merged[sid] = Session(sid=sid, title=" ".join(str(r.get("first_prompt") or "").split())[:40] or "（未命名）",
                                  cwd=r.get("cwd") or "", last_active=float(r.get("last") or 0), source="手机")
    ranked = sorted(merged.values(), key=lambda s: s.last_active, reverse=True)[: limit * 3]
    for s in ranked:
        p = find_transcript(s.sid, transcripts_dir)
        if p is None:
            s.last_active = -1  # 记录文件不在了，接不上
            continue
        s.last_active = max(s.last_active, p.stat().st_mtime)
    return sorted((s for s in ranked if s.last_active >= 0), key=lambda s: s.last_active, reverse=True)[:limit]


def recent_turns(path: Path, n: int = 4, tail_bytes: int = 800_000) -> list[tuple[str, str]]:
    """最近 n 轮对话：[(「你」, 你说的话), (「Claude」, 这一轮最后一段文字), …]。工具调用、思考、子代理都不要。"""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        data = f.read()
    lines = data.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # 第一行可能被截了一半
    msgs: list[list[str]] = []
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain") or e.get("isMeta") or e.get("type") not in ("user", "assistant"):
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, str):
            texts = [c]
        elif isinstance(c, list):
            blocks = [b for b in c if isinstance(b, dict)]
            if any(b.get("type") == "tool_result" for b in blocks):
                continue
            texts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
        else:
            continue
        text = "\n".join(t for t in texts if t.strip() and not t.lstrip().startswith("<")).strip()
        if not text:
            continue
        if e["type"] == "user":
            msgs.append(["你", text])
        elif msgs and msgs[-1][0] == "Claude":
            msgs[-1][1] = text  # 同一轮里后面的文字盖掉前面的，留最后的结论
        else:
            msgs.append(["Claude", text])
    keep = []
    for role, text in reversed(msgs):
        keep.append((role, text))
        if role == "你" and sum(1 for r, _ in keep if r == "你") >= n:
            break
    return list(reversed(keep))


def _registered_pids(sid: str, registry: Path) -> list[int]:
    """~/.claude/sessions/<pid>.json 里登记着这个会话的进程号（每个交互式 claude 进程启动时都会登记一份）。"""
    pids = []
    for f in Path(registry).glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("sessionId") == sid:
                pids.append(int(d.get("pid") or f.stem))
        except (OSError, ValueError, TypeError):
            continue
    return pids


def open_elsewhere(sid: str, registry: Path | None = None) -> bool:
    """有没有别的 Claude 进程（桌面版开着的会话、终端里的 claude）正挂着这个会话。飞书桥自己起的不算。

    桌面版每个开着的会话都有一个 claude.exe（Mac 上叫 claude）常驻。两处都看：
    - ~/.claude/sessions/<pid>.json 的登记：新开的会话命令行里没有会话 ID，只看命令行会漏
      （10-27 Mac 上实测：桌面版开着两个会话，重开过的命令行带 --resume=<会话ID>，新开的那个什么都不带）；
    - 进程命令行里的 --resume=<会话ID>：兜底，登记文件没有时也能认出重开过的会话。
    """
    if not valid_sid(sid):
        return False
    try:
        import psutil
    except ImportError:
        return False
    me = os.getpid()

    def someone_else(p, cmdline: list[str]) -> bool:
        if "feishu-bridge" in " ".join(cmdline):
            return False
        try:
            return me not in (a.pid for a in p.parents())  # 飞书桥起的子进程不算
        except (psutil.Error, OSError):
            return True

    for pid in _registered_pids(sid, registry or Path.home() / ".claude" / "sessions"):
        try:
            p = psutil.Process(pid)
            if (p.name() or "").lower() in ("claude.exe", "claude") and someone_else(p, p.cmdline() or []):
                return True
        except (psutil.Error, OSError):  # 进程已经退出，登记文件还没删
            continue
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() not in ("claude.exe", "claude"):
                continue
            cl = p.info["cmdline"] or []
        except (psutil.Error, OSError):
            continue
        if sid in " ".join(cl) and someone_else(p, cl):
            return True
    return False


def ago(ts: float, now: float | None = None) -> str:
    sec = max(0, (now or time.time()) - ts)
    if sec < 90:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)} 小时前"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def open_in_desktop(sid: str, claude_cmd: list[str], env: dict, cwd: str) -> tuple[bool, str]:
    """在 Claude 桌面版里打开这个会话（claude --desktop --resume <id>，要求 2.1.285+）。

    输出被接管时 --desktop 会拒绝（「can't run non-interactively ... or redirected output」，10-27 真机撞到），
    所以要给它一个终端：Windows 是隐藏的新控制台（失败时只拿得到退出码），Mac 是伪终端（能拿到报错原话）。
    """
    if not valid_sid(sid):
        return False, "会话 ID 不对"
    workdir = cwd if cwd and Path(cwd).is_dir() else None
    try:
        rc, out = osutil.run_in_terminal(claude_cmd + ["--desktop", "--resume", sid], workdir, env, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if rc != 0:
        return False, out[-300:] or f"claude --desktop 退出码 {rc}"
    return True, ""
