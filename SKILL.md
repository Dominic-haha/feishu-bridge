---
name: feishu-bridge
description: 飞书桥——在自己的电脑（Windows 或 Mac）上装一条「手机飞书 → 本机 Claude Code」的遥控通道：手机飞书私聊自己的机器人派活，电脑上的 Claude Code 执行，进度、审批卡、结果回到飞书；手机上还能列出和接着电脑上的会话、让电脑上的 Claude 桌面版打开某个会话，手机新开的会话会同步到电脑。用户说 飞书桥 / 用手机遥控电脑上的 Claude / 远程操控 Claude / 出门在外让电脑干活 / 在飞书里给电脑派任务 / 手机上审批 Claude 的操作 / 想要一个能控制自己电脑的飞书机器人 / 装同事那个飞书机器人，或者飞书桥已经装好但出了问题（发消息没反应、按钮点了没用、底部菜单不显示、Claude 登录失效、电脑上打开失败、Mac 上钥匙串弹窗或后台读不了文件）时，都用这个 skill，即使用户没说「飞书桥」三个字。
compatibility: Windows 10/11 或 macOS；Python 3.11+；Claude 桌面版（或原生安装的 claude）并能用 claude.ai 账号登录；用户要能在飞书开发者后台建企业自建应用。
---

# 飞书桥：手机飞书 → 本机 Claude Code

装好之后，用户在手机飞书里私聊**自己的**机器人发一句话，这台电脑上的 Claude Code（`claude -p`）就会执行；执行进度、需要批准的操作（审批卡）、最终结果都回到飞书。不需要服务器和公网 IP：飞书长连接是本机主动连出去的。一次只跑一个任务，其余排队。

skill 里带了完整代码（`assets/feishu-bridge/`）和安装脚本（`scripts/install.py`）。你的工作是带着用户走完安装：能在本机做的你来做，必须用户本人做的（复制密钥、登录、在手机上发消息）讲清楚让他做，然后**用日志核实**，别只听「好了」——这类配置很容易差一步，用户自己往往察觉不到。

## 先分清是哪种电脑

Windows 和 Mac 的流程一样，只是命令写法和几个系统细节不同。下文的 `PY` 指：

- **Windows**：`py`（没有就用 `python`），开机自启是「计划任务」，密钥存「Windows 凭据管理器」。
- **Mac**：安装目录里的 `.venv/bin/python3`（安装脚本会建这个虚拟环境：Homebrew 的 Python 不让往全局装依赖），开机自启是 launchd「开机代理」（`~/Library/LaunchAgents/feishu-bridge.plist`），密钥存「钥匙串」。Mac 支持是后加的，Mac 上遇到文档没写到的情况，如实告诉用户并记下现象。

所有命令都在**安装目录**下运行。

## 先确认能装

逐项检查，缺什么先补什么：

1. **Python 3.11+**：Windows `py -3 --version`；Mac `python3 --version`（Mac 自带的 /usr/bin/python3 多半是 3.9，不够）。Mac 上 `python3` 太旧时先看 `ls /opt/homebrew/bin/python3.1*`：已经有 Homebrew 装的 3.11 以上，下面第 1 步就直接用它（比如 `/opt/homebrew/bin/python3.12`）跑 install.py，`.venv` 会按这个版本建（10-27 实测 Mac mini 就是这样：`python3` 是 3.9.6，Homebrew 里有 3.12）。都没有或太旧，请用户从 python.org 装（Windows 勾选 Add to PATH），或经他同意用 `winget install Python.Python.3.12`（Windows）/ `brew install python@3.12`（Mac，有 Homebrew 的话）。
2. **Claude Code**：飞书桥会先找 Claude 桌面版自带的 claude（Windows 在 `%APPDATA%\Claude\claude-code\`，Mac 在 `~/Library/Application Support/Claude/claude-code/<版本>/<hash>/claude.app/Contents/MacOS/claude`），其次找 PATH 和 `~/.local/bin`、Homebrew 目录。都没有就请用户装 Claude 桌面版。Mac 上桌面版自带的和 `~/.local/bin` 的 claude 共用钥匙串里的登录，其中一个登录过，另一个也算登录了（10-27 实测）。
3. **飞书建应用的权限**：用户浏览器打开 https://open.feishu.cn/app 能看到「创建企业自建应用」按钮。

## 安装步骤

开始前先和用户定两个目录：

- **安装目录**：默认 Windows `%USERPROFILE%\feishu-bridge`，Mac `~/feishu-bridge`。
- **工作目录**：手机派的任务在哪个文件夹里跑，一般是他平时放工作文件的地方；它决定 Claude 默认能读写哪些文件、加载哪一份项目记忆。**Mac 上最好别选「文稿 / 桌面 / 下载」里的文件夹**：这三个是 macOS 受保护的位置。飞书桥第一次读写时，电脑上会弹授权框（申请的是飞书桥用的 Python），要有人在电脑前点「允许」，点过一次就记住（升级或换 Python 后会再问）；人在外面就没人点，任务会停在那里等，点了「不允许」就报「Operation not permitted」。手机上「接着聊」的会话在这三个文件夹里时也一样。所以用 `~/work` 这类普通文件夹；实在要用，装好后让用户在电脑前用手机发一个读这个文件夹的任务，弹框时点允许（详见 `references/troubleshooting.md`）。

### 1. 装代码

```
Windows：py "<skill 目录>\scripts\install.py" --target "%USERPROFILE%\feishu-bridge" --workdir "D:\他的工作目录"
Mac：    python3 "<skill 目录>/scripts/install.py" --target ~/feishu-bridge --workdir ~/work
         （python3 太旧就换成上面查到的 /opt/homebrew/bin/python3.12 这类完整路径）
