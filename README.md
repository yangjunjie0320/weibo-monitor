# weibo-monitor

微博账号监控：定时轮询指定博主的时间线，发现新帖后推送飞书 interactive 卡片到指定群。

## 工作方式

```
每 poll_interval_seconds 一轮：
  随机顺序遍历 accounts.yaml 里的账号
    → m.weibo.cn container API 抓时间线第 1 页（必要时翻页，最多 max_pages_per_account）
    → 与 state/seen.json 里的已见 mid 对比，找出新帖
    → 长文补抓全文 → 上传首图 → 组卡片 → 发到 chat_id
    → 成功后落 state（失败的下轮重试）
```

- 微博侧用游客 cookie（启动时自动获取，遇挑战自动刷新），无需登录。
- 首次见到某账号只落 state 不推送（防冷启动刷屏）。
- 卡片优先：图片上传失败不阻塞发卡。

## 快速开始

```bash
uv sync --extra dev
cp config.example.yaml config.yaml   # 填 app_id / app_secret
uv run python main.py --list-chats   # 机器人拉进群后，挑 chat_id 填进 config.yaml
uv run python -u main.py --once --dry-run   # 真实抓取但不发消息，验证抓取
uv run python -u main.py             # 常驻运行
```

飞书自建应用需要的权限：`im:message`（发消息）、`im:resource`（传图）、`im:chat:readonly`（列群）。

## 开发

```bash
uv run pytest -q
uv run ruff check .
```

测试 fixture 来自真实的 timeline API 响应，解析逻辑离线可测。

## 部署

见仓库外的 `maintain/`（launchd + git 拉取，不入本仓库）。
