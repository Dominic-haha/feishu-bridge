"""审批中转站：claude 拉起的审批 MCP（mcp_server.py）通过本机 HTTP 来问飞书桥要决定。

只监听 127.0.0.1 的随机端口；令牌每次启动重新生成，经 mcp.json 的 env 交给 MCP 进程。
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger("fb.broker")


@dataclass
class Pending:
    id: str
    task_id: int
    tool_name: str
    tool_input: dict
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = ""  # allow / allow_all / deny / timeout / cancelled
    by: str = ""
    card_msg_id: str = ""


def _deny(message: str) -> dict:
    return {"behavior": "deny", "message": message}


class Broker:
    def __init__(self, *, ask_user: Callable[[Pending], None],
                 send_file: Callable[[int, str, str], tuple[bool, str]],
                 on_timeout: Callable[[Pending], None], timeout_s: float) -> None:
        self.ask_user = ask_user          # 发审批卡；不该批（不是当前任务等）就抛异常
        self.send_file_cb = send_file
        self.on_timeout = on_timeout      # 超时后把审批卡改成「超时」
        self.timeout_s = timeout_s
        self.token = secrets.token_urlsafe(24)
        self._pending: dict[str, Pending] = {}
        self._allow_all: set[int] = set()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, name="broker", daemon=True).start()

    # ---------- 审批 ----------
    def ask(self, task_id: int, tool_name: str, tool_input: dict) -> dict:
        """阻塞到你在飞书上点了按钮、超时或任务被停。返回 --permission-prompt-tool 要的格式。"""
        if task_id in self._allow_all:
            return {"behavior": "allow", "updatedInput": tool_input}
        p = Pending(id=uuid.uuid4().hex[:12], task_id=task_id, tool_name=tool_name, tool_input=tool_input)
        with self._lock:
            self._pending[p.id] = p
        try:
            try:
                self.ask_user(p)
            except Exception as e:
                log.warning("审批卡没发出去：%s", e)
                return _deny(f"没能把审批卡发到飞书（{e}），这个操作按拒绝处理。")
            if not p.event.wait(self.timeout_s):
                with self._lock:
                    timed_out = not p.decision
                    if timed_out:
                        p.decision = "timeout"
                if timed_out:
                    try:
                        self.on_timeout(p)
                    except Exception:
                        log.exception("更新超时审批卡失败")
                    return _deny(f"用户 {max(1, int(self.timeout_s // 60))} 分钟内没有批准，按拒绝处理。"
                                 "不要重复申请同一个操作。")
        finally:
            with self._lock:
                self._pending.pop(p.id, None)
        if p.decision == "allow_all":
            self._allow_all.add(task_id)
        if p.decision in ("allow", "allow_all"):
            return {"behavior": "allow", "updatedInput": tool_input}
        if p.decision == "cancelled":
            return _deny("任务已被用户停止。")
        return _deny("用户在手机上拒绝了这个操作。不要重复申请，换个办法，或者停下来说明原因。")

    def decide(self, approval_id: str, decision: str, by: str) -> Pending | None:
        """按钮回调来了。已经处理过、超时或不存在返回 None。"""
        with self._lock:
            p = self._pending.get(approval_id)
            if p is None or p.decision:
                return None
            p.decision, p.by = decision, by
        p.event.set()
        return p

    def cancel_task(self, task_id: int) -> list[Pending]:
        """任务停了：它还在等的审批一律作废。"""
        with self._lock:
            ps = [p for p in self._pending.values() if p.task_id == task_id and not p.decision]
            for p in ps:
                p.decision = "cancelled"
        for p in ps:
            p.event.set()
        self._allow_all.discard(task_id)
        return ps

    def end_task(self, task_id: int) -> None:
        self._allow_all.discard(task_id)

    def has_pending(self, task_id: int) -> bool:
        with self._lock:
            return any(p.task_id == task_id and not p.decision for p in self._pending.values())

    def undecided(self) -> list[Pending]:
        """还在等你决定的审批（回文字审批时用）。"""
        with self._lock:
            return [p for p in self._pending.values() if not p.decision]

    def find_by_card(self, msg_id: str) -> Pending | None:
        """你引用回复的是哪张审批卡。"""
        with self._lock:
            return next((p for p in self._pending.values() if msg_id and p.card_msg_id == msg_id), None)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ---------- HTTP ----------
    def _make_handler(self):
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("broker " + fmt, *args)

            def _reply(self, code: int, obj: dict) -> None:
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                given = self.headers.get("X-Bridge-Token", "").encode("utf-8", "replace")
                if not hmac.compare_digest(given, broker.token.encode("utf-8")):
                    return self._reply(403, {"error": "forbidden"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                    task_id = int(body.get("task_id") or 0)
                except (ValueError, TypeError):
                    return self._reply(400, {"error": "bad request"})
                if self.path == "/approve":
                    inp = body.get("input") if isinstance(body.get("input"), dict) else {}
                    return self._reply(200, broker.ask(task_id, str(body.get("tool_name") or ""), inp))
                if self.path == "/send_file":
                    try:
                        ok, msg = broker.send_file_cb(task_id, str(body.get("path") or ""),
                                                      str(body.get("caption") or ""))
                    except Exception as e:
                        log.exception("send_file 出错")
                        ok, msg = False, f"发送失败：{e}"
                    return self._reply(200, {"ok": ok, "message": msg})
                return self._reply(404, {"error": "not found"})

        return Handler
