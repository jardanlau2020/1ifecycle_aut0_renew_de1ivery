#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACLClouds 续期脚本（renew-kit 迁移）验收 harness。

不碰真浏览器、不发真网络请求，全部用替身。三块：

  [A] 纯逻辑单测 —— Temps restant 解析 / 页面分类 / 续期窗口
  [B] run() 场景矩阵 —— 把浏览器层换成假实现，逐场景断言 Outcome 与
      「到底点没点按钮」。重点守两条不变量：
        · 读不到 Temps restant → UNKNOWN，且绝不点击
        · 未到窗口 / dry-run  → SKIPPED，且绝不点击
  [C] 静态与一致性 —— 旧实现的坑不得回归（硬编码 /usr/bin/chromium、
      散落的 exit 2/3、往 /tmp 丢截图）；workflow 与 README 必须和代码对得上

用法：
    python .verify/verify_aclclouds.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import contextlib
import ast
import importlib.util
import io
import os
import re
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # _sync/1ifecycle
SCRIPT = ROOT / "scripts" / "aclclouds_renew.py"
WORKFLOW = ROOT / ".github" / "workflows" / "aclclouds-renew.yml"
README = ROOT / "README.md"

#: 仓库里实际存在的 workflow（对 README 引用做交叉验证，防止再吹出不存在的文件）
KNOWN_WORKFLOWS = {
    "aclclouds-renew.yml",
    "monkey-renew.yml",
    "weirdhost-auto-renew.yml",
    "probe-cf.yml",
}

