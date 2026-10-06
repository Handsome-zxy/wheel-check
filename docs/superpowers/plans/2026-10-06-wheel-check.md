# wheel-check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个跨宿主可用的 `wheel-check` skill：开发前检索开源先例，有相关则结构化呈现并询问 a/b，无相关则静默跳过，检索失败则出声报因。

**Architecture:** 三层分离——`SKILL.md`（契约层：触发/静默/询问/能力阶梯，不含 API 细节）、`scripts/probe.py`（数据层：纯 stdlib，只输出硬事实，不做语义判断）、`references/*.md`（知识层：判定标准、查询模板、措辞）。契约层与数据层解耦，将来可换成 MCP 数据层而不改 SKILL.md。

**Tech Stack:** Python 3.8+ 标准库（`urllib` / `json` / `argparse` / `unittest` / `unittest.mock`），零 pip 依赖；Markdown 作为 skill 载体。

**Spec:** `docs/wheel-check-design.md`（v2，已获用户批准）

## Global Constraints

- 纯 Python 标准库，**禁止任何 pip 依赖**（含 pytest；测试用 `unittest`）。
- `probe.py` **绝不做**语义判断、相关性打分、排名决策；只返回硬事实与所用过滤档位。
- 输出契约的唯一"真没有"信号是 `status: "ok"` **且** `results: []`；任何其他组合模型都必须出声。
- 退出码：`0` = ok，`2` = degraded，`3` = bad-args。
- 所有 stdout 必须 UTF-8（`sys.stdout.reconfigure(encoding="utf-8")`）；错误信息走 stderr。
- 单请求超时 15 秒；重试不超过 2 次；总预算 ≤45 秒 / ≤3 次检索。
- 未认证 GitHub search 配额 **10 次/分钟**——测试必须 mock 网络，禁止在测试里打真实 API。
- 术语必须是**概念名词**，长短语会杀光召回（实测：`test impact analysis` total=3 全垃圾 vs `test selection` 命中 526★）。
- 仓库根：本仓库根目录；skill 载体目录：`skills/wheel-check/`。

## Review Focus

以下五类输入/失败模式最容易伤到真实用户，每一条都在其归属任务里配了测试：

1. **`--terms` 传空或全是分隔符** → 必须 `bad-args` 退出码 3 且 stderr 有说明，**绝不能**退化成"查了但没结果"（那会让 skill 静默跳过，是本项目最严重的错误）。
2. **GitHub 强过滤返回 0 条但 HN 有命中** → 必须返回 HN 的 `kind: "discussion"` 条目，不得因为 GitHub 为空就整体返回空数组。
3. **非 ASCII（中文）term 在 Windows GBK 控制台下** → 必须正常输出 UTF-8，不得 `UnicodeEncodeError` 崩溃。
4. **GitHub 403 限流** → 必须重试一次后返回 `status: degraded` + `reason: rate-limited`（退出码 2），**不得**返回 `ok` + 空数组。
5. **`--source hf` 但需求与 AI 无关** → `auto` 模式下不得调用 HF；显式 `--source hf` 时才调用。

---

### Task 1: 仓库骨架与测试基座

**Files:**
- Create: `skills/wheel-check/scripts/probe.py`
- Create: `tests/test_probe.py`
- Create: `tests/__init__.py`（空文件）
- Modify: `.gitignore`（已含 `__pycache__/`，确认即可）

**Interfaces:**
- Consumes: 无
- Produces: `probe.py` 模块可被 `tests/test_probe.py` 以 `importlib` 从 `skills/wheel-check/scripts/` 载入；模块级常量 `EXIT_OK = 0`、`EXIT_DEGRADED = 2`、`EXIT_BAD_ARGS = 3`。

- [ ] **Step 1: 写失败的测试（模块可导入 + 退出码常量）**

```python
# tests/test_probe.py
import importlib.util, pathlib, unittest

PROBE = pathlib.Path(__file__).resolve().parents[1] / "skills" / "wheel-check" / "scripts" / "probe.py"

def load_probe():
    spec = importlib.util.spec_from_file_location("probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class TestContract(unittest.TestCase):
    def test_module_exposes_exit_codes(self):
        p = load_probe()
        self.assertEqual((p.EXIT_OK, p.EXIT_DEGRADED, p.EXIT_BAD_ARGS), (0, 2, 3))

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_probe -v`（在仓库根执行）
Expected: FAIL —— `FileNotFoundError` 或 `ModuleNotFoundError`（`probe.py` 尚不存在）

