# 飞书桥

在手机飞书里私聊你自己的机器人发文字，这台电脑（Windows 或 Mac）上的 Claude Code（`claude -p`）就会在你设定的工作目录下执行。执行进度、需要你批准的操作、最终结果都会回到飞书。

- 不需要服务器，也不需要公网 IP：飞书长连接是本机主动连出去的。
- 一次只跑一个任务，其余排队（桌面和浏览器只有一份）。
- 需要授权的操作会一条一条发审批卡给你，5 分钟不点按拒绝处理。
- 手机上能列出、接着电脑上的 Claude 会话，也能让电脑上的 Claude 桌面版打开某个会话。

安装、飞书后台配置、手机用法、排错，见 skill 里的 `SKILL.md` 和 `references/`。下面是命令速查和技术说明。

## 命令

在安装目录下。`PY` 在 Windows 上是 `py`，在 Mac 上是 `.venv/bin/python3`（安装脚本建的虚拟环境）：

```
PY bridge.py --check            自检：App ID、密钥、claude、登录、飞书、放行规则
PY bridge.py --login            登录 Claude（claude auth login，凭证由 claude 自己保存）
PY setup_secrets.py             把飞书 App Secret 存进系统密钥库（输入不显示）
PY setup_secrets.py --show      看哪几项已经存了（不显示内容）
PY bridge.py --install-task     装开机自启（登录后启动，挂了自动重启）：Windows 计划任务 FeishuBridge，Mac 开机代理 feishu-bridge
PY bridge.py --uninstall-task   卸载开机自启
PY bridge.py --restart          重启（改了 config.toml、升级了代码之后）
PY bridge.py                    前台运行（调试用，Ctrl+C 退出）
PY bridge.py --console --fake-claude   不连飞书、用假 claude 演练流程
PY -m unittest discover -s tests       跑测试（Mac 上会多跑几个 Mac 专用的）
```

改了 `config.toml` 要 `--restart` 才生效。

