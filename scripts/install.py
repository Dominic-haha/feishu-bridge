"""把 skill 里带的飞书桥装到这台电脑：复制代码、生成 config.toml、装 Python 依赖。Windows 和 Mac 都行。

  Windows：py install.py --target "%USERPROFILE%\\feishu-bridge" --workdir "D:\\工作"
  Mac：    python3 install.py --target ~/feishu-bridge --workdir ~/work
  再跑一次（同一个 --target）= 升级代码，config.toml 和 data 不动
  公司网络访问 pypi 慢时加 --pip-index https://mirrors.aliyun.com/pypi/simple/

只做本机文件操作：不碰飞书后台、不存密钥、不装开机自启（那几步要用户本人在场）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "assets" / "feishu-bridge"
# 升级时保留：用户自己的配置、运行数据、Mac 上的虚拟环境
KEEP = {"config.toml", "data", "data-console", ".venv"}
MAC = sys.platform == "darwin"
# 装好后在安装目录里跑飞书桥用的命令。Mac 用安装目录里的虚拟环境：Homebrew 的 Python 不让往全局 pip install
PY = ".venv/bin/python3" if MAC else "py"


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform not in ("win32", "darwin"):
        sys.exit("飞书桥只支持 Windows 和 macOS。")
    if sys.version_info < (3, 11):
        sys.exit(f"需要 Python 3.11 以上，现在是 {sys.version.split()[0]}。")

    ap = argparse.ArgumentParser(description="安装或升级飞书桥")
    ap.add_argument("--target", default=str(Path.home() / "feishu-bridge"), help="安装目录")
    ap.add_argument("--workdir", default="", help="手机派的任务在哪个目录里跑；留空 = 用户主目录")
    ap.add_argument("--app-id", default="", help="飞书应用的 App ID（cli_ 开头），也可以之后再填")
    ap.add_argument("--pip-index", default="", help="pip 镜像地址")
    ap.add_argument("--skip-pip", action="store_true", help="不装依赖")
    ap.add_argument("--force", action="store_true", help="目标目录不是空的、也不是飞书桥时仍然装进去")
    a = ap.parse_args()

    target = Path(os.path.expandvars(a.target)).expanduser().resolve()
    workdir = Path(os.path.expandvars(a.workdir)).expanduser() if a.workdir else None
    if workdir is not None and not workdir.is_dir():
        sys.exit(f"工作目录不存在：{workdir}（先建好，或换一个）")
    if workdir is not None and "'" in str(workdir):
        sys.exit("工作目录路径里不能有单引号。")
    if a.app_id and not a.app_id.startswith("cli_"):
        sys.exit(f"App ID 应该以 cli_ 开头：{a.app_id}")
    if not (SRC / "bridge.py").exists():
        sys.exit(f"skill 里的飞书桥代码不全：{SRC}")

    upgrading = (target / "bridge.py").exists()
    if target.exists() and any(target.iterdir()) and not upgrading and not a.force:
        sys.exit(f"{target} 不是空目录，也不像飞书桥的安装目录。换一个目录，或确认无误后加 --force。")

    target.mkdir(parents=True, exist_ok=True)
    for item in SRC.iterdir():
        if item.name in KEEP or item.name == "__pycache__":
            continue
        dst = target / item.name
        if item.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(item, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, dst)
    print(("已升级" if upgrading else "已安装") + f"飞书桥代码：{target}")

    cfg = target / "config.toml"
    if cfg.exists():
        print("config.toml 已存在，保留原样（要改工作目录、App ID 就直接编辑它）")
    else:
        text = (target / "config.example.toml").read_text(encoding="utf-8")
        if workdir is not None:
            text = text.replace("workdir = ''", f"workdir = '{workdir}'", 1)
        if a.app_id:
            text = text.replace('app_id = ""', f'app_id = "{a.app_id}"', 1)
        cfg.write_text(text, encoding="utf-8")
        print(f"已生成 config.toml：工作目录 = {workdir or Path.home()}" + (f"，App ID = {a.app_id}" if a.app_id else ""))

    python = Path(sys.executable)
    if MAC:
        python = target / ".venv" / "bin" / "python3"
        if not python.exists():
            print("建虚拟环境：" + str(target / ".venv"))
            subprocess.run([sys.executable, "-m", "venv", str(target / ".venv")], check=True)
    if not a.skip_pip:
        cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r",
               str(target / "requirements.txt")]
        if a.pip_index:
            cmd += ["-i", a.pip_index]
        print("安装依赖：" + " ".join(cmd[3:]))
        r = subprocess.run(cmd)
        if r.returncode != 0:
            sys.exit("依赖没装上。公司网络慢的话，加 --pip-index https://mirrors.aliyun.com/pypi/simple/ 重跑。")

    if upgrading:
        steps = [f"重启飞书桥：{PY} bridge.py --restart"]
    else:
        steps = [] if a.app_id else ["建好飞书应用后，把 App ID 填进 config.toml 的 [feishu] app_id"]
        steps += [f"{PY} setup_secrets.py      ← 用户本人粘 App Secret（输入不显示）",
                  f"{PY} bridge.py --login     ← 用户本人登录 Claude",
                  f"{PY} bridge.py --check"]
    print(f"\n下一步（先 cd \"{target}\"）：")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")


if __name__ == "__main__":
    main()
