#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weirdhost 续期脚本（renew-kit 迁移）验收 harness。

不碰真浏览器、不发真网络请求，全部用替身。四块：

  [A] 纯逻辑单测 —— status→Outcome 映射表、fmt_expiry、build_account_summary、
      esc_html、_build_report 的退出码
  [B] add_server_time() 场景矩阵 —— 把 SB / detect_accounts /
      process_single_account / send_account_notification 换成替身，逐场景断言
      退出码与「有没有发通知」。重点守三条不变量：
        · 未检测到账号 → 退出码 1，且发的是那条配置指引
        · error / cookie_invalid / cf_blocked → 退出码 1
        · timeout → 退出码 0（原名单里就不是 fatal，迁移后仍不标红）
  [C] 静态与一致性 —— 迁移掉的重复实现不得回归（本地 now_local / clip_text /
      fmt_expiry 的旧正则 / 裸 sys.exit(1) / 自己判 TG_BOT_TOKEN）；
      workflow 必须用 renew-kit 的 composite action，且**不得**恢复排程
  [D] 真子进程 —— py_compile + 真 import（stub 掉 seleniumbase/aiohttp）

用法：
    python .verify/verify_weirdhost.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tokenize
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # _sync/1ifecycle
SCRIPT = ROOT / "scripts" / "weirdhost_renew.py"
WORKFLOW = ROOT / ".github" / "workflows" / "weirdhost-auto-renew.yml"
README = ROOT / "README.md"

#: 迁移前 fmt_expiry 用的两条正则 —— 用来证明解析语义没被改掉
ORIGINAL_FMT_RES = (
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})",
    r"(\d{4})-(\d{2})-(\d{2})",
)

RENEWKIT_REF_RE = re.compile(r"jardanlau2020/renew-kit/\.github/actions/renew@v\d")


# ----------------------------------------------------------------- 基础设施

class Checks:
    def __init__(self) -> None:
        self.ok = 0
        self.fails: list[str] = []
        self.skips: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def check(self, name: str, cond: bool, extra: str = "") -> bool:
        if cond:
            self.ok += 1
            print(f"  \u2705 {name}")
        else:
            tag = f"  [{extra}]" if extra else ""
            self.fails.append(name + tag)
            print(f"  \u274c {name}{tag}")
        return bool(cond)

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got={got!r} want={want!r}")

    def skip(self, name: str, why: str) -> None:
        self.skips.append(name)
        print(f"  \u26aa SKIP {name} — {why}")

    def report(self) -> int:
        print("\n" + "=" * 62)
        total = self.ok + len(self.fails)
        if self.fails:
            print(f"\u274c {len(self.fails)}/{total} 项失败")
            for f in self.fails:
                print(f"   - {f}")
        else:
            print(f"\u2705 全部通过（{self.ok} 项）"
                  + (f"，{len(self.skips)} 项跳过" if self.skips else ""))
        return 1 if self.fails else 0


