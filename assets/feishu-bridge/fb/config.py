"""读取 config.toml，缺的项用默认值补上。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .osutil import app_support_dir

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, dict] = {
    "feishu": {
        "app_id": "",
        "allowed_open_ids": [],
        "card_buttons": True,
    },
    "claude": {
        "path": "",
        "workdir": "",  # 留空 = 用户主目录
        "model": "",
        "max_turns": 100,
        "max_budget_usd": 10.0,
        "timeout_minutes": 30,
        "extra_mcp_servers": [],
        "setting_sources": "user,project",
        "git_bash_path": "",
    },
    "policy": {
        "stale_minutes": 10,
        "duplicate_seconds": 60,
        "approval_timeout_seconds": 300,
        "send_file_roots": [],  # 留空 = 只有工作目录
        "result_inline_chars": 3000,
        "inbox_dir": "",  # 留空 = 工作目录下的 _飞书收件
        "attach_wait_seconds": 120,
        "big_session_mb": 3,
        "auto_open_new_sessions": False,
        "keep_awake": True,
    },
}


@dataclass
class Config:
    app_id: str = ""
    allowed_open_ids: list[str] = field(default_factory=list)
    card_buttons: bool = True  # 应用没配卡片回调时设 False：卡片上不放按钮，改成回文字
    claude_path: str = ""
    workdir: Path = field(default_factory=Path.home)
    model: str = ""
    max_turns: int = 100
    max_budget_usd: float = 10.0
    timeout_minutes: float = 30
    extra_mcp_servers: list[str] = field(default_factory=list)
    setting_sources: str = "user,project"
    git_bash_path: str = ""
    stale_minutes: float = 10
    duplicate_seconds: float = 60
    approval_timeout_seconds: float = 300
    send_file_roots: list[Path] = field(default_factory=lambda: [Path.home()])
    result_inline_chars: int = 3000
    # 手机发来的图片、文件存到这里（Claude 不用审批就能读）；只发附件不发话，等这么多秒就直接让 Claude 看
    inbox_dir: Path = field(default_factory=lambda: Path.home() / "_飞书收件")
    attach_wait_seconds: float = 120
    # 会话记录超过这么多 MB，接着聊之前先确认（10-26 接着一个 8.5 MB 的会话跑了一次，估算约 $48）
    big_session_mb: float = 3
    # 手机上新开的会话跑完，自动在电脑的 Claude 桌面版里打开（桌面版会跳过去，侧边栏里就有了）
    auto_open_new_sessions: bool = False
    # Mac：飞书桥在跑的时候，接电源就不让电脑自动睡眠（见 osutil.keep_awake）；Windows 上不起作用
    keep_awake: bool = True
    base_dir: Path = BASE_DIR
    # 数据目录（任务库、日志、每次运行的记录）；测试时指到临时目录
    data_dir: Path = BASE_DIR / "data"
    # 桌面版会话元数据（侧边栏标题等）、Claude Code 会话记录；测试时指到临时目录
    desktop_sessions_dir: Path = field(
        default_factory=lambda: app_support_dir() / "Claude" / "claude-code-sessions")
    transcripts_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    # 下面两个是模板，里面的 {{...}} 路径占位符由 executor.render_runtime_files 换成本机路径后再用
    @property
    def settings_file(self) -> Path:
        return self.base_dir / "bridge-settings.json"

    @property
    def system_prompt_file(self) -> Path:
        return self.base_dir / "remote-mode.md"


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path) if path else BASE_DIR / "config.toml"
    raw: dict = {}
    if p.exists():
        with open(p, "rb") as f:
            raw = tomllib.load(f)

    def get(section: str, key: str):
        return raw.get(section, {}).get(key, DEFAULTS[section][key])

    workdir = Path(str(get("claude", "workdir")).strip() or Path.home())
    return Config(
        app_id=str(get("feishu", "app_id")).strip(),
        allowed_open_ids=[str(x).strip() for x in get("feishu", "allowed_open_ids") if str(x).strip()],
        card_buttons=bool(get("feishu", "card_buttons")),
        claude_path=str(get("claude", "path")).strip(),
        workdir=workdir,
        model=str(get("claude", "model")).strip(),
        max_turns=int(get("claude", "max_turns")),
        max_budget_usd=float(get("claude", "max_budget_usd")),
        timeout_minutes=float(get("claude", "timeout_minutes")),
        extra_mcp_servers=[str(x) for x in get("claude", "extra_mcp_servers")],
        setting_sources=str(get("claude", "setting_sources")).strip(),
        git_bash_path=str(get("claude", "git_bash_path")).strip(),
        stale_minutes=float(get("policy", "stale_minutes")),
        duplicate_seconds=float(get("policy", "duplicate_seconds")),
        approval_timeout_seconds=float(get("policy", "approval_timeout_seconds")),
        send_file_roots=[Path(x) for x in get("policy", "send_file_roots")] or [workdir],
        result_inline_chars=int(get("policy", "result_inline_chars")),
        inbox_dir=Path(str(get("policy", "inbox_dir")).strip() or workdir / "_飞书收件"),
        attach_wait_seconds=float(get("policy", "attach_wait_seconds")),
        big_session_mb=float(get("policy", "big_session_mb")),
        auto_open_new_sessions=bool(get("policy", "auto_open_new_sessions")),
        keep_awake=bool(get("policy", "keep_awake")),
    )
