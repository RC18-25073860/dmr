# DMR 量产文件管理平台

这是可重新安装的源码工程，包含本地平台、前端、批量上传和版本归档、邮件附件接入脚本、飞书清单读取、192 NAS 同步程序、数据库迁移与自动化测试。仓库不包含本机数据库、邮件、真实附件、账号、令牌、构建产物或 192 共享文件。

## 在另一台电脑运行

支持 macOS 或 Linux；Windows 请使用 WSL2 中的 Linux 环境。当前后端和邮件脚本使用 Unix `fcntl` 文件锁，不能直接用原生 Windows Python 运行。需要 Python 3.10+（推荐 3.12）、Node.js 20.19+ 或 22.12+、npm，以及联网安装依赖的权限。如果系统 `python3` 低于 3.10，使用 `DMR_PYTHON_BIN=python3.12 bash scripts/setup.sh`。

```bash
# 在仓库根目录
bash scripts/setup.sh
./启动本地DMR.command  # macOS；Linux 可改用下一行
# source scripts/env.sh && .venv/bin/python server.py --port 8920
```

若启动脚本没有执行权限，可运行 `bash 启动本地DMR.command`。浏览器打开 `http://127.0.0.1:8920/`。新机器的空数据库在页面首次创建管理员；旧电脑的数据不会随源码自动转移。正式迁移需另行备份、恢复数据库和附件，并单独核对权限。

手动安装等价于创建 `.venv`，安装 `requirements.lock.txt`，在 `frontend/` 执行 `npm ci && npm run build`，然后回到仓库根目录运行 `python server.py --port 8920`。安装命令不会连接飞书或 192。

## 可选接入

`.env.example` 是配置说明，**不会自动加载**。将需要的变量设置在运行进程的环境中，真实值不要提交到 GitHub。

- 飞书目录：设置 `DMR_FEISHU_ROOT_TOKEN` 和 `DMR_FEISHU_HOST`；安装并授权 `lark-cli` 后，平台才可读取实际清单。未配置时不会得到公司的真实目录。
- 邮件：`scripts/mail_sync.py` 是邮件获取、下载和提交平台的脚本；真实邮件账号、匹配规则及平台客户端令牌须在部署机器配置。不会因启动平台自动读取真实邮箱。
- DMR 转移清单归档：设置 `DMR_TRANSFER_ARCHIVE_MOUNT_ROOT` 为当前机器有写权限的共享目录挂载点。`DMR_TRANSFER_ARCHIVE_NETWORK_ROOT` 仅用于页面展示网络路径。文件名中的完整日期决定 `Y-年份/DMR-年份-月份/原文件名`。未配置或无权限时应保留待处理状态，不会伪称已写入 192。
- 192 同步：`nas_agent/README.md` 和 `deploy/` 有 worker、HTTPS、令牌及部署说明；每台机器须分别配置网络权限与凭证。源码不包含这些凭证。

该仓库可以在其他电脑安装和运行平台本身；飞书、邮箱和 NAS 的真实业务链路仍取决于那台电脑的授权、网络和配置。

## 验证

```bash
.venv/bin/python -m pytest -q tests
cd frontend && npm run build
```

测试中的本地真实样本是可选的：`DMR_REAL_WORKFLOW_DATA_DIR`、`DMR_REAL_BOM_SAMPLE`、`DMR_DOCUMENT_FIXTURE_DIR`、`DMR_SPREADSHEET_SAMPLE` 可指向你有权使用的独立样本目录。缺少样本时对应测试跳过；核心合成数据测试仍会运行。测试不写真实飞书或 192。

## 目录

| 目录 | 用途 |
| --- | --- |
| `backend/`、`server.py` | API、身份与权限、归档、清单、邮件和 NAS 任务 |
| `frontend/` | React 页面源码 |
| `nas_agent/` | 内网文件同步及读取程序 |
| `scripts/` | 安装、运行环境和邮件/飞书接入脚本 |
| `alembic/`、`deploy/` | 数据库迁移和部署模板 |
| `tests/` | 不依赖真实账号的回归测试与可选样本测试 |
| `docs/` | 项目文档 |

发布目录删去了开发时的一次性演示脚本、个人电脑路径绑定的浏览器排练脚本，以及所有运行数据。源工程保留在原位置；这里是独立的 GitHub 发布副本。
