# Architecture

## 当前模块边界

- `bot.py`
  NoneBot 启动入口，只负责初始化驱动和加载插件。
- `plugins/group_verify/__init__.py`
  插件入口，只负责注册事件和生命周期。
- `plugins/group_verify/service.py`
  入群验证核心业务：验证记录、超时任务、验证码渲染、状态汇总。
- `plugins/group_verify/onebot_runtime.py`
  OneBot 客户端发现、二维码扫描、隔离启动。
- `plugins/group_verify/onebot_providers.py`
  OneBot provider 注册表，按 `external / napcat / lagrange` 分离客户端识别与启动逻辑。
- `plugins/group_verify/web_admin.py`
  本地管理台路由层，只负责 HTTP 请求处理。
- `plugins/group_verify/config.py`
  统一解析运行配置，不在业务代码里直接读脚本环境。
- `scripts/lib/runtime.sh`
  Python 运行时抽象，支持 `project` 和 `venv` 两种模式。
- `project_config.py`
  项目级配置源，负责 `config/appsettings.json` 与 NoneBot 环境变量之间的同步。

## 运行原则

- OneBot 客户端视为外部组件，默认不自动安装。
- Python 依赖默认安装到项目内 `.runtime/`，避免污染系统环境。
- OneBot 隔离运行目录固定在 `third_party/onebot/runtime/`，避免复用系统 QQ 数据。

## 下一步建议

- 继续把 `web_admin.py` 的 HTML 渲染拆到独立模块或模板文件。
- 把 `service.py` 中模板管理和状态图渲染继续下沉到独立服务。
- 为 `scripts/` 增加统一的 `common.sh`，把环境读取、日志输出、错误处理抽出来。
