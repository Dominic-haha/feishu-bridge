"""规则：取消息文字、认指令、算消息时效、脱敏、send_file 路径校验、工具调用的一句话说明。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterable

COMMANDS = {
    "/status": "status", "/状态": "status",
    "/stop": "stop", "/停止": "stop",
    "/pause": "pause", "/急停": "pause",
    "/resume": "resume", "/恢复": "resume",
    "/help": "help", "/帮助": "help",
    "/sessions": "sessions", "/会话": "sessions",
    "/new": "detach", "/新会话": "detach",
    "/history": "history", "/记录": "history",
    "/open": "open", "/电脑打开": "open",
}

# 机器人底部菜单：飞书「发送文字消息」动作会以你的身份把菜单名原样发过来（中/英/日跟着客户端语言），
# 整句对上才算指令，免得一句正常的话被误认
MENU_WORDS = {
    "会话列表": "sessions", "切换会话": "sessions", "最新记录": "history",
    "电脑上打开": "open", "新会话": "detach",
    "session list": "sessions", "switch session": "sessions", "latest": "history",
    "open on pc": "open", "new session": "detach",
    "セッション一覧": "sessions", "セッション切替": "sessions", "最新記録": "history",
    "pcで開く": "open", "新規セッション": "detach",
}


def clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def extract_text(message_type: str, content: str) -> str | None:
    """文字、富文本消息取出纯文字；图片、文件、表情等返回 None。"""
    try:
        data = json.loads(content or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if message_type == "text":
        return str(data.get("text") or "").strip()
    if message_type == "post":
        body = data
        if "content" not in body:  # 按语言包了一层：{"zh_cn": {...}}
            body = next((v for v in data.values() if isinstance(v, dict) and "content" in v), {})
        lines = [str(body["title"])] if body.get("title") else []
        for para in body.get("content") or []:
            parts = []
            for el in para or []:
                tag = el.get("tag")
                if tag in ("text", "a", "code_block", "md"):
                    parts.append(str(el.get("text", "")))
                elif tag == "at":
                    parts.append("@" + str(el.get("user_name", "")))
            lines.append("".join(parts))
        return "\n".join(lines).strip()
    return None


def extract_attachments(message_type: str, content: str) -> list[tuple[str, str, str]]:
    """消息里的图片、文件：[(image|file, key, 原文件名)]。富文本里夹的图也算。"""
    try:
        data = json.loads(content or "{}")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    if message_type == "image" and data.get("image_key"):
        return [("image", str(data["image_key"]), "")]
    if message_type == "file" and data.get("file_key"):
        return [("file", str(data["file_key"]), str(data.get("file_name") or ""))]
    if message_type == "post":
        body = data if "content" in data else next(
            (v for v in data.values() if isinstance(v, dict) and "content" in v), {})
        return [("image", str(el["image_key"]), "") for para in body.get("content") or [] for el in para or []
                if isinstance(el, dict) and el.get("tag") == "img" and el.get("image_key")]
    return []


def image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name[:120] or "附件"


def parse_command(text: str) -> str | None:
    menu = MENU_WORDS.get(text.strip().lower())
    if menu:
        return menu
    head = text.strip().split(maxsplit=1)
    return COMMANDS.get(head[0].lower()) if head else None


def command_arg(text: str) -> str:
    """指令后面跟的参数，比如「/会话 3」的「3」；菜单发来的整句没有参数。"""
    if text.strip().lower() in MENU_WORDS:
        return ""
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def minutes_old(create_time_ms: int | str | None, now: float | None = None) -> float:
    """消息发出到现在过了几分钟。"""
    try:
        ms = int(create_time_ms or 0)
    except (TypeError, ValueError):
        return 0.0
    if ms <= 0:
        return 0.0
    return max(0.0, ((now if now is not None else time.time()) * 1000 - ms) / 60000)


# ---------- 脱敏：所有发往飞书的文字都过一遍 ----------
_REDACT = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[私钥已打码]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), "sk-ant-***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-***"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}"), "gh_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "xox-***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[JWT已打码]"),
    (re.compile(r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|app[_\-]?secret)"
                r"|密码|口令|密钥)(\s*[\"']?\s*[:：=]\s*[\"']?)([^\s\"'，,；;]{4,})"), r"\1\2***"),
]


def redact(text: str, extra: Iterable[str] = ()) -> str:
    if not text:
        return text
    for s in extra:
        if s and len(s) >= 8:
            text = text.replace(s, "***")
    for pat, rep in _REDACT:
        text = pat.sub(rep, text)
    return text


# ---------- send_file 路径校验 ----------
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_SENSITIVE_PART = re.compile(
    r"(?i)^(\.ssh|\.gnupg|\.credentials\.json|mt_deploy|mt_img_key\.txt|master\.key"
    r"|id_(rsa|ed25519|ecdsa|dsa)(\.pub)?|\.env(\..+)?|.*secret.*|.*\.pem|.*\.key"
    r"|keychains|.*\.keychain(-db)?)$")  # 后两个是 Mac 钥匙串


def _under(p: Path, root: Path) -> bool:
    try:
        root = Path(root).resolve()
    except OSError:
        return False
    return p == root or root in p.parents


def check_send_path(path: str, roots: Iterable[Path]) -> tuple[bool, str, Path | None]:
    """能不能把这个文件发到飞书：(可以吗, 不行的原因, 规范化后的路径)。"""
    if not path:
        return False, "没给文件路径", None
    try:
        p = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return False, f"文件不存在：{path}", None
    if not p.is_file():
        return False, f"不是文件：{p}", None
    roots = list(roots)
    if not any(_under(p, r) for r in roots):
        return False, "只能发这些目录下的文件：" + "、".join(str(r) for r in roots), None
    if any(_SENSITIVE_PART.match(part) for part in p.parts):
        return False, "看起来是密钥或凭据类文件，不发", None
    size = p.stat().st_size
    if size == 0:
        return False, "文件是空的", None
    if size > MAX_FILE_BYTES:
        return False, "文件超过 30MB，飞书发不了", None
    return True, "", p


def feishu_file_type(p: Path) -> str:
    return {".pdf": "pdf", ".doc": "doc", ".docx": "doc", ".xls": "xls", ".xlsx": "xls",
            ".ppt": "ppt", ".pptx": "ppt", ".mp4": "mp4", ".opus": "opus"}.get(p.suffix.lower(), "stream")


# ---------- 工具调用说明 ----------
_TOOL_KEY = {
    "Bash": "command", "PowerShell": "command", "Read": "file_path", "Edit": "file_path",
    "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path",
    "Glob": "pattern", "Grep": "pattern", "WebFetch": "url", "WebSearch": "query",
    "Task": "description", "Agent": "description", "Skill": "skill",
}


def tool_label(name: str) -> str:
    if name.startswith("mcp__"):
        return name[5:].replace("__", "·")
    return name


def tool_line(name: str, inp: dict | None, limit: int = 120) -> str:
    """进度卡上的一行：工具名 · 在干什么。"""
    inp = inp or {}
    if name in ("Bash", "PowerShell") and inp.get("description"):
        val = inp["description"]
    else:
        key = _TOOL_KEY.get(name)
        val = inp.get(key, "") if key else ""
        if not val and inp:
            val = json.dumps(inp, ensure_ascii=False)
    val = " ".join(str(val).split())
    return clip(f"{tool_label(name)} · {val}" if val else tool_label(name), limit)


def approval_detail(name: str, inp: dict | None, limit: int = 1500) -> tuple[str, str]:
    """审批卡上要给你看的内容：(详情, 一句话说明)。"""
    inp = inp or {}
    desc = str(inp.get("description", "")) if name in ("Bash", "PowerShell") else ""
    if name in ("Bash", "PowerShell"):
        body = str(inp.get("command", ""))
    elif name == "Write":
        body = f"写入 {inp.get('file_path', '')}\n\n{clip(str(inp.get('content', '')), 600)}"
    elif name in ("Edit", "MultiEdit"):
        if name == "MultiEdit":
            edits = inp.get("edits") or []
            old = "\n".join(str(e.get("old_string", "")) for e in edits)
            new = "\n".join(str(e.get("new_string", "")) for e in edits)
        else:
            old, new = str(inp.get("old_string", "")), str(inp.get("new_string", ""))
        body = f"修改 {inp.get('file_path', '')}\n- {clip(old, 300)}\n+ {clip(new, 300)}"
    elif name == "WebFetch":
        body = f"{inp.get('url', '')}\n{clip(str(inp.get('prompt', '')), 200)}"
    else:
        body = json.dumps(inp, ensure_ascii=False, indent=1)
    return clip(body, limit), clip(desc, 200)