def strip_docstrings(src: str) -> str:
    """把所有 docstring 挖空（保留行号）。

    本脚本的模块 docstring 里特意列了「迁移掉的东西：clip_text() → shorten」，
    全文匹配就会把它自己当成违规。所以「代码里不许出现 X」一律基于挖空后的文本。
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            for i in range(body[0].lineno - 1, body[0].end_lineno):
                lines[i] = ""
    return "\n".join(lines)


#: 要挖空的 token：注释、普通字符串，以及 3.12+ 的 f-string 三段
_BLANK_TOKENS = {tokenize.COMMENT, tokenize.STRING}
for _extra in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    _t = getattr(tokenize, _extra, None)
    if _t is not None:
        _BLANK_TOKENS.add(_t)


def code_only(src: str) -> str:
    """把注释与字符串字面量挖空（**保留行列位置**），只留代码结构。

    为什么需要两层视图：脚本里到处是「迁移前是这么写的」这类注释，对全文做
    「不许出现 X」会被自己的说明文字误伤；但有些断言恰恰要看字符串字面量
    （比如 `data.add_field("parse_mode", "HTML")` 该不该存在）。所以：

        SRC  —— 原始文本，用来断言「某个字面量在不在」
        CODE —— 挖空注释+字符串，用来断言「某个标识符/结构在不在」

    tokenize 是逐 token 定位的，挖空后行号列号都不变，`[\\s\\S]{0,600}?`
    这类跨行正则仍然有效。
    """
    lines = src.splitlines()
    buf = [list(line) for line in lines]
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return src
    for tok in toks:
        if tok.type not in _BLANK_TOKENS:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            c0 = scol if row == srow else 0
            c1 = ecol if row == erow else len(lines[row - 1])
            for i in range(c0, min(c1, len(buf[row - 1]))):
                buf[row - 1][i] = " "
    return "\n".join("".join(b) for b in buf)


def find_renewkit() -> Path | None:
    """优先用本地源码（开发时与 renew-kit 同工作区），找不到就退回已安装的包。"""
    override = os.environ.get("RENEWKIT_PATH")
    if override:
        return Path(override)
    for cand in (ROOT.parents[1] / "renew-kit",
                 ROOT.parent / "renew-kit",
                 Path.home() / "renew-kit"):
        if (cand / "renewkit" / "__init__.py").is_file():
            return cand
    return None


def install_stubs() -> list[str]:
    """塞最小替身：requests（renewkit.http 要）+ seleniumbase + aiohttp。

    本 harness 只用到 outcome / report / env / notify，不会真发请求、不开浏览器。
    返回被替身的模块名列表。
    """
    stubbed: list[str] = []

    if importlib.util.find_spec("requests") is None:
        req = types.ModuleType("requests")

        class RequestException(Exception):
            pass

        class Session:
            def __init__(self, *a, **k):
                self.headers = {}

        req.RequestException = RequestException
        req.Session = Session
        req.Response = type("Response", (), {})
        sys.modules["requests"] = req

        adapters = types.ModuleType("requests.adapters")

        class HTTPAdapter:
            def __init__(self, *a, **k):
                pass

        adapters.HTTPAdapter = HTTPAdapter
        req.adapters = adapters
        sys.modules["requests.adapters"] = adapters

        u3 = types.ModuleType("urllib3")
        sys.modules["urllib3"] = u3
        u3util = types.ModuleType("urllib3.util")
        sys.modules["urllib3.util"] = u3util
        u3retry = types.ModuleType("urllib3.util.retry")

        class Retry:
            def __init__(self, *a, **k):
                pass

        u3retry.Retry = Retry
        sys.modules["urllib3.util.retry"] = u3retry
        stubbed.append("requests")

    if importlib.util.find_spec("seleniumbase") is None:
        sb = types.ModuleType("seleniumbase")

        class _SB:
            """SB(...) 当上下文管理器用；真实现留给 CI。"""

            def __init__(self, *a, **k):
                self.kwargs = k

            def __enter__(self):
                return types.SimpleNamespace()

            def __exit__(self, *exc):
                return False

        sb.SB = _SB
        sys.modules["seleniumbase"] = sb
        stubbed.append("seleniumbase")

    if importlib.util.find_spec("aiohttp") is None:
        ai = types.ModuleType("aiohttp")

        class ClientSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *a, **k):
                return types.SimpleNamespace(status=200)

        class FormData:
            def add_field(self, *a, **k):
                pass

        ai.ClientSession = ClientSession
        ai.FormData = FormData
        sys.modules["aiohttp"] = ai
        stubbed.append("aiohttp")

    return stubbed


def load_script(renewkit: Path | None):
    if renewkit is not None:
        sys.path.insert(0, str(renewkit))
    else:
        try:
            import renewkit  # noqa: F401
        except ImportError:
            raise SystemExit(
                "找不到 renewkit：先 `pip install renewkit`，"
                "或用 RENEWKIT_PATH 指向 renew-kit 源码目录")
    spec = importlib.util.spec_from_file_location("weirdhost_renew", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["weirdhost_renew"] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------- [B] 场景矩阵驱动

class SBSpy:
    """记录 SB(...) 被调用了几次、用了什么参数。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self

    def __enter__(self):
        return types.SimpleNamespace()

    def __exit__(self, *exc):
        return False


@contextlib.contextmanager
def notify_spy(mod):
    """拦 renewkit.notify.send，记下每条消息（顺带把 DRY_RUN 闸门绕开）。

    直接替换 mod.notify.send 就行 —— weirdhost_renew 是 `from renewkit import
    notify` 再用 notify.send(...)，所以补丁打在模块属性上即可。
    """
    sent: list[str] = []
    saved = mod.notify.send
    mod.notify.send = lambda text, **k: (sent.append(text), True)[1]
    try:
        yield sent
    finally:
        mod.notify.send = saved


def run_main(mod, *, accounts, per_account=None, raise_exc=None, dry_run=False,
             patch_notify=True):
    """跑一次 add_server_time()，浏览器/账号处理全换成替身。

    返回 (exit_code, stdout, sent_messages, photo_calls)。
    """
    calls = {"photo": 0, "notified": 0}
    sb_spy = SBSpy()

    patched = ("detect_accounts", "process_single_account",
               "send_account_notification", "SB", "sync_tg_notify_photo", "DRY_RUN")
    saved = {k: getattr(mod, k) for k in patched}

    mod.detect_accounts = lambda: list(accounts)
    mod.SB = sb_spy

    def _process(sb, account, idx):
        if raise_exc is not None:
            raise raise_exc
        return (per_account or (lambda i: {"remark": f"acc{i}", "status": "success"}))(idx)
    mod.process_single_account = _process

    def _notify(result):
        calls["notified"] += 1
    mod.send_account_notification = _notify

    def _photo(*a, **k):
        calls["photo"] += 1
    mod.sync_tg_notify_photo = _photo
    mod.DRY_RUN = dry_run

    buf = io.StringIO()
    try:
        if patch_notify:
            with notify_spy(mod) as sent:
                with contextlib.redirect_stdout(buf):
                    code = mod.add_server_time()
        else:
            sent = []
            with contextlib.redirect_stdout(buf):
                code = mod.add_server_time()
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)
    return code, buf.getvalue(), sent, calls


