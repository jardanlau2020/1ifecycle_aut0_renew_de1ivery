# Lifecycle Auto Renew

自动续期系统 — 为免费云平台做 lifecycle 续期，防止到期关机。

## 平台续期矩阵

| 平台 | 续期周期 | 自动化 | 说明 |
|------|:------:|:----:|------|
| Monkey Network | 14 天 → +15 天 | ✅ | Pterodactyl Client API，每日自动检查并 Confirm |
| Weirdhost | 7 天 → +15 天 | ✅ | Selenium 浏览器自动化，绕过 Cloudflare Turnstile |
| ACLClouds | 未知 | ❌ | Laravel SPA，API 不可达，需浏览器手动操作 |
| host2play | ~8 小时 | ⚫ | 已暂弃 |
| zenode.fr | 无 | ❌ | 无 SLA，无已知续期机制 |

## 自动续期原理

### Monkey Network — `monkey-renew.yml`

```bash
# 每日 10:00 UTC (18:00 北京时间)
GET  /api/client/servers/{id}/lifecycle
  → can_confirm=true 时
POST /api/client/servers/{id}/lifecycle/confirm  (+15 天)
```

使用 `MONKEY_API_KEY` Secret，服务器 ID `2533c753`。

### Weirdhost — `weirdhost-auto-renew.yml`

```python
# 每日 04:20 UTC (12:20 北京时间)
SeleniumBase + Xvfb + remember_web Cookie
  → 浏览器访问 hub.weirdhost.xyz
  → 绕过 Cloudflare Turnstile
  → 点击续期按钮
  → 验证结果
```

使用 `WEIRDHOST_COOKIE_1` Secret（Laravel `remember_web_` Cookie）。

## Secrets 清单

| Secret | 用途 |
|--------|------|
| `MONKEY_API_KEY` | Monkey Network API 认证 |
| `WEIRDHOST_COOKIE_1` | Weirdhost 登录 Cookie |
| `TG_BOT_TOKEN` | Telegram 通知 Bot Token |
| `TG_CHAT_ID` | Telegram 通知 Chat ID |

## 手动触发

```bash
# Monkey 续期
MONKEY_API_KEY=*** bash scripts/check_and_renew.sh

# Weirdhost 续期
python scripts/weirdhost_renew.py
```

## 安全

- API Key / Cookie 全部走 GitHub Secrets，不写入代码
- 不绕过 CAPTCHA，遇到验证就停止并报告
- 曾暴露的凭证视为已泄露，建议轮换