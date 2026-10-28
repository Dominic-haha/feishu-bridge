# 飞书桥排错

下文的 `PY`：Windows 是 `py`，Mac 是安装目录里的 `.venv/bin/python3`。命令都在安装目录下跑。

先做三件事，大多数问题到这里就清楚了：

1. 跑 `PY bridge.py --check`（逐项打 ✅ / ❌ / ⚠️）。
2. 看 `data/bridge.log` 末尾：有没有 `connected to wss://`，有没有 WARNING / ERROR。Mac 上飞书桥根本起不来时，再看 `data/launchd.err.log`。
3. 看 `data/audit.jsonl` 末尾：用户说「发了」「点了」，这里有没有对应的记录（`task`、`command`、`approval`、`open_on_pc`……）。**没有记录 = 消息根本没到飞书桥**，往通道上找原因；有记录 = 到了，看记录里的状态和报错。

## 现象 → 原因 → 处理（两边通用）

| 现象 | 常见原因 | 处理 |
|---|---|---|
| 发消息完全没反应 | 飞书桥没在跑 | 看状态：Windows `Get-ScheduledTask -TaskName FeishuBridge`，Mac `launchctl print gui/$(id -u)/feishu-bridge`；拉起来：`PY bridge.py --restart`（还没装开机自启就先 `--install-task`） |
| | 电脑睡着了 / 关机了 | 唤醒电脑；积压的消息会先弹确认卡 |
| | 发错了机器人（发到了别的同名或旧机器人） | 让用户确认聊天窗口的机器人名字和 App ID 对得上 |
| | open_id 不在白名单 | 看 `bridge.log` 里「拒绝白名单外的发送人」；把正确的 open_id 写进 `allowed_open_ids`，重启 |
| | 事件没配或没保存长连接 | 后台「事件配置」里要有 `im.message.receive_v1`，订阅方式是长连接 |
| | 应用没发布 / 可用范围里没有这个人 | 「版本管理与发布」里看最新版本是不是「已发布」 |
| 有的消息有反应、有的没有，或者被别的程序回了 | 另一个程序也连着这个应用的长连接（飞书「集群模式」随机分发消息），比如应用原本给某个智能体/机器人服务用过 | 停掉那个程序，或者给飞书桥新建一个专用应用 |
| 审批卡按钮点了没反应 / 报错 | 「回调配置」里没加 `card.action.trigger`，或订阅方式不是长连接 | 补上后不用重启飞书桥 |
| 底部菜单不显示 | 应用是妙搭 / Aily /「创建飞书智能体应用」一键建的：菜单归平台管，后台配置不生效（底部常有一条「OpenClaw 驱动的智能助手 · 已关机，去唤醒」之类的状态栏） | 新建一个普通的企业自建应用给飞书桥用（见 feishu-console.md） |
| | 改了菜单没发布版本，或发布不到 5 分钟 | 发布版本；手机上退出聊天再进来 |
| | 菜单状态是「关闭」 | 后台打开 |
| | 飞书客户端低于 7.22 | 升级飞书 |
| 点菜单后被当成普通任务交给 Claude 了 | 菜单名和 `fb/policy.py` 的 `MENU_WORDS` 对不上 | 把后台菜单名改成表里的写法，或同步改 `MENU_WORDS` |
| 任务失败，提示「Claude 登录失效」，或 `--check` 显示「没登录」 | claude.ai 登录凭证被注销（访问令牌过期后续期被拒时，claude 会把 `~/.claude/.credentials.json` 里的令牌清空） | 用户在终端跑 `PY bridge.py --login` 重新登录；不用重启飞书桥 |
| 「电脑上打开」回「在电脑上正开着」 | 那个会话正在桌面版或终端里运行，claude 不让再开一份 | 直接到电脑上看；或者先「切换会话」选别的会话 |
| 「电脑上打开」回「claude --desktop 退出码 1」 | 多半是没登录（`--desktop` 要求 claude.ai 账号登录），或 claude 版本太旧（需要 2.1.285+） | `--check` 看登录；想看原话：在终端里跑 `claude --desktop --resume <会话ID>` |
| 手机上新开的会话没出现在电脑桌面版里 | 没开自动同步 | `config.toml` 的 `[policy] auto_open_new_sessions = true`，再 `PY bridge.py --restart`；或者每次手动点「会话 ▸ 电脑上打开」 |
| | 只点了「新会话」，还没发消息 | 「新会话」只是退出接着聊，本身不建会话；发下一条消息、跑完之后才有（`audit.jsonl` 里只有 `detach`、没有 `task` 就是这种情况，10-27 Mac 验收碰到） |
| 接着一个会话聊，结果特别慢、特别贵 | 会话记录很大，每次都带上全部上下文 | 记录超过 3 MB 会先弹确认卡；能新开会话就新开 |
| 手机发的图片 Claude 说打不开 / 下载失败 | 少了 `im:message:readonly` 权限 | 后台开通后发布新版本 |
| `--check` 说找不到 claude（Windows） | 没装 Claude 桌面版，或 Claude Code 是用 npm 装的（那是 claude.cmd） | 装桌面版；或在 `config.toml` 的 `[claude] path` 写 claude.exe 的完整路径 |
| `install.py` 装依赖时超时 | 公司网络访问 pypi.org 慢 | 加 `--pip-index https://mirrors.aliyun.com/pypi/simple/` 重跑 |
| 长连接「验证」一直失败 | 飞书桥没连上（密钥不对、没跑起来） | `--check` 看 App Secret 和飞书两项；`bridge.log` 里找报错 |
| 「删除应用」按钮是灰的 | 已启用的应用要管理员先停用 | 找企业管理员在管理后台停用后再删 |

