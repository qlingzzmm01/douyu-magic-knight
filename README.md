# 魔法骑士 · 自动挂机助手

斗鱼直播间 H5 小游戏「魔法骑士」的自动化挂机工具。Windows 桌面 GUI + 免安装单文件 exe。

> 仅供学习 Playwright / Cocos 运行时分析使用。使用本工具可能违反平台用户协议，账号风险自负。

## 功能

- **图形界面**：房间号 / 局数输入、无敌模式开关、实时状态条（剩余时间·击杀·金币·血量·无敌）、彩色日志
- **无敌模式**：调用游戏内 `player.setInvincible()` 维持无敌状态，真实免伤（不改血量，血条数值正常）；开启后只主动找精英/Boss，其余时间专心吃经验
- **自动选卡**：升级三选一自动评分。MAX 卡（选后技能满 6 级）无条件最高优先；已满级卡判为浪费；集中升级优于平均升级
- **自动弹窗处理**：复活弹窗不花骑士币、结算弹窗自动领取
- **多局循环**：打满指定局数自动停，免费次数用尽自动退出

## 快速开始（免安装）

1. 到 [Releases](../../releases) 下载 `魔法骑士挂机助手.exe`
2. 双击运行 → 点「登录斗鱼」登录账号（登录态保存在本机）
3. 点「无敌模式」→ 填房间号（默认 `2561707`）→ 点「开始挂机」

**运行要求**：Windows 10/11 x64 + Google Chrome 或 Microsoft Edge（程序自动查找）。无需安装 Python。

## 命令行模式

```
魔法骑士挂机助手.exe --login                    # 打开浏览器手动登录
魔法骑士挂机助手.exe --run --god --rounds 1     # 跑 1 局，无敌模式
魔法骑士挂机助手.exe --run --rid 2561707        # 普通躲避流
魔法骑士挂机助手.exe --selftest                 # 自检（driver / Chrome 可用性）
```

窗口程序没有控制台，日志写入 exe 同级目录的 `run.log`。

## 从源码运行

```bash
pip install playwright
python -m playwright install   # 仅首次；本工具用系统 Chrome，也可不装浏览器
python knight_gui.py           # 图形界面
python bot.py --god --rounds 3 # 命令行
```

需要系统 Python 3.11+（打包用的 GUI 依赖 tkinter，需完整版 Python）。

## 打包

```bat
打包exe.bat
```

产物：`魔法骑士自动挂机\魔法骑士挂机助手.exe`。需要完整版 Python（自带 tkinter），PyInstaller 会自动装。

Playwright 官方没有 PyInstaller hook，`knight_gui.spec` 里手动把 `site-packages/playwright/driver`（node.exe + cli.js，约 102MB）打进了包，`compute_driver_executable()` 才能命中。

## 项目结构

```
bot.py                    核心：Playwright 驱动 + 注入 JS（走位 / 无敌）
knight_gui.py             桌面 GUI + CLI 入口
knight_gui.spec           PyInstaller 打包配置
打包exe.bat               一键打包
启动桌面助手.bat           优先跑 exe，否则跑源码
```

## 原理简述

- 直接打开游戏面板页 `douyu.com/pages/vibe-lab-act202608-game/home?rid=<房间号>`，不依赖直播间入口
- 注入 JS 读取 Cocos 运行时对象（`fight` / `player` / `enemy` / `dropProp`），直接写 `fight.keyboardInput.dir` 控制走位，不模拟键盘
- 无敌来自逆向出的原型方法：`setInvincible(t)` → `state.invincibleUntil = max(...)`，而 `setHP()` 在减血分支首行判定 `isInvincible()` 直接返回 0
- 升级卡等级状态靠卡片节点判定，**不是**靠名字：徽标节点为 `new` / `max`，星星节点为 `star0..starN`

## 数据目录

默认存在 exe 同级：`chrome_profile/`（登录态）、`logs/`（每局战报 jsonl + 异常截图）。
exe 所在目录不可写（如装在 `Program Files`）时自动回退到 `%LOCALAPPDATA%\MagicKnight`。

## 免责声明

本项目仅用于技术学习。游戏每 10 秒向服务器上报战况，使用自动化/无敌会产生异常数据，可能导致成绩无效或账号风控。请自行评估风险，勿用于商业用途。
