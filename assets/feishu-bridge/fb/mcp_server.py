"""给 claude -p 用的 stdio MCP server，只用标准库。

两个工具：
  approve    —— --permission-prompt-tool 指向它：把权限请求转给飞书桥，等你在手机上点按钮
  send_file  —— 把本机文件（截图、表格、报告）发到你的飞书

靠环境变量找到飞书桥：FB_BROKER_URL、FB_BROKER_TOKEN、FB_TASK_ID、FB_APPROVE_WAIT。
"""
import json
import os
import sys
import urllib.request

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "approve",
        "description": "（内部）飞书桥的权限审批工具，Claude Code 需要授权时会自动调用。不要直接调用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "input": {"type": "object"},
                "tool_use_id": {"type": "string"},
            },
            "required": ["tool_name", "input"],
        },
    },
    {
        "name": "send_file",
        "description": "把本机的一个文件（截图、图片、表格、报告等）发到用户的飞书。"
                       "path 用绝对路径；只能发工作目录下的文件，单个不超过 30MB；图片会直接显示。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件的绝对路径"},
                "caption": {"type": "string", "description": "可选：一句话说明这是什么"},
            },
            "required": ["path"],
        },
    },
]


def _post(path, body, timeout):
    req = urllib.request.Request(
        os.environ.get("FB_BROKER_URL", "") + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Bridge-Token": os.environ.get("FB_BROKER_TOKEN", "")},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def call_tool(name, args):
    task_id = os.environ.get("FB_TASK_ID", "")
    if name == "approve":
        try:
            d = _post("/approve", {"task_id": task_id, "tool_name": args.get("tool_name", ""),
                                   "input": args.get("input") or {}, "tool_use_id": args.get("tool_use_id", "")},
                      timeout=float(os.environ.get("FB_APPROVE_WAIT", "360")))
        except Exception as e:  # 飞书桥不在了：一律拒绝，不能放行
            d = {"behavior": "deny", "message": f"审批服务不可用（{e}），按拒绝处理。"}
        return {"content": [{"type": "text", "text": json.dumps(d, ensure_ascii=False)}]}
    if name == "send_file":
        try:
            d = _post("/send_file", {"task_id": task_id, "path": args.get("path", ""),
                                     "caption": args.get("caption", "")}, timeout=180)
        except Exception as e:
            d = {"ok": False, "message": f"发送失败：{e}"}
        return {"content": [{"type": "text", "text": d.get("message", "")}], "isError": not d.get("ok")}
    raise KeyError(name)


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return _result(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "feishu-bridge", "version": "1.0.0"}})
    if mid is None:  # 通知（notifications/initialized 等）不用回
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            return _result(mid, call_tool(p.get("name"), p.get("arguments") or {}))
        except KeyError:
            return _error(mid, -32602, f"unknown tool: {p.get('name')}")
    return _error(mid, -32601, f"method not found: {method}")


def main():
    stdin = sys.stdin.buffer if sys.stdin else os.fdopen(0, "rb")
    stdout = sys.stdout.buffer if sys.stdout else os.fdopen(1, "wb")
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            out = _error(None, -32700, "parse error")
        else:
            out = handle(msg) if isinstance(msg, dict) else _error(None, -32600, "invalid request")
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


if __name__ == "__main__":
    main()