| | Windows | Mac |
|---|---|---|
| 密钥存在 | 凭据管理器（`feishu-bridge:*` 两项） | 钥匙串（服务名 `feishu-bridge`） |
| 开机自启 | 计划任务 `FeishuBridge` | `~/Library/LaunchAgents/feishu-bridge.plist`，另有 `data/launchd.*.log`（只记警告、报错和启动失败） |
| 停任务时杀子进程 | Job 对象 | 进程组 |
| 防睡眠 | 靠电源设置 | 飞书桥自带：`caffeinate -s -w <飞书桥进程号>`，接电源时生效（`[policy] keep_awake`） |
| 桌面版数据 | `%APPDATA%\Claude\` | `~/Library/Application Support/Claude/` |
| 桌面版自带的 claude | `%APPDATA%\Claude\claude-code\<版本>\<hash>\claude.exe` | `…/Claude/claude-code/<版本>/<hash>/claude.app/Contents/MacOS/claude` |
| 认「电脑上正开着」 | `~/.claude/sessions/<pid>.json` 的登记 + 进程命令行里的 `--resume=<会话ID>` | 同左（Mac 上新开的会话命令行里没有会话 ID，全靠登记文件） |

## 配置

`config.toml`（`config.example.toml` 里每项都有注释）。最常改的：

| 项 | 说明 |
|---|---|
| `[feishu] app_id` | 飞书应用的 App ID |
| `[feishu] allowed_open_ids` | 只接受这些人的单聊消息；留空 = 配对模式（收到消息只回 open_id） |
| `[claude] workdir` | 任务在哪个目录跑；留空 = 用户主目录 |
| `[claude] max_budget_usd` | 单个任务的花费上限（美元，按 Claude Code 自己的估算） |
| `[policy] send_file_roots` | Claude 只能把这些目录下的文件发给你；留空 = 只有工作目录 |
| `[policy] auto_open_new_sessions` | 手机上新开的会话跑完，自动在电脑的 Claude 桌面版里打开 |

`bridge-settings.json`、`remote-mode.md` 是模板：里面的 `{{WORKDIR_RULE}}` 这类占位符，飞书桥每次执行时按本机的安装目录和工作目录填好，写到 `data\` 下再用。

## 安全设计

拿到你飞书账号的人，就能操作这台电脑。所以按层设防：

| 层 | 措施 |
|---|---|
| 身份 | 只认 `allowed_open_ids` 里的人，而且只认单聊；陌生人的消息不回话，只记审计。应用的可用范围也只放你自己 |
| 通道 | 只有出站连接，没有入站端口。审批中转只监听 127.0.0.1 的随机端口，令牌每次启动重新生成 |
| 边界 | `bridge-settings.json` 的 deny 规则：读 `~/.ssh`、`.env`、`*.pem` 等私钥类文件和钥匙串；改 Claude 配置和 hooks；改飞书桥自己；`git push`、计划任务 / launchctl、注册表、`security`、`sudo`、关机 |
| 审批 | 其余需要授权的操作一律发审批卡；不使用 `bypassPermissions` |
| MCP | 用 `--strict-mcp-config`，远程任务默认只有飞书桥自己的两个工具；要加别的，在 `extra_mcp_servers` 里写名字 |
| 时效 | 积压的、重复的消息先确认再执行 |
| 急停 | 手机发 `/急停`；或者在电脑上停掉计划任务 |
| 回传 | 发往飞书的文字先脱敏（token、key、密码样式的字符串，以及密钥原文）；`send_file` 只能发 `send_file_roots` 下的文件，并拒绝密钥类文件名 |
| 进程 | Windows 上每个任务的进程树都放进 Job 对象，停止、超时时一起杀掉，飞书桥自己崩了系统也会把它们清掉；Mac 上整组杀，飞书桥被 launchd 停掉时先停任务再退出 |
| 留痕 | `data\audit.jsonl`（只追加）、`data\runs\<任务号>\events.jsonl`（Claude 的完整事件流），都只留在本机 |

两点要心里有数：

- 远程任务会加载你的**用户级 settings**（技能、模型偏好、语言、hooks 都带上）。你在桌面版对某个命令点过「总是允许」的话，远程任务也会直接放行。`--check` 会列出这类规则。
- 审批卡挡的是 Claude 做出你没预料到的事，挡不住「你的飞书账号被别人登录」。账号层面的风险只能靠 deny 规则和急停。

## 文件

```
bridge.py              入口：运行 / --check / --login / --console / --install-task / --restart
setup_secrets.py       把 App Secret 存进凭据管理器 / 钥匙串
config.toml            配置（config.example.toml 有注释）
bridge-settings.json   远程任务额外加的权限规则模板（只加 deny、加 allow，不删你原有的规则）
remote-mode.md         追加给 Claude 的远程模式说明模板（不能弹选择题、回复写给手机看……）
requirements.txt       Python 依赖
fb/app.py              主体：收消息、规则、排队、执行、审批、结果、会话同步
fb/executor.py         拼 claude -p 命令行、填模板、清理环境变量、读 stream-json、超时、Job 对象
fb/broker.py           审批中转（本机 HTTP）
fb/mcp_server.py       审批 MCP（approve、send_file），只用标准库，由 claude 拉起
fb/feishu.py           飞书长连接 + REST
fb/cards.py            卡片模板（JSON 2.0）
fb/policy.py           提取文字、认指令和菜单词（MENU_WORDS）、脱敏、路径校验
fb/store.py            SQLite 任务库
fb/sessions.py         电脑上的会话：读桌面版侧边栏元数据、摘最近几轮对话、在桌面版里打开
fb/osutil.py           Windows / Mac 各一套的系统操作：隐藏控制台或进程组、防双开、开机自启、伪终端
fb/credstore.py        密钥：Windows 凭据管理器 / Mac 钥匙串
data\                  bridge.db、bridge.log、audit.jsonl、runs\（运行后生成）
tests\                 测试，含假 claude（fake_claude.py）
```
