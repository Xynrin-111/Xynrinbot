# Compatibility

## 目标

这个项目要面向纯小白，就不能把机器人业务和某个具体 OneBot 客户端绑死。

更稳的设计应该是：

- 项目只负责验证逻辑、管理台、状态检查。
- OneBot 客户端作为“提供方”接入。
- 根据平台和部署类型给出不同默认建议。

## 推荐矩阵

| 平台 | `desktop` | `server` | 推荐 OneBot 提供方式 |
|---|---|---|---|
| Linux | 支持 | 支持 | `external` 优先，确认图形条件后可选 `napcat` |
| Windows | 支持 | 可运行但不推荐 | `external` |
| macOS | 支持 | 不建议 | `external` |
| Android | 可实验性使用 | 不建议 | `external` |

## 当前原则

- 默认 `APP_DEPLOY_PROFILE=desktop`
- 默认 `APP_PLATFORM=auto`
- 默认 `VERIFY_ONEBOT_PROVIDER=external`
- 项目不再默认自动安装 OneBot 客户端

## 为什么不直接 fork NapCat / LinuxQQ

- 这会把当前仓库变成上游客户端分叉仓库，维护面会远超机器人项目本身。
- 一旦 QQ、NapCat、OneBot 协议再变，你要同时维护上游兼容性和自己的业务逻辑。
- 对纯小白最重要的是“知道当前系统该怎么部署”，不是把所有上游问题都硬塞进这个仓库。

## 更合理的后续路线

1. 建立 `provider` 抽象层，区分 `external` / `napcat` / `lagrange`。
2. 管理台增加“当前平台推荐方案”卡片，而不是只扫目录。
3. 为不同平台提供独立启动器：
   - Linux: `bash`
   - Windows: `PowerShell`
   - macOS: `bash` 或 `.command`
   - Android: `termux` 脚本
4. 把“环境自检”做成统一 doctor 命令，优先给人看懂的建议。