```

它会复制代码、生成 `config.toml`（填好工作目录）、装依赖（Mac 上装进 `.venv`）。再跑一次就是升级，`config.toml`、`data`、`.venv` 不动；升级完要在安装目录跑 `PY bridge.py --restart`，安装脚本不会自己重启飞书桥。pip 在公司网络里超时，就加 `--pip-index https://mirrors.aliyun.com/pypi/simple/`。想确认环境没问题，可以在安装目录跑 `PY -m unittest discover -s tests`（1～2 分钟，全过即可；Mac 上会多跑几个只在 Mac 上才跑的测试，正好验证伪终端、进程组）。

### 2. 建飞书应用

按 `references/feishu-console.md` 的「第一段」做：建**普通的企业自建应用**、加机器人能力、开 4 项权限。

⚠ 必须是在开发者后台「创建企业自建应用」建的普通应用。妙搭（部署 OpenClaw）、Aily、开发者后台首页「创建飞书智能体应用」这类平台一键建的应用，底部那栏被平台接管，后台配的菜单不生效，平台的智能体还可能连上来和飞书桥抢消息。用户说「我已经有个机器人」时先问清是怎么建的。

如果你有能操作浏览器的工具、浏览器里登着用户的飞书，后台这些配置你可以代点（都能改回来）；但**创建版本并发布之前先跟用户确认**，App Secret 永远不要由你去看、复制或粘贴。

### 3. 填 App ID、存 App Secret

- App ID（`cli_` 开头）写进安装目录 `config.toml` 的 `[feishu] app_id`。
- App Secret 让用户本人粘：在终端里进安装目录跑 `PY setup_secrets.py`，提示时粘贴 App Secret 回车（输入不显示），第二项 Claude token 直接回车跳过。它存进凭据管理器 / 钥匙串，不落文件。**别让用户把 Secret 发到对话里**——聊天记录会留存，泄露后谁都能冒充这个机器人。
- Mac 上以后飞书桥读钥匙串时，偶尔会弹「python3 想要使用钥匙串中的……」：让用户输入登录密码、点「始终允许」。升级 Python 之后可能会再弹一次。

### 4. 登录 Claude

用户在终端跑 `PY bridge.py --login`：浏览器打开 claude.ai 授权页，登录授权后把网页给的授权码粘回终端。登录凭证由 claude 自己保存。然后你跑 `PY bridge.py --check`，除了「白名单为空」以外都应是 ✅（Mac 上工作目录在受保护文件夹里会有一条 ⚠️）。

### 5. 让飞书桥连上

飞书后台保存「长连接」之前会检测有没有客户端连着，所以先把飞书桥跑起来：

```
PY bridge.py --install-task
```

Windows 上它装计划任务 `FeishuBridge`，Mac 上装开机代理 `feishu-bridge`；都是登录后自动启动、异常退出后自动重启，装完立刻启动一次。看 `data/bridge.log` 出现 `connected to wss://` 就是连上了（Mac 上启动失败还可以看 `data/launchd.err.log`）。白名单为空时飞书桥处于「配对模式」：收到消息只回 open_id，不执行任务。

### 6. 配事件、回调、菜单，发布