## Mac 专有

Mac 支持是后加的，下面是已知会碰到的情况。遇到表里没有的，把 `--check` 输出、`data/bridge.log` 和 `data/launchd.err.log` 末尾记下来反馈。

| 现象 | 常见原因 | 处理 |
|---|---|---|
| 任务卡住不动，或读写文件报「Operation not permitted」 | 要读写的目录在「文稿 / 桌面 / 下载」里（工作目录，或手机上接着聊的会话所在目录），这三个是 macOS 受保护位置（工作目录在里面时 `--check` 有一条 ⚠️）。飞书桥第一次读写时电脑上会弹授权框，申请的是飞书桥用的 Python，不是 claude；没人点就停在那里等，点了「不允许」就报错。10-27 Mac mini 实测：这个 Python 早先被点过允许，接着聊 `~/Desktop` 里的会话直接能读、没再弹 | 到电脑前点「允许」，点过一次就记住（升级或换 Python 后会再问）；点过「不允许」的，到「系统设置 → 隐私与安全性 → 文件与文件夹」里把这个 Python 对应的文件夹打开，或在「完全磁盘访问权限」里加上它；常不在电脑前，就把工作目录换到 `~/work` 这类普通文件夹（改 `config.toml` 的 `workdir`，再 `--restart`） |
| 弹窗「python3 想要使用钥匙串中的……」 | 飞书桥读钥匙串里的 App Secret；换过 Python 版本后会再问 | 输入 Mac 登录密码，点「始终允许」 |
| `setup_secrets.py` 报 keyring 相关错误 | 依赖没装进 `.venv`，或用了全局 python3 跑 | 用安装目录里的 `.venv/bin/python3` 跑；必要时重跑 `install.py` |
| 飞书桥起不来，`bridge.log` 里没有新的启动记录 | 开机代理没装上，或启动就崩了 | `launchctl print gui/$(id -u)/feishu-bridge` 看状态；看 `data/launchd.err.log`；重装：`PY bridge.py --install-task` |
| 连飞书报 `CERTIFICATE_VERIFY_FAILED` | python.org 装的 Python 没有根证书 | 飞书桥启动时会自动改用 certifi 的证书；还不行就运行 Python 安装目录里的「Install Certificates.command」 |
| `--check` 找不到 claude | 桌面版自带的 claude 在 Mac 上包在 .app 里（`~/Library/Application Support/Claude/claude-code/<版本>/<hash>/claude.app/Contents/MacOS/claude`，10-27 实测），老版本的飞书桥按 Windows 的结构找，找不到；launchd 起的进程 PATH 也很短 | 升级飞书桥（重跑 `install.py`）；或终端里 `which claude` 找到路径，写进 `config.toml` 的 `[claude] path`；或装原生版 claude（在 `~/.local/bin`，飞书桥会自动找） |
| `install.py` 报「需要 Python 3.11 以上」 | Mac 上 `python3` 指的是系统自带的 3.9 | `ls /opt/homebrew/bin/python3.1*` 看有没有 Homebrew 装的新版本，有就用完整路径跑：`/opt/homebrew/bin/python3.12 install.py …`（10-27 Mac mini 就是这样）；没有再 `brew install python@3.12` |
| 手机上接着聊电脑上的会话，没弹「在电脑上正开着」，结果两边记录分叉 | 桌面版**新开的**会话，进程命令行里没有会话 ID；老版本只看命令行，3 分钟没动静就认不出来 | 升级飞书桥：10-27 起改为先看 `~/.claude/sessions/<pid>.json` 的登记（每个开着的会话都有），再看命令行 |
| 任务里找不到 git、brew 等命令 | 开机代理的 PATH 是装的时候从你的 shell 抄过去的，之后新装的工具不在里面 | 重跑 `PY bridge.py --install-task`，会重新抄一遍 PATH |
| 「电脑上打开」失败 | Mac 上会把 claude 的原话转给你（不只是退出码），按原话处理：没登录就 `--login`，会话正开着就到电脑上看 | — |
| 合上盖子后收不到消息 | Mac 合盖就睡眠 | 用的时候别合盖；或接电源 + 外接显示器（合盖模式） |
| 放着不动一阵子就收不到消息 | 电脑自动睡眠了 | 飞书桥自带防睡眠（`[policy] keep_awake`，默认开，只在接电源时有效）：`pgrep -fl "caffeinate -s -w"` 应该有一个、盯着飞书桥的进程号；没有就看 `config.toml` 里是不是关了，改完 `--restart` |
| `data/launchd.err.log` 越来越大 | 老版本把普通日志在这里又写了一遍，这个文件不轮转 | 升级飞书桥：10-27 起这里只写警告和报错，完整日志看 `data/bridge.log`；旧文件可以直接删 |
| `pip install` 报 externally-managed-environment | 用了 Homebrew 的 Python 往全局装 | 别手动全局装，用 `install.py`（它会建 `.venv`） |

想确认 macOS 有没有拦、拦的是谁：`/usr/bin/log show --last 10m --predicate 'process == "tccd"' | grep -A4 SystemPolicyDesktopFolder`（文稿、下载分别换成 `DocumentsFolder`、`DownloadsFolder`）。`AUTHREQ_RESULT` 的 `authValue=2` 是放行、`0` 是拦了；`AUTHREQ_ATTRIBUTION` 的 `responsible_path` 是被申请授权的程序。zsh 里 `log` 是内置命令，要写全路径 `/usr/bin/log`。

## 想看 Claude 具体干了什么

`data/runs/<任务号>/events.jsonl` 是那次任务的完整事件流（调了哪些工具、说了什么），`stderr.log` 是 claude 的报错输出。任务号在状态卡和结果卡上都有。

## 不连飞书先演练

```
PY bridge.py --console --fake-claude
```

用假的 claude 跑一遍流程，不花额度。卡片打印在终端里，键盘输入就当是在飞书里发的消息（`>om_3 文字` 表示引用回复，`!ap <审批ID> allow` 表示点审批按钮）。去掉 `--fake-claude` 就是真 claude。控制台模式的数据放在 `data-console`，和正式运行分开。
