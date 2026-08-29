# 通用续期/状态监控交付包

## 能做什么
- Monkey Network：用 Pterodactyl Client API 检查账号与服务器是否可见、记录服务器状态；当前 API 没有续期 endpoint，因此输出手动续期提醒。
- ACLClouds：检查公开 `/health`，确认天气 Worker 是否存活。
- 不绕过 CAPTCHA/Turnstile，不自动点击网页 Renew，不硬编码凭证。

## 环境变量
```bash
export MONKEY_API_KEY='替换成新生成的 key'
export MONKEY_SERVER_IDENTIFIER='2533c753'
export ACLCLOUDS_HEALTH_URL='http://141.11.237.77:30551/health'
python3 lifecycle_renewal_monitor.py
```

API key 必须通过环境变量注入，不要写进文件、GitHub 日志或聊天。

## 结果含义
- Monkey `api_ok=true` 且 `ok=true`：API 与服务器可见。
- ACLClouds HTTP 200：阿勒泰 Worker 健康端点正常。
- 续期仍须在各平台面板手动确认，因为当前已知 API 没有续期接口。

## 周期建议
- ACLClouds：每 2 日检查/提醒一次（现有 QwenPaw 提醒已建立）。
- Monkey Network：每日检查一次；到期规则确认后再调整。
- Weirdhost：保留现有每周提醒，剩余 7 日时 Confirm 增加 15 日。
