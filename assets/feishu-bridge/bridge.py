"""飞书桥入口（Windows 用 py，Mac 用 python3）。

  python bridge.py                 正式运行（连飞书长连接；开机自启时由计划任务 / launchd 起它）
  python bridge.py --check         自检：配置、凭据、claude、飞书连通、你的放行规则
  python bridge.py --console       本地控制台模式：不连飞书，键盘输入当消息（真的会调 claude）
  python bridge.py --console --fake-claude   同上，但用假 claude 演练流程，不花额度
  python bridge.py --install-task  装「登录时自动启动、挂了自动重启」：Windows 计划任务，Mac 开机代理；--uninstall-task 卸载
  python bridge.py --restart       重启（改了 config.toml 之后用）
  python bridge.py --login         用 claude 登录一次，凭证由 claude 自己保存（推荐）
  python bridge.py --setup-token   或者：生成一年期 token，再用 setup_secrets.py 存起来
"""
from __future__ import annotations

import argparse
import json
import locale
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from fb import __version__, credstore, osutil  # noqa: E402
from fb import config as fbconfig  # noqa: E402
from fb.executor import clean_env, find_claude, python_exe  # noqa: E402
from fb.osutil import PY  # noqa: E402

TASK_NAME = "FeishuBridge"
log = logging.getLogger("fb")