- [ ] **Step 3: 写最小实现**

```python
# skills/wheel-check/scripts/probe.py
#!/usr/bin/env python3
"""wheel-check data layer: GitHub / HN / HuggingFace retrieval. Hard facts only."""
import sys

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_BAD_ARGS = 3

def main(argv=None):
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_probe -v`
Expected: PASS（1 test）

- [ ] **Step 5: 提交**

```bash
git add tests skills/wheel-check/scripts/probe.py
git commit -m "feat(probe): add module skeleton with exit-code contract"
```

---

### Task 2: UTF-8 输出与参数校验

**Files:**
- Modify: `skills/wheel-check/scripts/probe.py`
- Modify: `tests/test_probe.py`

**Interfaces:**
- Consumes: Task 1 的 `EXIT_*` 常量
- Produces: `probe.build_parser() -> argparse.ArgumentParser`；`probe.validate_terms(raw: str) -> list[str]`（返回清洗后的 term 列表；空则抛 `ValueError`）；`probe.force_utf8()`。

- [ ] **Step 1: 写失败的测试**

```python
class TestValidateTerms(unittest.TestCase):
    def test_splits_and_strips_pipe_separated_terms(self):
        p = load_probe()
        self.assertEqual(p.validate_terms("test selection| affected tests "),
                         ["test selection", "affected tests"])

    def test_empty_string_raises(self):
        p = load_probe()
        with self.assertRaises(ValueError):
            p.validate_terms("")

    def test_only_separators_raises(self):
        p = load_probe()
        with self.assertRaises(ValueError):
            p.validate_terms("|  |")

class TestCliBadArgs(unittest.TestCase):
    def test_empty_terms_exits_3(self):
        p = load_probe()
        self.assertEqual(p.main(["search", "--terms", ""]), 3)

    def test_bad_source_exits_3(self):
        p = load_probe()
        self.assertEqual(p.main(["search", "--terms", "x", "--source", "nope"]), 3)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_probe -v`
Expected: FAIL —— `AttributeError: module 'probe' has no attribute 'validate_terms'`

- [ ] **Step 3: 写最小实现**

```python
def force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

def validate_terms(raw):
    terms = [t.strip() for t in (raw or "").split("|")]
    terms = [t for t in terms if t]
    if not terms:
        raise ValueError("--terms must contain at least one non-empty term")
    return terms

def build_parser():
    ap = argparse.ArgumentParser(prog="probe.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    s = sub.add_parser("search")
    s.add_argument("--terms", required=True)
    s.add_argument("--source", default="auto", choices=["auto", "github", "hn", "hf"])
    s.add_argument("--strict", action="store_true")
    s.add_argument("--limit", type=int, default=8)
    r = sub.add_parser("repo")
    r.add_argument("full_name")
    return ap

def main(argv=None):
    force_utf8()
    args = build_parser().parse_args(argv)
    if args.cmd == "search":
        try:
            validate_terms(args.terms)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return EXIT_BAD_ARGS
    return EXIT_OK
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_probe -v`
Expected: PASS（5 tests）

- [ ] **Step 5: 提交**

```bash
git add tests skills/wheel-check/scripts/probe.py
git commit -m "feat(probe): add utf-8 output, term validation and CLI surface"
```

---

### Task 3: GitHub 检索与三层过滤

**Files:**
- Modify: `skills/wheel-check/scripts/probe.py`
- Modify: `tests/test_probe.py`

**Interfaces:**
- Consumes: Task 2 的 `build_parser()` / `validate_terms()`
- Produces:
  - `probe.github_tiers() -> list[tuple[str, str]]` —— 三档 `(tier_name, filter_suffix)`，顺序为 `("strict", " stars:>50 pushed:>{18mo} fork:false")`、`("relaxed", " stars:>50 fork:false")`、`("plain", "")`
  - `probe.github_search(terms: list[str], limit: int, fetch=urlopen_json) -> dict` —— 返回 `{"tier": str, "total": int, "results": [repo_dict]}`；`fetch` 可注入以便测试
  - `probe.normalize_repo(item: dict) -> dict` —— 产出 `kind: "repo"` 的规范条目（`full_name/url/stars/pushed_at/license/archived/language/description`）

