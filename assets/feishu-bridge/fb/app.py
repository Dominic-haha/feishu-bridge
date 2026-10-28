"""飞书桥主体：收消息 → 过规则 → 排队 → 串行执行 → 进度、审批、结果回飞书。

线程：
  长连接线程（transport.run）  收消息、卡片回调；必须 3 秒内返回，所以只做去重和派活
  intake                       逐条处理收到的消息（规则、建任务、回状态卡）
  worker                       串行跑任务（桌面和浏览器只有一份，不并发）
  broker                       审批 MCP 的 HTTP 请求，阻塞等你点按钮
  heartbeat / redeliver        记在线时间；补发当时没发出去的结果
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cards, policy, sessions
from .broker import Broker, Pending
from .cards import Progress
from .config import Config
from .executor import RunResult, Runner, _classify, clean_env
from .osutil import PY
from .store import FINAL, Store
from .transport import Action, ActionReply, Incoming, Transport

log = logging.getLogger("fb.app")

HELP = [
    "直接发文字＝派一个任务，在这台电脑上用 Claude Code 执行。",
    "**引用回复**某个任务的卡片＝在同一个会话里接着聊。",
    "需要授权的操作会发审批卡给你，不点就按拒绝处理。",
    "`/状态`  看正在跑和排队的任务",
    "`/停止`  停掉正在跑的任务",
    "`/急停`  停掉当前任务、清空队列并暂停接单",
    "`/恢复`  恢复接单",
    "`/会话`  列出电脑上的会话（桌面版侧边栏的 + 手机上开的），点「接着聊」或回编号就接上",
    "`/记录`  看接上的会话最新几轮对话（电脑上聊过的也能看到）",
    "`/电脑打开`  在电脑的 Claude 桌面版里打开这个会话",
    "`/新会话`  不接着了，之后每条消息都新开会话",
    "底部菜单「会话」「切换会话」就是上面这几个会话指令的快捷键。",
]

_HINT = {
    "auth": f"Claude 登录失效：在电脑上跑 {PY} bridge.py --login 重新登录。",
    "limit": "Claude 额度用完了，晚点再试。",
}

# 回文字审批、确认（应用没配卡片回调时只能这样；配了也照样能用）
_ALLOW = {"1", "允许", "同意", "可以", "ok", "y", "yes"}
_ALLOW_ALL = {"2", "都允许", "全部允许", "本任务都允许"}
_DENY = {"0", "拒绝", "不行", "不同意", "n", "no"}
_RUN = {"1", "执行", "确认", "run"}
_IGNORE = {"0", "忽略", "取消"}
_REPLY_WORDS = _ALLOW | _ALLOW_ALL | _DENY | _RUN | _IGNORE


def _norm(text: str) -> str:
    return text.strip().strip("。.!！~～ ").lower()


# 只发了图片/文件、没说要干嘛时给 Claude 的话
_LOOK = "看看这些附件，说说里面是什么；如果是报错、数据或截图里有问题，给出你的判断和建议。"


class Bridge:
    def __init__(self, cfg: Config, store: Store, transport: Transport, *, token_getter=None,
                 claude_cmd: list[str] | None = None, secrets=(), opener=None) -> None:
        self.cfg, self.store, self.tx = cfg, store, transport
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.q: queue.Queue[int] = queue.Queue()
        self.paused = store.kv_get("paused") == "1"
        # 接上的电脑会话：{sid, title, cwd, source, last_active}；之后直接发的消息都接着它聊
        self.attached: dict | None = json.loads(store.kv_get("attached") or "null")
        # 在桌面版里打开会话；测试时换成假的
        self.opener = opener or (lambda sid, cwd: sessions.open_in_desktop(
            sid, self.runner.claude_cmd(), clean_env(self.cfg, None), cwd))
        # 你发来、还没配上文字的附件：{open_id: {paths, msg_id, chat_id, timer}}
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        # 这个会话是不是正被电脑上别的 Claude 进程开着；测试时换成假的
        self.open_checker = sessions.open_elsewhere
        self.current: dict | None = None
        self.progress: Progress | None = None
        self._secrets = [s for s in secrets if s]
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()
        self._card_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._last_push = 0.0
        self._dirty = False
        self._stop_evt = threading.Event()
        self._intake = ThreadPoolExecutor(max_workers=1, thread_name_prefix="intake")
        self.broker = Broker(ask_user=self._ask_user, send_file=self._send_file,
                             on_timeout=self._approval_timeout, timeout_s=cfg.approval_timeout_seconds)
        self.runner = Runner(cfg, broker_url=self.broker.url, broker_token=self.broker.token,
                             token_getter=token_getter, claude_cmd=claude_cmd)

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self.recover()
        for name, fn in (("worker", self._worker), ("heartbeat", self._heartbeat),
                         ("redeliver", self._redeliver_loop)):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def run_forever(self) -> None:
        self.start()
        self.tx.run(self.on_message, self.on_action)

    def shutdown(self) -> None:
        self._stop_evt.set()
        with self._pending_lock:
            for p in self._pending.values():
                if p.get("timer"):
                    p["timer"].cancel()
        self._stop_current()
        self.broker.close()
        self._intake.shutdown(wait=False, cancel_futures=True)

    def recover(self) -> None:
        """启动时：上次没跑完的任务标成中断；离线久了提醒你重发。"""
        stale = self.store.list_by_status("running", "queued")
        for t in stale:
            self.store.update(t["id"], status="interrupted", error="飞书桥重启，任务中断",
                              finished_at=time.time(), delivered=1)
            if t.get("card_msg_id"):
                self._try(self.tx.update_card, t["card_msg_id"],
                          self._status(self.store.get(t["id"]), "interrupted", note="飞书桥重启，任务中断"))
        last = float(self.store.kv_get("last_alive") or 0)
        offline_min = (time.time() - last) / 60 if last else 0
        parts = []
        if stale:
            parts.append(f"有 {len(stale)} 个任务因为重启被中断（{'、'.join('#%d' % t['id'] for t in stale)}），"
                         "需要的话请重发。")
        if offline_min >= 2:
            parts.append(f"刚才离线约 {offline_min:.0f} 分钟，这期间发的消息可能没收到，没回复的请重发。")
        if parts:
            for oid in self.cfg.allowed_open_ids:
                self._try(self.tx.send_text, oid, "🟢 飞书桥已上线。" + "".join(parts))
        self.store.kv_set("last_alive", str(time.time()))

    # ---------- 收消息 ----------
    def on_message(self, m: Incoming) -> None:
        with self._seen_lock:  # 飞书 3 秒没收到回应会重推
            if m.msg_id in self._seen:
                return
            self._seen[m.msg_id] = None
            while len(self._seen) > 2000:
                self._seen.popitem(last=False)
        self._intake.submit(self._guard, self._process_message, m)

    def _process_message(self, m: Incoming, attachments: list | None = None) -> None:
        if m.chat_type != "p2p":  # 机器人能读它所在的群，群消息量大，只记调试日志
            log.debug("忽略非单聊消息 chat=%s", m.chat_id)
            return
        if not self.cfg.allowed_open_ids:
            log.warning("配对模式：收到 open_id=%s 的消息", m.open_id)
            self._try(self.tx.reply_text, m.msg_id,
                      f"飞书桥还没配置白名单，暂不执行任何任务。\n你的 open_id 是：{m.open_id}\n"
                      "把它写进 config.toml 的 allowed_open_ids，再重启飞书桥。")
            return
        if m.open_id not in self.cfg.allowed_open_ids:
            log.warning("拒绝白名单外的发送人 open_id=%s", m.open_id)
            self.audit("reject_sender", open_id=m.open_id, msg_id=m.msg_id)
            return  # 不回话，免得暴露飞书桥的存在
        if attachments is None and m.msg_type in ("image", "file"):  # 先攒着，等你下一句话
            self._buffer_attachment(m)
            return
        text = policy.extract_text(m.msg_type, m.content)
        if text is None:
            self._try(self.tx.reply_text, m.msg_id, "这种消息暂时不支持。可以发文字、图片或文件。")
            return
        inline: list = []
        if attachments is None and m.msg_type == "post":  # 富文本里夹的图：直接和文字一起处理
            items = policy.extract_attachments(m.msg_type, m.content)
            if items:
                try:
                    inline = self._save_attachments(m, items)
                except Exception as e:
                    log.warning("下载附件失败：%s", e)
                    self._try(self.tx.reply_text, m.msg_id, f"图片没下载下来：{self._redact(str(e))}")
                    return
        if not text and not inline and not attachments:
            return
        cmd = policy.parse_command(text) if text else None
        if cmd:
            self.audit("command", cmd=cmd)
            self._command(cmd, m, policy.command_arg(text))
            return
        if text and (self._handle_session_pick(m, text) or self._handle_reply(m, text)):
            return
        if self.paused:
            self._try(self.tx.reply_text, m.msg_id, "飞书桥处于急停状态，这条没有执行。发 /恢复 后再派任务。")
            return
        files = list(attachments or []) + inline + (self._take_pending(m.open_id) if attachments is None else [])
        text = self._with_attachments(text or _LOOK, files)

        parent = None
        for ref in (m.parent_id, m.root_id):
            parent = self.store.find_by_message(ref) if ref else None
            if parent:
                break
        if parent and parent.get("session_id"):  # 引用回复＝接着那个任务的会话
            resume, session_id, cwd = 1, parent["session_id"], parent.get("cwd") or ""
        elif self.attached:  # 接上了电脑上的会话
            resume, session_id, cwd = 1, self.attached["sid"], self.attached.get("cwd") or ""
        else:
            resume, session_id, cwd = 0, str(uuid.uuid4()), ""

        reason = ""
        age = policy.minutes_old(m.create_time_ms)
        if age >= self.cfg.stale_minutes:
            reason = f"这条消息是 {age:.0f} 分钟前发的（可能是电脑离线时积压的），确认还要执行吗？"
        else:
            dup = self.store.recent_same_prompt(m.open_id, text, time.time() - self.cfg.duplicate_seconds)
            if dup:
                reason = f"和 {int(time.time() - dup['created_at'])} 秒前的任务 #{dup['id']} 内容一样，确认再跑一次吗？"
        if not reason and resume:
            warns = self._session_warnings(session_id)
            if warns:
                reason = "；".join(warns) + "。确认还要接着吗？"
        status = "confirm" if reason else "queued"
        tid = self.store.add_task(msg_id=m.msg_id, open_id=m.open_id, chat_id=m.chat_id, prompt=text,
                                  status=status, reason=reason, parent_task=parent["id"] if parent else None,
                                  session_id=session_id, resume=resume, cwd=cwd)
        if tid is None:
            log.info("飞书重推 %s，忽略", m.msg_id)
            return
        self.audit("task", task=tid, status=status, prompt=text, parent=parent["id"] if parent else None)
        task = self.store.get(tid)
        mid = self._try(self.tx.reply_card, m.msg_id, self._status(task, status, ahead=self._ahead(tid)))
        if mid:
            self.store.update(tid, card_msg_id=mid)
        if status == "queued":
            self.q.put(tid)

    def _ahead(self, tid: int) -> int:
        n = sum(1 for t in self.store.list_by_status("queued") if t["id"] < tid)
        return n + (1 if self.current else 0)

    # ---------- 附件 ----------
    def _save_attachments(self, m: Incoming, items: list[tuple[str, str, str]]) -> list[Path]:
        day = self.cfg.inbox_dir / time.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for kind, key, name in items:
            data = self.tx.download_resource(m.msg_id, key, kind)
            if len(data) > policy.MAX_FILE_BYTES:
                raise RuntimeError("附件超过 30MB")
            fname = policy.safe_filename(name) if name else "图片" + policy.image_ext(data)
            p = day / f"{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}_{fname}"  # 同一秒连发几张也不会互相覆盖
            p.write_bytes(data)
            out.append(p)
        self.audit("attachment", msg_id=m.msg_id, files=[str(p) for p in out])
        return out

    def _buffer_attachment(self, m: Incoming) -> None:
        items = policy.extract_attachments(m.msg_type, m.content)
        if not items:
            self._try(self.tx.reply_text, m.msg_id, "这条消息里没找到图片或文件。")
            return
        try:
            paths = self._save_attachments(m, items)
        except Exception as e:
            log.warning("下载附件失败：%s", e)
            self._try(self.tx.reply_text, m.msg_id, f"附件没下载下来：{self._redact(str(e))}")
            return
        with self._pending_lock:
            p = self._pending.setdefault(m.open_id, {"paths": [], "chat_id": m.chat_id})
            p["paths"] += paths
            p["msg_id"] = m.msg_id
            if p.get("timer"):
                p["timer"].cancel()
            t = threading.Timer(self.cfg.attach_wait_seconds,
                                lambda: self._intake.submit(self._guard, self._flush_pending, m.open_id))
            t.daemon = True
            p["timer"] = t
            t.start()
            n = len(p["paths"])
        kind = "图片" if items[0][0] == "image" else "文件"
        self._try(self.tx.reply_text, m.msg_id,
                  f"📎 收到{kind}（攒了 {n} 个）。接着发一句话说要做什么；"
                  f"{int(self.cfg.attach_wait_seconds)} 秒内没发，我就直接让 Claude 看。")

    def _take_pending(self, open_id: str) -> list[Path]:
        with self._pending_lock:
            p = self._pending.pop(open_id, None)
        if not p:
            return []
        if p.get("timer"):
            p["timer"].cancel()
        return p["paths"]

    def _flush_pending(self, open_id: str) -> None:
        """只发了附件、一直没说要干嘛：直接让 Claude 看。"""
        with self._pending_lock:
            p = self._pending.pop(open_id, None)
        if not p:
            return
        self._process_message(Incoming(msg_id=p["msg_id"], open_id=open_id, chat_id=p["chat_id"],
                                       content=json.dumps({"text": ""}), create_time_ms=int(time.time() * 1000)),
                              attachments=p["paths"])

    @staticmethod
    def _with_attachments(text: str, files: list) -> str:
        if not files:
            return text
        return text + "\n\n（我从手机飞书发来的附件，已经存到电脑上了，先用 Read 打开看）\n" + \
            "\n".join(f"- {p}" for p in files)

    def _session_warnings(self, sid: str) -> list[str]:
        """接着这个会话之前要提醒你的事：电脑上正开着、记录太大太费额度。"""
        out = []
        busy = self._busy_reason(sid)
        if busy:
            out.append(busy + "，两边同时往一个会话里写会串")
        path = sessions.find_transcript(sid, self.cfg.transcripts_dir)
        if path:
            mb = path.stat().st_size / 1024 / 1024
            if mb >= self.cfg.big_session_mb:
                out.append(f"这个会话记录已经 {mb:.1f} MB，接着跑一次要把整段上下文读一遍，非常费额度")
        return out

    def _busy_reason(self, sid: str) -> str:
        """这个会话是不是正在电脑上用着：被别的 Claude 进程开着，或者记录 3 分钟内被别人改过。飞书桥自己的不算。"""
        if self.open_checker(sid):
            return "这个会话在电脑上的 Claude 里正开着"
        cur = self.current
        if cur and cur.get("session_id") == sid:
            return ""
        path = sessions.find_transcript(sid, self.cfg.transcripts_dir)
        if not path:
            return ""
        mtime = path.stat().st_mtime
        mine = self.store.last_finished(sid)
        if time.time() - mtime < 180 and not (mine and mtime <= mine + 15):
            return f"这个会话{sessions.ago(mtime)}在电脑上还有动静"
        return ""

    def _status(self, task: dict, state: str, **kw) -> dict:
        return cards.status_card(task, state, buttons=self.cfg.card_buttons, label=self._session_label(task), **kw)

    # ---------- 电脑上的会话 ----------
    def _session_label(self, task: dict) -> str:
        a = self.attached
        if a and task.get("session_id") == a["sid"]:
            return f"接着会话「{policy.clip(self._redact(a['title']), 24)}」"
        return ""

    def _list_sessions(self, limit: int = 10) -> list[sessions.Session]:
        return sessions.list_sessions(self.cfg.desktop_sessions_dir, self.cfg.transcripts_dir,
                                      self.store.bridge_sessions(200), limit=limit)

    def _find_session(self, sid: str) -> sessions.Session | None:
        if not sessions.find_transcript(sid, self.cfg.transcripts_dir):
            return None
        for s in sessions.desktop_sessions(self.cfg.desktop_sessions_dir):
            if s.sid == sid:
                return s
        for r in self.store.bridge_sessions(500):
            if r["sid"] == sid:
                return sessions.Session(sid=sid, title=" ".join(str(r["first_prompt"] or "").split())[:40] or "（未命名）",
                                        cwd=r.get("cwd") or str(self.cfg.workdir),
                                        last_active=float(r.get("last") or 0), source="手机")
        return None

    def _attach(self, sid: str, reply_to: str) -> None:
        """接上电脑上的一个会话，把最近几轮对话推过来。"""
        s = self._find_session(sid)
        if s is None:
            self._try(self.tx.reply_text, reply_to, "找不到这个会话的记录了（可能删了或归档了），发 /会话 重新看看。")
            return
        self.attached = s.to_dict()
        self.store.kv_set("attached", json.dumps(self.attached, ensure_ascii=False))
        self.audit("attach", sid=sid, title=s.title, source=s.source)
        path = sessions.find_transcript(sid, self.cfg.transcripts_dir)
        turns = [(r, self._redact(t)) for r, t in sessions.recent_turns(path)]
        warns = self._session_warnings(sid)
        warn = ("；".join(warns) + "。接着发的消息会先让你确认。") if warns else ""
        tip = ("之后直接发消息＝接着这个会话聊（在它原来的目录里跑）。"
               "/记录 看最新对话，/电脑打开 在电脑上打开它，/新会话 退出。")
        self._try(self.tx.reply_card, reply_to, cards.history_card(self._redact(s.title), turns, warn=warn, tip=tip))

    def _handle_session_pick(self, m: Incoming, text: str) -> bool:
        """引用回复会话列表卡发编号。"""
        if not m.parent_id or m.parent_id != self.store.kv_get("last_list_msg"):
            return False
        word = _norm(text)
        if word.isdigit():
            self._pick(int(word), m.msg_id)
        else:
            self._try(self.tx.reply_text, m.msg_id, "回编号就行，比如 3。")
        return True

    def _pick(self, n: int, reply_to: str) -> None:
        sids = json.loads(self.store.kv_get("last_list") or "[]")
        if 1 <= n <= len(sids):
            self._attach(sids[n - 1], reply_to)
        else:
            self._try(self.tx.reply_text, reply_to, "编号不对。发 /会话 看最新的列表。")

    def _sessions_command(self, m: Incoming, arg: str) -> None:
        if arg.isdigit():
            self._pick(int(arg), m.msg_id)
            return
        ss = self._list_sessions(10)
        items = [dict(s.to_dict(), title=self._redact(s.title), ago=sessions.ago(s.last_active)) for s in ss]
        mid = self._try(self.tx.reply_card, m.msg_id, cards.sessions_card(items, self.cfg.card_buttons))
        self.store.kv_set("last_list", json.dumps([s.sid for s in ss]))
        if mid:
            self.store.kv_set("last_list_msg", mid)

    def _history_command(self, m: Incoming) -> None:
        a = self.attached
        if not a:
            self._try(self.tx.reply_text, m.msg_id,
                      "还没接上会话。发 /会话 选一个；或者引用回复某个任务的结果卡，就是接着那个任务聊。")
            return
        path = sessions.find_transcript(a["sid"], self.cfg.transcripts_dir)
        if not path:
            self._try(self.tx.reply_text, m.msg_id, "这个会话的记录找不到了，发 /会话 重新选一个。")
            return
        turns = [(r, self._redact(t)) for r, t in sessions.recent_turns(path)]
        self._try(self.tx.reply_card, m.msg_id, cards.history_card(
            self._redact(a["title"]), turns, header="最新记录",
            tip=f"记录最后更新：{sessions.ago(path.stat().st_mtime)}"))

    def _open_command(self, m: Incoming) -> None:
        a = self.attached
        if a:
            sid, title, cwd = a["sid"], a["title"], a.get("cwd") or ""
        else:  # 没接上会话：打开最近一个在手机上跑过的
            last = next((t for t in self.store.recent(20) if t.get("session_id") and t["status"] in FINAL), None)
            if not last:
                self._try(self.tx.reply_text, m.msg_id, "还没有能打开的会话。")
                return
            sid, title, cwd = last["session_id"], last["prompt"], last.get("cwd") or str(self.cfg.workdir)
        if self.open_checker(sid):  # 正在别处跑的会话 claude 不肯再开一份（「is open in another terminal」）
            self.audit("open_on_pc", sid=sid, ok=False, why="open_elsewhere")
            self._try(self.tx.reply_text, m.msg_id,
                      f"「{policy.clip(self._redact(title), 30)}」在电脑上正开着（桌面版或终端里），直接到电脑上看就行。")
            return
        ok, err = self.opener(sid, cwd)
        self.audit("open_on_pc", sid=sid, ok=ok)
        self._try(self.tx.reply_text, m.msg_id,
                  f"已在电脑的 Claude 桌面版里打开「{policy.clip(self._redact(title), 30)}」。" if ok
                  else f"没打开：{self._redact(err)}")

    def _handle_reply(self, m: Incoming, text: str) -> bool:
        """引用回复审批卡/确认卡，或者只有一件事在等时直接回「允许」「执行」这类词。处理了返回 True。"""
        word = _norm(text)
        decision = ("allow" if word in _ALLOW else "allow_all" if word in _ALLOW_ALL
                    else "deny" if word in _DENY else "")
        run = "run" if word in _RUN else "ignore" if word in _IGNORE else ""

        # 审批
        p = self.broker.find_by_card(m.parent_id) if m.parent_id else None
        if p is None and m.parent_id and self.store.get_approval_by_card(m.parent_id):
            self._try(self.tx.reply_text, m.msg_id, "这个审批已经处理过或失效了。")
            return True
        if p is None and not m.parent_id and decision:
            pending = self.broker.undecided()
            if len(pending) > 1:
                self._try(self.tx.reply_text, m.msg_id,
                          f"现在有 {len(pending)} 个审批在等，请引用回复具体那张审批卡。")
                return True
            p = pending[0] if pending else None
        if p is not None:
            if not decision:
                self._try(self.tx.reply_text, m.msg_id, "没看懂。引用回复审批卡：回 1 允许，2 本任务都允许，0 拒绝。")
            elif self._decide(p.id, decision, m.open_id, update_card=True)[0] is None:
                self._try(self.tx.reply_text, m.msg_id, "这个审批已经处理过或失效了。")
            return True

        # 确认（离线积压、重复的消息）
        t = self.store.find_by_message(m.parent_id) if m.parent_id else None
        if not (t and t["status"] == "confirm" and t.get("card_msg_id") == m.parent_id):
            t = None
            if not m.parent_id and run:
                waiting = self.store.list_by_status("confirm")
                if len(waiting) > 1:
                    self._try(self.tx.reply_text, m.msg_id, "有好几个任务在等确认，请引用回复具体那张卡片。")
                    return True
                t = waiting[0] if waiting else None
        if t is not None:
            if not run:
                self._try(self.tx.reply_text, m.msg_id, "没看懂。引用回复确认卡：回「执行」或「忽略」。")
                return True
            rep = self._on_confirm(t["id"], run)
            if rep.card is not None and t.get("card_msg_id"):
                self._try(self.tx.update_card, t["card_msg_id"], rep.card)
            if rep.toast_type == "warning":
                self._try(self.tx.reply_text, m.msg_id, rep.toast)
            return True

        # 审批超时之后才回的「允许」之类：别当成新任务跑
        if not m.parent_id and word in _REPLY_WORDS:
            self._try(self.tx.reply_text, m.msg_id, "现在没有在等你批准或确认的事。")
            return True
        return False

    def _decide(self, aid: str, d: str, by: str, update_card: bool = False) -> tuple[Pending | None, dict | None]:
        """按钮或回文字决定了一个审批。已经处理过、失效的返回 (None, None)。"""
        p = self.broker.decide(aid, d, by)
        if p is None:
            return None, None
        self.store.update_approval(aid, decision=d, decided_by=by, decided_at=time.time())
        self.audit("approval", id=aid, task=p.task_id, tool=p.tool_name, decision=d)
        self._intake.submit(self._guard, self._push, True)  # 回调要 3 秒内返回，改状态卡放后台
        card = cards.approval_done_card(p.task_id, p.tool_name,
                                        self._redact(policy.approval_detail(p.tool_name, p.tool_input)[0]), d)
        if update_card and p.card_msg_id:
            self._try(self.tx.update_card, p.card_msg_id, card)
        return p, card

    def _command(self, cmd: str, m: Incoming, arg: str = "") -> None:
        if cmd == "sessions":
            self._sessions_command(m, arg)
        elif cmd == "history":
            self._history_command(m)
        elif cmd == "open":
            self._open_command(m)
        elif cmd == "detach":
            was = self.attached
            self.attached = None
            self.store.kv_set("attached", "null")
            self._try(self.tx.reply_text, m.msg_id,
                      (f"已退出「{policy.clip(self._redact(was['title']), 30)}」。" if was else "") +
                      "之后每条消息都新开一个会话。")
        elif cmd == "help":
            self._try(self.tx.reply_card, m.msg_id, cards.info_card("飞书桥怎么用", HELP))
        elif cmd == "status":
            self._try(self.tx.reply_card, m.msg_id, self._status_card())
        elif cmd == "stop":
            cur = self.current
            text = f"正在停止任务 #{cur['id']}…" if cur and self._stop_current() else "现在没有在跑的任务。"
            self._try(self.tx.reply_text, m.msg_id, text)
        elif cmd == "pause":
            self.paused = True
            self.store.kv_set("paused", "1")
            n = 0
            for t in self.store.list_by_status("queued", "confirm"):
                self.store.update(t["id"], status="stopped", error="急停", finished_at=time.time(), delivered=1)
                if t.get("card_msg_id"):
                    self._try(self.tx.update_card, t["card_msg_id"],
                              self._status(self.store.get(t["id"]), "stopped", note="急停，已取消"))
                n += 1
            stopped = self._stop_current()
            self._try(self.tx.reply_text, m.msg_id,
                      "🛑 已急停：" + ("当前任务已停止，" if stopped else "") +
                      f"排队的 {n} 个任务已取消。之后发的消息都不会执行，发 /恢复 解除。")
        elif cmd == "resume":
            self.paused = False
            self.store.kv_set("paused", "0")
            self._try(self.tx.reply_text, m.msg_id, "✅ 已恢复接单。")

    def _status_card(self) -> dict:
        lines = []
        if self.paused:
            lines.append("🛑 **急停中**：不接新任务，发 /恢复 解除")
        cur, prog = self.current, self.progress
        if cur:
            el = cards.fmt_elapsed(time.time() - prog.started) if prog else ""
            lines.append(f"🏃 正在跑 #{cur['id']}（{el}）：{policy.clip(cur['prompt'], 60)}")
        else:
            lines.append("现在没有在跑的任务")
        queued = self.store.list_by_status("queued")
        if queued:
            lines.append("⏳ 排队：" + "、".join(f"#{t['id']}" for t in queued))
        confirm = self.store.list_by_status("confirm")
        if confirm:
            lines.append("❓ 等你确认：" + "、".join(f"#{t['id']}" for t in confirm))
        recent = [t for t in self.store.recent(8) if t["status"] in FINAL][:3]
        if recent:
            lines.append("最近：" + "；".join(f"#{t['id']} {cards.STATUS[t['status']][0]}" for t in recent))
        return cards.info_card("飞书桥状态", lines)

    # ---------- 卡片按钮 ----------
    def on_action(self, a: Action) -> ActionReply:
        if a.open_id not in self.cfg.allowed_open_ids:
            self.audit("reject_action", open_id=a.open_id)
            return ActionReply("你没有权限操作这张卡片", "error")
        v = a.value or {}
        try:
            k = v.get("k")
            if k == "ap":
                return self._on_approval(str(v.get("a", "")), str(v.get("d", "")), a.open_id)
            if k == "confirm":
                return self._on_confirm(int(v.get("t") or 0), str(v.get("d", "")))
            if k == "stop":
                return self._on_stop(int(v.get("t") or 0))
            if k == "sess":  # 会话列表卡上的「接着聊」；读记录、发卡片放后台，回调先返回
                self._intake.submit(self._guard, self._attach, str(v.get("s", "")), a.msg_id)
                return ActionReply("正在接上这个会话…", "info")
        except Exception:
            log.exception("卡片回调出错")
            return ActionReply("出错了，详情看电脑上的日志", "error")
        return ActionReply("看不懂这个按钮", "warning")

    def _on_approval(self, aid: str, d: str, by: str) -> ActionReply:
        if d not in ("allow", "allow_all", "deny"):
            return ActionReply("看不懂这个按钮", "warning")
        p, card = self._decide(aid, d, by)
        if p is None:  # 处理过了、超时了，或者飞书桥重启过
            row = self.store.get_approval(aid)
            if not row:
                return ActionReply("这个审批已失效", "warning")
            if not row.get("decision"):
                self.store.update_approval(aid, decision="cancelled", decided_at=time.time())
                row["decision"] = "cancelled"
            detail = policy.approval_detail(row["tool_name"], json.loads(row["tool_input"] or "{}"))[0]
            return ActionReply("这个审批已经处理过或失效了", "warning",
                               cards.approval_done_card(row["task_id"], row["tool_name"], self._redact(detail),
                                                        row["decision"]))
        toast = {"allow": "已允许", "allow_all": "这个任务内都允许", "deny": "已拒绝"}[d]
        return ActionReply(toast, "info" if d == "deny" else "success", card)

    def _on_confirm(self, tid: int, d: str) -> ActionReply:
        task = self.store.get(tid)
        if not task:
            return ActionReply("找不到这个任务", "warning")
        if task["status"] != "confirm":
            return ActionReply("这个任务已经处理过了", "warning", self._status(task, task["status"]))
        if d == "run":
            if self.paused:
                return ActionReply("急停中，先发 /恢复", "warning")
            self.store.update(tid, status="queued", reason="")
            self.audit("confirm", task=tid, decision="run")
            card = self._status(self.store.get(tid), "queued", ahead=self._ahead(tid))
            self.q.put(tid)
            return ActionReply("已加入队列", "success", card)
        self.store.update(tid, status="ignored", finished_at=time.time(), delivered=1)
        self.audit("confirm", task=tid, decision="ignore")
        return ActionReply("已忽略", "info", self._status(self.store.get(tid), "ignored"))

    def _on_stop(self, tid: int) -> ActionReply:
        cur = self.current
        if cur and cur["id"] == tid:
            self._intake.submit(self._guard, self._stop_current)  # 要改审批卡，放后台，回调先返回
            return ActionReply("正在停止…", "info")
        return ActionReply("这个任务已经不在运行了", "warning")

    def _stop_current(self) -> bool:
        cur = self.current
        if not cur:
            return False
        self._cancel_approvals(cur["id"])
        return self.runner.stop()

    def _cancel_approvals(self, tid: int) -> None:
        for p in self.broker.cancel_task(tid):
            self.store.update_approval(p.id, decision="cancelled", decided_at=time.time())
            if p.card_msg_id:
                detail = self._redact(policy.approval_detail(p.tool_name, p.tool_input)[0])
                self._try(self.tx.update_card, p.card_msg_id,
                          cards.approval_done_card(tid, p.tool_name, detail, "cancelled"))

    # ---------- 执行 ----------
    def _worker(self) -> None:
        while not self._stop_evt.is_set():
            try:
                tid = self.q.get(timeout=1)
            except queue.Empty:
                continue
            task = self.store.get(tid)
            if not task or task["status"] != "queued":
                continue
            if self.paused:
                self.store.update(tid, status="stopped", error="急停", finished_at=time.time(), delivered=1)
                continue
            self._guard(self._run_task, task)

    def _run_task(self, task: dict) -> None:
        tid = task["id"]
        self.store.update(tid, status="running", started_at=time.time())
        task = self.store.get(tid)
        if not task.get("card_msg_id"):  # 当时状态卡没发出去，补一张
            mid = self._try(self.tx.send_card, task["open_id"], self._status(task, "running"))
            if mid:
                self.store.update(tid, card_msg_id=mid)
                task["card_msg_id"] = mid
        self.current, self.progress = task, Progress()
        self.audit("start", task=tid, resume=bool(task.get("resume")), session=task.get("session_id"))
        self._push(force=True)
        threading.Thread(target=self._ticker, args=(tid,), name="ticker", daemon=True).start()
        try:
            res = self.runner.run(task, self._on_event)
        except Exception as e:
            log.exception("任务 #%s 执行出错", tid)
            res = RunResult(error=f"飞书桥内部错误：{e}")
        finally:
            self._cancel_approvals(tid)
            self.broker.end_task(tid)
            self.current, self.progress = None, None
        state = "stopped" if res.stopped and not res.timed_out else ("done" if res.ok else "failed")
        self.store.update(tid, status=state, finished_at=time.time(),
                          session_id=res.session_id or task["session_id"], cost_usd=res.cost_usd,
                          num_turns=res.num_turns, exit_code=res.exit_code, result=res.text, error=res.error)
        self.audit("finish", task=tid, status=state, cost=res.cost_usd, turns=res.num_turns,
                   denials=res.denials, error=res.error[:300])
        final = self.store.get(tid)
        note = "超时" if res.timed_out else ""
        if res.denials:
            note = (note + " · " if note else "") + f"被拒 {res.denials} 次"
        if final.get("card_msg_id"):
            with self._card_lock:
                self._try(self.tx.update_card, final["card_msg_id"],
                          self._status(final, state, note=(note + " · " if note else "") + "结果见下一条"))
        self._deliver(final, res.error_kind)
        if state == "done" and not task.get("resume") and self.cfg.auto_open_new_sessions:
            self._auto_open(final)

    def _auto_open(self, task: dict) -> None:
        """手机上新开的会话跑完，自动在电脑的桌面版里打开，桌面版侧边栏里就有它了（auto_open_new_sessions）。"""
        sid, cwd = task.get("session_id") or "", task.get("cwd") or str(self.cfg.workdir)
        ok, err = self.opener(sid, cwd)
        self.audit("open_on_pc", sid=sid, ok=ok, why="auto")
        if not ok:
            log.warning("自动在电脑上打开会话 %s 失败：%s", sid, err)
            self._try(self.tx.reply_text, task["msg_id"], f"没能自动在电脑上打开这个会话：{self._redact(err)}")

    def _on_event(self, kind: str, data) -> None:
        p = self.progress
        if p is None:
            return
        if kind == "tool":
            name, inp, sub = data
            if name == "mcp__bridge__approve":
                return
            p.n_tools += 1
            p.steps.append(("↳ " if sub else "") + self._redact(policy.tool_line(name, inp)))
            del p.steps[:-12]
        elif kind == "text":
            p.last_text = self._redact(str(data))
        elif kind == "note":
            p.note = self._redact(str(data))
        self._push()

    def _push(self, force: bool = False) -> None:
        """节流更新状态卡：最多 3 秒一次（飞书限制单条消息每秒 5 次更新）。"""
        task, p = self.current, self.progress
        if not task or p is None or not task.get("card_msg_id"):
            return
        now = time.time()
        if not force and now - self._last_push < 3:
            self._dirty = True
            return
        with self._card_lock:
            self._last_push, self._dirty = now, False
            p.waiting = self.broker.has_pending(task["id"])
            self._try(self.tx.update_card, task["card_msg_id"],
                      self._status(task, "waiting" if p.waiting else "running", progress=p))

    def _ticker(self, tid: int) -> None:
        while not self._stop_evt.is_set():
            time.sleep(1)
            cur = self.current
            if not cur or cur["id"] != tid:
                return
            if self._dirty or time.time() - self._last_push >= 30:
                self._push()

    def _deliver(self, task: dict, error_kind: str = "") -> bool:
        """发结果卡（新消息，手机会响）；太长的结果另附 .md 文件。"""
        state = task["status"] if task["status"] in ("done", "failed", "stopped") else "failed"
        text = self._redact(task.get("result") or "")
        err = self._redact(task.get("error") or "") if state != "done" else ""
        hint = _HINT.get(error_kind or _classify(text + "\n" + err), "") if state != "done" else ""
        if hint:
            err = f"{err}\n{hint}" if err else hint
        attached, file_path = "", None
        if len(text) > self.cfg.result_inline_chars:
            file_path = self.cfg.runs_dir / str(task["id"]) / f"任务{task['id']}结果.md"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(text, encoding="utf-8")
            attached = file_path.name
            text = policy.clip(text, min(1200, self.cfg.result_inline_chars))
        try:
            mid = self.tx.reply_card(task["msg_id"], cards.result_card(task, state, text, error=err, attached=attached,
                                                                       label=self._session_label(task)))
        except Exception as e:
            log.warning("任务 #%s 的结果没发出去，稍后重试：%s", task["id"], e)
            return False
        self.store.update(task["id"], result_msg_id=mid, delivered=1)
        if file_path:
            self._try(self.tx.send_file, task["open_id"], file_path, task["msg_id"])
        return True

    # ---------- broker 回调 ----------
    def _ask_user(self, p: Pending) -> None:
        cur = self.current
        if not cur or cur["id"] != p.task_id:
            raise RuntimeError("这不是正在执行的任务")
        detail, desc = policy.approval_detail(p.tool_name, p.tool_input)
        self.store.add_approval(p.id, p.task_id, p.tool_name, json.dumps(p.tool_input, ensure_ascii=False)[:20000])
        card = cards.approval_card(p.id, cur, p.tool_name, self._redact(detail), self._redact(desc),
                                   self.cfg.approval_timeout_seconds,
                                   buttons=self.cfg.card_buttons)
        p.card_msg_id = self.tx.reply_card(cur["msg_id"], card)  # 发不出去会抛异常，broker 按拒绝处理
        self.store.update_approval(p.id, card_msg_id=p.card_msg_id)
        self.audit("approval_ask", id=p.id, task=p.task_id, tool=p.tool_name)
        self._push(force=True)

    def _approval_timeout(self, p: Pending) -> None:
        self.store.update_approval(p.id, decision="timeout", decided_at=time.time())
        self.audit("approval_timeout", id=p.id, task=p.task_id, tool=p.tool_name)
        if p.card_msg_id:
            detail = self._redact(policy.approval_detail(p.tool_name, p.tool_input)[0])
            self._try(self.tx.update_card, p.card_msg_id,
                      cards.approval_done_card(p.task_id, p.tool_name, detail, "timeout"))
        self._push(force=True)

    def _send_file(self, task_id: int, path: str, caption: str) -> tuple[bool, str]:
        cur = self.current
        if not cur or cur["id"] != task_id:
            return False, "只有正在执行的任务能发文件"
        ok, why, p = policy.check_send_path(path, self.cfg.send_file_roots)
        if not ok:
            self.audit("send_file_reject", task=task_id, path=path, why=why)
            return False, why
        if p.suffix.lower() in policy.IMAGE_EXT and p.stat().st_size <= policy.MAX_IMAGE_BYTES:
            self.tx.send_image(cur["open_id"], p, reply_to=cur["msg_id"])
        else:
            self.tx.send_file(cur["open_id"], p, reply_to=cur["msg_id"])
        if caption:
            self._try(self.tx.reply_text, cur["msg_id"], self._redact(f"📎 {p.name}：{caption}"))
        self.audit("send_file", task=task_id, path=str(p))
        return True, f"已发到飞书：{p.name}"

    # ---------- 后台 ----------
    def _heartbeat(self) -> None:
        while not self._stop_evt.wait(60):
            self._guard(self.store.kv_set, "last_alive", str(time.time()))

    def _redeliver_loop(self) -> None:
        while not self._stop_evt.wait(60):
            for t in self.store.undelivered(time.time() - 86400):
                cur = self.current
                if not (cur and cur["id"] == t["id"]):
                    self._guard(self._deliver, t)

    # ---------- 小工具 ----------
    def audit(self, event: str, **kw) -> None:
        """审计日志：只追加，只留本机。"""
        line = json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **kw}, ensure_ascii=False)
        with self._audit_lock:
            with open(self.cfg.data_dir / "audit.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _redact(self, text: str) -> str:
        return policy.redact(text, self._secrets)

    @staticmethod
    def _try(fn, *args):
        """调飞书接口，失败只记日志（进度卡这类丢一次无所谓的）。"""
        try:
            return fn(*args)
        except Exception as e:
            log.warning("%s 失败：%s", getattr(fn, "__name__", fn), e)
            return None

    @staticmethod
    def _guard(fn, *args):
        try:
            return fn(*args)
        except Exception:
            log.exception("%s 出错", getattr(fn, "__name__", fn))
            return None
