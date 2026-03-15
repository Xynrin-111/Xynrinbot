# NoneBot Group Verify Bot

基于 `NoneBot2 + OneBot v11 + SQLAlchemy Async + SQLite + Playwright` 的 QQ 群新人入群验证机器人。

目标是把“新人进群发验证码、超时或输错自动踢出、网页可视化管理”这套流程做成一个可直接部署的完整项目，而不是只给插件片段。

当前项目默认把 OneBot 客户端视为外部组件，以降低 `NapCat / LinuxQQ / OneBot` 上游更新对本项目的直接冲击。跨平台策略见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。

## 功能特性

- 新人入群后立即发送验证码图片和提示文案。
- 支持按群启用，支持多个目标群。
- 超级管理员自动跳过验证。
- 连续输错达到上限自动踢出。
- 超时未验证自动踢出。
- 重复入群会重置验证码和状态，旧验证码立刻失效。
- 机器人重启后自动恢复未过期的待验证任务。
- 提供本地网页管理台，可直接配置目标群、超级管理员、模板和 OneBot 客户端。
- 管理台已拆成概览 / 配置 / 模板库 / 系统 / 日志多入口，而不是单页堆叠。
- 支持 SMTP 配置与测试邮件发送。
- 支持统一代理配置，`pip`、Playwright 和 OneBot 下载链路可共享同一套代理环境变量。
- 支持服务状态图，显示 CPU / 内存 / 磁盘 / GPU / 进程信息。
- 验证记录已独立为单独命令，不再挤占状态图版面。
- 验证码模板支持模板库多版本保存与切换，不再局限于单个自定义覆盖文件。

## 适合什么环境

可以部署在服务器上，但有几个前提：

- 推荐 `Linux` 服务器。
- 需要 `Python 3.10+`。
- 需要能运行 `Playwright Chromium`。
- 如果你要在服务器上直接跑 `NapCat / LinuxQQ`，服务器最好具备图形环境或至少能提供 `Xvfb` 之类的运行条件。
- 机器人账号必须已经能在 OneBot 客户端里正常登录 QQ。

更直白一点：

- 纯 NoneBot 服务端逻辑可以跑在服务器。
- QQ 客户端这一层能不能稳定跑，取决于你的 OneBot 客户端方案。
- 本项目默认把 OneBot 客户端视为外部组件，不再强制绑定 `NapCat`。
- 如果你确认当前机器适合跑 `NapCat / LinuxQQ`，仍然可以显式启用项目内安装脚本。

## 已带脚本

项目里已经有安装/启动脚本，不需要你自己再写：

- `install.sh`
  面向在线安装。支持交互式选择 `desktop / server` 模式，并可在安装时直接写入 WebUI 地址、端口、管理台访问范围和 OneBot 安装策略。
- `scripts/run.sh`
  推荐入口。会自动补齐 Python 依赖、Playwright、`config/appsettings.json`，默认使用项目本地依赖目录 `.runtime/`，且默认不自动安装 OneBot 客户端。
- `scripts/bootstrap.sh`
  只做初始化，不启动服务。
- `scripts/start.sh`
  只启动，不执行初始化。
- `scripts/install_onebot.sh`
  安装 OneBot 客户端。默认跳过；如果显式设置 `ONEBOT_CLIENT=napcat`，才会安装 `NapCat`。
- `scripts/update.sh`
  在线更新当前项目代码，默认保留 `config/appsettings.json`、`.env`、`.venv`、`.runtime/`、`data/`、`third_party/` 和 `.install-meta`，适合后续重复升级。

