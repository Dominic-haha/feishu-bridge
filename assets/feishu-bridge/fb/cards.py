"""飞书卡片（JSON 2.0）。卡片都带 update_multi，后面才能 PATCH 更新；单张卡片上限 30KB。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .policy import clip, tool_label

STATUS = {
    "confirm": ("❓ 要执行吗？", "yellow"),
    "queued": ("⏳ 排队中", "grey"),
    "running": ("🏃 执行中", "blue"),
    "waiting": ("🔐 等你批准", "orange"),
    "done": ("✅ 完成", "green"),
    "failed": ("❌ 失败", "red"),
    "stopped": ("⏹ 已停止", "grey"),
    "interrupted": ("⚠️ 中断了", "orange"),
    "ignored": ("已忽略", "grey"),
}

DECISION = {
    "allow": ("✅ 已允许", "green"),
    "allow_all": ("✅ 本任务内都允许", "green"),
    "deny": ("🚫 已拒绝", "red"),
    "timeout": ("⌛ 超时没批，按拒绝处理", "grey"),
    "cancelled": ("已作废（任务已结束）", "grey"),
}


@dataclass
class Progress:
    """执行中卡片要显示的东西。"""
    started: float = field(default_factory=time.time)
    steps: list[str] = field(default_factory=list)
    n_tools: int = 0
    last_text: str = ""
    note: str = ""
    waiting: bool = False


def fmt_elapsed(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"


def size(card: dict) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def _card(title: str, template: str, elements: list[dict]) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {"elements": elements},
    }


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def _grey(text: str) -> dict:
    return _md(f"<font color='grey'>{text}</font>")


def _button(text: str, value: dict, kind: str = "default") -> dict:
    return {
        "tag": "button",
        "type": kind,
        "size": "medium",
        "text": {"tag": "plain_text", "content": text},
        "behaviors": [{"type": "callback", "value": value}],
    }


def _buttons(*btns: dict) -> dict:
    return {"tag": "column_set", "flex_mode": "flow",
            "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in btns]}


def _code(text: str) -> str:
    # 内容里自带 ``` 会把代码块提前截断
    return "```\n" + (text or "").replace("```", "ʼʼʼ") + "\n```"


def _head(task: dict, limit: int = 300) -> dict:
    return _md(f"**#{task['id']}** {clip(task.get('prompt', ''), limit)}")


def _stats(task: dict) -> str:
    parts = []
    if task.get("started_at") and task.get("finished_at"):
        parts.append("耗时 " + fmt_elapsed(task["finished_at"] - task["started_at"]))
    if task.get("num_turns"):
        parts.append(f"{task['num_turns']} 轮")
    if task.get("cost_usd"):
        parts.append(f"约 ${task['cost_usd']:.2f}")
    return " · ".join(parts)


def status_card(task: dict, state: str, *, progress: Progress | None = None, ahead: int = 0,
                note: str = "", buttons: bool = True, label: str = "") -> dict:
    title, tpl = STATUS[state]
    els = [_head(task)]
    if label:
        els.append(_grey(f"↪ {label}"))
    if state == "confirm":
        els.append(_md(f"⚠️ {task.get('reason', '')}"))
        if buttons:
            els.append(_buttons(
                _button("执行", {"k": "confirm", "t": task["id"], "d": "run"}, "primary"),
                _button("忽略", {"k": "confirm", "t": task["id"], "d": "ignore"})))
        else:
            els.append(_md("引用回复这张卡片：回 **执行** 或 **忽略**"))
    elif state == "queued":
        els.append(_grey(f"前面还有 {ahead} 个任务" if ahead else "马上开始"))
    elif state in ("running", "waiting"):
        p = progress or Progress()
        if p.steps:
            els.append(_md("\n".join(f"· {s}" for s in p.steps[-6:])))
        if p.last_text:
            els.append(_grey(clip(" ".join(p.last_text.split()), 160)))
        stat = f"已运行 {fmt_elapsed(time.time() - p.started)} · 调用工具 {p.n_tools} 次"
        if p.note:
            stat += " · " + p.note
        if not buttons:
            stat += " · 发 /停止 可以停掉"
        els.append(_grey(stat))
        if buttons:
            els.append(_buttons(_button("停止", {"k": "stop", "t": task["id"]}, "danger")))
    else:
        line = _stats(task)
        if note:
            line = f"{line} · {note}" if line else note
        if line:
            els.append(_grey(line))
    return _card(title, tpl, els)


def result_card(task: dict, state: str, text: str, *, error: str = "", attached: str = "", label: str = "") -> dict:
    """任务结束后单独发的结果卡（新消息，手机会响）。text、error 由调用方先脱敏、截断。"""
    title, tpl = STATUS[state]
    els = [_head(task, 120)]
    if label:
        els.append(_grey(f"↪ {label}"))
    els.append({"tag": "hr"})
    if text:
        els.append(_md(text))
    if attached:
        els.append(_grey(f"完整结果较长，已作为文件「{attached}」发送"))
    if error:
        els.append(_md("**出错**：" + clip(error, 800)))
    stats = _stats(task)
    els.append(_grey((stats + " · " if stats else "") + "引用回复这条消息＝接着聊"))
    return _card(title, tpl, els)


def approval_card(approval_id: str, task: dict, tool_name: str, detail: str, desc: str,
                  timeout_s: float, buttons: bool = True) -> dict:
    head = f"**{tool_label(tool_name)}**" + (f"：{desc}" if desc else "")
    minutes = max(1, int(round(timeout_s / 60)))
    els = [
        _md(head),
        _md(_code(detail)),
        _grey(f"任务 #{task['id']}：{clip(task.get('prompt', ''), 80)}"),
    ]
    if buttons:
        els += [
            _grey(f"{minutes} 分钟内不点＝拒绝。「本任务都允许」只管这一个任务，结束就失效。"),
            _buttons(
                _button("允许", {"k": "ap", "a": approval_id, "d": "allow"}, "primary"),
                _button("本任务都允许", {"k": "ap", "a": approval_id, "d": "allow_all"}),
                _button("拒绝", {"k": "ap", "a": approval_id, "d": "deny"}, "danger")),
        ]
    else:
        els += [
            _md("引用回复这张卡片：回 **1** 允许　**2** 本任务都允许　**0** 拒绝"),
            _grey(f"只有这一个审批在等时，直接发「允许」「拒绝」也行。{minutes} 分钟内不回＝拒绝；"
                  "「本任务都允许」只管这一个任务，结束就失效。"),
        ]
    return _card("🔐 需要你批准", "orange", els)


def approval_done_card(task_id: int, tool_name: str, detail: str, decision: str) -> dict:
    title, tpl = DECISION.get(decision, DECISION["deny"])
    return _card(title, tpl, [
        _md(f"**{tool_label(tool_name)}**"),
        _md(_code(clip(detail, 600))),
        _grey(f"任务 #{task_id} · {time.strftime('%H:%M:%S')}"),
    ])


def info_card(title: str, lines: list[str], template: str = "blue") -> dict:
    return _card(title, template, [_md("\n".join(lines))])


def _short_dir(cwd: str) -> str:
    cwd = (cwd or "").rstrip("\\/")
    return cwd.replace("\\", "/").rsplit("/", 1)[-1] or cwd


def sessions_card(items: list[dict], buttons: bool = True) -> dict:
    """电脑上的会话列表。items 每项：sid、title、cwd、source、ago。"""
    if not items:
        return _card("电脑上的会话", "blue", [_md("没找到能接着聊的会话。")])
    els = []
    for i, s in enumerate(items, 1):
        where = s["source"] + (f" · {_short_dir(s['cwd'])}" if s.get("cwd") else "")
        line = _md(f"**{i}.** {clip(s['title'], 40)}\n<font color='grey'>{where} · {s['ago']}</font>")
        if buttons:
            els.append({"tag": "column_set", "flex_mode": "none", "columns": [
                {"tag": "column", "width": "weighted", "weight": 4, "vertical_align": "center", "elements": [line]},
                {"tag": "column", "width": "auto", "vertical_align": "center",
                 "elements": [_button("接着聊", {"k": "sess", "s": s["sid"]}, "primary")]}]})
        else:
            els.append(line)
    els.append(_grey(("点「接着聊」，或者" if buttons else "") + "引用回复这张卡片发编号，或者发「/会话 3」。"
                     "接上以后直接发消息就是接着那个会话聊，发 /新会话 退出。"))
    return _card("电脑上的会话", "blue", els)


def history_card(title: str, turns: list[tuple[str, str]], *, header: str = "已接上", warn: str = "",
                 tip: str = "") -> dict:
    """接上会话时、/记录 时推给你的最近几轮对话。turns 由调用方先脱敏。"""
    els = []
    if warn:
        els.append(_md(f"⚠️ {warn}"))
    if not turns:
        els.append(_grey("（这个会话还没有能显示的对话）"))
    for role, text in turns:
        els.append(_md(f"**{role}**：{clip(' '.join(text.split()) if role == '你' else text, 400)}"))
    if tip:
        els.append(_grey(tip))
    return _card(f"{header}：{clip(title, 30)}", "turquoise", els)
