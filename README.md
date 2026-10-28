# 飞书桥：手机飞书遥控电脑上的 Claude Code

装好以后：你在手机飞书里私聊**你自己的**机器人发一句话，你电脑上的 Claude Code 就去干活；要做写文件、跑命令这类事时，手机上弹审批卡给你点；干完把结果发回飞书。手机上还能列出、接着电脑上的 Claude 会话，让电脑上的 Claude 桌面版打开某个会话。

不用服务器，也不用公网 IP：是你的电脑主动连飞书（长连接）。

## 需要什么

- 一台 **Windows 10/11 或 Mac** 电脑，装了 **Claude 桌面版**，能用 claude.ai 账号登录。
- **Python 3.11 或更新**（没有的话 Claude 会带你装；Mac 自带的 python3 一般太旧）。
- 能在 [飞书开发者后台](https://open.feishu.cn/app) 建「企业自建应用」。
- 电脑要开着、别睡着，才能收到手机上的消息。

Mac 支持是后加的，Windows 上用得比较久。

## 怎么装

这个 skill 要在你电脑上跑脚本、开终端，所以要在 **Claude 桌面版的 Code**（Claude Code）里用，网页版聊天里用不了。

**方法一（最省事）**：在 Claude Code 里说「把 https://github.com/Dominic-haha/feishu-bridge 装成 skill，再带我装飞书桥」。

**方法二（自己放）**：

1. 把这个仓库放进 skills 文件夹，放好后应该有 `…/skills/feishu-bridge/SKILL.md`：
   - Mac：`git clone https://github.com/Dominic-haha/feishu-bridge ~/.claude/skills/feishu-bridge`
   - Windows（PowerShell）：`git clone https://github.com/Dominic-haha/feishu-bridge "$env:USERPROFILE\.claude\skills\feishu-bridge"`
   - 没装 git：点本页的 **Code → Download ZIP**，解压出来的文件夹叫 `feishu-bridge-main`，改名成 `feishu-bridge` 再放进 skills 文件夹。skills 文件夹在 Windows 是 `%USERPROFILE%\.claude\skills\`（资源管理器地址栏输入它回车），在 Mac 是 `~/.claude/skills/`（访达里按 Shift+Command+G 输入这个路径）；`.claude` 是隐藏文件夹，没有就新建。
2. 在 Claude 桌面版的 Code 里**新开一个会话**，说一句「**帮我装飞书桥**」。

Claude 会一步步带你做完，全程大约 20～30 分钟。有几步必须你本人来：
1. 在飞书后台复制 App Secret，粘进终端。
2. 登录 Claude。
3. 在手机上给机器人发一条消息，完成配对。

装的过程中遇到问题，直接把现象告诉 Claude，skill 里带了排查表（`references/troubleshooting.md`）。

**更新**：用 git 装的，在 skill 文件夹里运行 `git pull`；用 ZIP 装的，重新下载替换。然后跟 Claude 说「升级飞书桥」。

## 注意

- 机器人要**新建一个普通的企业自建应用**，别拿妙搭、Aily 或「创建飞书智能体应用」建的机器人来用，那种的底部菜单不生效。
- 应用的可用范围只放你自己。App Secret 别发到任何聊天里。
- Mac 上工作目录别放在「文稿」「桌面」「下载」里：第一次读写这些文件夹时，电脑上会弹授权框，人不在电脑前就没人点，任务会卡住。
- 手机上每派一个任务，都算你自己的 Claude 用量。