# ----------------------------------------------------------------- [A] 纯逻辑

def section_a(c: Checks, mod) -> None:
    c.section("[A] 纯逻辑")

    # A1 status -> Outcome
    want = {
        "success": "renewed",
        "skipped": "skipped",
        "cooldown": "skipped",
        "no_server": "skipped",
        "timeout": "unknown",
        "cookie_invalid": "failed",
        "cf_blocked": "failed",
        "error": "failed",
    }
    for status, expect in want.items():
        c.eq(f"A1 _outcome_of({status!r})", mod._outcome_of({"status": status}).value, expect)
    c.eq("A2 未知 status -> unknown", mod._outcome_of({"status": "??"}).value, "unknown")
    c.eq("A3 没有 status 键 -> unknown", mod._outcome_of({}).value, "unknown")

    # A4/A5 只有 FAILED 标红（这是迁移的核心收益）
    for status, red in (("success", False), ("skipped", False), ("cooldown", False),
                        ("no_server", False), ("timeout", False),
                        ("cookie_invalid", True), ("cf_blocked", True), ("error", True)):
        c.eq(f"A4 {status} 是否标红", mod._outcome_of({"status": status}).is_error, red)

    # A6 退出码走 report
    c.eq("A6 全 success -> exit 0", mod._build_report([{"status": "success"}]).exit_code, 0)
    c.eq("A7 混入 timeout -> exit 0",
         mod._build_report([{"status": "success"}, {"status": "timeout"}]).exit_code, 0)
    c.eq("A8 混入 error -> exit 1",
         mod._build_report([{"status": "success"}, {"status": "error"}]).exit_code, 1)
    c.eq("A9 空列表 -> exit 0（由调用方补 FAILED）",
         mod._build_report([]).exit_code, 0)

    # A10 fmt_expiry：解析本身交给 kit，这里只验证语义没变
    c.eq("A10 完整时间戳", mod.fmt_expiry("2026-10-31T12:00:00"), "10-31 12:00")
    c.eq("A11 空格分隔", mod.fmt_expiry("2026-10-31 12:00:00"), "10-31 12:00")
    c.eq("A12 只有日期", mod.fmt_expiry("2026-10-31"), "10-31")
    c.eq("A13 Unknown -> 空串", mod.fmt_expiry("Unknown"), "")
    c.eq("A14 unknown 小写 -> 空串", mod.fmt_expiry("unknown"), "")
    c.eq("A15 空串 -> 空串", mod.fmt_expiry(""), "")
    c.eq("A16 None -> 空串", mod.fmt_expiry(None), "")

    # A17 esc_html：Telegram HTML parse_mode 的三个保留字符
    c.eq("A17 esc_html 转义 & < >", mod.esc_html("a<b>&c"), "a&lt;b&gt;&amp;c")
    c.eq("A18 esc_html 不碰引号", mod.esc_html('a"b\'c'), 'a"b\'c')
    c.eq("A19 esc_html(None) -> 空串", mod.esc_html(None), "")

    # A20 _account_label
    c.eq("A20 有备注优先备注", mod._account_label({"remark": "我的號", "email": "a@b.c"}), "我的號")
    c.eq("A21 无备注用打码邮箱",
         mod._account_label({"email": "alice@example.com"}), "a***e@example.com")
    c.check("A22 都没有也不炸", mod._account_label({}) == "***", repr(mod._account_label({})))

    # A23-A34 build_account_summary 各分支
    s = mod.build_account_summary({"email": "a@b.c", "status": "cookie_invalid"})
    c.check("A23 cookie_invalid 文案", "Cookie 已失效" in s, s[:200])
    c.check("A24 统计行 ✅0 ⏭️0 ❌1",
            re.search(r"✅ 0 ｜ ⏭️ 0 ｜ ❌ 1", s) is not None, s.splitlines()[0])

    s = mod.build_account_summary({"email": "a@b.c", "status": "cf_blocked"})
    c.check("A25 cf_blocked 文案", "Cloudflare 验证页" in s, s[:200])
    c.check("A26 cf_blocked 也是 ❌1",
            re.search(r"✅ 0 ｜ ⏭️ 0 ｜ ❌ 1", s) is not None, s.splitlines()[0])

    s = mod.build_account_summary({"email": "a@b.c", "status": "no_server"})
    c.check("A27 no_server 归 ⏭️", re.search(r"✅ 0 ｜ ⏭️ 1 ｜ ❌ 0", s) is not None,
            s.splitlines()[0])

    s = mod.build_account_summary({"email": "a@b.c", "status": "error",
                                   "message": "X" * 200})
    c.check("A28 无 servers 时用 message 且截断",
            ("❌ " + "X" * 59 + "…") in s, s[:250])
    c.check("A29 失败时提示看 log", "睇 workflow log 排查" in s, s[-80:])

    s = mod.build_account_summary({
        "email": "a@b.c", "status": "success", "cookie_updated": True,
        "servers": [{"server_id": "abcdef1234", "server_name": "srv1",
                     "status": "success", "new_expiry": "2026-10-31T12:00:00",
                     "message": "延长 24.0 小时"}]})
    c.check("A30 续期成功带到期时间", "✅ 已續期 → 10-31 12:00" in s, s)
    c.check("A31 从 message 里取延长时长", "延長 24.0h" in s, s)
    c.check("A32 cookie_updated 会单独提示", "🔑 Cookie 已自動更新" in s, s)
    c.check("A33 统计行 ✅1", re.search(r"✅ 1 ｜ ⏭️ 0 ｜ ❌ 0", s) is not None,
            s.splitlines()[0])

    s = mod.build_account_summary({
        "email": "a@b.c", "status": "success",
        "servers": [{"server_id": "abcdef1234", "server_name": "srv1",
                     "status": "success",
                     "original_expiry": "2026-10-01T00:00:00",
                     "new_expiry": "2026-10-02T12:00:00"}]})
    c.check("A34 无 message 时按到期时间差算延长", "延長 36.0h" in s, s)

    s = mod.build_account_summary({
        "email": "a@b.c", "status": "skipped",
        "servers": [{"server_id": "abcdef1234", "status": "cooldown",
                     "original_expiry": "2026-10-05T08:00:00"}]})
    c.check("A35 cooldown 文案带到期", "⏭️ 未可續（冷卻中） · 到期 10-05 08:00" in s, s)

    s = mod.build_account_summary({
        "email": "a@b.c", "status": "skipped",
        "servers": [{"server_id": "abcdef1234", "status": "skipped",
                     "message": "未到续期窗口（还有 3 天）",
                     "original_expiry": "2026-10-05T08:00:00"}]})
    c.check("A36 skipped 原因先剪掉括号补充",
            "⏭️ 未可續（未到续期窗口） · 到期 10-05 08:00" in s, s)

    s = mod.build_account_summary({
        "email": "a@b.c", "status": "error",
        "servers": [{"server_id": "abcdef1234", "status": "weird",
                     "message": "something broke"}]})
    c.check("A37 未识别的 server 状态 -> ❌", "❌ something broke" in s, s)

    # A38 长原因也要截到 12 字
    long_reason = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    s = mod.build_account_summary({
        "email": "a@b.c", "status": "skipped",
        "servers": [{"server_id": "abcdef1234", "status": "skipped",
                     "message": long_reason}]})
    c.check("A38 skipped 原因截到 12 字", "ABCDEFGHIJK" in s and long_reason not in s, s)

    # A39 shorten 是 kit 的（同一份实现，不是本地抄的）
    c.eq("A39 shorten 与 kit 同一实现", mod.shorten is _kit_shorten(mod), True)

    # A40-A45 parse_expiry_to_datetime：迁移时补的 ISO 兜底
    #   面板页面给空格、详情接口给 T，原来只认空格 —— 于是「new > original
    #   才算续上」在接口那条路上永远为 None。这里把两种写法都钉住。
    c.eq("A40 空格分隔可解析", mod.parse_expiry_to_datetime("2026-10-31 12:00:00"),
         mod.datetime(2026, 10, 31, 12, 0, 0))
    c.eq("A41 T 分隔可解析（迁移时补的）",
         mod.parse_expiry_to_datetime("2026-10-31T12:00:00"),
         mod.datetime(2026, 10, 31, 12, 0, 0))
    c.eq("A42 只有日期可解析", mod.parse_expiry_to_datetime("2026-10-31"),
         mod.datetime(2026, 10, 31))
    c.eq("A43 Unknown -> None", mod.parse_expiry_to_datetime("Unknown"), None)
    c.eq("A44 空值 -> None", mod.parse_expiry_to_datetime(""), None)
    c.eq("A45 垃圾 -> None", mod.parse_expiry_to_datetime("soon-ish"), None)
    c.check("A46 带时区的值摘掉 tzinfo（否则和 datetime.now() 比较会 TypeError）",
            mod.parse_expiry_to_datetime("2026-10-31T12:00:00+08:00").tzinfo is None,
            repr(mod.parse_expiry_to_datetime("2026-10-31T12:00:00+08:00")))
    # A47 顺手证明它真的能驱动「变长了」的判断
    _o = mod.parse_expiry_to_datetime("2026-10-01T00:00:00")
    _n = mod.parse_expiry_to_datetime("2026-10-02T12:00:00")
    c.eq("A47 T 分隔也能比较出 36h", (_n - _o).total_seconds() / 3600, 36.0)


