"""把飞书 App Secret（和可选的 Claude 长期 token）存进系统密钥库（Windows 凭据管理器 / macOS 钥匙串），
输入、复制时都不显示。Windows 用 py，Mac 用 python3：

  python setup_secrets.py                  逐项输入；直接回车＝这一项不改
  python setup_secrets.py --from-mcp feishu  从 ~/.claude.json 里叫 feishu 的 MCP 配置复制 App Secret
                                           （只认 App ID 和 config.toml 一致的那组）
  python setup_secrets.py --show           只看哪几项已经存了（不显示内容）
  python setup_secrets.py --delete         两项都删掉

Claude 认证二选一：python bridge.py --login 登录一次（推荐，凭证由 claude 自己保存）；
或者 claude setup-token 生成长期 token，再用本脚本存进来。
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fb import config as fbconfig  # noqa: E402
from fb import credstore  # noqa: E402
from fb.osutil import PY  # noqa: E402

ITEMS = [
    (credstore.APP_SECRET, "飞书 App Secret（开发者后台 → 凭证与基础信息）"),
    (credstore.CLAUDE_TOKEN, "Claude 长期 token（claude setup-token 打印的那串；用 --login 登录过就回车跳过）"),
]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--from-mcp", metavar="NAME")
    args = ap.parse_args()
    if args.show:
        for name, label in ITEMS:
            print(("✅ 已存  " if credstore.read(name) else "—  没存  ") + label)
        return
    if args.delete:
        for name, label in ITEMS:
            print(("已删除  " if credstore.delete(name) else "本来就没有  ") + label)
        return
    if args.from_mcp:
        app_id = fbconfig.load().app_id
        secret = credstore.secret_from_mcp(Path.home() / ".claude.json", args.from_mcp, app_id)
        credstore.write(credstore.APP_SECRET, secret)
        print(f"已把 MCP「{args.from_mcp}」里 {app_id} 的 App Secret 存进{credstore.STORE_NAME}（没有显示）。")
        return
    for name, label in ITEMS:
        had = credstore.read(name) is not None
        value = getpass.getpass(f"{label}{'（已存过，回车跳过）' if had else ''}：").strip()
        if value:
            credstore.write(name, value)
            print("  已保存")
        else:
            print("  跳过")
    print(f"完成。接着跑：{PY} bridge.py --check")


if __name__ == "__main__":
    main()
