"""端到端测试：内存版飞书 + 假 claude，把收消息 → 排队 → 执行 → 审批 → 回结果整条链路走一遍。

跑法：python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fb.app import Bridge  # noqa: E402
from fb.config import Config  # noqa: E402
from fb.executor import python_exe  # noqa: E402
from fb.store import Store  # noqa: E402
from fb.transport import Action, Incoming, MemoryTransport, card_text  # noqa: E402

ME = "ou_me"
FAKE = [python_exe(), str(BASE / "tests" / "fake_claude.py")]


def wait_for(pred, timeout=20.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("等待超时")


def buttons(card):
    out = []

    def walk(els):
        for el in els:
            if el.get("tag") == "button":
                out.append(el["behaviors"][0]["value"])
            for col in el.get("columns", []):
                walk(col.get("elements", []))

    walk(card["body"]["elements"])
    return out


class _Base(unittest.TestCase):
    CARD_BUTTONS = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        self.root = d / "work"
        self.root.mkdir()
        self.argv_log = d / "argv.jsonl"
        os.environ["FAKE_CLAUDE_ARGV"] = str(self.argv_log)
        self.cfg = Config(app_id="cli_test", allowed_open_ids=[ME], workdir=self.root, data_dir=d / "data",
                          send_file_roots=[self.root], approval_timeout_seconds=10, timeout_minutes=1,
                          card_buttons=self.CARD_BUTTONS)
        self.store = Store(self.cfg.data_dir / "t.db")
        self.tx = MemoryTransport()
        self.b = self._bridge()
        self._n = 0

    def _bridge(self):
        b = Bridge(self.cfg, self.store, self.tx, claude_cmd=FAKE, secrets=["my-app-secret-value"])
        b.start()
        return b

    def tearDown(self):
        self.b.shutdown()
        time.sleep(0.3)
        self.store.close()
        os.environ.pop("FAKE_CLAUDE_ARGV", None)
        self.tmp.cleanup()

    # ---------- 辅助 ----------
    def send(self, text, *, msg_id=None, open_id=ME, age_min=0.0, parent="", chat_type="p2p", msg_type="text"):
        self._n += 1
        mid = msg_id or f"um_{self._n}_{time.time_ns()}"
        content = json.dumps({"text": text}, ensure_ascii=False) if msg_type == "text" else "{}"
        self.b.on_message(Incoming(msg_id=mid, open_id=open_id, chat_id="oc_1", chat_type=chat_type,
                                   msg_type=msg_type, content=content,
                                   create_time_ms=int((time.time() - age_min * 60) * 1000), parent_id=parent))
        return mid

    def task_of(self, mid):
        return wait_for(lambda: self.store.find_by_message(mid))

    def wait_status(self, tid, *statuses, timeout=20.0):
        return wait_for(lambda: (t := self.store.get(tid)) and t["status"] in statuses and t, timeout)

    def msg(self, msg_id):
        return next(m for m in self.tx.snapshot() if m["id"] == msg_id)

    def result_text(self, tid):
        t = wait_for(lambda: (x := self.store.get(tid)) and x["delivered"] and x["result_msg_id"] and x)
        return card_text(self.msg(t["result_msg_id"])["card"])

    def pending_approval(self):
        def find():
            for m in self.tx.snapshot():
                if m["kind"] != "card":
                    continue
                for v in buttons(m["card"]):
                    if v.get("k") == "ap":
                        row = self.store.get_approval(v["a"])
                        if row and not row["decision"] and self.b.broker.has_pending(row["task_id"]):
                            return v["a"]
            return None
        return wait_for(find)

    def argv(self):
        return [json.loads(x) for x in self.argv_log.read_text(encoding="utf-8").splitlines()]

    def replies_to(self, mid, kind="text"):
        return [m for m in self.tx.snapshot() if m["reply_to"] == mid and m["kind"] == kind]


class FlowTest(_Base):
    # ---------- 基本流程 ----------
    def test_basic_task(self):
        mid = self.send("你好")
        t = self.wait_status(self.task_of(mid)["id"], "done")
        self.assertIn("完成：你好", self.result_text(t["id"]))
        status = self.msg(t["card_msg_id"])
        self.assertEqual(MemoryTransport.title(status), "✅ 完成")
        self.assertGreaterEqual(status["updates"], 2)  # 至少：执行中、完成
        self.assertEqual(status["reply_to"], mid)
        a = self.argv()[-1]
        self.assertEqual(a[a.index("--permission-prompt-tool") + 1], "mcp__bridge__approve")
        self.assertEqual(a[a.index("--session-id") + 1], t["session_id"])
        self.assertNotIn("--resume", a)
        self.assertAlmostEqual(t["cost_usd"], 0.0123)
        audit = (self.cfg.data_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event": "finish"', audit)

    def test_queue_is_serial(self):
        t1 = self.task_of(self.send("SLEEP:1.5 第一个"))
        self.wait_status(t1["id"], "running")
        t2 = self.task_of(self.send("第二个"))
        self.wait_status(t2["id"], "done")
        a, b = self.store.get(t1["id"]), self.store.get(t2["id"])
        self.assertLessEqual(a["finished_at"], b["started_at"] + 0.01)
        first_version = self.msg(b["card_msg_id"])["history"][0]
        self.assertIn("前面还有 1 个任务", card_text(first_version))

    # ---------- 审批 ----------
    def test_approval_allow(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        aid = self.pending_approval()
        rep = self.b.on_action(Action(ME, {"k": "ap", "a": aid, "d": "allow"}))
        self.assertEqual(rep.toast, "已允许")
        self.assertEqual(rep.card["header"]["title"]["content"], "✅ 已允许")
        self.wait_status(t["id"], "done")
        self.assertIn("step0:allow", self.result_text(t["id"]))
        self.assertEqual(self.store.get_approval(aid)["decision"], "allow")
        # 再点一次：提示已处理
        again = self.b.on_action(Action(ME, {"k": "ap", "a": aid, "d": "deny"}))
        self.assertEqual(again.toast_type, "warning")

    def test_approval_deny(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        self.b.on_action(Action(ME, {"k": "ap", "a": self.pending_approval(), "d": "deny"}))
        self.wait_status(t["id"], "done")
        self.assertIn("step0:deny", self.result_text(t["id"]))

    def test_approval_timeout(self):
        self.b.broker.timeout_s = 1
        t = self.task_of(self.send("APPROVE 没人理"))
        self.wait_status(t["id"], "done")
        self.assertIn("step0:deny", self.result_text(t["id"]))
        rows = [m for m in self.tx.snapshot() if m["kind"] == "card" and "超时" in MemoryTransport.title(m)]
        self.assertEqual(len(rows), 1)  # 审批卡被改成「超时」

    def test_allow_all(self):
        t = self.task_of(self.send("APPROVE2 两步"))
        self.b.on_action(Action(ME, {"k": "ap", "a": self.pending_approval(), "d": "allow_all"}))
        self.wait_status(t["id"], "done")
        self.assertIn("step0:allow; step1:allow", self.result_text(t["id"]))
        ap_cards = [m for m in self.tx.snapshot() if m["kind"] == "card"
                    and any(v.get("k") == "ap" for v in buttons(m["card"]))]
        self.assertEqual(len(ap_cards), 1)  # 第二步没再问

    def test_stranger_cannot_click(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        aid = self.pending_approval()
        rep = self.b.on_action(Action("ou_other", {"k": "ap", "a": aid, "d": "allow"}))
        self.assertEqual(rep.toast_type, "error")
        self.b.on_action(Action(ME, {"k": "ap", "a": aid, "d": "deny"}))
        self.wait_status(t["id"], "done")

    # ---------- 停止、超时、失败 ----------
    def test_stop_button(self):
        t = self.task_of(self.send("SLEEP:30 慢任务"))
        self.wait_status(t["id"], "running")
        wait_for(lambda: self.b.runner._proc is not None)
        rep = self.b.on_action(Action(ME, {"k": "stop", "t": t["id"]}))
        self.assertEqual(rep.toast, "正在停止…")
        t = self.wait_status(t["id"], "stopped", timeout=10)
        self.assertLess(t["finished_at"] - t["started_at"], 15)
        self.assertIn("已停止", self.result_text(t["id"]))

    def test_stop_cancels_pending_approval(self):
        t = self.task_of(self.send("APPROVE 等审批时停"))
        aid = self.pending_approval()
        self.send("/停止")
        self.wait_status(t["id"], "stopped", "done", timeout=10)
        self.assertEqual(self.store.get_approval(aid)["decision"], "cancelled")

    def test_timeout(self):
        self.cfg.timeout_minutes = 0.05  # 3 秒
        t = self.task_of(self.send("SLEEP:30 会超时"))
        t = self.wait_status(t["id"], "failed", timeout=15)
        self.assertIn("超过", t["error"])

    def test_crash(self):
        t = self.wait_status(self.task_of(self.send("FAIL"))["id"], "failed")
        self.assertIn("boom", t["error"])
        self.assertIn("boom", self.result_text(t["id"]))

    # ---------- 确认、去重、白名单 ----------
    def test_stale_message_needs_confirm(self):
        t = self.task_of(self.send("离线时发的", age_min=30))
        self.assertEqual(t["status"], "confirm")
        card = wait_for(lambda: self.store.get(t["id"])["card_msg_id"] and self.msg(self.store.get(t["id"])["card_msg_id"]))
        self.assertIn("30 分钟前", card_text(card["card"]))
        rep = self.b.on_action(Action(ME, {"k": "confirm", "t": t["id"], "d": "run"}))
        self.assertEqual(rep.toast, "已加入队列")
        self.wait_status(t["id"], "done")

    def test_stale_ignore(self):
        t = self.task_of(self.send("离线时发的", age_min=30))
        self.b.on_action(Action(ME, {"k": "confirm", "t": t["id"], "d": "ignore"}))
        self.assertEqual(self.store.get(t["id"])["status"], "ignored")
        time.sleep(0.5)
        self.assertFalse(self.argv_log.exists())  # 根本没启动 claude

    def test_duplicate_needs_confirm(self):
        first = self.task_of(self.send("一样的内容"))
        self.wait_status(first["id"], "done")
        second = self.task_of(self.send("一样的内容"))
        self.assertEqual(second["status"], "confirm")
        self.assertIn(f"#{first['id']}", second["reason"])

    def test_redelivery_dedup(self):
        self.send("同一条", msg_id="dup1")
        self.send("同一条", msg_id="dup1")
        t = self.wait_status(self.task_of("dup1")["id"], "done")
        time.sleep(0.3)
        self.assertEqual(len(self.store.recent(10)), 1)
        self.assertEqual(len(self.argv()), 1)
        self.assertEqual(t["msg_id"], "dup1")

    def test_stranger_ignored_silently(self):
        self.send("我是陌生人", open_id="ou_other")
        time.sleep(0.5)
        self.assertEqual(self.tx.snapshot(), [])
        self.assertIn("reject_sender", (self.cfg.data_dir / "audit.jsonl").read_text(encoding="utf-8"))

    def test_group_message_ignored(self):
        self.send("群里@我", chat_type="group")
        time.sleep(0.5)
        self.assertEqual(self.tx.snapshot(), [])

    def test_pairing_mode(self):
        self.cfg.allowed_open_ids = []
        mid = self.send("你好", open_id="ou_new_person")
        r = wait_for(lambda: self.replies_to(mid))
        self.assertIn("ou_new_person", r[0]["text"])
        self.assertEqual(self.store.recent(5), [])

    def test_unsupported_message_type(self):
        mid = self.send("", msg_type="sticker")
        self.assertIn("暂时不支持", wait_for(lambda: self.replies_to(mid))[0]["text"])

    # ---------- 续聊 ----------
    def test_resume_by_quote_reply(self):
        t1 = self.wait_status(self.task_of(self.send("第一轮"))["id"], "done")
        res_id = wait_for(lambda: self.store.get(t1["id"])["result_msg_id"])
        t2 = self.task_of(self.send("第二轮", parent=res_id))
        self.assertEqual(t2["parent_task"], t1["id"])
        self.wait_status(t2["id"], "done")
        a = self.argv()[-1]
        self.assertEqual(a[a.index("--resume") + 1], t1["session_id"])

    def test_resume_fallback_when_session_missing(self):
        t1 = self.wait_status(self.task_of(self.send("第一轮"))["id"], "done")
        t2 = self.task_of(self.send("NOSESSION 第二轮", parent=t1["card_msg_id"]))
        t2 = self.wait_status(t2["id"], "done")
        tried, retried = self.argv()[-2], self.argv()[-1]
        self.assertIn("--resume", tried)
        self.assertIn("--session-id", retried)
        self.assertNotEqual(t2["session_id"], t1["session_id"])

    # ---------- 回传文件、长结果、脱敏 ----------
    def test_send_file(self):
        img = self.root / "截图.png"
        img.write_bytes(b"\x89PNG fake")
        t = self.wait_status(self.task_of(self.send(f"SENDFILE:{img}"))["id"], "done")
        self.assertIn("sendfile:ok", self.result_text(t["id"]))
        sent = [m for m in self.tx.snapshot() if m["kind"] == "image"]
        self.assertEqual(Path(sent[0]["path"]), img.resolve())

    def test_send_file_rejected_outside_root(self):
        outside = Path(self.tmp.name) / "外面.txt"
        outside.write_text("x", encoding="utf-8")
        t = self.wait_status(self.task_of(self.send(f"SENDFILE:{outside}"))["id"], "done")
        self.assertIn("sendfile:err:只能发", self.result_text(t["id"]))
        self.assertEqual([m for m in self.tx.snapshot() if m["kind"] in ("image", "file")], [])

    def test_big_result_goes_to_file(self):
        t = self.wait_status(self.task_of(self.send("BIG"))["id"], "done")
        self.assertIn("已作为文件", self.result_text(t["id"]))
        files = wait_for(lambda: [m for m in self.tx.snapshot() if m["kind"] == "file"])
        self.assertTrue(files[0]["path"].endswith(".md"))

    def test_result_redacted(self):
        t = self.wait_status(self.task_of(self.send("SECRET"))["id"], "done")
        text = self.result_text(t["id"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)
        self.assertNotIn("hunter22", text)

    # ---------- 指令 ----------
    def test_pause_and_resume(self):
        t = self.task_of(self.send("SLEEP:30 正在跑"))
        self.wait_status(t["id"], "running")
        wait_for(lambda: self.b.runner._proc is not None)
        queued = self.task_of(self.send("排队中的"))
        mid = self.send("/急停")
        self.assertIn("已急停", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.wait_status(t["id"], "stopped", timeout=10)
        self.assertEqual(self.store.get(queued["id"])["status"], "stopped")
        mid = self.send("急停时派的")
        self.assertIn("急停", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.send("/恢复")
        wait_for(lambda: not self.b.paused)
        self.wait_status(self.task_of(self.send("恢复后派的"))["id"], "done")

    def test_status_and_help(self):
        mid = self.send("/状态")
        card = wait_for(lambda: self.replies_to(mid, "card"))[0]["card"]
        self.assertEqual(card["header"]["title"]["content"], "飞书桥状态")
        mid = self.send("/帮助")
        self.assertIn("引用回复", card_text(wait_for(lambda: self.replies_to(mid, "card"))[0]["card"]))

    # ---------- 故障恢复 ----------
    def test_redeliver_after_send_failure(self):
        t = self.task_of(self.send("SLEEP:1 结果发不出去"))
        self.wait_status(t["id"], "running")
        self.tx.fail_sends = True
        t = self.wait_status(t["id"], "done")
        time.sleep(0.3)
        self.assertEqual(self.store.get(t["id"])["delivered"], 0)
        self.tx.fail_sends = False
        for row in self.store.undelivered(time.time() - 600):
            self.b._deliver(row)
        self.assertEqual(self.store.get(t["id"])["delivered"], 1)

    def test_recover_marks_interrupted(self):
        tid = self.store.add_task(msg_id="old", open_id=ME, chat_id="c", prompt="重启前在跑", status="running",
                                  session_id="s")
        self.store.kv_set("last_alive", str(time.time() - 600))
        self.b.shutdown()
        self.b = self._bridge()
        self.assertEqual(self.store.get(tid)["status"], "interrupted")
        notice = [m for m in self.tx.snapshot() if m["kind"] == "text" and m["to"] == ME]
        self.assertIn("离线约 10 分钟", notice[-1]["text"])
        self.assertIn(f"#{tid}", notice[-1]["text"])


class TextReplyTest(_Base):
    """「花花来啦」这种没配卡片回调的应用：卡片上不放按钮，审批、确认都靠回文字。"""
    CARD_BUTTONS = False

    def pending(self):
        return wait_for(lambda: (ps := self.b.broker.undecided()) and ps[0].card_msg_id and ps[0])

    def test_cards_have_no_buttons(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        p = self.pending()
        card = self.msg(p.card_msg_id)["card"]
        self.assertEqual(buttons(card), [])
        self.assertIn("引用回复这张卡片", card_text(card))
        running = self.msg(self.store.get(t["id"])["card_msg_id"])["card"]
        self.assertEqual(buttons(running), [])
        self.assertIn("/停止", card_text(running))
        self.send("1", parent=p.card_msg_id)
        self.wait_status(t["id"], "done")

    def test_quote_reply_allow(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        p = self.pending()
        self.send("1", parent=p.card_msg_id)
        self.wait_status(t["id"], "done")
        self.assertIn("step0:allow", self.result_text(t["id"]))
        self.assertEqual(MemoryTransport.title(self.msg(p.card_msg_id)), "✅ 已允许")  # 审批卡被改成结果

    def test_bare_deny_when_only_one_pending(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        self.pending()
        self.send("拒绝")
        self.wait_status(t["id"], "done")
        self.assertIn("step0:deny", self.result_text(t["id"]))

    def test_allow_all_by_text(self):
        t = self.task_of(self.send("APPROVE2 两步"))
        p = self.pending()
        self.send("2", parent=p.card_msg_id)
        self.wait_status(t["id"], "done")
        self.assertIn("step0:allow; step1:allow", self.result_text(t["id"]))

    def test_unclear_reply_keeps_waiting(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        p = self.pending()
        mid = self.send("嗯？", parent=p.card_msg_id)
        self.assertIn("没看懂", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.assertTrue(self.b.broker.has_pending(t["id"]))
        self.send("允许", parent=p.card_msg_id)
        self.wait_status(t["id"], "done")

    def test_late_reply_to_decided_approval(self):
        t = self.task_of(self.send("APPROVE 跑个命令"))
        p = self.pending()
        self.send("0", parent=p.card_msg_id)
        self.wait_status(t["id"], "done")
        mid = self.send("1", parent=p.card_msg_id)
        self.assertIn("已经处理过", wait_for(lambda: self.replies_to(mid))[0]["text"])

    def test_confirm_by_quote_reply(self):
        t = self.task_of(self.send("离线时发的", age_min=30))
        card_id = wait_for(lambda: self.store.get(t["id"])["card_msg_id"])
        self.assertEqual(buttons(self.msg(card_id)["card"]), [])
        self.send("执行", parent=card_id)
        self.wait_status(t["id"], "done")

    def test_bare_ignore_with_one_confirm(self):
        t = self.task_of(self.send("离线时发的", age_min=30))
        wait_for(lambda: self.store.get(t["id"])["card_msg_id"])
        self.send("忽略")
        self.wait_status(t["id"], "ignored")

    def test_stray_reply_word_is_not_a_task(self):
        mid = self.send("允许")
        self.assertIn("没有在等", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.assertEqual(self.store.recent(5), [])

    def test_quote_reply_1_to_result_still_continues(self):
        # Claude 在结果里问「选 1 还是 2」时，引用回复「1」要接着聊，不能被当成审批
        t1 = self.wait_status(self.task_of(self.send("第一轮"))["id"], "done")
        res_id = wait_for(lambda: self.store.get(t1["id"])["result_msg_id"])
        t2 = self.task_of(self.send("1", parent=res_id))
        self.assertEqual(t2["parent_task"], t1["id"])
        self.wait_status(t2["id"], "done")


if __name__ == "__main__":
    unittest.main()
