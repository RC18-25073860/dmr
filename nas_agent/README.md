# DMR 192 NAS Agent

本目录是 DMR 本地项目预留给 192 云盘接入的内网 worker。它不保存真实账号，不连接真实 192；只有平台提供 `nas-backend` 接口、管理员配置 token 和群晖 HTTPS 地址后才能运行。

## 环境变量

- `DMR_NAS_PLATFORM_URL`: DMR 平台 HTTPS 地址，例如 `https://dmr.example.com/api/`
- `DMR_NAS_WORKER_TOKEN`: 平台给内网 worker 的专用 token
- `DMR_NAS_BASE_URL`: 群晖 HTTPS 地址，例如 `https://nas.example.local:5001/`
- `DMR_NAS_CA_FILE`: 可选，放在项目目录内的内网 CA 文件
- `DMR_NAS_TEMP_DIR`: 可选，默认 `.runtime/nas-agent-tmp`
- `DMR_NAS_READER_ALLOWED_ORIGIN`: reader 允许的 DMR 前端 HTTPS 来源；默认取 `DMR_NAS_PLATFORM_URL` 的 origin
- `DMR_NAS_READER_HOST`: reader 监听地址，默认 `127.0.0.1`
- `DMR_NAS_READER_PORT`: reader 监听端口，默认 `8910`
- `DMR_NAS_READER_SSL_CERT`: HTTPS reader 证书文件，必须放在项目目录内
- `DMR_NAS_READER_SSL_KEY`: HTTPS reader 私钥文件，必须放在项目目录内

`DMR_NAS_VERIFY_TLS=false` 不允许用于交付运行。192 地址必须是 HTTPS，且需要系统信任证书或在项目目录内提供 CA 文件。

## 平台需要保留的接口

worker 调用路径统一为 `POST/GET {DMR_NAS_PLATFORM_URL}/storage-sync/...`，并带 `Authorization: Bearer <DMR_NAS_WORKER_TOKEN>`。

- `POST account-checks/claim`
- `POST account-checks/{user_id}/credentials`
- `POST account-checks/{user_id}/result`
- `POST claim`
- `POST files/{file_id}/credentials`
- `GET files/{file_id}/content`，请求头 `X-Sync-Lease`
- `POST files/{file_id}/heartbeat`
- `POST files/{file_id}/result`
- `POST read/authorize`

账号验证只检查个人账号能登录 Synology File Station；它不代表该账号已经拥有某个具体归档目录的写入权限。目录写入权限会在文件同步任务上传时由 `files/{file_id}/result` 回传结果。

worker 不会在 192 上自动创建正式目录。`nas_path` 必须由文控/IT 在 192 中提前建好并授权；目录不存在或无权写入时，同步任务会失败并回写给平台处理。

浏览器打开 NAS 文件时，DMR 后端使用 `POST /api/nas/files/{file_id}/read-ticket`，返回 `read_base_url`、`ticket`、`expires_at` 和 `target_path`。前端再用 `POST` 把 ticket 发到 `read_base_url`；ticket 不放在 GET URL 里，避免进入浏览器历史、代理日志或服务访问日志。reader 支持 `application/json` 和 `application/x-www-form-urlencoded`，不依赖 multipart。

## 启动方式

worker 领取账号验证和文件同步任务：

```bash
cd /path/to/DMR-GitHub-ready
source scripts/env.sh
DMR_NAS_PLATFORM_URL=https://dmr.example.com/api/ \
DMR_NAS_WORKER_TOKEN=由平台配置的同步令牌 \
DMR_NAS_BASE_URL=https://nas.example.local:5001/ \
.venv/bin/python -m nas_agent.worker
```

reader 提供浏览器下载/预览入口，必须配置 HTTPS 证书和私钥：

```bash
cd /path/to/DMR-GitHub-ready
source scripts/env.sh
DMR_NAS_PLATFORM_URL=https://dmr.example.com/api/ \
DMR_NAS_WORKER_TOKEN=同一个同步服务令牌 \
DMR_NAS_BASE_URL=https://nas.example.local:5001/ \
DMR_NAS_READER_ALLOWED_ORIGIN=https://dmr.example.com \
DMR_NAS_READER_SSL_CERT=.runtime/certs/reader.crt \
DMR_NAS_READER_SSL_KEY=.runtime/certs/reader.key \
.venv/bin/python -m nas_agent.reader
```

`DMR_NAS_WORKER_TOKEN` 对应后端的 `DMR_NAS_SYNC_TOKEN`；两端必须配置为同一个值。

## 本地验证

```bash
cd /path/to/DMR-GitHub-ready
source scripts/env.sh
.venv/bin/pytest tests/test_nas_agent.py
```

测试全部使用 mock 平台和 mock Synology，不会访问真实 192，不需要真实账号。