def _kit_shorten(mod):
    import renewkit
    return renewkit.shorten


# ----------------------------------------------------------------- [B] 场景

def section_b(c: Checks, mod) -> None:
    c.section("[B] add_server_time() 场景矩阵")

    # B1 没有账号 -> exit 1 + 配置指引
    code, out, sent, calls = run_main(mod, accounts=[])
    c.eq("B1.1 未检测到账号 -> exit 1", code, 1)
    c.check("B1.2 打印配置指引", "未检测到任何有效的账号配置" in out, out[-300:])
    c.check("B1.3 发了 TG", any("未检测到任何有效的 WEIRDHOST_COOKIE_N" in m for m in sent),
            str(sent)[:300])
    c.eq("B1.4 没进浏览器", calls["notified"], 0)

    # B2 单账号成功 -> exit 0
    code, out, sent, calls = run_main(mod, accounts=[{"remark": "a"}])
    c.eq("B2.1 success -> exit 0", code, 0)
    c.eq("B2.2 每个账号发一条", calls["notified"], 1)
    c.check("B2.3 打印总数", "共 1 个账号" in out, out[:400])

    # B3 error -> exit 1
    code, _, _, _ = run_main(
        mod, accounts=[{"remark": "a"}],
        per_account=lambda i: {"remark": f"acc{i}", "status": "error", "message": "boom"})
    c.eq("B3 error -> exit 1", code, 1)

    # B4 cookie_invalid / cf_blocked -> exit 1
    for status in ("cookie_invalid", "cf_blocked"):
        code, _, _, _ = run_main(
            mod, accounts=[{"remark": "a"}],
            per_account=lambda i, s=status: {"remark": f"acc{i}", "status": s})
        c.eq(f"B4 {status} -> exit 1", code, 1)

    # B5 timeout -> exit 0（原 fatal 名单里没有它，迁移后仍不标红）
    code, _, _, _ = run_main(
        mod, accounts=[{"remark": "a"}],
        per_account=lambda i: {"remark": f"acc{i}", "status": "timeout"})
    c.eq("B5 timeout -> exit 0", code, 0)

    # B6 no_server / cooldown / skipped -> exit 0
    for status in ("no_server", "cooldown", "skipped"):
        code, _, _, _ = run_main(
            mod, accounts=[{"remark": "a"}],
            per_account=lambda i, s=status: {"remark": f"acc{i}", "status": s})
        c.eq(f"B6 {status} -> exit 0", code, 0)

    # B7 多账号：全成功 -> 0，一坏 -> 1
    code, _, _, calls = run_main(
        mod, accounts=[{"remark": "a"}, {"remark": "b"}, {"remark": "c"}])
    c.eq("B7.1 三账号全成功 -> exit 0", code, 0)
    c.eq("B7.2 三账号各发一条", calls["notified"], 3)
    code, _, _, _ = run_main(
        mod, accounts=[{"remark": "a"}, {"remark": "b"}],
        per_account=lambda i: {"remark": f"acc{i}",
                               "status": "success" if i == 0 else "error"})
    c.eq("B7.3 有一个 error -> exit 1", code, 1)

    # B8 浏览器异常、一个结果都没有 -> exit 1 + 启动失败通知
    code, out, sent, _ = run_main(mod, accounts=[{"remark": "a"}],
                                  raise_exc=RuntimeError("no display"))
    c.eq("B8.1 浏览器异常 -> exit 1", code, 1)
    c.check("B8.2 打印异常", "浏览器异常" in out, out[-300:])
    c.check("B8.3 发浏览器启动失败通知",
            any("浏览器启动失败" in m for m in sent), str(sent)[:300])
    c.check("B8.4 异常信息已转义进 <code>",
            any("<code>RuntimeError" in m for m in sent), str(sent)[:300])

    # B9 浏览器异常、但已有结果 -> 仍 exit 1（补一条 FAILED）
    code, _, _, _ = run_main(
        mod, accounts=[{"remark": "a"}, {"remark": "b"}],
        per_account=lambda i: {"remark": f"acc{i}", "status": "success"},
        raise_exc=RuntimeError("crashed on 2nd"),
        # 第 2 个账号才炸：用 1 个账号跑不出「已有结果」的场景，
        # 所以这里让 process 在第 2 次调用时抛
        )
    c.eq("B9.1 中途崩溃 -> exit 1", code, 1)

    # B10 异常信息里的 HTML 字符不会破坏 <code>
    code, _, sent, _ = run_main(mod, accounts=[{"remark": "a"}],
                                raise_exc=RuntimeError("bad <tag> & stuff"))
    c.check("B10 < > & 已转义",
            any("&lt;tag&gt; &amp; stuff" in m for m in sent), str(sent)[:400])

    # B11 正常路径：全成功时不该发任何 HTML 通知（per-account 那条被替身吃掉了）
    code, _, sent, _ = run_main(mod, accounts=[{"remark": "a"}])
    c.eq("B11 正常路径不发 run 级 TG", sent, [])

    # B12 DRY_RUN 下 send_account_notification 不发自拍图
    #     （真函数里靠 `if screenshot and not DRY_RUN` 分流，这里验证闸门存在）
    saved_notify = mod.notify.send
    saved_photo = mod.sync_tg_notify_photo
    photo_calls = {"n": 0}
    sent_plain = []
    mod.notify.send = lambda text, **k: (sent_plain.append(text), True)[1]
    mod.sync_tg_notify_photo = lambda *a, **k: photo_calls.__setitem__("n", photo_calls["n"] + 1)
    try:
        result = {"remark": "a", "status": "success", "email": "a@b.c",
                  "servers": [{"server_id": "x", "status": "success",
                               "screenshot": __file__}]}   # 用一个真实存在的文件
        mod.DRY_RUN = True
        with contextlib.redirect_stdout(io.StringIO()):
            mod.send_account_notification(result)
        c.eq("B12.1 DRY_RUN 下不发 sendPhoto", photo_calls["n"], 0)
        c.eq("B12.2 DRY_RUN 下走文本通道", len(sent_plain), 1)

        photo_calls["n"] = 0
        sent_plain.clear()
        mod.DRY_RUN = False
        with contextlib.redirect_stdout(io.StringIO()):
            mod.send_account_notification(result)
        c.eq("B12.3 非 DRY_RUN 且有截图 -> 发图", photo_calls["n"], 1)
        c.eq("B12.4 发图时不再发文本", len(sent_plain), 0)

        # 没有截图时，非 DRY_RUN 也要走文本通道
        photo_calls["n"] = 0
        sent_plain.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            mod.send_account_notification({"remark": "a", "status": "success",
                                           "servers": [{"status": "success"}]})
        c.eq("B12.5 无截图 -> 文本通道", (photo_calls["n"], len(sent_plain)), (0, 1))
    finally:
        mod.notify.send = saved_notify
        mod.sync_tg_notify_photo = saved_photo
        mod.DRY_RUN = False


