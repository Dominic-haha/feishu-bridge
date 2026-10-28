"""传输层接口和两个本地实现：测试用的内存版、调试用的控制台版。飞书实现在 feishu.py。"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Incoming:
    """收到的一条飞书消息。"""
    msg_id: str
    open_id: str
    chat_id: str = ""
    chat_type: str = "p2p"
    msg_type: str = "text"
    content: str = "{}"
    create_time_ms: int = 0
    parent_id: str = ""
    root_id: str = ""


@dataclass
class Action:
    """卡片按钮被点了。"""
    open_id: str
    value: dict
    msg_id: str = ""


@dataclass
class ActionReply:
    """回给飞书的：弹一句 toast，可选把这张卡片换成新内容。"""
    toast: str = ""
    toast_type: str = "info"  # info / success / warning / error
    card: dict | None = None


OnMessage = Callable[[Incoming], None]
OnAction = Callable[[Action], ActionReply]


class Transport:
    """发消息类方法返回飞书消息 ID；run() 阻塞，直到 close()。"""

    def run(self, on_message: OnMessage, on_action: OnAction) -> None:
        raise NotImplementedError

    def send_card(self, open_id: str, card: dict) -> str:
        raise NotImplementedError

    def reply_card(self, msg_id: str, card: dict) -> str:
        raise NotImplementedError

    def update_card(self, msg_id: str, card: dict) -> None:
        raise NotImplementedError

    def send_text(self, open_id: str, text: str) -> str:
        raise NotImplementedError

    def reply_text(self, msg_id: str, text: str) -> str:
        raise NotImplementedError

    def send_image(self, open_id: str, path: Path, reply_to: str = "") -> str:
        raise NotImplementedError

    def send_file(self, open_id: str, path: Path, reply_to: str = "") -> str:
        raise NotImplementedError

    def download_resource(self, msg_id: str, key: str, kind: str) -> bytes:
        """下载你发来的图片（kind=image）或文件（kind=file）。"""
        raise NotImplementedError

    def close(self) -> None:
        pass


class MemoryTransport(Transport):
    """测试用：消息记在内存里。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.fail_sends = False  # 置 True 模拟飞书接口出错
        self.resources: dict[str, bytes] = {}  # 测试里预置「你发的图片/文件」的内容
        self.downloads: list[tuple[str, str, str]] = []
        self._n = 0
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def _add(self, **m) -> str:
        if self.fail_sends:
            raise RuntimeError("模拟飞书接口失败")
        with self._lock:
            self._n += 1
            m["id"] = f"om_{self._n}"
            m["updates"] = 0
            m["history"] = [m["card"]] if m.get("card") else []
            self.messages.append(m)
        return m["id"]

    def send_card(self, open_id, card):
        return self._add(kind="card", to=open_id, reply_to="", card=card)

    def reply_card(self, msg_id, card):
        return self._add(kind="card", to="", reply_to=msg_id, card=card)

    def update_card(self, msg_id, card):
        if self.fail_sends:
            raise RuntimeError("模拟飞书接口失败")
        with self._lock:
            for m in self.messages:
                if m["id"] == msg_id:
                    m["card"] = card
                    m["updates"] += 1
                    m["history"].append(card)
                    return
        raise KeyError(msg_id)

    def send_text(self, open_id, text):
        return self._add(kind="text", to=open_id, reply_to="", text=text)

    def reply_text(self, msg_id, text):
        return self._add(kind="text", to="", reply_to=msg_id, text=text)

    def send_image(self, open_id, path, reply_to=""):
        return self._add(kind="image", to=open_id, reply_to=reply_to, path=str(path))

    def send_file(self, open_id, path, reply_to=""):
        return self._add(kind="file", to=open_id, reply_to=reply_to, path=str(path))

    def download_resource(self, msg_id, key, kind):
        self.downloads.append((msg_id, key, kind))
        if key not in self.resources:
            raise RuntimeError(f"模拟下载失败：{key}")
        return self.resources[key]

    def run(self, on_message, on_action):
        self._closed.wait()

    def close(self):
        self._closed.set()

    # ---------- 测试辅助 ----------
    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(m) for m in self.messages]

    @staticmethod
    def title(m: dict) -> str:
        return m["card"]["header"]["title"]["content"] if m.get("card") else ""


def card_text(card: dict) -> str:
    """把卡片摊平成文字（控制台显示、测试断言用）。"""
    out = [f"【{card['header']['title']['content']}】"]

    def walk(els):
        for el in els:
            tag = el.get("tag")
            if tag == "markdown":
                out.append(el["content"].replace("<font color='grey'>", "").replace("</font>", ""))
            elif tag == "hr":
                out.append("—" * 10)
            elif tag == "column_set":
                for col in el.get("columns", []):
                    walk(col.get("elements", []))
            elif tag == "button":
                v = el["behaviors"][0]["value"]
                out.append(f"[{el['text']['content']}] → {json.dumps(v, ensure_ascii=False)}")

    walk(card.get("body", {}).get("elements", []))
    return "\n".join(out)


class ConsoleTransport(MemoryTransport):
    """本地调试：卡片打到终端，键盘输入当作你在飞书里发的消息。

      直接打字            = 派任务
      >om_3 继续          = 引用回复 om_3 那条消息（续聊）
      !ap <审批ID> allow|allow_all|deny
      !confirm <任务号> run|ignore
      !stop <任务号>
      !quit
    """

    OPEN_ID = "ou_console"

    def _add(self, **m):
        mid = super()._add(**m)
        head = f"\n=== {mid}" + (f"（回复 {m['reply_to']}）" if m.get("reply_to") else "") + " ==="
        if m["kind"] == "card":
            print(head + "\n" + card_text(m["card"]), flush=True)
        elif m["kind"] == "text":
            print(head + "\n" + m["text"], flush=True)
        else:
            print(head + f"\n[{m['kind']}] {m['path']}", flush=True)
        return mid

    def update_card(self, msg_id, card):
        super().update_card(msg_id, card)
        print(f"  ↻ {msg_id} 更新为：{card['header']['title']['content']}", flush=True)

    def run(self, on_message, on_action):
        print(self.__class__.__doc__, flush=True)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line == "!quit":
                break
            if line.startswith("!"):
                parts = line[1:].split()
                value = None
                if len(parts) == 3 and parts[0] == "ap":
                    value = {"k": "ap", "a": parts[1], "d": parts[2]}
                elif len(parts) == 3 and parts[0] == "confirm" and parts[1].isdigit():
                    value = {"k": "confirm", "t": int(parts[1]), "d": parts[2]}
                elif len(parts) == 2 and parts[0] == "stop" and parts[1].isdigit():
                    value = {"k": "stop", "t": int(parts[1])}
                if value is None:
                    print("看不懂这个指令", flush=True)
                    continue
                rep = on_action(Action(self.OPEN_ID, value))
                print(f"  toast[{rep.toast_type}]：{rep.toast}", flush=True)
                if rep.card:
                    print(card_text(rep.card), flush=True)
                continue
            parent = ""
            if line.startswith(">"):
                parent, _, line = line[1:].partition(" ")
            on_message(Incoming(msg_id=f"console_{time.time_ns()}", open_id=self.OPEN_ID,
                                chat_id="console", content=json.dumps({"text": line}, ensure_ascii=False),
                                create_time_ms=int(time.time() * 1000), parent_id=parent))