def setup_logging(data_dir: Path, console: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(data_dir / "bridge.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:  # pythonw 下没有 stderr
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        # Mac 上 launchd 把 stderr 写进 data/launchd.err.log，那里不轮转：只留警告和报错，别把 bridge.log 再抄一遍
        sh.setLevel(logging.WARNING if console or not sys.stderr.isatty() else logging.INFO)
        root.addHandler(sh)


# ---------- 自检 ----------
def check(cfg: fbconfig.Config) -> int:
    bad = 0

    def line(ok: bool, text: str, warn: bool = False) -> None:
        nonlocal bad
        if not ok and not warn:
            bad += 1
        print(("✅ " if ok else ("⚠️ " if warn else "❌ ")) + text)

    line(bool(cfg.app_id), "App ID：" + (cfg.app_id or "没填（config.toml 的 [feishu] app_id）"))
    line(bool(cfg.allowed_open_ids), "白名单：" + ("、".join(cfg.allowed_open_ids) if cfg.allowed_open_ids
                                             else "空——先启动，给机器人发条消息拿到你的 open_id"),
         warn=not cfg.allowed_open_ids)
    secret = credstore.read(credstore.APP_SECRET)
    line(bool(secret), "App Secret：" + (f"已存在{credstore.STORE_NAME}" if secret
                                        else f"没存，跑 {PY} setup_secrets.py"))
    token = credstore.read(credstore.CLAUDE_TOKEN)
    line(cfg.workdir.is_dir(), f"工作目录：{cfg.workdir}")
    if osutil.MAC:  # 读写「文稿/桌面/下载」要用户授权：第一次在电脑上弹框（申请的是飞书桥的 Python，10-27 实测），人不在电脑前就卡住
        protected = [Path.home() / d for d in ("Documents", "Desktop", "Downloads")]
        inside = any(cfg.workdir == p or p in cfg.workdir.parents for p in protected)
        line(not inside, "工作目录在「文稿 / 桌面 / 下载」里：第一次读写时电脑上会弹授权框，要有人点「允许」，"
             "人不在电脑前任务就卡住；建议换到 ~/work 这类普通文件夹"
             if inside else "工作目录不在 macOS 受保护的文件夹里", warn=inside)

    cmd = None
    try:
        cmd = find_claude(cfg.claude_path)
        ver = subprocess.run(cmd + ["--version"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60, **osutil.no_window()).stdout.strip()
        line(True, f"claude：{cmd[-1]}（{ver}）")
    except Exception as e:
        line(False, f"claude：{e}")
    if cmd:  # 密钥库里有长期 token 就用它，没有就看 claude 自己的登录态（--login）
        try:
            r = subprocess.run(cmd + ["auth", "status"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60, env=clean_env(cfg, token), cwd=str(cfg.workdir),
                               **osutil.no_window())
            try:
                method = json.loads(r.stdout).get("authMethod", "?")
            except ValueError:
                method = r.stdout.strip()[:80] or r.stderr.strip()[:80]
            ok = r.returncode == 0 and method not in ("none", "")
            source = f"{credstore.STORE_NAME}里的长期 token" if token else "claude 自己保存的登录"
            line(ok, f"Claude 认证：{method}（{source}）" if ok else f"Claude 认证：没登录，跑 {PY} bridge.py --login")
        except Exception as e:
            line(False, f"Claude 认证：{e}")

    if cfg.app_id and secret:
        try:
            from fb.feishu import FeishuTransport
            bot = FeishuTransport(cfg.app_id, secret).check()
            line(True, f"飞书：机器人「{bot.get('app_name', '?')}」可用")
        except Exception as e:
            line(False, f"飞书：{e}")

    # 远程任务会继承用户级 settings 的放行规则：命令、写文件类的规则要心里有数
    if "user" in cfg.setting_sources:
        try:
            s = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
            risky = [r for r in (s.get("permissions") or {}).get("allow") or []
                     if r.split("(")[0] in ("Bash", "PowerShell", "Edit", "Write", "WebFetch")]
            line(not risky, "用户级放行规则：" + ("没有命令/写文件类" if not risky else
                                            "远程任务也会直接放行这些：" + "、".join(risky)), warn=bool(risky))
        except (OSError, ValueError):
            pass
    print("\n自检通过。" if not bad else f"\n有 {bad} 项要处理。")
    return 0 if not bad else 1


# ---------- 计划任务 ----------
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>飞书桥：手机飞书派任务给这台电脑上的 Claude Code</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT30S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{pythonw}</Command><Arguments>"{script}"</Arguments><WorkingDirectory>{workdir}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    # schtasks 按系统代码页（中文 Windows 是 GBK）输出；getencoding 不受 PYTHONUTF8 影响
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          encoding=locale.getencoding(), errors="replace")


def install_task(cfg: fbconfig.Config) -> int:
    if not osutil.WINDOWS:
        return _install_agent(cfg)
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    xml = TASK_XML.format(user=escape(user), pythonw=escape(python_exe(windowless=True)),
                          script=escape(str(BASE / "bridge.py")), workdir=escape(str(BASE)))
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(path).write_text(xml, encoding="utf-16")
        r = _schtasks("/Create", "/TN", TASK_NAME, "/XML", path, "/F")
    finally:
        os.unlink(path)
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        return r.returncode
    r = _schtasks("/Run", "/TN", TASK_NAME)
    print((r.stdout + r.stderr).strip())
    return r.returncode


def uninstall_task() -> int:
    if not osutil.WINDOWS:
        osutil.launchctl("bootout", f"{osutil.gui_domain()}/{osutil.AGENT_LABEL}")
        path = osutil.launch_agent_path()
        if path.exists():
            path.unlink()
        print(f"已卸载开机代理（{path}）")
        return 0
    _schtasks("/End", "/TN", TASK_NAME)
    r = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    print((r.stdout + r.stderr).strip())
    return r.returncode


def _install_agent(cfg: fbconfig.Config) -> int:
    """Mac：装 launchd 开机代理（~/Library/LaunchAgents/feishu-bridge.plist），装完立刻启动。"""
    path = osutil.launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(osutil.launch_agent_plist(sys.executable, BASE / "bridge.py", BASE, cfg.data_dir,
                                               osutil.login_shell_path()))
    domain = osutil.gui_domain()
    osutil.launchctl("bootout", f"{domain}/{osutil.AGENT_LABEL}")  # 装过旧的先卸掉，没装过会报错，忽略
    for _ in range(5):  # 刚卸掉时马上装回去偶尔报 5: Input/output error，等一下再试
        r = osutil.launchctl("bootstrap", domain, str(path))
        if r.returncode == 0:
            break
        time.sleep(1)
    print((r.stdout + r.stderr).strip() or f"已装开机代理并启动：{path}")
    return r.returncode


def restart() -> int:
    """重启飞书桥：改了 config.toml、升级了代码之后用。"""
    if osutil.WINDOWS:
        _schtasks("/End", "/TN", TASK_NAME)
        if not osutil.wait_until_free("feishu-bridge"):  # 10-27 撞过：旧的还没退完就起新的，新的自己退了，结果一个都没在跑
            print("旧的飞书桥 30 秒内没退出，先不启动新的；在任务管理器里结束 pythonw 后再试")
            return 1
        r = _schtasks("/Run", "/TN", TASK_NAME)
    else:
        if not osutil.launch_agent_path().exists():
            print(f"还没装开机自启：先跑 {PY} bridge.py --install-task")
            return 1
        r = osutil.launchctl("kickstart", "-k", f"{osutil.gui_domain()}/{osutil.AGENT_LABEL}")
    print((r.stdout + r.stderr).strip() or "已重启")
    return r.returncode


def _stop_on_sigterm() -> None:
    """Mac：launchd 停飞书桥发的是 SIGTERM，转成和 Ctrl-C 一样的正常退出，先停掉正在跑的任务。"""
    def handler(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)


# ---------- 运行 ----------
def main() -> None:
    ap = argparse.ArgumentParser(description="飞书桥")
    ap.add_argument("--config", help="配置文件路径，默认 config.toml")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--console", action="store_true")
    ap.add_argument("--fake-claude", action="store_true")
    ap.add_argument("--install-task", action="store_true")
    ap.add_argument("--uninstall-task", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--setup-token", action="store_true")
    ap.add_argument("--login", action="store_true")
    args = ap.parse_args()
    # 输出被管道接走时 Windows 默认按 GBK 编码，卡片里的 emoji 会让 print 直接崩
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.stdin is not None and not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8")
    if osutil.MAC and not os.environ.get("SSL_CERT_FILE"):
        try:  # python.org 装的 Python 默认没有根证书，连飞书长连接会报 CERTIFICATE_VERIFY_FAILED
            import certifi
            os.environ["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass

    cfg = fbconfig.load(args.config)
    if args.check:
        sys.exit(check(cfg))
    if args.install_task:
        sys.exit(install_task(cfg))
    if args.uninstall_task:
        sys.exit(uninstall_task())
    if args.restart:
        sys.exit(restart())
    if args.setup_token:
        cmd = find_claude(cfg.claude_path) + ["setup-token"]
        print("运行：", " ".join(cmd), f"\n登录完成后会打印一串 token，复制下来，再跑 {PY} setup_secrets.py 粘进去。\n")
        sys.exit(subprocess.call(cmd, env=clean_env(cfg, None)))
    if args.login:
        cmd = find_claude(cfg.claude_path) + ["auth", "login"]
        print("运行：", " ".join(cmd))
        print("浏览器没自动打开的话，复制下面出来的链接去浏览器打开；登录、授权后网页会给一串授权码，\n"
              "把它粘回这里回车。成功后凭证由 claude 自己保存，不用再复制任何东西。\n")
        sys.exit(subprocess.call(cmd, env=clean_env(cfg, None)))

    from fb.app import Bridge
    from fb.store import Store

    if args.console:
        from fb.transport import ConsoleTransport
        cfg.data_dir = BASE / "data-console"  # 和正式运行的任务库分开
        setup_logging(cfg.data_dir, console=True)
        if not osutil.single_instance("feishu-bridge-console"):
            sys.exit("已经有一个控制台模式在跑")
        cfg.allowed_open_ids = [ConsoleTransport.OPEN_ID]
        claude_cmd = [python_exe(), str(BASE / "tests" / "fake_claude.py")] if args.fake_claude else None
        token = None if args.fake_claude else credstore.read(credstore.CLAUDE_TOKEN)
        bridge = Bridge(cfg, Store(cfg.data_dir / "bridge.db"), ConsoleTransport(),
                        token_getter=lambda: token, claude_cmd=claude_cmd, secrets=[token])
        try:
            bridge.run_forever()
        finally:
            bridge.shutdown()
        return

    setup_logging(cfg.data_dir, console=False)
    if not osutil.single_instance("feishu-bridge"):
        log.error("已经有一个飞书桥在运行，这个退出")
        sys.exit(0)  # 退出码 0：别让计划任务 / launchd 当成失败反复重启
    if not osutil.WINDOWS:
        _stop_on_sigterm()
    if cfg.keep_awake and osutil.keep_awake():
        log.info("接电源时不让电脑自动睡眠（caffeinate，飞书桥退出就解除）")
    secret = credstore.read(credstore.APP_SECRET)
    if not cfg.app_id or not secret:
        log.error("缺 App ID 或 App Secret：先填 config.toml，再跑 %s setup_secrets.py", PY)
        sys.exit(1)
    from fb.feishu import FeishuTransport
    bridge = Bridge(cfg, Store(cfg.data_dir / "bridge.db"), FeishuTransport(cfg.app_id, secret),
                    token_getter=lambda: credstore.read(credstore.CLAUDE_TOKEN),
                    secrets=[secret, credstore.read(credstore.CLAUDE_TOKEN)])
    log.info("飞书桥 v%s 启动：工作目录 %s，白名单 %s", __version__, cfg.workdir, cfg.allowed_open_ids or "（空，配对模式）")
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl-C / 停止信号，飞书桥退出")
    finally:
        bridge.shutdown()


if __name__ == "__main__":
    main()