## 快速开始

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd nonebot-group-verify-bot
```

### 2. 一键初始化

```bash
bash install.sh --local-bootstrap
```

如果当前终端可交互，脚本会先询问：

- 安装模式：`desktop` 或 `server`
- WebUI 监听地址
- WebUI 端口
- 管理台是否仅允许本机访问
- 启动后是否自动打开管理台
- 是否自动安装 OneBot 客户端

这一步会自动：

- 创建项目本地依赖目录 `.runtime/`（可用 `PYTHON_RUNTIME_MODE=venv` 切回 `.venv`）
- 安装项目依赖
- 安装 Playwright Chromium
- 初始化 `config/appsettings.json`
- 按你的选择安装 OneBot 客户端或跳过

如果你不想交互，也可以直接用环境变量：

```bash
INSTALL_PROFILE=server \
APP_HOST=127.0.0.1 \
APP_PORT=8080 \
ADMIN_LOCAL_ONLY=true \
AUTO_OPEN_ADMIN_UI=false \
INSTALL_ONEBOT_CLIENT=none \
INTERACTIVE_INSTALL=0 \
bash install.sh --local-bootstrap
```

如果你已经有自己的 OneBot 客户端，不想自动安装：

```bash
ONEBOT_CLIENT=none bash scripts/run.sh --bootstrap-only
```

如果你更想保留旧的 `.venv` 方式：

```bash
PYTHON_RUNTIME_MODE=venv bash scripts/run.sh --bootstrap-only
```

### 3. 启动机器人

```bash
bash scripts/run.sh
```

启动后访问：

- 首次引导页：`http://<HOST>:<PORT>/admin/setup`
- 管理台：`http://<HOST>:<PORT>/admin`

## 在线安装

如果你把这个项目直接作为独立仓库发布到 GitHub，可以直接下载仓库根目录的 `install.sh`。

默认行为：

- 从 GitHub 下载整个项目仓库
- 复制到本地目标目录
- 自动进入交互式安装配置
- 自动执行初始化
- 不自动启动机器人

示例：

```bash
curl -fsSL https://raw.githubusercontent.com/Xynrin-111/Xynrinbot/main/install.sh | bash
```

也可以自定义：

```bash
REPO_REF=main \
INSTALL_DIR=$HOME/Xynrinbot \
curl -fsSL https://raw.githubusercontent.com/Xynrin-111/Xynrinbot/main/install.sh | bash
```

如果你要下载后直接启动：

```bash
AUTO_START=1 curl -fsSL https://raw.githubusercontent.com/Xynrin-111/Xynrinbot/main/install.sh | bash
```

如果你想跳过交互并直接指定参数：

```bash
INSTALL_PROFILE=server \
APP_HOST=127.0.0.1 \
APP_PORT=8080 \
ADMIN_LOCAL_ONLY=true \
AUTO_OPEN_ADMIN_UI=false \
INSTALL_ONEBOT_CLIENT=none \
INTERACTIVE_INSTALL=0 \
curl -fsSL https://raw.githubusercontent.com/Xynrin-111/Xynrinbot/main/install.sh | bash
```

说明：

- `desktop` 模式默认本机监听 `127.0.0.1:8080`，自动打开管理台，默认不自动安装 OneBot 客户端
- `server` 模式默认本机监听 `127.0.0.1:8080`，不自动打开管理台，并默认跳过 OneBot 安装
- 如果你把 `APP_HOST` 设为 `0.0.0.0` 且 `ADMIN_LOCAL_ONLY=false`，管理台会直接暴露到网络，请自行做好安全控制

## 更新项目

项目目录内新增了更新脚本：

```bash
bash scripts/update.sh
```

默认会：

- 按 `.install-meta` 记录的仓库、分支和子目录重新下载项目
- 替换项目代码
- 保留 `.env`
- 保留 `.venv`
- 保留 `.runtime`
- 保留 `data/`
- 保留 `third_party/`
- 重新同步 Python 依赖

如果你需要临时改更新来源：

```bash
REPO_REF=main bash scripts/update.sh
```

## 服务器部署建议

### 方案 A：整套都部署在服务器

适合你已经确认服务器能稳定运行 `NapCat / LinuxQQ`。

步骤：

1. 上传项目到服务器。
2. 执行 `bash scripts/run.sh --bootstrap-only`。
3. 执行 `bash scripts/run.sh`。
4. 打开管理台完成 OneBot 客户端登录和基础配置。

注意：

- 优先使用 SSH 隧道或内网 ACL 访问管理台；`VERIFY_ADMIN_LOCAL_ONLY=true` 时，管理台只允许本机直连，不支持经反代访问。
- 如果你确实要设置 `VERIFY_ADMIN_LOCAL_ONLY=false`，必须同时配置 `VERIFY_ADMIN_USERNAME` 和 `VERIFY_ADMIN_PASSWORD`。
- 机器人必须是目标群管理员，否则无法踢人。

