# Codex Reset Notification

中文 Windows 桌面重置公告监测与通知工具，版本 1.0.0。独立社区项目，不隶属于 OpenAI。数据来自 [codex-reset.com](https://codex-reset.com/)，公开公告不等于个人额度或本人到账状态，无需登录 OpenAI 或 X。

## 下载与启动

在 [GitHub Releases](https://github.com/soranatsu/Codex-Reset-Notification/releases/latest) 下载 `Codex-Reset-Notification-1.0.0-Windows-x64.zip`，完整解压到有写入权限的目录，双击 `启动.cmd` 或 `Windows/CodexResetNotification.exe`。无需预装 Python，也无需管理员权限；请保留旁边的 `_internal` 文件夹。SHA-256 文件可用于核对下载完整性。

目标为 Windows 10/11 x64。已在 Windows 11 x64 验证，并在移除 Python 路径、全新数据目录及含中文和空格的解压目录下验证冻结 EXE。尚未在第二台物理电脑、Windows 10 或 ARM64 原生环境测试；EXE 未签名。

打开程序默认停止监测。在中文界面配置通知、时区及接口联系信息，保存后点击“开始监测”。先用“模拟通知测试”，真实通知测试会再次显示目的地址并要求确认。详见 [中文用户指南](docs/中文用户指南.md)。

## 功能

- SMTP 账户、服务器、端口、STARTTLS／SSL/TLS、发件地址及多个收件地址；可选 Bark。
- 默认 300 秒监测，最低 60 秒，遵守 `Retry-After`。首次成功检查只建立基线，不发送历史公告。
- 区分全局重置、预告、可留存重置卡和到账观察；原始帖子 ID 与生命周期去重。
- 可编辑时区和每日 12:00、18:00 提醒；有明确公开完成证据后停止该批提醒。
- SQLite 持久状态和发送队列，退避重试、断线补查、来源过期提示、状态与安全日志。
- “服务器已接受”与“送达未确认”分别显示；确认丢失时不自动重发。
- 用户单独授权后才保存到 Windows 凭据管理器；否则仅在当前进程保留秘密。仅提供用户主动选择的开机启动入口。

凭据由使用者在本机输入，发布包不含任何真实邮箱、密码或密钥。配置与队列可能含用户邮箱，位于 `%LOCALAPPDATA%\ResetNotice`；不要上传运行数据。内部数据目录和启动项名称保留 `ResetNotice`，以兼容本地早期版本。

电脑关机、睡眠、离线或程序关闭时无法保证监测。第三方账户到账不证明本人到账；Bark 不能强制突破 iOS 静音或专注模式。本项目发布不会改变既有云端提醒。

## 源码、构建与验证

源码使用 Python 标准库和 `tzdata`。安装 `requirements.txt` 后运行 `python app.py`，`python app.py --demo` 可离线演示。推荐 Python 3.14 x64；本次测试为 3.14.7。

`python scripts/check.py` 执行测试，`python scripts/build_release.py` 或 `build.ps1` 重建便携 ZIP。锁定依赖和重复构建步骤见 [构建说明](docs/构建说明.md)。构建包含测试、冻结 GUI 检查、依赖检查，以及源码与二进制中的用户路径／秘密／运行数据检查。

测试采用虚构地址和本地 mock／模拟 SMTP、HTTP。真实收件箱、Bark 手机显示、原生系统凭据读写及开机启动需由使用者自行选择验证。已验证结果随 ZIP 提供，其他限制见 [v1.0.0 发布说明](docs/发布说明-v1.0.0.md)。

## 来源与许可

遵守 [公开 API 文档](https://codex-reset.com/developers)：可识别的 `CodexResetNotification/1.0` User-Agent、使用者填写的联系信息、来源署名、至少一分钟轮询；仅 GET `/api/feed` 与 `/api/timeline`。不使用收费 X API，不读取个人额度。

项目开源许可证尚待维护者决定，详见 [LICENSING.md](LICENSING.md)。第三方组件各自的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与发布包的 `licenses` 目录。
