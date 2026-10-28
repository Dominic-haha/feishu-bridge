"""单元测试：规则、卡片、任务库、审批中转站、审批 MCP、凭据、执行器的命令和环境变量。

跑法：python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fb import cards, credstore, osutil, policy  # noqa: E402
from fb.broker import Broker  # noqa: E402
from fb.config import Config, load  # noqa: E402
from fb.executor import (CLAUDE_EXE, Runner, clean_env, desktop_claudes, find_claude, python_exe,  # noqa: E402
                         rule_path)
from fb.store import Store  # noqa: E402


class PolicyTest(unittest.TestCase):
    def test_extract_text(self):
        self.assertEqual(policy.extract_text("text", json.dumps({"text": " 你好 "})), "你好")
        post = {"zh_cn": {"title": "标题", "content": [[{"tag": "text", "text": "第一行"}],
                                                       [{"tag": "a", "text": "链接"}, {"tag": "at", "user_name": "张三"}]]}}
        self.assertEqual(policy.extract_text("post", json.dumps(post)), "标题\n第一行\n链接@张三")
        self.assertIsNone(policy.extract_text("image", json.dumps({"image_key": "x"})))
        self.assertIsNone(policy.extract_text("text", "不是json"))

    def test_parse_command(self):
        self.assertEqual(policy.parse_command("/急停"), "pause")
        self.assertEqual(policy.parse_command("/状态 现在"), "status")
        self.assertEqual(policy.parse_command("/STOP"), "stop")
        self.assertIsNone(policy.parse_command("帮我查一下"))
        self.assertIsNone(policy.parse_command("   "))

    def test_menu_words(self):
        # 底部菜单发来的是菜单名本身，中英日都认；整句才算，夹在话里不算
        self.assertEqual(policy.parse_command("切换会话"), "sessions")
        self.assertEqual(policy.parse_command(" 最新记录 "), "history")
        self.assertEqual(policy.parse_command("Open on PC"), "open")
        self.assertEqual(policy.parse_command("新規セッション"), "detach")
        self.assertEqual(policy.command_arg("Session list"), "")
        self.assertIsNone(policy.parse_command("新会话怎么开"))
        self.assertIsNone(policy.parse_command("latest news"))

    def test_minutes_old(self):
        now = time.time()
        self.assertAlmostEqual(policy.minutes_old(int((now - 600) * 1000), now), 10, places=3)
        self.assertEqual(policy.minutes_old(None), 0.0)
        self.assertEqual(policy.minutes_old("abc"), 0.0)

    def test_redact(self):
        s = ("token=abcd1234efgh sk-ant-oat01-ABCDEFGHIJKLMNOP 密码：hunter22 "
             "ghp_abcdefghijklmnopqrstuvwxyz123456 max_tokens: 4096 "
             "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----")
        r = policy.redact(s, ["my-app-secret-value"])
        for leaked in ("abcd1234efgh", "ABCDEFGHIJKLMNOP", "hunter22", "abcdefghijklmnopqrstuvwxyz123456", "AAAA"):
            self.assertNotIn(leaked, r)
        self.assertIn("max_tokens: 4096", r)  # 正常文字别误伤
        self.assertEqual(policy.redact("x my-app-secret-value y", ["my-app-secret-value"]), "x *** y")

    def test_check_send_path(self):
        with tempfile.TemporaryDirectory() as d:
            root, other = Path(d) / "work", Path(d) / "other"
            root.mkdir()
            other.mkdir()
            ok_file = root / "报告.xlsx"
            ok_file.write_bytes(b"x")
            (root / "sub").mkdir()
            nested = root / "sub" / "a.png"
            nested.write_bytes(b"x")
            outside = other / "b.txt"
            outside.write_bytes(b"x")
            secret = root / "app_secret.txt"
            secret.write_bytes(b"x")
            ssh = root / ".ssh"
            ssh.mkdir()
            (ssh / "k").write_bytes(b"x")
            empty = root / "empty.txt"
            empty.write_bytes(b"")
            self.assertTrue(policy.check_send_path(str(ok_file), [root])[0])
            self.assertTrue(policy.check_send_path(str(nested), [root])[0])
            self.assertIn("只能发", policy.check_send_path(str(outside), [root])[1])
            self.assertIn("密钥", policy.check_send_path(str(secret), [root])[1])
            self.assertIn("密钥", policy.check_send_path(str(ssh / "k"), [root])[1])
            self.assertIn("空", policy.check_send_path(str(empty), [root])[1])
            self.assertIn("不存在", policy.check_send_path(str(root / "nope.txt"), [root])[1])
            self.assertIn("不是文件", policy.check_send_path(str(root / "sub"), [root])[1])
            self.assertIn("只能发", policy.check_send_path(str(root / ".." / "other" / "b.txt"), [root])[1])

    def test_tool_line_and_detail(self):
        self.assertEqual(policy.tool_line("Bash", {"command": "ls", "description": "看目录"}), "Bash · 看目录")
        self.assertEqual(policy.tool_line("mcp__scrapling__fetch", {"url": "u"}), 'scrapling·fetch · {"url": "u"}')
        detail, desc = policy.approval_detail("Bash", {"command": "x" * 5000, "description": "跑"})
        self.assertLessEqual(len(detail), 1500)
        self.assertEqual(desc, "跑")
        self.assertIn("修改 a.py", policy.approval_detail("Edit", {"file_path": "a.py", "old_string": "1",
                                                                   "new_string": "2"})[0])


class CardsTest(unittest.TestCase):
    def setUp(self):
        self.task = {"id": 7, "prompt": "提" * 5000, "reason": "原因", "started_at": 1, "finished_at": 100,
                     "num_turns": 3, "cost_usd": 0.5}

    def _buttons(self, card):
        out = []

        def walk(els):
            for el in els:
                if el.get("tag") == "button":
                    out.append(el["behaviors"][0]["value"])
                for col in el.get("columns", []):
                    walk(col.get("elements", []))

        walk(card["body"]["elements"])
        return out

    def test_all_cards_small_and_valid(self):
        prog = cards.Progress(steps=["步骤" * 200] * 20, n_tools=99, last_text="想法" * 500, note="备注")
        all_cards = [cards.status_card(self.task, s, progress=prog, ahead=2) for s in cards.STATUS]
        all_cards += [cards.result_card(self.task, "done", "结" * 3000, error="错" * 3000, attached="a.md"),
                      cards.approval_card("abc", self.task, "Bash", "命" * 1500, "说明", 300),
                      cards.info_card("标题", ["行"] * 50)]
        all_cards += [cards.approval_done_card(7, "Bash", "x" * 5000, d) for d in cards.DECISION]
        for c in all_cards:
            self.assertEqual(c["schema"], "2.0")
            self.assertTrue(c["config"]["update_multi"])
            self.assertLess(cards.size(c), 28 * 1024)
            json.dumps(c, ensure_ascii=False)

    def test_buttons(self):
        ap = cards.approval_card("abc", self.task, "Bash", "ls", "", 300)
        self.assertEqual([b["d"] for b in self._buttons(ap)], ["allow", "allow_all", "deny"])
        self.assertTrue(all(b["k"] == "ap" and b["a"] == "abc" for b in self._buttons(ap)))
        conf = cards.status_card(self.task, "confirm")
        self.assertEqual(self._buttons(conf), [{"k": "confirm", "t": 7, "d": "run"},
                                               {"k": "confirm", "t": 7, "d": "ignore"}])
        run = cards.status_card(self.task, "running", progress=cards.Progress())
        self.assertEqual(self._buttons(run), [{"k": "stop", "t": 7}])
        self.assertEqual(self._buttons(cards.status_card(self.task, "done")), [])

    def test_no_button_mode(self):
        ap = cards.approval_card("abc", self.task, "Bash", "ls", "", 300, buttons=False)
        self.assertEqual(self._buttons(ap), [])
        self.assertIn("**1** 允许", json.dumps(ap, ensure_ascii=False))
        conf = cards.status_card(self.task, "confirm", buttons=False)
        self.assertEqual(self._buttons(conf), [])
        self.assertIn("**执行**", json.dumps(conf, ensure_ascii=False))
        run = cards.status_card(self.task, "running", progress=cards.Progress(), buttons=False)
        self.assertEqual(self._buttons(run), [])

    def test_code_fence_escaped(self):
        c = cards.approval_card("abc", self.task, "Bash", "echo ```oops```", "", 300)
        self.assertEqual(json.dumps(c, ensure_ascii=False).count("```"), 2)


class StoreTest(unittest.TestCase):
    def test_store(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            tid = s.add_task(msg_id="m1", open_id="o", chat_id="c", prompt="p", status="queued", session_id="s")
            self.assertIsNotNone(tid)
            self.assertIsNone(s.add_task(msg_id="m1", open_id="o", chat_id="c", prompt="p", status="queued",
                                         session_id="s"))  # 重推去重
            s.update(tid, card_msg_id="card1", result_msg_id="res1")
            for ref in ("m1", "card1", "res1"):
                self.assertEqual(s.find_by_message(ref)["id"], tid)
            self.assertIsNone(s.find_by_message(""))
            self.assertEqual(s.recent_same_prompt("o", "p", time.time() - 60)["id"], tid)
            self.assertIsNone(s.recent_same_prompt("o", "p", time.time() + 60))
            s.kv_set("k", "1")
            s.kv_set("k", "2")
            self.assertEqual(s.kv_get("k"), "2")
            s.add_approval("a1", tid, "Bash", "{}")
            s.update_approval("a1", decision="allow")
            self.assertEqual(s.get_approval("a1")["decision"], "allow")
            s.update(tid, status="done", finished_at=time.time())
            self.assertEqual([t["id"] for t in s.undelivered(time.time() - 60)], [tid])
            s.close()


def _post(url, token, body, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Bridge-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.timeouts = []
        self.b = Broker(ask_user=self.asked.append, send_file=lambda t, p, c: (True, f"sent {p}"),
                        on_timeout=self.timeouts.append, timeout_s=5)

    def tearDown(self):
        self.b.close()

    def _ask_async(self, task_id=1):
        out, before = {}, len(self.asked)
        th = threading.Thread(target=lambda: out.update(r=self.b.ask(task_id, "Bash", {"command": "ls"})))
        th.start()
        for _ in range(100):
            if len(self.asked) > before:  # 等这一次的审批卡发出去；之前发过的不算，不然没等到就往下比了
                break
            time.sleep(0.02)
        return th, out

    def test_token_required(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _post(self.b.url + "/approve", "wrong", {"task_id": 1})
        self.assertEqual(cm.exception.code, 403)
        cm.exception.close()

    def test_allow_then_allow_all(self):
        th, out = self._ask_async()
        self.assertTrue(self.b.has_pending(1))
        self.assertIsNotNone(self.b.decide(self.asked[0].id, "allow_all", "me"))
        th.join(5)
        self.assertEqual(out["r"], {"behavior": "allow", "updatedInput": {"command": "ls"}})
        self.assertIsNone(self.b.decide(self.asked[0].id, "deny", "me"))  # 第二次点无效
        # 本任务都允许：同一任务不再问
        self.assertEqual(self.b.ask(1, "Bash", {"command": "pwd"})["behavior"], "allow")
        self.assertEqual(len(self.asked), 1)
        self.b.end_task(1)
        th, out = self._ask_async()
        self.assertEqual(len(self.asked), 2)
        self.b.decide(self.asked[1].id, "deny", "me")
        th.join(5)
        self.assertEqual(out["r"]["behavior"], "deny")

    def test_timeout_and_cancel(self):
        self.b.timeout_s = 0.3
        r = self.b.ask(2, "Bash", {"command": "ls"})
        self.assertEqual(r["behavior"], "deny")
        self.assertEqual(len(self.timeouts), 1)
        self.b.timeout_s = 5
        th, out = self._ask_async(3)
        self.assertEqual(len(self.b.cancel_task(3)), 1)
        th.join(5)
        self.assertEqual(out["r"]["behavior"], "deny")
        self.assertIn("停止", out["r"]["message"])

    def test_ask_user_failure_denies(self):
        def boom(p):
            raise RuntimeError("飞书挂了")
        self.b.ask_user = boom
        self.assertEqual(self.b.ask(1, "Bash", {})["behavior"], "deny")

    def test_http_send_file(self):
        r = _post(self.b.url + "/send_file", self.b.token, {"task_id": 1, "path": "a.png"})
        self.assertEqual(r, {"ok": True, "message": "sent a.png"})


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.b = Broker(ask_user=self.asked.append, send_file=lambda t, p, c: (False, "不行"),
                        on_timeout=lambda p: None, timeout_s=5)

    def tearDown(self):
        self.b.close()

    def _spawn(self, url):
        env = dict(os.environ, FB_BROKER_URL=url, FB_BROKER_TOKEN=self.b.token, FB_TASK_ID="5",
                   FB_APPROVE_WAIT="10")
        return subprocess.Popen([python_exe(), "-I", str(BASE / "fb" / "mcp_server.py")], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, env=env)

    @staticmethod
    def _rpc(p, obj):
        p.stdin.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        p.stdin.flush()
        if "id" not in obj:
            return None
        return json.loads(p.stdout.readline().decode())

    def test_protocol_and_tools(self):
        p = self._spawn(self.b.url)
        try:
            init = self._rpc(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2099-01-01", "capabilities": {}}})
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            self._rpc(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = self._rpc(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["approve", "send_file"])
            self.assertEqual(self._rpc(p, {"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

            def click():
                for _ in range(200):
                    if self.asked:
                        self.b.decide(self.asked[0].id, "allow", "me")
                        return
                    time.sleep(0.02)
            threading.Thread(target=click).start()
            r = self._rpc(p, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "approve", "arguments": {"tool_name": "Bash",
                                                                          "input": {"command": "dir"}}}})
            self.assertEqual(json.loads(r["result"]["content"][0]["text"]),
                             {"behavior": "allow", "updatedInput": {"command": "dir"}})
            self.assertEqual(self.asked[0].task_id, 5)
            r = self._rpc(p, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "send_file", "arguments": {"path": "x"}}})
            self.assertTrue(r["result"]["isError"])
            self.assertEqual(r["result"]["content"][0]["text"], "不行")
        finally:
            p.stdin.close()
            p.wait(5)
            p.stdout.close()

    def test_broker_down_means_deny(self):
        p = self._spawn("http://127.0.0.1:9")  # 没人监听
        try:
            r = self._rpc(p, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "approve", "arguments": {"tool_name": "Bash", "input": {}}}})
            self.assertEqual(json.loads(r["result"]["content"][0]["text"])["behavior"], "deny")
        finally:
            p.stdin.close()
            p.wait(5)
            p.stdout.close()


class SecretFromMcpTest(unittest.TestCase):
    def test_args_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "claude.json"
            p.write_text(json.dumps({"mcpServers": {
                "feishu": {"command": "cmd", "args": ["/c", "npx", "pkg", "mcp", "-a", "cli_x", "-s", "S3CRET",
                                                      "-l", "zh"]},
                "envstyle": {"command": "x", "env": {"LARK_APP_ID": "cli_y", "LARK_APP_SECRET": "ENV_SECRET"}},
            }}), encoding="utf-8")
            self.assertEqual(credstore.secret_from_mcp(p, "feishu", "cli_x"), "S3CRET")
            self.assertEqual(credstore.secret_from_mcp(p, "envstyle", "cli_y"), "ENV_SECRET")
            with self.assertRaises(ValueError):  # App ID 对不上：不能拿别的应用的密钥
                credstore.secret_from_mcp(p, "feishu", "cli_other")
            with self.assertRaises(KeyError):
                credstore.secret_from_mcp(p, "nope", "cli_x")


class CredstoreTest(unittest.TestCase):
    def test_roundtrip(self):
        prefix = "feishu-bridge-selftest:"
        try:
            self.assertIsNone(credstore.read("x", prefix))
            credstore.write("x", "值-123", prefix)
            self.assertEqual(credstore.read("x", prefix), "值-123")
            with self.assertRaises(ValueError):
                credstore.write("x", "", prefix)
        finally:
            credstore.delete("x", prefix)
        self.assertIsNone(credstore.read("x", prefix))


class ExecutorTest(unittest.TestCase):
    def test_clean_env(self):
        cfg = Config(approval_timeout_seconds=300)
        os.environ["CLAUDECODE"] = "1"
        os.environ["ANTHROPIC_BASE_URL"] = "https://evil.example"
        try:
            env = clean_env(cfg, "tok")
        finally:
            os.environ.pop("CLAUDECODE", None)
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
        self.assertEqual(env["MT_FEISHU_NOTIFY_DISABLE"], "1")
        self.assertEqual(env["MCP_TOOL_TIMEOUT"], str(420 * 1000))
        self.assertIn("PATH", env)

    def test_build_cmd(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(max_budget_usd=5, model="opus", data_dir=Path(tmp.name))  # 渲染出的配置写进临时目录
        r = Runner(cfg, broker_url="u", broker_token="t", claude_cmd=["claude.exe"])
        cmd = r.build_cmd({"session_id": "sid"}, Path("m.json"), resume=False)
        self.assertEqual(Path(cmd[cmd.index("--settings") + 1]).parent, Path(tmp.name))
        self.assertEqual(cmd[:2], ["claude.exe", "-p"])
        for flag, val in (("--permission-prompt-tool", "mcp__bridge__approve"), ("--permission-mode", "default"),
                          ("--setting-sources", "user,project"), ("--session-id", "sid"),
                          ("--max-budget-usd", "5"), ("--model", "opus"), ("--mcp-config", "m.json")):
            self.assertEqual(cmd[cmd.index(flag) + 1], val)
        self.assertIn("--strict-mcp-config", cmd)
        cmd = r.build_cmd({"session_id": "sid"}, Path("m.json"), resume=True)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sid")
        self.assertNotIn("--session-id", cmd)

    def test_mcp_config(self):
        with tempfile.TemporaryDirectory() as d:
            r = Runner(Config(), broker_url="http://127.0.0.1:1", broker_token="tok", claude_cmd=["x"])
            p = r.write_mcp_config(Path(d), 9)
            spec = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["bridge"]
            self.assertEqual(spec["env"]["FB_TASK_ID"], "9")
            self.assertEqual(spec["env"]["FB_BROKER_TOKEN"], "tok")
            self.assertIn("SYSTEMROOT" if osutil.WINDOWS else "HOME", spec["env"])  # MCP 进程要的系统变量得带上
            self.assertTrue(spec["args"][-1].endswith("mcp_server.py"))

    def test_find_claude(self):
        with self.assertRaises(FileNotFoundError):
            find_claude(str(Path(tempfile.gettempdir()) / "nope" / CLAUDE_EXE))
        try:
            cmd = find_claude()
        except FileNotFoundError:
            self.skipTest("本机找不到 claude（没装桌面版，PATH 上也没有）")
        self.assertTrue(Path(cmd[0]).name.lower() == CLAUDE_EXE)

    def test_desktop_claudes_newest_first(self):
        # 桌面版自带的 claude 按版本号挑最新的（2.1.10 比 2.1.9 新，不能按字符串比）；两种目录结构都认
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for rel in (f"2.1.9/aaa/{CLAUDE_EXE}", f"2.1.10/bbb/{CLAUDE_EXE}", f"2.0.5/{CLAUDE_EXE}"):
                (base / rel).parent.mkdir(parents=True)
                (base / rel).write_text("")
            got = [p.relative_to(base).parts[0] for p in desktop_claudes(base)]
            self.assertEqual(got, ["2.1.10", "2.1.9", "2.0.5"])

    def test_desktop_claudes_mac_app_bundle(self):
        # Mac 上桌面版自带的 claude 包在 .app 里：<版本>/<hash>/claude.app/Contents/MacOS/claude（10-27 实测）
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for rel in ("2.1.308/aaa/claude.app/Contents/MacOS/claude", "2.1.309/bbb/claude.app/Contents/MacOS/claude"):
                (base / rel).parent.mkdir(parents=True)
                (base / rel).write_text("")
            got = [p.relative_to(base).parts[0] for p in desktop_claudes(base)]
            self.assertEqual(got, ["2.1.309", "2.1.308"])


class OsUtilTest(unittest.TestCase):
    """Windows / Mac 分开的那些事。标了 skipUnless 的只在对应系统上跑——Mac 上验收时跑一遍测试就能验到。"""

    def test_rule_path(self):
        self.assertEqual(rule_path(Path(r"D:\克劳德\feishu-bridge")), "//d/克劳德/feishu-bridge")
        self.assertEqual(rule_path(Path("/Users/me/work")), "//Users/me/work")  # Mac 的绝对路径也要 //

    def test_launch_agent_plist(self):
        import plistlib
        raw = osutil.launch_agent_plist("/usr/local/bin/python3", Path("/Users/me/feishu-bridge/bridge.py"),
                                        Path("/Users/me/feishu-bridge"), Path("/Users/me/feishu-bridge/data"),
                                        "/Users/me/.local/bin:/usr/bin:/bin")
        p = plistlib.loads(raw)
        self.assertEqual(p["Label"], "feishu-bridge")
        self.assertEqual(p["ProgramArguments"][0], "/usr/local/bin/python3")
        self.assertTrue(p["ProgramArguments"][1].endswith("bridge.py"))
        self.assertTrue(p["RunAtLoad"])
        self.assertEqual(p["KeepAlive"], {"SuccessfulExit": False})  # 正常退出（已有一个在跑）不再拉起
        self.assertIn(".local/bin", p["EnvironmentVariables"]["PATH"])
        self.assertTrue(p["StandardErrorPath"].endswith("launchd.err.log"))

    def test_single_instance(self):
        key = f"feishu-bridge-test-{os.getpid()}-{time.time_ns()}"
        self.assertTrue(osutil.single_instance(key))
        self.assertFalse(osutil.single_instance(key))  # 同一个名字第二次就拿不到
        self.assertFalse(osutil.wait_until_free(key, timeout=1))  # 还占着：等不到

    def test_wait_until_free(self):
        # 重启时先停旧的、等它真退出再起新的（10-27 撞过：没等，新的一看「已经有一个」就退了，一个都没在跑）
        key = f"feishu-bridge-test-{os.getpid()}-{time.time_ns()}"
        h = osutil._try_lock(key)
        self.assertIsNotNone(h)
        threading.Timer(1.0, osutil._release, args=(h,)).start()  # 模拟旧进程 1 秒后退出
        t0 = time.time()
        self.assertTrue(osutil.wait_until_free(key, timeout=10))
        self.assertGreaterEqual(time.time() - t0, 0.5)

    @unittest.skipUnless(osutil.WINDOWS, "只在 Windows 上")
    def test_windows_hidden_console(self):
        kw = osutil.hidden_console()
        self.assertEqual(kw["creationflags"], subprocess.CREATE_NEW_CONSOLE)
        self.assertEqual(kw["startupinfo"].wShowWindow, 0)

    @unittest.skipIf(osutil.WINDOWS, "只在 Mac / Linux 上")
    def test_posix_run_in_terminal_gives_a_tty(self):
        # claude --desktop 要求输出是终端；伪终端里 isatty() 应该是 True，输出也能拿回来
        rc, out = osutil.run_in_terminal([sys.executable, "-c", "import sys; print('tty', sys.stdout.isatty())"],
                                         None, dict(os.environ), timeout=30)
        self.assertEqual(rc, 0)
        self.assertIn("tty True", out)

    @unittest.skipIf(osutil.WINDOWS, "只在 Mac / Linux 上")
    def test_posix_kill_tree_kills_children(self):
        # 停任务时要连 claude 起的子进程一起杀（Windows 靠 Job 对象，Mac 靠进程组）
        import psutil
        code = ("import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code], **osutil.hidden_console())
        deadline = time.time() + 10
        while not psutil.Process(proc.pid).children() and time.time() < deadline:
            time.sleep(0.1)
        kids = psutil.Process(proc.pid).children(recursive=True)
        self.assertTrue(kids)
        osutil.kill_tree(proc)
        proc.wait(timeout=10)
        _, alive = psutil.wait_procs(kids, timeout=10)
        self.assertEqual(alive, [])

    @unittest.skipUnless(osutil.MAC, "只在 Mac 上")
    def test_mac_keep_awake_ends_with_bridge(self):
        # 防睡眠的 caffeinate 盯着飞书桥的进程号：飞书桥一退，它也得跟着退，不能一直挂着不让电脑睡
        import psutil
        code = (f"import sys; sys.path.insert(0, {str(BASE)!r}); from fb import osutil; "
                "p = osutil.keep_awake(); print(p.pid, ' '.join(p.args), flush=True)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        pid, args = r.stdout.split(" ", 1)
        self.assertIn("caffeinate -s -w", args)
        deadline = time.time() + 10
        while psutil.pid_exists(int(pid)) and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(psutil.pid_exists(int(pid)))


def _event(event_type, event):
    return json.dumps({"schema": "2.0", "header": {"event_id": f"ev_{event_type}", "event_type": event_type,
                                                   "create_time": "1792740000000", "token": "",
                                                   "app_id": "cli_test", "tenant_key": "t"},
                       "event": event}, ensure_ascii=False).encode("utf-8")


class FeishuDispatchTest(unittest.TestCase):
    """伪造的飞书事件包喂给真的 lark-oapi 分发器（长连接收到的就是这种包），不联网。"""

    def setUp(self):
        from fb.feishu import build_handler
        from fb.transport import ActionReply
        self.msgs, self.actions = [], []

        def on_action(a):
            self.actions.append(a)
            return ActionReply("已允许", "success", {"schema": "2.0", "body": {"elements": []}})

        self.h = build_handler(self.msgs.append, on_action)

    def test_message(self):
        self.h._do_without_validation(_event("im.message.receive_v1", {
            "sender": {"sender_id": {"open_id": "ou_me"}, "sender_type": "user"},
            "message": {"message_id": "om_1", "parent_id": "om_0", "root_id": "om_0",
                        "create_time": "1792740000000", "chat_id": "oc_1", "chat_type": "p2p",
                        "message_type": "text", "content": json.dumps({"text": "你好"})}}))
        m = self.msgs[0]
        self.assertEqual((m.msg_id, m.open_id, m.chat_type, m.parent_id, m.create_time_ms),
                         ("om_1", "ou_me", "p2p", "om_0", 1792740000000))
        self.assertEqual(policy.extract_text(m.msg_type, m.content), "你好")

    def test_bot_sender_ignored(self):
        self.h._do_without_validation(_event("im.message.receive_v1", {
            "sender": {"sender_id": {"open_id": "ou_bot"}, "sender_type": "app"},
            "message": {"message_id": "om_2", "chat_type": "p2p", "message_type": "text",
                        "content": "{}", "create_time": "1"}}))
        self.assertEqual(self.msgs, [])

    def test_card_action_response(self):
        from lark_oapi.core.json import JSON
        r = self.h._do_without_validation(_event("card.action.trigger", {
            "operator": {"open_id": "ou_me"}, "action": {"tag": "button", "value": {"k": "ap", "a": "x", "d": "allow"}},
            "context": {"open_message_id": "om_9", "open_chat_id": "oc_1"}}))
        a = self.actions[0]
        self.assertEqual((a.open_id, a.value, a.msg_id), ("ou_me", {"k": "ap", "a": "x", "d": "allow"}, "om_9"))
        out = json.loads(JSON.marshal(r))  # 长连接客户端就是这样把回包序列化回去的
        self.assertEqual(out["toast"], {"type": "success", "content": "已允许"})
        self.assertEqual(out["card"]["type"], "raw")
        self.assertEqual(out["card"]["data"]["schema"], "2.0")

    def test_ws_url_redacted_in_logs(self):
        import logging
        from fb.feishu import _RedactWsUrl
        rec = logging.LogRecord("Lark", logging.INFO, "", 0, "connected to %s [conn_id=1]",
                                ("wss://x/ws/v2?fpid=1&access_key=SECRETKEY&ticket=SECRETTICKET",), None)
        _RedactWsUrl().filter(rec)
        msg = rec.getMessage()
        self.assertNotIn("SECRETKEY", msg)
        self.assertNotIn("SECRETTICKET", msg)
        self.assertIn("fpid=1", msg)

    def test_other_events_are_acked(self):
        # 应用可能订阅了的其他事件（表情回复、机器人进群、文档评论……），外加一个没听说过的
        for et, ev in (("im.message.reaction.created_v1", {"message_id": "om_1", "reaction_type": {"emoji_type": "OK"}}),
                       ("im.chat.member.bot.added_v1", {"chat_id": "oc_2"}),
                       ("drive.notice.comment_add_v1", {"comment_id": "c"}),
                       ("im.chat.access_event.bot_p2p_chat_entered_v1", {"chat_id": "oc_1"}),
                       ("some.future.event_v9", {})):
            self.assertIsNone(self.h._do_without_validation(_event(et, ev)))  # 不抛错 = 回「收到」
        self.assertEqual((self.msgs, self.actions), ([], []))


class ConfigTest(unittest.TestCase):
    def test_example_loads(self):
        # 示例配置不带任何人的路径：工作目录、可发送目录、收件目录都跟着用户主目录走
        cfg = load(BASE / "config.example.toml")
        self.assertEqual(cfg.workdir, Path.home())
        self.assertEqual(cfg.setting_sources, "user,project")
        self.assertEqual(cfg.approval_timeout_seconds, 300)
        self.assertEqual(cfg.send_file_roots, [Path.home()])
        self.assertEqual(cfg.inbox_dir, Path.home() / "_飞书收件")
        self.assertFalse(cfg.auto_open_new_sessions)
        self.assertTrue(cfg.keep_awake)

    def test_derived_defaults_follow_workdir(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text("[claude]\nworkdir = 'D:\\工作'\n", encoding="utf-8")
            cfg = load(p)
            self.assertEqual(cfg.workdir, Path(r"D:\工作"))
            self.assertEqual(cfg.send_file_roots, [Path(r"D:\工作")])
            self.assertEqual(cfg.inbox_dir, Path(r"D:\工作") / "_飞书收件")

    def test_settings_json_valid(self):
        s = json.loads((BASE / "bridge-settings.json").read_text(encoding="utf-8"))
        self.assertIn("mcp__bridge__send_file", s["permissions"]["allow"])
        self.assertIn("Read(~/.ssh/**)", s["permissions"]["deny"])

    def test_render_runtime_files(self):
        # 模板里的路径占位符换成本机路径；换完还是合法 JSON，也没有漏掉的 {{...}}
        from fb.executor import render_runtime_files, rule_path
        self.assertEqual(rule_path(Path(r"D:\克劳德\feishu-bridge")), "//d/克劳德/feishu-bridge")
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(workdir=Path(r"E:\同事的工作"), data_dir=Path(d), send_file_roots=[Path(r"E:\同事的工作")])
            settings, prompt = render_runtime_files(cfg)
            s = json.loads(settings.read_text(encoding="utf-8"))
            self.assertIn("Edit(//e/同事的工作/.claude/settings.json)", s["permissions"]["deny"])
            self.assertIn(f"Edit({rule_path(BASE)}/**)", s["permissions"]["deny"])
            text = prompt.read_text(encoding="utf-8")
            self.assertIn(r"`E:\同事的工作`", text)
            self.assertIn(str(BASE), text)
            self.assertNotIn("{{", settings.read_text(encoding="utf-8") + text)


if __name__ == "__main__":
    unittest.main()
