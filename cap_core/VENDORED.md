# cap_core — vendored Cap PoW solver（hashwx）

## 出處

- 上游：[`tiagozip/cap`](https://github.com/tiagozip/cap)（Apache-2.0），2026-10-05 clone。
  - `hashwx.js` ← `core/src/hashwx.js`
  - `hashwx-wasm.js` ← `core/src/hashwx-wasm.js`（WASM 以 base64 內嵌，零 runtime 檔案依賴）
- 內嵌 WASM 本體上游檔：`core/vendor/hashwx.wasm`（commit
  `74b567a31276a4c5d7cb232a5b7639467f49f961`，**LGPL-3.0**，見 `hashwx-LICENSE.txt`、
  `hashwx-COMMIT.txt`）。

**未改一行**，純搬運；`package.json` 只為令 Node 以 ESM 解析 `hashwx.js`。

## 用途

ACLClouds 登入閘（`https://cap.aclclouds.com/235a82a3e3/`，sitekey `235a82a3e3`）
係自架 Cap 驗證碼 —— 純 JSON protocol：`POST /challenge` 攞 N 個 hashwx 挑戰 →
解 nonce 使 `hashwx(seed, nonce) ≤ U64_MAX / d` → `POST /redeem` 換一次性 token。
官方 core 本身零依賴，直接用，**唔使由零寫 solver**。

## 接口（solve.mjs）

```
node cap_core/solve.mjs <challenge.json> <solutions.json>
```

- 入：`POST /challenge` 的完整 JSON（`{token, challenges:[{protocol,payload:{c,d,n}}]}`）
- 出：`{token, solutions:[{protocol:"hashwx", nonce:"<十進位>"}]}`，可直接 POST `/redeem`

## 紀律

- 只服務**用戶自己帳號**嘅登入驗證（2026-10-05 用戶批「做 ①」）。
- 唔用於繞過任何第三方人機閘；唔轉授權。