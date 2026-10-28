"""电脑会话：列表、读最近几轮对话、接上以后接着聊、/记录、/电脑打开、/新会话。

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

from fb import sessions  # noqa: E402
from fb.transport import Action, MemoryTransport, card_text  # noqa: E402
from test_flow import ME, _Base, buttons, wait_for  # noqa: E402

SID_A = "11111111-2222-3333-4444-555555555555"
SID_B = "66666666-7777-8888-9999-aaaaaaaaaaaa"
SID_OLD = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def _line(**e):
    return json.dumps(e, ensure_ascii=False)


def write_transcript(path: Path, turns: list[tuple[str, str]], mtime: float | None = None) -> None:
    """按 Claude Code 的格式写一份会话记录：提问、思考、中间文字、调工具、工具结果、结论。"""
    lines = [_line(type="queue-operation", operation="enqueue")]
    for q, a in turns:
        lines += [
            _line(type="user", message={"role": "user", "content": q}),
            _line(type="assistant", message={"content": [{"type": "thinking", "thinking": "想一想"}]}),
            _line(type="assistant", message={"content": [{"type": "text", "text": "我先看看"}]}),
            _line(type="assistant", message={"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}),
            _line(type="user", message={"content": [{"type": "tool_result", "content": "工具输出"}]}),
            _line(type="user", isMeta=True, message={"content": "元信息"}),
            _line(type="assistant", isSidechain=True, message={"content": [{"type": "text", "text": "子代理的话"}]}),
            _line(type="assistant", message={"content": [{"type": "text", "text": a}]}),
        ]
    lines.append(_line(type="user", message={"content": [{"type": "text", "text": "<system-reminder>提醒</system-reminder>"}]}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))


def write_desktop_meta(base: Path, sid: str, title: str, cwd: str, last_ms: float, archived=False) -> None:
    d = base / "acct" / "org"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"local_{sid[:8]}.json").write_text(json.dumps({
        "sessionId": f"local_{sid}", "cliSessionId": sid, "title": title, "cwd": cwd,
        "createdAt": last_ms - 1000, "lastActivityAt": last_ms, "isArchived": archived}, ensure_ascii=False),
        encoding="utf-8")


class SessionUnitTest(unittest.TestCase):
    def test_recent_turns(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            write_transcript(p, [("第一问", "结论一"), ("第二问", "结论二"), ("第三问", "结论三")])
            self.assertEqual(sessions.recent_turns(p, n=2),
                             [("你", "第二问"), ("Claude", "结论二"), ("你", "第三问"), ("Claude", "结论三")])
            # 只读文件尾巴时，被截半的第一行要丢掉，不能报错
            self.assertEqual(sessions.recent_turns(p, n=1, tail_bytes=900)[-1], ("Claude", "结论三"))

    def test_desktop_and_list(self):
        with tempfile.TemporaryDirectory() as d:
            meta, tr = Path(d) / "meta", Path(d) / "projects"
            now = time.time()
            write_desktop_meta(meta, SID_A, "京东看板刷新", r"D:\克劳德", (now - 3600) * 1000)
            write_desktop_meta(meta, SID_OLD, "归档了的", r"D:\克劳德", now * 1000, archived=True)
            write_transcript(tr / "D-----" / f"{SID_A}.jsonl", [("刷新看板", "好了")], mtime=now - 3600)
            write_transcript(tr / "D-----" / f"{SID_B}.jsonl", [("手机上问的", "答")], mtime=now - 60)
            bridge = [{"sid": SID_B, "first_prompt": "手机上问的\n第二行", "cwd": "", "last": now - 60},
                      {"sid": "22222222-2222-2222-2222-222222222222", "first_prompt": "记录文件没了", "cwd": "", "last": now}]
            got = sessions.list_sessions(meta, tr, bridge, limit=10)
            self.assertEqual([(s.sid, s.source) for s in got], [(SID_B, "手机"), (SID_A, "电脑")])
            self.assertEqual(got[0].title, "手机上问的 第二行")
            self.assertEqual(got[1].title, "京东看板刷新")
            self.assertEqual(sessions.find_transcript("../../evil", tr), None)

    def test_open_in_desktop_runs_in_a_terminal(self):
        # 输出被接管时 claude --desktop 会拒绝（10-27 真机撞到），所以要放进终端里跑（Windows 隐藏控制台，Mac 伪终端）
        from unittest import mock
        with mock.patch.object(sessions.osutil, "run_in_terminal") as run:
            run.return_value = (0, "")
            self.assertEqual(sessions.open_in_desktop(SID_A, ["claude"], {}, ""), (True, ""))
            self.assertEqual(run.call_args.args[0], ["claude", "--desktop", "--resume", SID_A])
            run.return_value = (1, "")  # Windows 拿不到输出，只能报退出码
            self.assertEqual(sessions.open_in_desktop(SID_A, ["claude"], {}, ""), (False, "claude --desktop 退出码 1"))
            run.return_value = (1, f"Session {SID_A} is open in another terminal.")  # Mac 能拿到原话
            self.assertIn("open in another terminal", sessions.open_in_desktop(SID_A, ["claude"], {}, "")[1])

    def test_open_elsewhere_reads_session_registry(self):
        # 桌面版新开的会话命令行里没有会话 ID，要看 ~/.claude/sessions/<pid>.json 的登记（10-27 Mac 上实测）
        from unittest import mock
        import psutil

        class FakeProc:
            def __init__(self, pid, cmdline):
                self.pid, self._cmdline = pid, cmdline

            def name(self):
                return "claude"

            def cmdline(self):
                return self._cmdline

            def parents(self):
                return []

        procs = {111: FakeProc(111, ["claude", "--output-format", "stream-json"]),  # 桌面版新开的会话
                 222: FakeProc(222, ["claude", "-p", "--settings", "/Users/me/feishu-bridge/data/bridge-settings.json"])}

        def process(pid):
            if pid not in procs:
                raise psutil.NoSuchProcess(pid)
            return procs[pid]

        with tempfile.TemporaryDirectory() as d:
            reg = Path(d)
            (reg / "111.json").write_text(json.dumps({"pid": 111, "sessionId": SID_A, "kind": "interactive"}))
            (reg / "222.json").write_text(json.dumps({"pid": 222, "sessionId": SID_B}))  # 飞书桥自己起的
            (reg / "333.json").write_text(json.dumps({"pid": 333, "sessionId": SID_OLD}))  # 进程退了，文件还在
            (reg / "bad.json").write_text("{")
            with mock.patch.object(psutil, "Process", side_effect=process), \
                    mock.patch.object(psutil, "process_iter", return_value=[]):
                self.assertTrue(sessions.open_elsewhere(SID_A, reg))
                self.assertFalse(sessions.open_elsewhere(SID_B, reg))
                self.assertFalse(sessions.open_elsewhere(SID_OLD, reg))

    def test_ago(self):
        now = 1_800_000_000
        self.assertEqual(sessions.ago(now - 30, now), "刚刚")
        self.assertEqual(sessions.ago(now - 600, now), "10 分钟前")
        self.assertEqual(sessions.ago(now - 7200, now), "2 小时前")


class SessionFlowTest(_Base):
    def setUp(self):
        super().setUp()
        d = Path(self.tmp.name)
        self.cfg.desktop_sessions_dir = d / "meta"
        self.cfg.transcripts_dir = d / "projects"
        self.project = d / "某个项目"
        self.project.mkdir()
        self.transcript = self.cfg.transcripts_dir / "proj" / f"{SID_A}.jsonl"
        write_desktop_meta(self.cfg.desktop_sessions_dir, SID_A, "京东看板刷新", str(self.project),
                           (time.time() - 3600) * 1000)
        write_transcript(self.transcript, [("刷新到昨天", "已刷新，POP 成交 12.3 万"), ("再看下自营", "自营 8.1 万")],
                         mtime=time.time() - 3600)
        self.opened = []
        self.b.opener = lambda sid, cwd: (self.opened.append((sid, cwd)) or (True, ""))
        self.open_now = set()  # 假装「电脑上正开着」的会话
        self.b.open_checker = lambda sid: sid in self.open_now

    def cwds(self):
        return (self.argv_log.parent / (self.argv_log.name + ".cwd")).read_text(encoding="utf-8").splitlines()

    def list_card(self):
        mid = self.send("/会话")
        return wait_for(lambda: self.replies_to(mid, "card"))[0]

    def attach_by_button(self):
        card = self.list_card()
        v = next(v for v in buttons(card["card"]) if v.get("k") == "sess")
        self.assertEqual(v["s"], SID_A)
        rep = self.b.on_action(Action(ME, v, msg_id=card["id"]))
        self.assertIn("正在接上", rep.toast)
        return wait_for(lambda: self.replies_to(card["id"], "card"))[0]

    def test_list_shows_desktop_session(self):
        text = card_text(self.list_card()["card"])
        self.assertIn("京东看板刷新", text)
        self.assertIn("电脑 · 某个项目", text)

    def test_attach_pushes_history_then_continues_in_its_dir(self):
        hist = card_text(self.attach_by_button()["card"])
        self.assertIn("已接上：京东看板刷新", hist)
        self.assertIn("自营 8.1 万", hist)
        self.assertNotIn("工具输出", hist)
        self.assertNotIn("⚠️", hist)  # 一小时前的会话，不提示「电脑上有动静」
        t = self.wait_status(self.task_of(self.send("那天猫呢"))["id"], "done")
        a = self.argv()[-1]
        self.assertEqual(a[a.index("--resume") + 1], SID_A)
        # Mac 上临时目录的 /var 是 /private/var 的软链，claude 进程里拿到的是真实路径，比之前先都换成真实路径
        self.assertEqual(Path(self.cwds()[-1]).resolve(), self.project.resolve())
        self.assertEqual(t["cwd"], str(self.project))
        status = self.msg(t["card_msg_id"])
        self.assertIn("接着会话「京东看板刷新」", card_text(status["card"]))
        self.assertIn("接着会话「京东看板刷新」", self.result_text(t["id"]))

    def test_bottom_menu(self):
        # 点底部菜单＝以你的身份发出菜单名
        mid = self.send("切换会话")
        self.assertIn("京东看板刷新", card_text(wait_for(lambda: self.replies_to(mid, "card"))[0]["card"]))
        mid = self.send("最新记录")
        self.assertIn("还没接上", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.assertFalse(self.argv_log.exists())  # 菜单词不会被当成任务交给 Claude

    def test_pick_by_number(self):
        card = self.list_card()
        mid = self.send("1", parent=card["id"])
        self.assertIn("已接上", card_text(wait_for(lambda: self.replies_to(mid, "card"))[0]["card"]))
        mid = self.send("/新会话")
        self.assertIn("已退出「京东看板刷新」", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.wait_status(self.task_of(self.send("新问题"))["id"], "done")
        self.assertIn("--session-id", self.argv()[-1])
        mid = self.send("/会话 1")
        self.assertIn("已接上", card_text(wait_for(lambda: self.replies_to(mid, "card"))[0]["card"]))
        mid = self.send("/会话 9")
        self.assertIn("编号不对", wait_for(lambda: self.replies_to(mid))[0]["text"])

    def test_history_and_open(self):
        mid = self.send("/记录")
        self.assertIn("还没接上", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.attach_by_button()
        write_transcript(self.transcript, [("刷新到昨天", "已刷新"), ("电脑上新问的", "电脑上的新回答")])
        mid = self.send("/记录")
        hist = card_text(wait_for(lambda: self.replies_to(mid, "card"))[0]["card"])
        self.assertIn("最新记录", hist)
        self.assertIn("电脑上的新回答", hist)
        mid = self.send("/电脑打开")
        self.assertIn("已在电脑的 Claude 桌面版里打开", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.assertEqual(self.opened, [(SID_A, str(self.project))])

    def test_auto_open_new_session_on_pc(self):
        self.cfg.auto_open_new_sessions = True
        t = self.wait_status(self.task_of(self.send("手机上新开的话题"))["id"], "done")
        wait_for(lambda: self.opened)
        self.assertEqual(self.opened, [(t["session_id"], str(self.cfg.workdir))])
        # 接着已有会话聊的不再打开（电脑上已经有它了）
        self.attach_by_button()
        self.wait_status(self.task_of(self.send("接着聊"))["id"], "done")
        time.sleep(0.3)
        self.assertEqual(len(self.opened), 1)

    def test_no_auto_open_by_default(self):
        self.wait_status(self.task_of(self.send("新话题"))["id"], "done")
        time.sleep(0.3)
        self.assertEqual(self.opened, [])

    def test_open_on_pc_when_already_open(self):
        self.attach_by_button()
        self.open_now.add(SID_A)
        mid = self.send("/电脑打开")
        self.assertIn("在电脑上正开着", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.assertEqual(self.opened, [])  # 没再去调 claude --desktop

    def test_busy_warning(self):
        os.utime(self.transcript, None)  # 刚刚被改过 = 电脑上可能正开着
        hist = card_text(self.attach_by_button()["card"])
        self.assertIn("在电脑上还有动静", hist)

    def test_busy_session_needs_confirm_before_running(self):
        self.attach_by_button()
        os.utime(self.transcript, None)  # 接上以后，电脑上又在这个会话里说话了
        t = self.task_of(self.send("接着来"))
        self.assertEqual(t["status"], "confirm")
        self.assertIn("在电脑上还有动静", t["reason"])
        self.assertFalse(self.argv_log.exists())  # 没确认就没跑
        self.b.on_action(Action(ME, {"k": "confirm", "t": t["id"], "d": "run"}))
        self.wait_status(t["id"], "done")

    def test_open_on_pc_needs_confirm_even_if_idle(self):
        # 记录一小时没动过，但桌面版里正挂着它（10-26 真出过：空闲 11 分钟，保护没拦住）
        self.attach_by_button()
        self.open_now.add(SID_A)
        t = self.task_of(self.send("接着来"))
        self.assertEqual(t["status"], "confirm")
        self.assertIn("在电脑上的 Claude 里正开着", t["reason"])

    def test_big_session_needs_confirm(self):
        self.cfg.big_session_mb = 0.0001
        hist = card_text(self.attach_by_button()["card"])
        self.assertIn("MB", hist)
        t = self.task_of(self.send("接着来"))
        self.assertEqual(t["status"], "confirm")
        self.assertIn("非常费额度", t["reason"])

    def test_attach_survives_restart(self):
        self.attach_by_button()
        self.b.shutdown()
        self.b = self._bridge()
        self.assertEqual(self.b.attached["sid"], SID_A)

    def test_vanished_session(self):
        card = self.list_card()
        self.transcript.unlink()
        mid = self.send("1", parent=card["id"])
        self.assertIn("找不到这个会话", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.assertIsNone(self.b.attached)


if __name__ == "__main__":
    unittest.main()