#: 迁移前的原始正则，用来证明解析语义没被改动
ORIGINAL_REMAIN_RE = r"Temps restant\s*[:\s]*(\d+)\s*j\s*(?:(\d+)\s*h)?"


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
    """把所有 docstring 挖空（保留行号），用于「代码里不许出现 X」这类断言。

    不这么做会被解释性注释误伤：本脚本的 docstring 里特意写了
    「原实现硬编码 /usr/bin/chromium」「不再丢 /tmp」来说明为什么不这么做，
    直接全文匹配就会把它们当成违规。
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
            doc = body[0]
            for i in range(doc.lineno - 1, doc.end_lineno):
                lines[i] = ""
    return "\n".join(lines)


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


def install_stubs() -> bool:
    """本机没有 requests 时塞一个最小替身，好让 renewkit 能被 import。

    renewkit.http 顶层 `import requests`，但本 harness 只用到
    outcome / report / env，不会真的发请求。返回是否装了替身。
    """
    if importlib.util.find_spec("requests") is not None:
        return False

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
    return True


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
    spec = importlib.util.spec_from_file_location("aclclouds_renew", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aclclouds_renew"] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------- [B] 场景矩阵驱动

class FakeDriver:
    def __init__(self, url: str) -> None:
        self.current_url = url
        self.quit_called = False

    def quit(self) -> None:
        self.quit_called = True

    def save_screenshot(self, path: str) -> None:
        pass

    def find_element(self, *a, **k):
        raise AssertionError("harness 不应该触碰真实 DOM")


def run_scenario(mod, *, text="", err="", url="https://aclclouds.com/server/f743cf50",
                 click_result="已点击 Renouveler", after=None,
                 build_exc=None, dry_run=False, before_hint=None):
    """把浏览器层全部换成替身，跑一次 mod.run()。

    返回 (report, calls)；calls["click"] 是 click_renew 的调用次数。
    """
    calls = {"click": 0, "shots": []}

    patched = ("build_driver", "open_server_page", "click_renew",
               "wait_until_increased", "shot", "DRY_RUN")
    saved = {k: getattr(mod, k) for k in patched}

    if build_exc is not None:
        def _build():
            raise build_exc
        mod.build_driver = _build
    else:
        mod.build_driver = lambda: FakeDriver(url)

    mod.open_server_page = lambda d: (text, err)

    def _click(d):
        calls["click"] += 1
        return click_result
    mod.click_renew = _click

    def _wait(d, before, timeout=12.0):
        return (before if after is None else after, text)
    mod.wait_until_increased = _wait

    def _shot(d, name):
        calls["shots"].append(name)
        return name
    mod.shot = _shot
    mod.DRY_RUN = dry_run

    report = mod.RenewReport(mod.SERVICE)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            mod.run(report)
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)
    calls["out"] = buf.getvalue()
    return report, calls


def one(report):
    """取唯一一条结果（顺带断言确实只有一条）。"""
    assert len(report.results) == 1, f"期望 1 条结果，实际 {len(report.results)}"
    return report.results[0]


# ----------------------------------------------------------------- 主流程

def main() -> int:
    c = Checks()
    print("ACLClouds 迁移验收 harness")
    print(f"脚本: {SCRIPT}")
    print(f"harness 目录: {HERE}")

    renewkit = find_renewkit()
    stubbed = install_stubs()
    print(f"renew-kit: {renewkit or '（用已安装的 renewkit）'}"
          + ("（已注入 requests 替身）" if stubbed else ""))

    # ---------------------------------------------------------- [A] 语法
    c.section("[A] 语法与导入")
    src = SCRIPT.read_text(encoding="utf-8")
    try:
        compile(src, str(SCRIPT), "exec")
        c.check("aclclouds_renew.py 可编译", True)
    except SyntaxError as exc:
        c.check("aclclouds_renew.py 可编译", False, str(exc))
        return c.report()

    c.check("导入 renewkit（已迁移，不再自建脚手架）",
            "from renewkit import" in src)

    mod = load_script(renewkit)
    c.eq("SERVICE 名", mod.SERVICE, "ACLClouds")
    c.eq("解析正则与原实现逐字一致（解析语义未变）",
         mod._REMAIN_RE.pattern, ORIGINAL_REMAIN_RE)
    c.check("解析正则带 IGNORECASE",
            bool(mod._REMAIN_RE.flags & re.IGNORECASE))

    # ------------------------------------------------- [B] 纯逻辑单测
    c.section("[B1] 纯逻辑：parse_remaining_hours")
    c.eq("'Temps restant: 3j 4h' -> 76h", mod.parse_remaining_hours("Temps restant: 3j 4h"), 76)
    c.eq("'Temps restant: 3j' -> 72h", mod.parse_remaining_hours("Temps restant: 3j"), 72)
    c.eq("'Temps restant : 0j 5h' -> 5h", mod.parse_remaining_hours("Temps restant : 0j 5h"), 5)
    c.eq("全大写 'TEMPS RESTANT: 1J 2H' -> 26h",
         mod.parse_remaining_hours("TEMPS RESTANT: 1J 2H"), 26)
    c.eq("夹杂其它正文也能捞出来",
         mod.parse_remaining_hours("Serveur\nTemps restant: 2j 3h\nStatut: actif"), 51)
    c.eq("无该字段 -> None", mod.parse_remaining_hours("Bienvenue, aucune info"), None)
    c.eq("空串 -> None", mod.parse_remaining_hours(""), None)
    c.eq("None 输入 -> None", mod.parse_remaining_hours(None), None)

    c.section("[B2] 纯逻辑：format_remaining")
    c.eq("76h -> '3j 4h'", mod.format_remaining(76), "3j 4h")
    c.eq("72h -> '3j 0h'", mod.format_remaining(72), "3j 0h")
    c.eq("None -> '未知'", mod.format_remaining(None), "未知")

    c.section("[B3] 纯逻辑：classify_page")
    normal_url = "https://aclclouds.com/server/f743cf50"
    c.check("Cloudflare 挑战页 -> TRANSIENT",
            mod.classify_page(normal_url, "Just a moment...") is mod.Outcome.TRANSIENT)
    c.check("CF 标记优先于正文里的 'Connexion'（顺序不变量）",
            mod.classify_page(normal_url, "Just a moment... Connexion") is mod.Outcome.TRANSIENT)
    c.check("人机验证 -> FAILED",
            mod.classify_page(normal_url, "Anti-bot confirmation required") is mod.Outcome.FAILED)
    c.check("跳登录页 -> FAILED",
            mod.classify_page("https://aclclouds.com/auth/login", "Connexion") is mod.Outcome.FAILED)
    c.check("正常页面 -> None",
            mod.classify_page(normal_url, "Temps restant: 3j 4h") is None)

    c.section("[B3b] 纯逻辑：classify_page_detail 的截图名分流")
    c.eq("CF 页 -> cf 截图",
         mod.classify_page_detail(normal_url, "Just a moment...")[1], "aclclouds-cf.png")
    c.eq("人机验证 -> antibot 截图",
         mod.classify_page_detail(normal_url, "Anti-bot confirmation")[1], "aclclouds-antibot.png")
    c.eq("掉登录 -> login 截图",
         mod.classify_page_detail("https://aclclouds.com/auth/login", "Connexion")[1],
         "aclclouds-login.png")
    c.eq("正常页 -> 空截图名",
         mod.classify_page_detail(normal_url, "Temps restant: 3j 4h")[1], "")

    c.section("[B4] 纯逻辑：window_open")
    c.eq("48h（=窗口边界）-> True", mod.window_open(48, 2), True)
    c.eq("49h -> False", mod.window_open(49, 2), False)
    c.eq("0h -> True", mod.window_open(0, 2), True)
    c.eq("None -> False（保守，不瞎点）", mod.window_open(None, 2), False)
    c.eq("显式窗口 1 天：25h -> False", mod.window_open(25, 1), False)

    # --------------------------------------------- [B5] run() 场景矩阵
    c.section("[B5] run() 场景矩阵（浏览器层全部替身）")

    # (场景, kwargs, 期望 Outcome, 期望点击次数, 期望截图名或 None)
    scenarios = [
        ("页面无内容", dict(text="", err="页面无内容（TimeoutException）"),
         mod.Outcome.TRANSIENT, 0, "aclclouds-page-empty.png"),
        ("Cloudflare 挑战页", dict(text="Just a moment..."),
         mod.Outcome.TRANSIENT, 0, "aclclouds-cf.png"),
        ("人机验证", dict(text="Anti-bot confirmation required"),
         mod.Outcome.FAILED, 0, "aclclouds-antibot.png"),
        ("会话失效跳登录",
         dict(text="Connexion", url="https://aclclouds.com/auth/login"),
         mod.Outcome.FAILED, 0, "aclclouds-login.png"),
        ("正常页但读不到 Temps restant", dict(text="Bienvenue sur le panneau"),
         mod.Outcome.UNKNOWN, 0, "aclclouds-parse-failed.png"),
        ("未到续期窗口（剩 5 天）", dict(text="Temps restant: 5j 0h"),
         mod.Outcome.SKIPPED, 0, None),
        ("dry-run（窗口已开也不点）", dict(text="Temps restant: 1j 0h", dry_run=True),
         mod.Outcome.SKIPPED, 0, None),
        ("窗口已开但找不到按钮", dict(text="Temps restant: 1j 0h", click_result=""),
         mod.Outcome.FAILED, 1, "aclclouds-button-missing.png"),
        ("窗口已开，点击并验证增加", dict(text="Temps restant: 1j 0h", after=72),
         mod.Outcome.RENEWED, 1, None),
        ("窗口已开，点击后未增加", dict(text="Temps restant: 1j 0h", after=None),
         mod.Outcome.FAILED, 1, "aclclouds-renew-failed.png"),
        ("浏览器起不来", dict(build_exc=RuntimeError("chrome not found")),
         mod.Outcome.FAILED, 0, None),
    ]

    for label, kw, want_outcome, want_clicks, want_shot in scenarios:
        report, calls = run_scenario(mod, **kw)
        r = one(report)
        ok = True
        ok &= c.check(f"{label} → {want_outcome.value}",
                      r.outcome is want_outcome,
                      f"got={r.outcome.value} detail={r.detail!r}")
        ok &= c.check(f"{label} · 点击次数 == {want_clicks}",
                      calls["click"] == want_clicks, f"got={calls['click']}")
        if want_shot is None:
            ok &= c.check(f"{label} · 无失败截图", calls["shots"] == [], f"got={calls['shots']}")
        else:
            ok &= c.check(f"{label} · 截图 {want_shot}", want_shot in calls["shots"],
                          f"got={calls['shots']}")
        if not ok:                       # 失败才回放脚本日志，方便定位
            for line in calls["out"].splitlines():
                print(f"       │ {line}")

    # --------------------------------------------- [B6] 退出码语义
    c.section("[B6] 退出码语义：只有 FAILED 才 exit 1")
    exit_cases = [
        ("Cloudflare → 不标红", dict(text="Just a moment..."), 0),
        ("未到窗口 → 不标红", dict(text="Temps restant: 5j 0h"), 0),
        ("读不到剩余 → 不标红（但记 UNKNOWN）",
         dict(text="Bienvenue"), 0),
        ("会话失效 → 标红", dict(text="Connexion", url="https://aclclouds.com/auth/login"), 1),
        ("点了没生效 → 标红", dict(text="Temps restant: 1j 0h"), 1),
    ]
    for label, kw, want_rc in exit_cases:
        report, _ = run_scenario(mod, **kw)
        c.eq(label, report.exit_code, want_rc)

    # ------------------------------------------------- [B7] main() 缺凭证
    c.section("[B7] main()：缺 ACLCLOUDS_SESSION_COOKIE 时 exit 1")
    saved_cookie, saved_tg = mod.COOKIE, os.environ.get("TG_BOT_TOKEN")
    os.environ.pop("TG_BOT_TOKEN", None)
    os.environ.pop("TG_CHAT_ID", None)
    mod.COOKIE = ""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
    finally:
        mod.COOKIE = saved_cookie
        if saved_tg is not None:
            os.environ["TG_BOT_TOKEN"] = saved_tg
    out = buf.getvalue()
    c.eq("缺凭证 -> exit 1", rc, 1)
    c.check("报告里点名缺失的变量", "ACLCLOUDS_SESSION_COOKIE" in out, out[-200:])

    # ---------------------------------------------------- [C] 静态检查
    c.section("[C1] 旧实现的坑不得回归")
    code = strip_docstrings(src)          # 只看可执行代码，docstring 里解释性提及不算
    c.check("没有硬编码 /usr/bin/chromium", "/usr/bin/chromium" not in code)
    c.check("binary_location 由 CHROMIUM 变量控制",
            "opts.binary_location = CHROMIUM" in code)
    c.check("binary_location 赋值被 `if CHROMIUM:` 包住",
            code.index("if CHROMIUM:") < code.index("opts.binary_location = CHROMIUM"))
    c.check("不再往 /tmp 丢截图", "/tmp" not in code)
    c.eq("全文件只有一处 sys.exit", code.count("sys.exit("), 1)
    c.check("该处就是 sys.exit(main())", "sys.exit(main())" in code)
    c.check("没有散落的 exit 2 / exit 3",
            "sys.exit(2)" not in code and "sys.exit(3)" not in code)
    c.check("截图目录可配置（ACLCLOUDS_SHOT_DIR）", "ACLCLOUDS_SHOT_DIR" in code)
    c.check("支持 DRY_RUN", "dry_run" in code)

    c.section("[C2] workflow 与代码对得上")
    wf = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""
    if not WORKFLOW.is_file():
        c.check("aclclouds-renew.yml 存在（README 曾声称有，实际没有）", False)
    else:
        c.check("aclclouds-renew.yml 存在", True)
        c.check("引用 scripts/aclclouds_renew.py", "scripts/aclclouds_renew.py" in wf)
        c.check("注入 ACLCLOUDS_SESSION_COOKIE", "ACLCLOUDS_SESSION_COOKIE" in wf)
        c.check("有 schedule 排程", "schedule:" in wf)
        c.check("每 12 小时一次（cron 小时位 4,16）", "4,16" in wf)
        c.check("复用 renew-kit composite action",
                "renew-kit/.github/actions/renew@" in wf)
        c.check("pip-packages 装 selenium", re.search(r"pip-packages:.*selenium", wf) is not None)
        c.check("失败产物含 *.png", "*.png" in wf)
        c.check("关掉 action 的兜底 TG（脚本自己已发，避免双推）",
                re.search(r"notify-on-failure:\s*[\"']?false", wf) is not None)
        # run #39 里 Cookie 失效的告警一条都没发出去，日志写着
        #   「Telegram 未配置（TG_BOT_TOKEN / TG_CHAT_ID），跳过通知」
        # 根因是 workflow 传 secrets.TG_BOT_TOKEN —— 本仓库没这个名字，
        # 只有 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。这里把「必须改名映射」
        # 钉死，防止再有人照着 renewkit 的环境变量名去猜 secret 名。
        c.check("TG 走 secret 改名映射（secrets.TELEGRAM_BOT_TOKEN）",
                "secrets.TELEGRAM_BOT_TOKEN" in wf)
        c.check("TG_CHAT_ID 走 secret 改名映射（secrets.TELEGRAM_CHAT_ID）",
                "secrets.TELEGRAM_CHAT_ID" in wf)
        # 只看非注释行：注释里为了说明坑，会原样写错名字，那是说明不是引用。
        # （别用 `^\s*[^#\n]` —— `\s*` 会退让一格去匹配那个空格，注释行照样命中。）
        _bad = [ln for ln in wf.splitlines()
                if "secrets.TG_" in ln and not ln.lstrip().startswith("#")]
        c.check("不再引用不存在的 secrets.TG_*", not _bad, str(_bad[:2]))

    c.section("[C3] README 与事实对得上")
    rd = README.read_text(encoding="utf-8") if README.is_file() else ""
    if not README.is_file():
        c.check("README.md 存在", False)
    else:
        c.check("README.md 存在", True)
        c.check("ACLClouds 行写明每 12 小时", "每 12 小时" in rd)
        c.check("README 引用 aclclouds-renew.yml", "aclclouds-renew.yml" in rd)
        c.check("不再吹『绕过 Cloudflare Turnstile』", "绕过 Cloudflare Turnstile" not in rd)
        c.check("Monkey 服务器 ID 走 secret，不再写死",
                "MONKEY_SERVER_IDENTIFIER" in rd and "服务器 ID `2533c753`" not in rd)
        c.check("说明了 renew-kit 依赖", "renew-kit" in rd)
        mentioned = set(re.findall(r"[\w.-]+\.yml", rd))
        bogus = mentioned - KNOWN_WORKFLOWS
        c.check("README 引用的 workflow 都真实存在", not bogus, f"不存在的: {sorted(bogus)}")

    c.section("[C4] renew-kit 版本 pin 三处一致")
    def _grab(pattern, text):
        m = re.search(pattern, text)
        return m.group(1) if m else None
    pin_uses = _grab(r"actions/renew@([\w.]+)", wf)
    pin_ref = _grab(r"renewkit-ref:\s*([\w.]+)", wf)
    pin_readme = _grab(r"renew-kit@([\w.]+)", rd)
    c.check("三处 pin 都解析到了", all((pin_uses, pin_ref, pin_readme)),
            f"uses={pin_uses} ref={pin_ref} readme={pin_readme}")
    c.eq("action 版本 == renewkit-ref", pin_uses, pin_ref)
    c.eq("action 版本 == README 里的 pin", pin_uses, pin_readme)

    return c.report()


if __name__ == "__main__":
    sys.exit(main())
