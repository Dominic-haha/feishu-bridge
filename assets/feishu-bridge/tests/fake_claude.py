"""假的 claude CLI：按 claude -p 的 stream-json 协议吐事件，离线测试飞书桥用，不花额度。

它会像真 claude 一样，按 --mcp-config 拉起审批 MCP（fb/mcp_server.py），走 JSON-RPC 调工具。
提示词里的关键词决定它干什么：
  APPROVE           申请执行一条 Bash（走 --permission-prompt-tool）
  APPROVE2          连着申请两次（测「本任务都允许」）
  SENDFILE:<路径>   调 send_file 工具
  SLEEP:<秒>        中途睡一会儿（测停止、超时）
  FAIL              不出 result，直接退出 1
  NOSESSION         带了 --resume 时，假装会话不存在
  BIG               结果特别长
  SECRET            结果里带像密钥的字符串
环境变量 FAKE_CLAUDE_ARGV：把命令行参数追加写进这个文件，测试拿来断言。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class Mcp:
    def __init__(self, cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            spec = json.load(f)["mcpServers"]["bridge"]
        env = dict(os.environ)
        env.update(spec.get("env") or {})
        self.p = subprocess.Popen([spec["command"], *spec.get("args", [])], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, env=env)
        self._id = 0
        init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "fake-claude", "version": "0"}})
        assert init["result"]["capabilities"].get("tools") is not None
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _write(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        self.p.stdin.flush()

    def request(self, method, params):
        self._id += 1
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        return json.loads(self.p.stdout.readline().decode("utf-8"))

    def call(self, name, args):
        return self.request("tools/call", {"name": name, "arguments": args})["result"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def main():
    ap = argparse.ArgumentParser()
    for flag in ("--output-format", "--setting-sources", "--settings", "--mcp-config", "--permission-mode",
                 "--permission-prompt-tool", "--max-turns", "--max-budget-usd", "--append-system-prompt-file",
                 "--session-id", "--resume", "--model"):
        ap.add_argument(flag)
    for flag in ("-p", "--verbose", "--strict-mcp-config"):
        ap.add_argument(flag, action="store_true")
    args, _ = ap.parse_known_args()
    log = os.environ.get("FAKE_CLAUDE_ARGV")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
        with open(log + ".cwd", "a", encoding="utf-8") as f:  # 在哪个目录被拉起来的
            f.write(os.getcwd() + "\n")

    prompt = sys.stdin.buffer.read().decode("utf-8")
    if "NOSESSION" in prompt and args.resume:
        sys.stderr.write(f"No conversation found with session ID: {args.resume}\n")
        sys.exit(1)
    if "FAIL" in prompt:
        sys.stderr.write("boom：假装崩了\n")
        sys.exit(1)

    sid = args.resume or args.session_id or str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "fake",
          "mcp_servers": [{"name": "bridge", "status": "connected"}]})
    mcp = Mcp(args.mcp_config) if args.mcp_config else None
    prompt_tool = (args.permission_prompt_tool or "").split("__")[-1]
    notes = []
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"收到：{prompt[:50]}"}]}})

    if "SLEEP:" in prompt:
        sec = float(prompt.split("SLEEP:")[1].split()[0])
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_sleep", "name": "Bash",
             "input": {"command": f"sleep {sec}", "description": "等一会儿"}}]}})
        time.sleep(sec)

    for i in range(2 if "APPROVE2" in prompt else 1 if "APPROVE" in prompt else 0):
        inp = {"command": f"echo step{i}", "description": f"第 {i + 1} 步"}
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"tu{i}", "name": "Bash", "input": inp}]}})
        res = mcp.call(prompt_tool, {"tool_name": "Bash", "input": inp, "tool_use_id": f"tu{i}"})
        d = json.loads(res["content"][0]["text"])
        notes.append(f"step{i}:{d['behavior']}" + (f"({d.get('message', '')})" if d["behavior"] == "deny" else ""))
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}",
             "content": "ok" if d["behavior"] == "allow" else d.get("message", "")}]}})

    if "SENDFILE:" in prompt:
        path = prompt.split("SENDFILE:")[1].split()[0]
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_file", "name": "mcp__bridge__send_file", "input": {"path": path}}]}})
        res = mcp.call("send_file", {"path": path, "caption": "测试文件"})
        notes.append(("sendfile:err:" if res.get("isError") else "sendfile:ok:") + res["content"][0]["text"])

    text = "完成：" + prompt.strip()[:40] + (" | " + "; ".join(notes) if notes else "")
    if "BIG" in prompt:
        text += "\n" + "很长的结果。" * 2000
    if "SECRET" in prompt:
        text += " key=sk-ant-oat01-abcdefghijklmnopqrstuvwxyz password: hunter22"
    if mcp:
        mcp.close()
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid,
          "total_cost_usd": 0.0123, "num_turns": 3, "duration_ms": 1234})


main()