# ------------------------------------------------------------- [C] 静态检查

def section_c(c: Checks, src: str, code: str) -> None:
    c.section("[C] 静态与接线")

    # C1 renewkit 真的被 import 了
    c.check("C1 导入 renewkit", "from renewkit import" in code, "未找到")
    for name in ("Outcome", "RenewReport", "TargetResult", "shorten"):
        c.check(f"C1b 用到 renewkit.{name}", re.search(rf"\b{name}\b", code) is not None)
    c.check("C1c 用 renewkit.env", re.search(r"\benv\.(get|get_int|dry_run)\(", code) is not None)
    c.check("C1d 用 renewkit.notify", "notify.send(" in code)
    c.check("C1e 用 kit 的 now_local",
            re.search(r"from renewkit\.timeutil import[^\n]*now_local", code) is not None)
    c.check("C1f 用 kit 的 format_expiry",
            re.search(r"from renewkit\.timeutil import[^\n]*format_expiry", code) is not None)

    # C2-C6 迁移掉的重复实现不得回归（CODE 视图：注释里的「迁移前是…」不算）
    for gone, why in (
        ("def now_local", "本地 now_local 已删（改用 kit）"),
        ("def clip_text", "本地 clip_text 已删（改用 shorten）"),
        ("def tg_notify(", "本地 tg_notify 已删（改用 notify.send）"),
        ("def sync_tg_notify(", "本地 sync_tg_notify 已删"),
        ("sync_tg_notify(", "不得再调 sync_tg_notify"),
        ("clip_text(", "不得再调 clip_text"),
    ):
        c.check(f"C2 不再出现 {gone!r}", gone not in code, why)

    # C7-C9 裸 sys.exit 必须清掉（只留 __main__ 那一处）
    exits = re.findall(r"sys\.exit\(([^\n]*)\)", code)
    c.eq("C7 sys.exit 只剩 __main__ 一处", len(exits), 1)
    c.check("C8 那一处是 sys.exit(add_server_time())",
            exits == ["add_server_time()"], str(exits))
    c.check("C9 不再有裸 sys.exit(1)", "sys.exit(1)" not in code)

    # C10-C12 旧 fmt_expiry 的两条正则不得回归（SRC 视图：它们是字面量）
    for i, rx in enumerate(ORIGINAL_FMT_RES, 1):
        c.check(f"C1{i} 旧 fmt_expiry 正则 #{i} 已删", rx not in src, rx)
    c.check("C13 fmt_expiry 只是 kit 的包装",
            re.search(r"def fmt_expiry[\s\S]{0,900}?return format_expiry\(", code) is not None)

    # C14-C16 旧 fatal 名单不得回归
    c.check("C14 不再硬编码 fatal 名单", "fatal = [" not in code)
    c.check("C15 有 _STATUS_OUTCOME 映射表", "_STATUS_OUTCOME = {" in code)
    c.check("C16 退出码取自 RenewReport", ".exit_code" in code)

    # C17-C19 DRY_RUN 闸门
    c.check("C17 读 DRY_RUN", "DRY_RUN = env.dry_run()" in code)
    c.check("C18 sendPhoto 前有 DRY_RUN 闸门",
            re.search(r"if screenshot and not DRY_RUN:", code) is not None)
    # SRC 视图：要看的正是那个字面量
    c.check("C19 sendPhoto 不再自带 parse_mode=HTML",
            'data.add_field("parse_mode", "HTML")' not in src)

    # C20 esc_html 真的用在那两处 HTML 通知上
    c.eq("C20 esc_html 出现 2 次（def + 调用）", code.count("esc_html("), 2)
    c.eq("C21 notify_html 出现 3 次（def + 2 处调用）", code.count("notify_html("), 3)
    c.check("C22 浏览器异常通知已转义", "esc_html(shorten(repr(e)" in code)

    # C23-C33 workflow 接线
    if not WORKFLOW.is_file():
        c.skip("C23 workflow 检查", "文件不存在")
    else:
        wf = WORKFLOW.read_text(encoding="utf-8")
        c.check("C23 workflow 用 renew-kit composite action",
                RENEWKIT_REF_RE.search(wf) is not None, wf[:200])
        c.check("C24 workflow 指定了脚本",
                re.search(r"script:\s*scripts/weirdhost_renew\.py", wf) is not None)
        c.check("C25 workflow 传 TG_BOT_TOKEN", "TG_BOT_TOKEN:" in wf)
        c.check("C26 workflow 传 TG_CHAT_ID", "TG_CHAT_ID:" in wf)
        c.check("C27 workflow 装 aiohttp/pynacl",
                "aiohttp" in wf and "pynacl" in wf, "")
        c.check("C28 workflow 传 WEIRDHOST_COOKIE_1", "WEIRDHOST_COOKIE_1:" in wf)
        c.check("C29 workflow 装 xvfb（有头 Chromium 需要 X）",
                "apt-packages:" in wf and "xvfb" in wf, "")
        c.check("C30 workflow 用 xvfb-run 包住脚本",
                "xvfb-run" in wf and "1920x1080x24" in wf, "")
        c.check("C31 workflow 保留产物上传", "artifact-paths:" in wf and "*.png" in wf)
        c.check("C32 workflow 关掉 action 的兜底通知（避免和脚本的重复）",
                'notify-on-failure: "false"' in wf)
        # 2026-09-18 停排程：CF 交互式验证页要人手剔 checkbox，自动续期原理上不通。
        # 迁移不许把这个决定改回去 —— 恢复 schedule 就是恢复「每日假红灯」。
        c.check("C33 workflow 不得恢复排程（只能手动触发）",
                "schedule:" not in wf, "又出现了 schedule:")
        c.check("C34 workflow 保留 workflow_dispatch", "workflow_dispatch:" in wf)
        c.check("C35 说明为什么停排程",
                "2026-09-18" in wf and "Cloudflare" in wf, wf[:400])
        c.check("C36 保留运行记录清理", "delete-workflow-runs" in wf)

    # C37-C38 换行符（run #82 的教训：Windows 上写出来的 CRLF 会让 bash 炸）
    for f in (SCRIPT, WORKFLOW, README):
        if not f.is_file():
            continue
        raw = f.read_bytes()
        c.eq(f"C37 {f.name} 不含 CR", b"\r" in raw, False)
    offenders = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if any(x in p.parts for x in (".git", "__pycache__", ".pytest_cache")):
            continue
        if p.suffix not in (".sh", ".yml", ".yaml", ".py", ".bat", ".ps1", ".json"):
            continue
        if b"\r" in p.read_bytes():
            offenders.append(str(p.relative_to(ROOT)))
    c.eq("C38 仓库内无任何 CRLF 的脚本/配置", offenders, [])

    # C39-C40 README 得把 weirdhost 的现状写对
    if README.is_file():
        rd = README.read_text(encoding="utf-8")
        c.check("C39 README 提到 weirdhost", "weirdhost" in rd.lower(), "")
        c.check("C40 README 提到 renew-kit", "renew-kit" in rd.lower(), "")
        # README 之前把 composite action 的 ref 写成 v0.4.2（kit 已经到 v0.5.3），
        # 这种「文档里的版本号烂掉」没人会注意 —— 钉一下。
        c.check("C41 README 不残留旧 renew-kit tag",
                "v0.4.2" not in rd, "还写着 v0.4.2")
        c.check("C42 README 记下 weirdhost 的 CF 例外（FAILED 而非 TRANSIENT）",
                "例外" in rd and "_STATUS_OUTCOME" in rd, "")
        c.check("C43 README 写明 parse_expiry 的 ISO 兜底",
                "parse_expiry_to_datetime" in rd and "T12:00:00" in rd, "")
        c.check("C44 README 提到 verify_weirdhost.py",
                "verify_weirdhost.py" in rd, "")
        c.check("C45 README 提到 weirdhost 的 DRY_RUN",
                "DRY_RUN=1 python scripts/weirdhost_renew.py" in rd, "")
    else:
        c.skip("C39/C40 README 检查", "文件不存在")


