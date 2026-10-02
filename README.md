# Lifecycle Auto Renew

免费云平台的自动续期系统 —— 在到期前把机器续上，防止被回收。

## 平台续期矩阵

| 平台 | 续期周期 | 自动化 | 说明 |
|------|:------:|:----:|------|
| Monkey Network | 14 天 → +15 天 | ✅ | Pterodactyl Client API，每日 10:00 UTC 检查并 Confirm |
| ACLClouds | 4 天 → +4 天 | ✅ | Selenium + Chrome，CDP 设 Cookie，每 12 小时检查（窗口 ≤2 天） |
| Weirdhost | 7 天 → +15 天 | ⚫ | 已停排程：Cloudflare 交互式验证需人工，自动化原理上不通 |
| host2play | ~8 小时 | ⚫ | 已暂弃 |
| zenode.fr | 无 | ❌ | 无 SLA，无已知续期机制 |

> ⚫ = 保留脚本、只留 `workflow_dispatch` 手动触发，不再定时跑（免得天天发假红灯）。

## 公共骨架：renew-kit

所有续期脚本共用 [renew-kit](https://github.com/jardanlau2020/renew-kit)，
只写平台自己的业务逻辑，公共部分（配置读取 / 结果分类 / 重试 / Telegram /
报告排版 / 退出码）都在骨架里：

| 能力 | 模块 |
|------|------|
| 环境变量读取、DRY_RUN、必填校验 | `renewkit.env` |
| 结果语义（RENEWED / SKIPPED / ALREADY_MAX / UNKNOWN / TRANSIENT / FAILED） | `renewkit.outcome` |
| 带退避重试的 HTTP 会话、5xx 归类 | `renewkit.http` |
| 中文报告排版 + 退出码 + 发 TG | `renewkit.report` |
| Telegram 发送（POST，避免长消息撞 414） | `renewkit.notify` |

关键约定：**只有 `FAILED` 才 `exit 1`**。上游 5xx / 超时 / Cloudflare 挑战页
一律记 `TRANSIENT` 并 `exit 0`，不标红——日更 + 续期窗口足够容忍漏一次。

`aclclouds-renew.yml` 直接复用了 renew-kit 的 composite action
（`renew-kit/.github/actions/renew@v0.4.1`），依赖安装与 renewkit 注入无需各仓库重写。

## 自动续期原理

### Monkey Network — `monkey-renew.yml`

```bash
# 每日 10:00 UTC (18:00 北京时间)
GET  /api/client/servers/{id}/lifecycle   -> can_confirm / days_remaining
POST /api/client/servers/{id}/lifecycle/confirm   (+15 天，仅 can_confirm=true 时)
```

脚本 `scripts/check_and_renew.sh`。服务器 ID 走 Secret `MONKEY_SERVER_IDENTIFIER`，
不写死在代码里（旧值 `2533c753` 已被平台删除，曾连续 5 单 404）。

⚠️ 现状：账号下 **0 台服务器**，lifecycle 接口回 404。脚本会打印账号下现役服务器
列表并 `exit 0`（不标红）。要恢复自动续期：去面板开新机 → 把 8 位 identifier
更新进 Secret `MONKEY_SERVER_IDENTIFIER`。

### ACLClouds — `aclclouds-renew.yml`

```python
# 每 12 小时：04:20 / 16:20 UTC (12:20 / 00:20 北京时间)
Selenium + Chrome (headless) + CDP 注入 __Host-aclclouds_session
  → 打开 /server/<id>
  → 读「Temps restant: Nj Nh」
  → 窗口开启（剩余 ≤2 天）才点 Renouveler，并确认弹窗
  → 校验剩余时间确实变长（没变长 = FAILED）
```

脚本 `scripts/aclclouds_renew.py`，Secret `ACLCLOUDS_SESSION_COOKIE`。

几个刻意的保守设计：

- **读不到 `Temps restant` → `UNKNOWN` 且绝不点击**。原实现会把它当成「窗口没开」
  静默放过，看起来一片绿，实际 Cookie 早失效了。
- **Cloudflare 挑战页 → `TRANSIENT` / exit 0**，等下一次排程；人机验证
  （Anti-bot confirmation）→ `FAILED`，因为这需要人工。
- 浏览器路径不写死。留空交给 Selenium Manager 自动获取 Chrome for Testing；
  要指定自有 Chromium 就设 `ACLCLOUDS_CHROMIUM`。
- 失败截图落在工作目录，由 workflow 上传为 artifact（原来是丢 `/tmp`，跑完即失）。

手动触发时可勾 `dry_run`：只读剩余时间、不点按钮，用来验证 Cookie 还有没有效。

### Weirdhost — `weirdhost-auto-renew.yml`

脚本 `scripts/weirdhost_renew.py`（SeleniumBase + Xvfb）。

**已停排程。** hub.weirdhost.xyz 上了 Cloudflare 交互式验证页
（"Performing security verification"，需人手勾 checkbox），实测 GHA runner
（Azure）、NAS 容器、Hetzner FI、Tencent SG 四个出口全部 403，连真浏览器都卡在
挑战页。属 human-only 步骤，继续自动跑只会每日发假红灯。
现改为人工续期 + 定时提醒；要复测就手动 `workflow_dispatch`。

### 排障工具

| 文件 | 用途 |
|------|------|
| `.github/workflows/probe-cf.yml` + `scripts/probe_cf.py` | 探测 Cloudflare 出口可达性 |
| `scripts/proxy_up.py` | 拉起本地代理，给上面的探测用 |

## Secrets 清单

| Secret | 用途 | 必填 |
|--------|------|:----:|
| `MONKEY_API_KEY` | Monkey Network API 认证 | 用 Monkey 时 |
| `MONKEY_SERVER_IDENTIFIER` | Monkey 服务器 8 位短 ID | 用 Monkey 时 |
| `ACLCLOUDS_SESSION_COOKIE` | ACLClouds `__Host-aclclouds_session` | 用 ACLClouds 时 |
| `ACLCLOUDS_SERVER_ID` | 覆盖默认服务器 ID（默认 `f743cf50`） | 可选 |
| `WEIRDHOST_COOKIE_1` ~ `_5` | Weirdhost `remember_web_` Cookie（多账号） | 用 Weirdhost 时 |
| `REPO_TOKEN` | Weirdhost 脚本回写仓库用 | 用 Weirdhost 时 |
| `TG_BOT_TOKEN` | Telegram 通知 Bot Token | 可选 |
| `TG_CHAT_ID` | Telegram 通知 Chat ID | 可选 |

未配置 TG 时通知静默跳过，**不影响续期结论**。

## 手动触发

```bash
# Monkey 续期
MONKEY_API_KEY=*** SERVER_ID=xxxxxxxx bash scripts/check_and_renew.sh

# ACLClouds 续期（真点按钮）
ACLCLOUDS_SESSION_COOKIE=*** python3 scripts/aclclouds_renew.py

# ACLClouds 只检查不点击（验证 Cookie 是否还有效）
ACLCLOUDS_SESSION_COOKIE=*** DRY_RUN=1 python3 scripts/aclclouds_renew.py

# Weirdhost 续期
WEIRDHOST_COOKIE_1=*** python scripts/weirdhost_renew.py
```

脚本依赖 `renewkit`：

```bash
pip install "renewkit @ git+https://github.com/jardanlau2020/renew-kit@v0.4.1"
pip install selenium          # ACLClouds 需要
```

## 验收测试

```bash
python .verify/verify_aclclouds.py
```

不碰真浏览器、不发网络请求，用替身把 ACLClouds 脚本的纯逻辑、`run()` 场景矩阵
（含「读不到剩余时间绝不点击」「未到窗口绝不点击」两条不变量）、退出码语义，
以及 workflow / README 与代码的一致性全部断言一遍。改脚本后先跑这个。

## 目录结构

```
.github/workflows/
  aclclouds-renew.yml           # ACLClouds，每 12 小时
  monkey-renew.yml              # Monkey Network，每日
  weirdhost-auto-renew.yml      # Weirdhost，仅手动（已停排程）
  probe-cf.yml                  # Cloudflare 出口探测
scripts/
  aclclouds_renew.py            # ACLClouds 续期（renew-kit）
  check_and_renew.sh            # Monkey 续期
  weirdhost_renew.py            # Weirdhost 续期
  probe_cf.py / proxy_up.py     # 排障
.verify/
  verify_aclclouds.py           # ACLClouds 迁移验收 harness
lifecycle_renewal_config.example.json   # 纯提醒模式的配置示例（非自动化路径）
wasmer_app/                             # 与续期无关的历史遗留（Wasmer 天气卡片应用）
```

## 安全

- API Key / Cookie 全部走 GitHub Secrets，不写入代码
- 不绕过 CAPTCHA，遇到人机验证就停下并报告（Weirdhost 因此停排程）
- 曾暴露的凭证视为已泄露，建议轮换