- [ ] **Step 1: 写失败的测试**

```python
FAKE_REPO = {
    "full_name": "Tinder/bazel-diff", "html_url": "https://github.com/Tinder/bazel-diff",
    "stargazers_count": 526, "pushed_at": "2026-10-06T04:00:00Z",
    "license": {"spdx_id": "Apache-2.0"}, "archived": False,
    "language": "Java", "description": "Incremental build/test selection",
}

class TestGithubTiers(unittest.TestCase):
    def test_strict_tier_hit_wins(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            return {"total_count": 1, "items": [FAKE_REPO]}
        out = p.github_search(["test selection"], 8, fetch=fetch)
        self.assertEqual(out["tier"], "strict")
        self.assertEqual(len(calls), 1)
        self.assertIn("stars%3A%3E50", calls[0].replace(":", "%3A"))
        self.assertEqual(out["results"][0]["full_name"], "Tinder/bazel-diff")
        self.assertEqual(out["results"][0]["kind"], "repo")

    def test_falls_through_to_relaxed_then_plain(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            if len(seen) == 1:
                return {"total_count": 0, "items": []}
            if len(seen) == 2:
                return {"total_count": 0, "items": []}
            return {"total_count": 1, "items": [FAKE_REPO]}
        out = p.github_search(["test selection"], 8, fetch=fetch)
        self.assertEqual(out["tier"], "plain")
        self.assertEqual(len(seen), 3)

    def test_all_tiers_empty_returns_ok_empty(self):
        p = load_probe()
        out = p.github_search(["nonsense"], 8, fetch=lambda url, **kw: {"total_count": 0, "items": []})
        self.assertEqual(out["tier"], "none")
        self.assertEqual(out["results"], [])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_probe -v`
Expected: FAIL —— `AttributeError: module 'probe' has no attribute 'github_search'`

- [ ] **Step 3: 写最小实现**

关键点：一次查询内用括号 OR 合并 terms；每档只发**一次**请求（配额 10/分钟）；`fetch` 默认走 `urlopen_json`，测试注入假实现。

```python
import json, urllib.parse, urllib.request
from datetime import date, timedelta

UA = {"User-Agent": "wheel-check", "Accept": "application/vnd.github+json"}

def _pushed_floor(months=18):
    return (date.today() - timedelta(days=30 * months)).isoformat()

def GITHUB_TIERS():
    return [
        ("strict",  " stars:>50 pushed:>%s fork:false" % _pushed_floor()),
        ("relaxed", " stars:>50 fork:false"),
        ("plain",   ""),
    ]

def urlopen_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def build_query(terms, suffix):
    q = " OR ".join('"%s"' % t for t in terms) if len(terms) > 1 else terms[0]
    if len(terms) > 1:
        q = "(%s)" % q
    return q + suffix

def normalize_repo(item):
    return {
        "kind": "repo",
        "full_name": item.get("full_name"),
        "url": item.get("html_url"),
        "stars": item.get("stargazers_count"),
        "pushed_at": (item.get("pushed_at") or "")[:10],
        "license": (item.get("license") or {}).get("spdx_id"),
        "archived": bool(item.get("archived")),
        "language": item.get("language"),
        "description": item.get("description"),
    }

def github_search(terms, limit, fetch=urlopen_json, strict_only=False):
    tiers = GITHUB_TIERS()[:1] if strict_only else GITHUB_TIERS()
    for tier, suffix in tiers:
        url = ("https://api.github.com/search/repositories?q="
               + urllib.parse.quote(build_query(terms, suffix))
               + "&sort=stars&order=desc&per_page=%d" % limit)
        data = fetch(url)
        items = data.get("items") or []
        if items:
            return {"tier": tier, "total": data.get("total_count", 0),
                    "results": [normalize_repo(i) for i in items]}
    return {"tier": "none", "total": 0, "results": []}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_probe -v`
Expected: PASS（8 tests）

- [ ] **Step 5: 提交**

```bash
git add tests skills/wheel-check/scripts/probe.py
git commit -m "feat(probe): add tiered GitHub search (strict -> relaxed -> plain)"
```

---

### Task 4: 限流处理与 degraded 状态

**Files:**
- Modify: `skills/wheel-check/scripts/probe.py`
- Modify: `tests/test_probe.py`

