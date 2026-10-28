"""飞书传输层：长连接（WebSocket，本机主动连出去）收消息和卡片回调，REST 接口发消息。

需要的应用权限：im:message.p2p_msg:readonly（收单聊）、im:message:send_as_bot（发/回/改卡片）、
im:resource（上传图片和文件）、im:message:readonly（下载你发来的图片和文件）。事件订阅 im.message.receive_v1，
回调配置 card.action.trigger，两处都选「使用长连接接收」。要用普通的企业自建应用：妙搭、Aily、
「创建飞书智能体应用」这类平台一键建的应用，底部菜单归平台管，开发者后台配的不生效。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
import uuid
from pathlib import Path

import requests

from .policy import feishu_file_type
from .transport import Action, ActionReply, Incoming, Transport

API = "https://open.feishu.cn/open-apis"
log = logging.getLogger("fb.feishu")

_TOKEN_INVALID = {99991663, 99991664, 99991665, 99991661}
_RATE_LIMITED = {99991400, 230020}


class _AckUnhandled:
    """应用订阅了、但飞书桥没有处理器的事件（表情回复、机器人进群、文档评论……）也回「收到」。
    SDK 默认会抛 processor not found、回 500，飞书就会一直重推。"""

    def __init__(self, inner) -> None:
        self._inner = inner

    def _do_without_validation(self, payload: bytes):
        from lark_oapi.core.exception import EventException
        try:
            return self._inner._do_without_validation(payload)
        except EventException as e:
            if "processor not found" not in str(e):
                raise
            log.debug("忽略不处理的事件：%s", e)
            return None

    def __getattr__(self, name):
        return getattr(self._inner, name)


class FeishuError(RuntimeError):
    pass


class _RedactWsUrl(logging.Filter):
    """lark SDK 连上时会把带 access_key、ticket 的长连接地址打进日志，打码。"""
    _pat = re.compile(r"(access_key|ticket)=[^&\s\]]+")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "access_key=" in msg or "ticket=" in msg:
            record.msg, record.args = self._pat.sub(r"\1=***", msg), ()
        return True


def build_handler(on_message, on_action):
    """飞书事件分发器：收消息、卡片回调交给飞书桥，其余事件只回「收到」。"""
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    def h_message(data) -> None:
        try:
            ev = data.event
            s, m = ev.sender, ev.message
            if s is not None and s.sender_type and s.sender_type != "user":
                return  # 别的机器人、应用发的不理
            on_message(Incoming(
                msg_id=m.message_id or "",
                open_id=(s.sender_id.open_id if s and s.sender_id else "") or "",
                chat_id=m.chat_id or "", chat_type=m.chat_type or "", msg_type=m.message_type or "",
                content=m.content or "{}", create_time_ms=int(m.create_time or 0),
                parent_id=m.parent_id or "", root_id=m.root_id or ""))
        except Exception:
            log.exception("处理消息事件出错")

    def h_card(data):
        try:
            ev = data.event
            rep = on_action(Action(
                open_id=(ev.operator.open_id if ev.operator else "") or "",
                value=(ev.action.value if ev.action else None) or {},
                msg_id=(ev.context.open_message_id if ev.context else "") or ""))
        except Exception:
            log.exception("处理卡片回调出错")
            rep = ActionReply("出错了，详情看电脑上的日志", "error")
        body: dict = {}
        if rep.toast:
            body["toast"] = {"type": rep.toast_type, "content": rep.toast}
        if rep.card is not None:
            body["card"] = {"type": "raw", "data": rep.card}
        return P2CardActionTriggerResponse(body)

    return _AckUnhandled(lark.EventDispatcherHandler.builder("", "")
                         .register_p2_im_message_receive_v1(h_message)
                         .register_p2_card_action_trigger(h_card)
                         .build())


class FeishuTransport(Transport):
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self._secret = app_secret
        self._token = ""
        self._token_exp = 0.0
        self._lock = threading.Lock()
        self._http = requests.Session()
        self._http.trust_env = False  # 飞书是国内服务，不走系统代理
        self._ws = None

    # ---------- REST ----------
    def _tenant_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 300:
                return self._token
            r = self._http.post(f"{API}/auth/v3/tenant_access_token/internal",
                                json={"app_id": self.app_id, "app_secret": self._secret}, timeout=15)
            d = r.json()
            if d.get("code") != 0:
                raise FeishuError(f"取 tenant_access_token 失败：{d.get('code')} {d.get('msg')}")
            self._token = d["tenant_access_token"]
            self._token_exp = time.time() + int(d.get("expire", 7200))
            return self._token

    def _call(self, method: str, path: str, *, params=None, json_body=None, data=None, files=None,
              retries: int = 3, raw: bool = False) -> dict:
        last: Exception | None = None
        for i in range(retries):
            try:
                r = self._http.request(method, API + path, params=params, json=json_body, data=data,
                                       files=files,
                                       headers={"Authorization": f"Bearer {self._tenant_token()}"},
                                       timeout=30)
                d = r.json()
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(1 + 2 * i)
                continue
            code = d.get("code")
            if code == 0:
                return d if raw else (d.get("data") or {})
            if code in _TOKEN_INVALID:
                self._token_exp = 0
                last = FeishuError(f"{code} {d.get('msg')}")
                continue
            if code in _RATE_LIMITED or r.status_code == 429:
                last = FeishuError(f"限频 {code}")
                time.sleep(1 + 2 * i)
                continue
            raise FeishuError(f"{method} {path} → {code} {d.get('msg')}")
        raise FeishuError(f"{method} {path} 连续失败：{last}")

    def _send(self, open_id: str, msg_type: str, content: dict, reply_to: str = "") -> str:
        body = {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False),
                "uuid": uuid.uuid4().hex}  # 同一个 uuid 重试时飞书只发一次
        if reply_to:
            d = self._call("POST", f"/im/v1/messages/{reply_to}/reply", json_body=body)
        else:
            d = self._call("POST", "/im/v1/messages", params={"receive_id_type": "open_id"},
                           json_body={"receive_id": open_id, **body})
        return d.get("message_id", "")

    def send_card(self, open_id, card):
        return self._send(open_id, "interactive", card)

    def reply_card(self, msg_id, card):
        return self._send("", "interactive", card, reply_to=msg_id)

    def update_card(self, msg_id, card):
        self._call("PATCH", f"/im/v1/messages/{msg_id}",
                   json_body={"content": json.dumps(card, ensure_ascii=False)}, retries=2)

    def send_text(self, open_id, text):
        return self._send(open_id, "text", {"text": text})

    def reply_text(self, msg_id, text):
        return self._send("", "text", {"text": text}, reply_to=msg_id)

    def send_image(self, open_id, path, reply_to=""):
        p = Path(path)
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        d = self._call("POST", "/im/v1/images", data={"image_type": "message"},
                       files={"image": (p.name, p.read_bytes(), mime)})
        return self._send(open_id, "image", {"image_key": d["image_key"]}, reply_to=reply_to)

    def send_file(self, open_id, path, reply_to=""):
        p = Path(path)
        d = self._call("POST", "/im/v1/files", data={"file_type": feishu_file_type(p), "file_name": p.name},
                       files={"file": (p.name, p.read_bytes(), "application/octet-stream")})
        return self._send(open_id, "file", {"file_key": d["file_key"]}, reply_to=reply_to)

    def download_resource(self, msg_id, key, kind):
        r = self._http.get(f"{API}/im/v1/messages/{msg_id}/resources/{key}", params={"type": kind},
                           headers={"Authorization": f"Bearer {self._tenant_token()}"}, timeout=120)
        if r.status_code != 200 or r.headers.get("Content-Type", "").startswith("application/json"):
            try:
                d = r.json()
                raise FeishuError(f"下载附件失败：{d.get('code')} {d.get('msg')}")
            except ValueError:
                raise FeishuError(f"下载附件失败：HTTP {r.status_code}")
        return r.content

    def check(self) -> dict:
        """自检：凭据能不能换到 token、机器人能力开没开。"""
        d = self._call("GET", "/bot/v3/info", raw=True, retries=1)
        return d.get("bot") or {}

    # ---------- 长连接 ----------
    def run(self, on_message, on_action):
        import lark_oapi as lark

        logging.getLogger("Lark").addFilter(_RedactWsUrl())
        self._ws = lark.ws.Client(self.app_id, self._secret, event_handler=build_handler(on_message, on_action),
                                  log_level=lark.LogLevel.INFO)
        self._ws.on_reconnecting = lambda: log.warning("飞书长连接断了，正在重连")
        self._ws.on_reconnected = lambda: log.info("飞书长连接已恢复")
        log.info("连接飞书长连接…")
        self._ws.start()  # 阻塞；SDK 自己断线重连