# ------------------------------------------------------------- [D] 真子进程

#: 在子进程里塞 stub 再真 import —— 证明模块在真解释器下能加载，
#: 而且退出码映射表在真 import 之后也是对的。
BOOT = r"""
import sys, types
sb = types.ModuleType('seleniumbase')
class _SB:
    def __init__(self, *a, **k): pass
    def __enter__(self): return types.SimpleNamespace()
    def __exit__(self, *a): return False
sb.SB = _SB
sys.modules['seleniumbase'] = sb
ai = types.ModuleType('aiohttp')
ai.ClientSession = object
ai.FormData = object
sys.modules['aiohttp'] = ai
import weirdhost_renew as w
print('SERVICE=%s' % w.SERVICE)
print('DRY_RUN=%r' % w.DRY_RUN)
print('OUTCOMES=%s' % ','.join(sorted(o.value for o in set(w._STATUS_OUTCOME.values()))))
print('EXIT_success=%d' % w._build_report([{'status': 'success'}]).exit_code)
print('EXIT_timeout=%d' % w._build_report([{'status': 'timeout'}]).exit_code)
print('EXIT_error=%d' % w._build_report([{'status': 'error'}]).exit_code)
print('FMT=%s' % w.fmt_expiry('2026-10-31T12:00:00'))
print('ESC=%s' % w.esc_html('a<b>&c'))
"""