**Interfaces:**
- Consumes: Task 3 的 `github_search(..., fetch=...)`
- Produces:
  - `probe.RateLimited(Exception)`
  - `probe.Timeout(Exception)`
  - `probe.search_envelope(terms, source="auto", limit=8, fetch=urlopen_json) -> tuple[int, dict]` —— 返回 `(exit_code, envelope)`；envelope 含 `status / reason / sources / results`
  - `probe.EMPTY_OK(terms)` 语义：`status="ok"` 且 `results == []` 是唯一"真没有"信号

- [ ] **Step 1: 写失败的测试**

```python
class TestDegraded(unittest.TestCase):
    def test_rate_limited_after_retry_is_degraded(self):
        p = load_probe()
        calls = []
        def fetch(url, **kw):
            calls.append(url)
            raise p.RateLimited("403")
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, 2)
        self.assertEqual(env["status"], "degraded")
        self.assertEqual(env["reason"], "rate-limited")
        self.assertEqual(env["results"], [])
        self.assertEqual(len(calls), 2)   # 原始 + 1 次重试，不超过 2 次

    def test_transient_then_ok(self):
        p = load_probe()
        state = {"n": 0}
        def fetch(url, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise p.RateLimited("403")
            return {"total_count": 1, "items": [FAKE_REPO]}
        code, env = p.search_envelope(["x"], source="github", fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(len(env["results"]), 1)

    def test_empty_is_ok_and_not_degraded(self):
        p = load_probe()
        code, env = p.search_envelope(["x"], source="github",
                                      fetch=lambda url, **kw: {"total_count": 0, "items": []})
        self.assertEqual(code, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["results"], [])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_probe -v`
Expected: FAIL —— `AttributeError: module 'probe' has no attribute 'RateLimited'`

- [ ] **Step 3: 写最小实现**

```python
import time, urllib.error

class RateLimited(Exception): pass
class Timeout(Exception): pass

def _classify(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return RateLimited(str(exc.code))
        return Timeout("http-%s" % exc.code)
    return Timeout(type(exc).__name__)

def _with_retry(fn, attempts=2, backoff=1.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:          # noqa: BLE001 - 分类后统一处理
            last = _classify(e)
            if isinstance(last, RateLimited) and i + 1 < attempts:
                time.sleep(backoff)
                continue
            raise last
    raise last

def search_envelope(terms, source="auto", limit=8, fetch=urlopen_json, strict_only=False):
    def call():
        return github_search(terms, limit, fetch=fetch, strict_only=strict_only)
    try:
        gh = _with_retry(call)
    except RateLimited as e:
        return EXIT_DEGRADED, {"status": "degraded", "reason": "rate-limited",
                               "sources": [{"source": "github", "status": "degraded"}],
                               "results": []}
    except Timeout as e:
        return EXIT_DEGRADED, {"status": "degraded", "reason": "timeout",
                               "sources": [{"source": "github", "status": "degraded"}],
                               "results": []}
    return EXIT_OK, {"status": "ok", "reason": None,
                     "sources": [{"source": "github", "tier": gh["tier"],
                                  "total": gh["total"], "status": "ok"}],
                     "results": gh["results"]}
```

并把 `main()` 的 `search` 分支接到 `search_envelope`，`print(json.dumps(env, ensure_ascii=False))` 后返回 `code`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_probe -v`
Expected: PASS（11 tests）

- [ ] **Step 5: 提交**

```bash
git add tests skills/wheel-check/scripts/probe.py
git commit -m "feat(probe): add retry, degraded status and envelope contract"
```

---

### Task 5: Hacker News 补充源

**Files:**
- Modify: `skills/wheel-check/scripts/probe.py`
- Modify: `tests/test_probe.py`

**Interfaces:**
- Consumes: Task 4 的 `search_envelope(...)`
- Produces: `probe.hn_search(terms, limit, fetch=urlopen_json) -> list[dict]`，条目形如
  `{"kind": "discussion", "title": str, "url": "https://news.ycombinator.com/item?id=...", "points": int, "created_at": "YYYY-MM-DD", "external_url": str|None}`

- [ ] **Step 1: 写失败的测试**

```python
FAKE_HN = {"nbHits": 1, "hits": [{
    "title": "Show HN: Testrim – Intelligent Test Impact Analysis",
    "objectID": "47382910", "points": 42, "created_at": "2026-03-11T10:00:00Z",
    "url": "https://github.com/example/testrim",
}]}