### 方案 B：服务器跑 NoneBot，另一台机器跑 OneBot 客户端

这也是可行的，但你需要自己保证：

- OneBot 反向 WebSocket 能连到本项目；
- `HOST`、`PORT`、`ONEBOT_ACCESS_TOKEN` 双方一致；
- 网络连通和安全策略你自己处理好。

如果你准备上传 GitHub，建议在仓库说明里优先推荐方案 A 或写清楚“默认以本机/同机部署为主”。

## 环境要求

- Python `3.10` 到 `3.12` 推荐
- Linux 推荐
- `python3-venv`
- `curl` 或 `wget`
- Playwright 所需系统依赖

如果是 Debian / Ubuntu，至少先确保：

```bash
sudo apt update
sudo apt install -y python3 python3-venv curl
```

## 手动安装

项目当前以 `config/appsettings.json` 为主配置源，`.env` 仅作为兼容导出文件存在。

其中新增了两组常用配置：

- `smtp`
  用于管理台测试发信与后续邮件能力接入。
- `proxy`
  用于统一注入 `HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY`，让脚本安装与下载链路保持一致。

如果你不想用脚本，可以手动部署。

### 1. 创建项目本地依赖目录

```bash
mkdir -p .runtime/site-packages .runtime/playwright
```

### 2. 安装依赖

```bash
python3 -m pip install -U --target .runtime/site-packages .
```

### 3. 安装 Playwright 浏览器

```bash
PYTHONPATH=.runtime/site-packages PLAYWRIGHT_BROWSERS_PATH=.runtime/playwright \
python3 -m playwright install chromium
```

如果服务器缺系统依赖：

```bash
PYTHONPATH=.runtime/site-packages PLAYWRIGHT_BROWSERS_PATH=.runtime/playwright \
python3 -m playwright install-deps chromium
```

### 4. 初始化项目配置

```bash
python3 scripts/projectctl.py init
```

### 5. 启动

```bash
bash scripts/run.sh --start-only
```

## 关键配置

最重要的配置项通常只有这些：

```json
{
  "app": {
    "host": "127.0.0.1",
    "port": 8080
  },
  "admin": {
    "local_only": true,
    "username": "admin",
    "password": "请改成强密码"
  },
  "onebot": {
    "access_token": ""
  },
  "verify": {
    "superusers": [123456789],
    "target_groups": [123456789, 987654321],
    "timeout_minutes": 5,
    "max_error_times": 3,
    "playwright_browser": "chromium"
  }
}
```

说明：

- `verify.superusers` 是超级管理员 QQ 列表。
- `verify.target_groups` 是启用验证的群号列表。
- `onebot.access_token` 如果 OneBot 端配置了，这里必须一致。
- `admin.local_only=true` 时，管理台只允许本机直连访问。
- 如果 `admin.local_only=false`，必须配置 `admin.username` 和 `admin.password`，并且建议再加内网 ACL 或 SSH 隧道。

## OneBot 配置示例

最常见的是反向 WebSocket：

```yaml
OneBot:
  Implementations:
    - Type: ReverseWebSocket
      Host: 127.0.0.1
      Port: 8080
      Suffix: /onebot/v11/ws
      AccessToken: ""
```

要求：

- `Host` / `Port` 要和 `config/appsettings.json` 中的 `app.host` / `app.port` 一致。
- 如果有 `AccessToken`，必须与 `ONEBOT_ACCESS_TOKEN` 一致。
- 机器人账号必须已经登录成功并加入目标群。
- 机器人必须有群管理权限。

## 管理台

管理台主要负责：

- 首次启动向导
- 目标群和超级管理员配置
- OneBot 客户端检测与启动
- 二维码识别
- 验证码模板切换
- 验证提示文案编辑
- 查看最近验证记录
- 重启机器人

默认地址：

- `http://127.0.0.1:8080/admin/setup`
- `http://127.0.0.1:8080/admin`

## 超级管理员命令

支持 `@机器人`，也支持大多数纯文本命令。

```text
@机器人
@机器人 帮助
@机器人 服务状态
@机器人 验证记录
@机器人 验证记录 15
@机器人 列表
@机器人 状态 123456789
@机器人 开启 123456789
@机器人 关闭 123456789
@机器人 设置超时 123456789 8
@机器人 设置次数 123456789 5

服务状态
验证记录
验证记录 15
列表
状态 123456789
开启 123456789
关闭 123456789
设置超时 123456789 8
设置次数 123456789 5
```