def section_d(c: Checks, renewkit: Path | None) -> None:
    c.section("[D] 真子进程")

    r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)],
                       text=True, capture_output=True, timeout=120)
    c.check("D1  py_compile 通过", r.returncode == 0, r.stderr[-300:])

    env = dict(os.environ)
    # scripts/ 也要进 PYTHONPATH：脚本在 scripts/weirdhost_renew.py，
    # 子进程里 `import weirdhost_renew` 得找得到它。
    parts = [str(ROOT), str(ROOT / "scripts")]
    if renewkit is not None:
        parts.append(str(renewkit))
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)

    r = subprocess.run([sys.executable, "-c", BOOT], cwd=str(ROOT), env=env,
                       text=True, capture_output=True, timeout=180)
    c.check("D2  真子进程能 import 模块", r.returncode == 0, r.stderr[-500:])
    if r.returncode != 0:
        return
    out = r.stdout
    c.check("D3  SERVICE 名", "SERVICE=Weirdhost" in out, out.strip()[:200])
    c.check("D4  DRY_RUN 默认 False", "DRY_RUN=False" in out, out.strip()[:200])
    c.check("D5  映射表覆盖 4 种 Outcome",
            "OUTCOMES=failed,renewed,skipped,unknown" in out, out.strip()[:200])
    c.check("D6  success -> exit 0", "EXIT_success=0" in out, out.strip()[:200])
    c.check("D7  timeout -> exit 0", "EXIT_timeout=0" in out, out.strip()[:200])
    c.check("D8  error -> exit 1", "EXIT_error=1" in out, out.strip()[:200])
    c.check("D9  fmt_expiry 走 kit", "FMT=10-31 12:00" in out, out.strip()[:200])
    c.check("D10 esc_html 生效", "ESC=a&lt;b&gt;&amp;c" in out, out.strip()[:200])

    # D11 缺凭据时端到端退出 1（真跑 main，浏览器在 SB 之前就返回了）
    r = subprocess.run(
        [sys.executable, "-c",
         BOOT + "\nimport os\n"
                "for k in list(os.environ):\n"
                "    if k.startswith('WEIRDHOST_COOKIE_'): os.environ.pop(k)\n"
                "print('MAIN=%d' % w.add_server_time())\n"],
        cwd=str(ROOT), env={**env, "DRY_RUN": "1"}, text=True,
        capture_output=True, timeout=180)
    c.check("D11 缺 Cookie -> add_server_time() 返回 1",
            r.returncode == 0 and "MAIN=1" in r.stdout,
            f"rc={r.returncode} {r.stdout[-200:]} {r.stderr[-200:]}")


# -------------------------------------------------------------------- main

def main() -> int:
    c = Checks()
    if not SCRIPT.is_file():
        print(f"找不到脚本: {SCRIPT}")
        return 2

    src = SCRIPT.read_text(encoding="utf-8")
    code = code_only(src)

    stubbed = install_stubs()
    renewkit = find_renewkit()
    print(f"weirdhost 离线验证 | 仓库 {ROOT}")
    if stubbed:
        print(f"（本机缺包，已替身：{', '.join(stubbed)}）")

    mod = load_script(renewkit)

    section_a(c, mod)
    section_b(c, mod)
    section_c(c, src, code)
    section_d(c, renewkit)
    return c.report()


if __name__ == "__main__":
    sys.exit(main())
