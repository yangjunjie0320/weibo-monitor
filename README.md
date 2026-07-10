# weibo-monitor

微博账号监控：定时轮询指定博主的时间线，发现新帖后推送飞书 interactive 卡片到指定群。

## 工作方式

```
每 poll_interval_seconds 一轮：
  将 accounts.yaml 的账号交错分为两个官方 CLI 批次
    → statuses user_timeline_batch 每组获取最新 5 条
    → 与 state/seen.json 里的已见 mid 对比，找出新帖
    → 可选旧接口补抓长文 → 上传首图 → 组卡片 → 发到 chat_id
    → 成功后落 state（失败的下轮重试）
```

- 主时间线使用微博开放平台官方 CLI，需要 OAuth、开发者认证和 Plus 及以上套餐。
- Plus 方案每小时两次读取、每次返回 5 条，约 7200 Credits/月。不会自动翻页或购买额度。
- 旧接口只补抓长文；遇到 403/418/429/432 会单独熔断 12 小时，正文退回截断版，
  不影响官方主周期健康状态。
- 首次见到某账号只落 state 不推送（防冷启动刷屏）。
- 卡片优先：图片上传失败不阻塞发卡。

## 快速开始

```bash
uv sync --extra dev
uv run python -m playwright install chromium
npm install --prefix .tools/weibo-cli --omit=dev --no-audit --no-fund --ignore-scripts \
  @weibo-ai/weibo-cli@0.8.3
cp config.example.yaml config.yaml   # 填 app_id / app_secret
chmod 600 config.yaml
uv run python main.py --list-chats   # 机器人拉进群后，挑 chat_id 填进 config.yaml
uv run python -u main.py --once --dry-run   # 真实抓取但不发消息，验证抓取
uv run python -u main.py             # 常驻运行
```

`--dry-run` 只在内存中去重，不会修改 `seen.json`。两个运维检查入口：

```bash
uv run python main.py --self-check   # 不访问网络，检查配置/依赖/状态/权限
uv run python main.py --source-check # 不读取微博，检查 OAuth、套餐和批量命令权限
uv run python main.py --probe        # 只抓一个账号；0 正常 / 2 限流 / 1 故障
```

飞书自建应用需要的权限：`im:message`（发消息）、`im:resource`（传图）、`im:chat:readonly`（列群）。

## 开发

```bash
uv run pytest -q
uv run ruff check .
```

测试 fixture 来自真实的 timeline API 响应，解析逻辑离线可测。

## 部署

见仓库外的 `maintain/`。`remote_update.sh` 是显式触发的一键安全部署：本地门禁、
精确 SHA、锁定依赖、健康检查和失败回滚；项目不会在远端无人值守地自动拉取代码。
