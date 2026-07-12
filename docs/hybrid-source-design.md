# 混合抓取源设计（mobile 为主，official CLI 兜底）

2026-07-12 提出。目标：每小时轮询继续，但正常情况下走免费的 m.weibo.cn
登录 cookie 方案（mobile），官方 CLI 只在 mobile 被限流/封禁期间顶上，
把 Credits 消耗从贴上限的 ~7200/月 降到接近 0。

## 方案选择

| 方案 | Credits/月 | 弹性 | 结论 |
|---|---|---|---|
| 纯 official_cli（现状） | ~7200（上限 7500） | 无风控风险 | 太贵 |
| 纯 mobile | 0 | 限流时整体熔断停摆 | 有停摆风险 |
| 主备混合（本设计） | 正常 0，故障期 ≤2 次/小时 | 限流自动切换、恢复自动切回 | 采用 |

按小时轮询估算风控风险：加固后的 mobile 曾以 10 分钟一轮（约 6 倍强度）
连续运行 5 天无封禁，每小时一轮属低风险区间。

## 架构

新增 `weibo_source: "hybrid"`。新建 `HybridClient`（src/weibo_hybrid.py），
实现与现有两个客户端一致的接口（ensure_cookie / timeline_page /
fetch_extend / prepare_cycle / reload_static_cookie），内部持有
mobile `WeiboClient` 和 `OfficialCliClient` 各一个：

- **每轮开始**（prepare_cycle）：若 `mobile_blocked_until` 未到期则本轮
  用 CLI（调用其 prepare_cycle 批量拉取），否则用 mobile（no-op）。
- **轮中失效切换**：mobile 的 timeline_page 抛 RateLimitedError 时，
  HybridClient 记录 `mobile_blocked_until`（指数退避，沿用 legacy extend
  熔断的持久化模式，落 state/hybrid-source.json，重启不重置），随即为
  全部账号调 CLI 的 prepare_cycle（2 次调用），本轮剩余账号无缝改走 CLI。
- **恢复**：blocked_until 到期后下一轮自动回到 mobile；成功一轮则退避清零。
- **长文展开**：不变，始终走 mobile cookie（已有独立熔断）。
- **全局熔断保留**：只有 CLI 也抛 RateLimitedError（Credits 耗尽等）才会
  传到 monitor 触发现有的整体退避——两源全挂时这是正确行为。

## 需要动的现有代码

1. `monitor.py:237` 账号间延迟：现在按 `weibo_source == "mobile"` 判断，
   改为询问 client 的 `requires_account_delay` 属性（hybrid 按当轮实际源返回）。
2. `monitor.py` cookie 刷新条件：`weibo_source` 为 mobile/hybrid 或
   legacy_extend_enabled 时刷新（在 eebdf7f 基础上加 hybrid）。
3. `main.py` 装配：hybrid 模式同时构造两个子客户端。
4. 可观测性：cycle summary 和 health.json 增加 `source` 字段与
   `mobile_blocked_until`，切换/恢复各打一条 INFO 日志；verify.sh 不用改。

## 风险与对策

- mobile 长期被限流 → CLI 兜底持续消耗 Credits：health 里可见 source 与
  blocked_until，必要时人工干预；最坏情况等于现状（纯 CLI），不会超额。
- 轮中切换重复推送：不会——去重靠 state/seen.json 的 mid，与源无关。
- 两源数据结构差异：已有统一的 extract_mblogs/parse_post 路径，CLI 客户端
  本就把官方 payload 适配成 mobile 卡片结构。

## 上线步骤

1. 实现 + 单测（HybridClient 切换/恢复/持久化；monitor 延迟跟随当轮源）。
2. 本地 `--dry-run` 验证 mobile 路径真实抓取正常。
3. 远程 config 改 `weibo_source: "hybrid"`（poll_interval 保持 3600），
   `remote_update.sh` 部署。
4. 观察两个轮询周期：确认 source=mobile、无限流、推送与归档正常。