class TestHackerNews(unittest.TestCase):
    def test_hn_item_is_normalized_as_discussion(self):
        p = load_probe()
        out = p.hn_search(["incremental test selection"], 5, fetch=lambda url, **kw: FAKE_HN)
        self.assertEqual(len(out), 1)
        item = out[0]
        self.assertEqual(item["kind"], "discussion")
        self.assertEqual(item["points"], 42)
        self.assertEqual(item["created_at"], "2026-03-11")
        self.assertEqual(item["url"], "https://news.ycombinator.com/item?id=47382910")
        self.assertEqual(item["external_url"], "https://github.com/example/testrim")
        self.assertNotIn("stars", item)          # HN 不提供硬事实
        self.assertNotIn("license", item)

    def test_hn_used_when_github_is_empty(self):
        p = load_probe()
        def fetch(url, **kw):
            if "hn.algolia.com" in url:
                return FAKE_HN
            return {"total_count": 0, "items": []}
        code, env = p.search_envelope(["incremental test selection"], source="auto", fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual([r["kind"] for r in env["results"]], ["discussion"])

    def test_hn_not_called_when_github_has_hits(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            if "hn.algolia.com" in url:
                return FAKE_HN
            return {"total_count": 1, "items": [FAKE_REPO]}
        p.search_envelope(["test selection"], source="auto", fetch=fetch)
        self.assertFalse(any("hn.algolia.com" in u for u in seen))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_probe -v`
Expected: FAIL —— `AttributeError: module 'probe' has no attribute 'hn_search'`

- [ ] **Step 3: 写最小实现**

```python
def hn_search(terms, limit, fetch=urlopen_json):
    q = " ".join(terms)
    url = ("https://hn.algolia.com/api/v1/search?query=" + urllib.parse.quote(q)
           + "&tags=story&hitsPerPage=%d" % limit)
    data = fetch(url)
    out = []
    for h in data.get("hits", []):
        oid = h.get("objectID")
        out.append({
            "kind": "discussion",
            "title": h.get("title"),
            "url": "https://news.ycombinator.com/item?id=%s" % oid,
            "points": h.get("points"),
            "created_at": (h.get("created_at") or "")[:10],
            "external_url": h.get("url"),
        })
    return out
```

并在 `search_envelope` 里：GitHub 结果为**空**时（`gh["results"] == []`）追加 `hn_search`，`sources` 里加一条 `{"source": "hn", "status": "ok", "total": len(hn)}`；HN 抛异常则该项标 `"status": "degraded"` 但**不**影响整体 `ok`（单源失败不算整体失败），同时把 `env["reason"]` 设为 `"partial-source-failure"`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_probe -v`
Expected: PASS（14 tests）

- [ ] **Step 5: 提交**

```bash
git add tests skills/wheel-check/scripts/probe.py
git commit -m "feat(probe): add Hacker News supplement source for concept-phrase recall"
```

---

### Task 6: Hugging Face 条件源与 repo 查询

**Files:**
- Modify: `skills/wheel-check/scripts/probe.py`
- Modify: `tests/test_probe.py`

**Interfaces:**
- Consumes: Task 5 的 `search_envelope(...)`
- Produces:
  - `probe.is_ai_requirement(terms: list[str]) -> bool` —— 命中 `AI_TERMS` 中任一关键词即 True
  - `probe.AI_TERMS: set[str]` —— `{"ai","llm","model","embedding","diffusion","speech","vision","transcribe","whisper","fine-tune","finetune","rag","agent"}`
  - `probe.hf_search(terms, limit, fetch=urlopen_json) -> list[dict]`，条目 `{"kind": "model", "model_id", "downloads", "likes", "url"}`
  - `probe.repo_detail(full_name, fetch=urlopen_json) -> dict` —— 单仓库硬事实（用于补全 HN 命中）

- [ ] **Step 1: 写失败的测试**

```python
FAKE_HF = [{"modelId": "openai/whisper-large-v3", "downloads": 2400000, "likes": 4100}]

class TestHuggingFaceGate(unittest.TestCase):
    def test_ai_requirement_detected(self):
        p = load_probe()
        self.assertTrue(p.is_ai_requirement(["local speech to text"]))
        self.assertFalse(p.is_ai_requirement(["incremental test selection"]))

    def test_hf_queried_only_for_ai_terms(self):
        p = load_probe()
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            if "huggingface.co" in url:
                return FAKE_HF
            return {"total_count": 0, "items": []}
        p.search_envelope(["incremental test selection"], source="auto", fetch=fetch)
        self.assertFalse(any("huggingface.co" in u for u in seen))
        seen.clear()
        p.search_envelope(["local speech to text"], source="auto", fetch=fetch)
        self.assertTrue(any("huggingface.co" in u for u in seen))

    def test_hf_item_is_normalized_as_model(self):
        p = load_probe()
        out = p.hf_search(["speech to text"], 5, fetch=lambda url, **kw: FAKE_HF)
        self.assertEqual(out[0]["kind"], "model")
        self.assertEqual(out[0]["model_id"], "openai/whisper-large-v3")
        self.assertEqual(out[0]["url"], "https://huggingface.co/openai/whisper-large-v3")
        self.assertEqual(out[0]["downloads"], 2400000)

    def test_repo_detail_returns_hard_facts(self):
        p = load_probe()
        out = p.repo_detail("Tinder/bazel-diff", fetch=lambda url, **kw: FAKE_REPO)
        self.assertEqual(out["kind"], "repo")
        self.assertEqual(out["stars"], 526)
        self.assertEqual(out["license"], "Apache-2.0")

    def test_repo_detail_rejects_bad_full_name(self):
        p = load_probe()
        self.assertEqual(p.main(["repo", "not-a-full-name"]), 3)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_probe -v`
Expected: FAIL —— `AttributeError: module 'probe' has no attribute 'is_ai_requirement'`

- [ ] **Step 3: 写最小实现**

```python
AI_TERMS = {"ai", "llm", "model", "embedding", "diffusion", "speech", "vision",
            "transcribe", "whisper", "fine-tune", "finetune", "rag", "agent"}

def is_ai_requirement(terms):
    words = set()
    for t in terms:
        words.update(w.strip(".,()[]").lower() for w in t.split())
    return bool(words & AI_TERMS)

def hf_search(terms, limit, fetch=urlopen_json):
    q = " ".join(terms)
    url = ("https://huggingface.co/api/models?search=" + urllib.parse.quote(q)
           + "&limit=%d&sort=downloads&direction=-1" % limit)
    data = fetch(url)
    return [{"kind": "model", "model_id": m.get("modelId") or m.get("id"),
             "downloads": m.get("downloads"), "likes": m.get("likes"),
             "url": "https://huggingface.co/%s" % (m.get("modelId") or m.get("id"))}
            for m in (data or [])]

def repo_detail(full_name, fetch=urlopen_json):
    url = "https://api.github.com/repos/" + full_name
    return normalize_repo(fetch(url))
```

`main()` 的 `repo` 分支：`full_name` 不含 `/` 则 stderr 报错并返回 `EXIT_BAD_ARGS`。
`search_envelope` 中：`source in ("auto","hf")` 且 `is_ai_requirement(terms)` 为真时调用 `hf_search`，结果**追加**（不替换 GitHub 结果）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_probe -v`
Expected: PASS（19 tests）

- [ ] **Step 5: 提交**

```bash
git add tests skills/wheel-check/scripts/probe.py
git commit -m "feat(probe): add gated HuggingFace source and repo detail lookup"
```

---

### Task 7: RED 基线 —— 先看它失败

**Files:**
- Create: `evals/cases.jsonl`
- Create: `evals/results-RED.md`

**Interfaces:**
- Consumes: 无（本任务**不写 skill**）
- Produces: 8 条场景提示的基线行为记录

- [ ] **Step 1: 写用例集**

```jsonl
{"id":"c1","prompt":"我想做个工具，改完文件后只跑受影响的测试","expect":"speak","note":"应命中 test selection 类项目"}
{"id":"c2","prompt":"帮我写个终端里的 markdown 阅读器","expect":"speak","note":"应命中 glow 等"}
{"id":"c3","prompt":"我打算做一个提交前扫描密钥泄露的钩子","expect":"speak","note":"gitleaks/trufflehog 类"}
{"id":"c4","prompt":"我想做个本地语音转文字的小工具","expect":"speak","note":"AI 类，应触发 HF 条件源"}
{"id":"c5","prompt":"做一个把我们公司内部的报销审批流程搬到网页上的系统","expect":"silent","note":"假需求：无对应开源项目"}
{"id":"c6","prompt":"写个脚本把我这台机器上 2019 年的照片按人脸分组归档","expect":"silent","note":"假需求"}
{"id":"c7","prompt":"订单页面在 Safari 上点提交没反应，帮我修一下","expect":"no-trigger","note":"bug 修复，不应触发"}
{"id":"c8","prompt":"帮我把 src/utils.ts 里的 formatDate 函数改成用 dayjs","expect":"no-trigger","note":"改现有代码，不应触发"}
```

- [ ] **Step 2: 派干净 subagent 跑 c1–c8（不安装任何 skill）**

对每条用例派一个**全新 subagent**，prompt 仅含用例原文 + 一句 `请按你的判断处理这个请求。`，并**不**提供任何 wheel-check 提示。要求它输出：① 是否做了先例检索 ② 若做了，检索了什么、发现了什么 ③ 最终回复用户的完整文字。

- [ ] **Step 3: 记录基线行为（原话摘录）**

在 `evals/results-RED.md` 里逐条记录：是否检索、关键词是什么、是否编造 star 数、是否在"无相关"时仍然啰嗦输出、是否在 bug 修复场景也去检索。**这一节必须包含 subagent 的原话**，不得概括。

- [ ] **Step 4: 归纳失败模式**

列出基线中出现的具体失败（预期会看到：直接从零开始写代码、把"没有明确搜到"说成"这个想法很新"、编造或不给出处、对 c7/c8 也做无谓检索）。这些失败模式**决定 SKILL.md 要写什么**。

- [ ] **Step 5: 提交**

```bash
git add evals/cases.jsonl evals/results-RED.md
git commit -m "test(evals): add RED baseline scenarios and observed baseline behavior"
```

---

### Task 8: GREEN —— SKILL.md 与 references

**Files:**
- Create: `skills/wheel-check/SKILL.md`
- Create: `skills/wheel-check/references/triage-rubric.md`
- Create: `skills/wheel-check/references/query-patterns.md`
- Create: `skills/wheel-check/references/verdicts.md`
- Create: `skills/wheel-check/references/reuse-map.md`
- Create: `evals/results-GREEN.md`

**Interfaces:**
- Consumes: Task 7 归纳的失败模式（SKILL.md 必须逐条针对）
- Produces: 可被任何宿主加载的 skill 目录

- [ ] **Step 1: 写 SKILL.md（针对 Task 7 的失败模式，最小充分）**

frontmatter 的 description **只写何时使用、不写流程**（writing-skills 的 SDO 要求）：

```yaml
---
name: wheel-check
description: Use when the user is about to build something new — a project, repo,
  skill, plugin, subsystem or tool — before any code is written. Searches open-source
  communities for projects that already solve the same problem and returns a short
  structured list, or stays completely silent when nothing relevant exists. Do not
  use for bug fixes, refactors, or edits to existing code.
---
```

正文必须含（每条对应一个已观测的失败模式）：
1. 触发/不触发清单（对应 c7/c8 误触发）；
2. 三步流程 + 能力阶梯（L1 probe.py → L2 宿主 HTTP → L3 web_search 且标注"未核实"）（对应编造数字）；
3. **静默契约**：`status:ok` + 空 `results` → 只回一句"未发现可直接复用方案"，随后立即继续开发、不询问（对应 c5/c6 啰嗦）；
4. **出声契约**：`degraded` → 必须报原因（对应"把没搜成说成没有"）；
5. 结构化清单五字段 + a/b 询问（对应"不给用户选择"）；
6. Stop-and-Ask 四条（spec §7）；
7. 反规避表（Task 9 迭代填充）。

- [ ] **Step 2: 写四个 references**

- `triage-rubric.md`：什么算"真正解决同一问题"（共享领域不算）、成熟度证据字段、排除专利/代码抄袭类；
- `query-patterns.md`：去品牌化三步 + 中→英术语映射 + **概念名词优先规则**（含实测反例：`test impact analysis` 全灭 vs `test selection` 命中）+ 重试链顺序；
- `verdicts.md`：三种终态的措辞模板（清单 / 静默一句话 / degraded 报错）；
- `reuse-map.md`：spec §8.1 的借鉴映射表原文。

- [ ] **Step 3: 安装到 DSH 并跑 GREEN**

```powershell
# 用目录联接把仓库里的 skill 暴露给 DSH（Windows 上 junction 无需管理员权限；可随时删除，不复制文件）
New-Item -ItemType Junction -Path "$env:USERPROFILE\.dsh\skills\wheel-check" `
         -Target ".\skills\wheel-check"
```

（`-ItemType SymbolicLink` 在 Windows 上需要管理员权限，**不要用**；类 Unix 宿主用 `ln -s`。）

同 Task 7 的 8 条用例，**同样派干净 subagent**，此时 skill 可被发现。（若某宿主不支持 junction，退回"拷贝到 skill 目录"。）

- [ ] **Step 4: 记录 GREEN 行为到 `evals/results-GREEN.md`**

逐条对比 RED：c1–c4 是否出声并给 a/b、c5–c6 是否静默、c7–c8 是否不触发、数字是否有出处。

- [ ] **Step 5: 提交**

```bash
git add skills/wheel-check evals/results-GREEN.md
git commit -m "feat(skill): add wheel-check contract, references and GREEN results"
```

---

### Task 9: REFACTOR —— 堵规避话术

**Files:**
- Modify: `skills/wheel-check/SKILL.md`
- Modify: `evals/results-GREEN.md`

**Interfaces:**
- Consumes: Task 8 的 GREEN 结果
- Produces: 更严的 SKILL.md

- [ ] **Step 1: 从 GREEN 结果里提取新的规避话术**（例："这次改动很小，跳过检索""我已经知道答案了""用户只是随口一说"）

- [ ] **Step 2: 对每条规避话术在 SKILL.md 加一条显式反制**（写进"反规避"表），并对形塑类问题用"正向配方"而非"禁止清单"

- [ ] **Step 3: 重跑失败的那几条用例，验证已堵住**

- [ ] **Step 4: 把结果追加到 `evals/results-GREEN.md`**，并计算静默正确率 / 出声正确率 / 触发正确率

- [ ] **Step 5: 提交**

```bash
git add skills/wheel-check/SKILL.md evals/results-GREEN.md
git commit -m "refactor(skill): close rationalization loopholes found in GREEN run"
```

---

### Task 10: README、LICENSE 与安装说明

**Files:**
- Create: `README.md`（中英双语，中文在前）
- Create: `LICENSE`（MIT）
- Modify: `docs/wheel-check-design.md`（若实现与 spec 有偏差，追加"实现偏差记录"一节）

**Interfaces:**
- Consumes: 全部前置任务
- Produces: 可分发仓库

- [ ] **Step 1: 写 README**：一句话定位、安装（3 种宿主）、触发场景、静默契约、隐私与配额说明、已知限制（含实测数字）、与同类项目的差异（借 spec §1.3 的四个空白）

- [ ] **Step 2: 加 MIT LICENSE**

- [ ] **Step 3: 端到端验证**：从干净目录把 `skills/wheel-check/` 拷进一个新的 skill 目录，跑 `probe.py health` 与一次 `search`，确认零依赖可用

- [ ] **Step 4: 记录实现偏差**（若有）到 spec 末尾

- [ ] **Step 5: 提交**

```bash
git add README.md LICENSE docs/wheel-check-design.md
git commit -m "docs: add bilingual README, MIT license and install notes"
```

---

## 自检记录

**1. Spec 覆盖**：§4.1 契约 → Task 8；§4.2/4.3 CLI 与 envelope → Task 2/4；§5.1 能力阶梯 → Task 8 Step 1 第 2 条；§5.3 数据流（含重试链）→ Task 3/5/6；§5.5 源策略 → Task 5/6；§6 异常 → Task 4；§7 Stop-and-Ask → Task 8 Step 1 第 6 条；§8 借鉴 → Task 8 Step 2；§10 验证 → Task 7/8/9；§11 M2 验收 → Task 9 Step 4。

**2. 占位符扫描**：无 TBD/TODO；每个代码步骤都给了可执行代码。

**3. 类型一致性**：`search_envelope` 签名（Task 4 定义）在 Task 5/6 复用一致；`normalize_repo`（Task 3）被 Task 6 的 `repo_detail` 复用；`kind` 取值 `repo|discussion|model` 与 spec §4.3 一致。

**4. Review Focus**：五条风险已按计划头部的 Review Focus 段落要求，分别落在 Task 2（空 terms）、Task 5（GitHub 空 → HN）、Task 2（UTF-8）、Task 4（限流）、Task 6（HF 门控）。