按 `references/feishu-console.md` 的「第二段」做：事件 `im.message.receive_v1`、回调 `card.action.trigger`（两处都选长连接并「验证」），底部菜单，然后创建版本发布。可用范围只放用户自己时显示「本次发布免审核」，提交即生效。

### 7. 配对

请用户在手机飞书里搜到这个机器人，随便发一句话。看 `data/bridge.log` 里的 `配对模式：收到 open_id=ou_…`，把这个 open_id 写进 `config.toml` 的 `allowed_open_ids = ["ou_…"]`，然后重启：

```
PY bridge.py --restart
```

open_id 是「每个应用一套」的，换了应用要重新配对。改了 `config.toml` 都要 `--restart` 才生效。

### 8. 验收

请用户在手机上依次做，你每一步都去看 `data/audit.jsonl` 核对（`command` / `attach` / `open_on_pc` / `task` / `approval_ask` / `approval` / `finish` 事件）：

1. 底部菜单点「切换会话」→ 弹出会话列表卡（审计里 `command: sessions`）。
2. 点键盘图标切到输入框，发一句要写文件的任务，比如「在工作目录里建一个 hi.txt，写一句你好」→ 审批卡点「允许」→ 收到结果卡（`finish` 的 `status` 是 `done`）。
3. 「切换会话」里选一个电脑上的会话点「接着聊」，再点「会话 ▸ 电脑上打开」→ 电脑上的桌面版切到那个会话（`open_on_pc` 的 `ok` 是 `true`）。

都过了，把 `references/phone-usage.md` 的用法讲给用户（或直接发给他）。问他要不要打开「手机新开的会话跑完自动同步到电脑桌面版」：`config.toml` 的 `[policy] auto_open_new_sessions = true`，再 `--restart`。代价是他在电脑上干活时，桌面版会突然跳到新会话；而且会话跑完就在电脑上开着了（桌面版给它起了进程），再从手机接着聊会先弹「正开着」的确认（10-27 Mac 实测）。

最后提醒一句：**电脑睡着就收不到消息**。Windows 在电源设置里把插电时的睡眠改成「从不」。Mac 上飞书桥运行时自带防睡眠（`config.toml` 的 `[policy] keep_awake`，默认开）：接着电源就不会自己睡，显示器照常熄屏；笔记本拔了电源照常睡，合上盖子通常还是会睡。

## 安全底线

飞书桥等于「拿到这个飞书账号的人能操作这台电脑」，所以层层设防。装的时候别为了省事拆掉其中任何一层：

- **只认一个人**：`allowed_open_ids` 只放用户自己；应用「可用范围」也只放他自己。陌生人的消息飞书桥不回话，只记审计。
- **审批**：需要授权的操作一条条发审批卡，5 分钟不点按拒绝。不要改成 `bypassPermissions`。注意：用户在桌面版里对某些命令点过「总是允许」的，远程任务也会直接放行，`--check` 会列出这类规则，提醒用户看一眼。
- **禁区**：`bridge-settings.json` 的 deny 规则挡住读私钥/`.env`/钥匙串、改 Claude 配置和飞书桥自己、`git push`、计划任务/开机代理（launchctl）、注册表、`security`、`sudo`、关机。用户有别的敏感文件（比如密钥文件），加到 deny 里。
- **急停**：手机发 `/急停`；手机丢了就在电脑上 `PY bridge.py --uninstall-task`，或请管理员停用这个应用。
- **一个应用只给飞书桥用**：两个程序连同一个应用的长连接时，飞书把消息随机分给其中一个，表现为「有的消息没反应」。

## 出问题时

先跑 `PY bridge.py --check`，再看 `data/bridge.log`、`data/audit.jsonl`，然后对照 `references/troubleshooting.md` 的现象表。最常见的：应用不是普通自建应用（菜单不显示）、回调没配（按钮没反应）、Claude 登录失效（任务报登录错误，重新 `PY bridge.py --login`）；Mac 上还有工作目录在受保护文件夹里、钥匙串弹窗没点「始终允许」。

## 文件

- `assets/feishu-bridge/`：飞书桥代码（含 `README.md` 技术说明和测试），由 `scripts/install.py` 复制到安装目录，不要直接在 skill 目录里运行。
- `references/feishu-console.md`：开发者后台逐步配置，包括菜单名称必须一字不差的原因。
- `references/phone-usage.md`：手机上怎么用，装完给用户看。
- `references/troubleshooting.md`：现象 → 原因 → 处理（Windows、Mac 分开写了）。