补充：

- `帮助` 只支持 `@机器人` 触发。
- `验证记录` 默认显示最近 `10` 条，可指定 `1-20` 条。
- `设置超时` 范围是 `1-120` 分钟。
- `设置次数` 范围是 `1-10` 次。

## 项目结构

```text
nonebot-group-verify-bot/
├── bot.py
├── pyproject.toml
├── README.md
├── scripts/
│   ├── run.sh
│   ├── bootstrap.sh
│   ├── start.sh
│   ├── install_onebot.sh
│   └── check_env.py
└── plugins/
    └── group_verify/
        ├── __init__.py
        ├── config.py
        ├── db.py
        ├── models.py
        ├── service.py
        ├── web_admin.py
        └── templates/
```

## 常见问题

### 1. 能不能部署到服务器？

能。

但要分两层看：

- 机器人业务服务可以部署在服务器。
- QQ / OneBot 客户端能否稳定运行，要看服务器环境和你的客户端方案。

如果你用的是 Linux 服务器，并且能正常跑 `NapCat`，这个项目就能直接部署。

### 2. 有安装脚本吗？

有，直接用：

```bash
bash scripts/run.sh --bootstrap-only
```

或者一步到位：

```bash
bash scripts/run.sh
```

### 3. 为什么验证码图片发不出来？

大概率是 Playwright 或 Chromium 依赖不完整。

先执行：

```bash
bash scripts/run.sh --bootstrap-only
```

### 4. 为什么到时间了没踢人？

常见原因：

- 机器人不是群管理员；
- 被处理用户权限高于机器人；
- OneBot 已离线。

### 5. 为什么命令无效？

常见原因：

- 发命令的人不在 `verify.superusers`；
- 群号不在 `verify.target_groups`；
- 修改 `config/appsettings.json` 后没重启；
- 纯文本发送了 `帮助`，但这个命令只支持 `@机器人`。

## 开源发布建议

如果你准备上传 GitHub，建议至少补这几项：

- 增加仓库截图：管理台首页、状态图、验证码图各一张。
- 增加 `LICENSE`。
- 把 `config/appsettings.json.example` 保持为可直接复制的最小示例。
- 在 Releases 或 README 里说明当前默认支持的 OneBot 接入方式是 `external`。
- 如果后续支持 Docker，再单独补一个 `Docker 部署` 章节。

另外，发布到 GitHub 时不要带上本地运行痕迹：

- `.env`
- `data/`
- `third_party/`
- `build/`
- `*.egg-info/`

## 技术栈

- NoneBot2
- OneBot v11
- SQLAlchemy Async
- SQLite
- Playwright
- psutil

## 参考项目

- NoneBot2: https://github.com/nonebot/nonebot2
- OneBot: https://github.com/botuniverse/onebot
- NoneBot OneBot Adapter: https://github.com/nonebot/adapter-onebot
- Playwright Python: https://playwright.dev/python/

## 免责声明

**⚠️ 使用本项目即表示您已阅读并同意以下条款：**

1. **仅供学习交流**：本项目仅供个人学习研究交流使用，禁止用于任何商业目的。

2. **无任何担保**：本项目按"原样"提供，不提供任何明示或暗示的保证，包括但不限于：
   - 稳定性保证
   - 安全性保证
   - 适用性保证

3. **使用风险自担**：您使用本项目所产生的一切风险（包括但不限于）：
   - QQ 账号被封禁
   - 群聊功能异常
   - 数据丢失
   - 其他任何损失

   **均由您自行承担，开发者不承担任何责任。**

4. **账号安全**：
   - 请使用小号进行测试
   - 建议开启 QQ 设备锁
   - 勿在生产环境直接使用未经验证的配置

5. **技术支持**：本项目不提供任何形式的技术支持文档或保证，开发者有权随时停止维护。

6. **遵守平台规则**：使用本项目时，请确保遵守腾讯服务条款及相关法律法规，因违规使用导致的任何后果由您自行负责。

**如果您不同意以上条款，请立即停止使用本项目。**
