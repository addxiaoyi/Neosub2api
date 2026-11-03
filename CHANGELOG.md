# 更新日志

## 0.1.0 - 2026-11-02

### 安全
- 上游配置（`/api/settings/*`）读写一律要求管理员，返回中的 `api_key` 只给掩码
- 管理员口令不再写死：缺省时首次启动随机生成，写入 `data/admin-password.txt`（0600）
- docker compose 默认只映射 `127.0.0.1:8010`，需要对外访问须显式设置 `UNIFIED_BIND_HOST`
- 网关匿名开关只接受严格布尔值，字符串 `"false"` 不再被当作真值
- 数据目录收紧到 0700、库文件（含 WAL/SHM）收紧到 0600
- 登录失败按 IP + 用户名节流：连续 5 次失败锁定 15 分钟
- 服务端固定安全响应头（nosniff / X-Frame-Options / Referrer-Policy / CSP frame-ancestors）
- CORS 改为精确方法与请求头白名单，不再通配
- 接口文档默认关闭，需显式设置 `UNIFIED_ENABLE_DOCS=1`
- 上游 4xx/5xx 只回显状态码，内部地址与响应体只进日志

### 新增
- `GET/POST/PATCH/DELETE /api/tokens`：把既有的 `u2a_` 网关密钥薄封装成「访问令牌」视图，写操作仅管理员
- `GET /api/system/changelog`：解析本文件，文件缺失时返回空列表而不是报错
- `GET /api/system/update-status`：如实报告本部署不支持在线更新

### 说明
- 本部署不提供在线更新，也不操作宿主机 docker；版本升级需自行拉取镜像与代码
