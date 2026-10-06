# wheel-check

> **动手前先查开源先例：** 在写第一行代码之前，先真查一次开源社区——有真正解决同一问题的项目就给一份带证据的短清单并请你选 a/b；确实没有就回一句"未发现可直接复用的开源方案。"然后立刻继续原任务；**查失败则出声说明**，绝不假装查过。
>
> **Before you build it, check whether someone already did.** A portable agent skill (plain text + one zero-dependency Python script) that searches GitHub / Hacker News / Hugging Face for projects already solving the same problem — and speaks up when the search itself failed.

- 设计文档（binding spec）：[`docs/wheel-check-design.md`](docs/wheel-check-design.md) · v2.2
- 行为证据：[`evals/`](evals/) （`cases.jsonl` 10 条用例 + RED/GREEN 记录）
- 实现与 spec 的偏差：[`docs/wheel-check-design.md` §13 实现偏差记录](docs/wheel-check-design.md)
- 决策记录（每条裁决的理由与"判断错了要付什么代价"）：[`docs/rulings.md`](docs/rulings.md)
- 许可：MIT · 2026

---

## 中文

### 1. 一句话定位

当用户要**新建**一样东西（项目、仓库、skill、plugin、子系统、工具）而代码还没开始写时，`wheel-check` 先做一次真实检索，然后只说三件事之一：**有对口先例**（五字段清单 + 证据 + a/b 询问）、**确实没有**（一句话，静默继续）、**没查成**（出声说明哪个源没应答）。

分工是刻意的：**脚本保证数字是真的，模型保证判断是准的**——`probe.py` 只回硬事实（stars / 最后一次推送（`pushed_at`）/ license / archived / 链接）与所用过滤档位，不做任何相关性判断、打分或排名；"是不是同一个问题"由模型按 `references/triage-rubric.md` 判定。

### 2. 它解决什么问题：与同类工具的四处不同

问题本身：没有任何机制会强制 agent 在动手前查"这件事别人是不是已经成熟地做完了"，结果是重复造轮子（design.md §1.1）。同类工具大多反过来做——用模型猜 star 数、用关键词猜相关性，并把"没搜成"与"没搜到"混为一谈。本 skill 的差异来自 design.md §1.3 的"明确不做"清单：

1. **不打分。** 没有 0–100 评分、评分卡、趋势或 pivot 建议。§1.3 的理由：评分正是现有同类工具"打断人"的根源。这里只给事实与"是不是同一问题"的判定。
2. **不阻断、不代劳。** 不否决用户的开发决定，不自动改代码、不自动装依赖。清单是**供料**：本轮以 a/b 询问收束，动手发生在用户选定之后。
3. **不扇出包注册表。** 不查 10–19 个注册表（实测为噪音）。只查三个源，广度来自过滤档位与检索词，不来自源的数量。
4. **不做别的问题。** 代码抄袭检测（与"先例发现"是不同的东西）、多轮对话式市场调研（那是 idea-reality-mcp 的商业定位）都不做。§1.3 的其余非目标：代码自动改写 / 自动采纳、eval 平台化、DSH 插件层常驻 prompt。

### 3. 怎么工作：能力阶梯（逐级降级，降级必须让用户看见）

| 层 | 手段 | 生效条件 | 硬事实可信度 |
|---|---|---|---|
| **L1** | 本 skill 自带的 `scripts/probe.py`（解释器按 `python` → `py` → `python3` 探测） | 宿主有 shell 且解释器可用 | **完全可信** |
| **L2** | 宿主原生 HTTP 工具直接取 `api.github.com` | L1 不可用 | 可信（编排由模型自己承担） |
| **L3** | 宿主 `web_search` / `web_fetch` 查网页 | L1、L2 都不可用 | **不可信——star / 日期必须标"未核实"** |

- 能用 L1 就用 L1，否则退 L2，再否则退 L3；**任何一次降级都要写在用户看得到的地方**，绝不静默降级。
- 预算：一次"新建"最多 3 次检索、总耗时 ≤45 秒；预算耗尽的结论要标"未查全"，**绝不**因为超预算就说"没有先例"。

### 4. 行为契约：什么时候出声、什么时候闭嘴

按下面的顺序判断（顺序不可换——补充源失败时，信封恰好长成"真的没有"的形状）：

| 顺序 | 观察到的形状 | 说什么 |
|---|---|---|
| 1 | 任一 `sources[i].status` 不是 `"ok"` | **出声**：这次没查全，说出是哪个源没有应答；工具给了原因就一并说，没给就不猜 |
| 2 | 信封 `status` 是 `"degraded"` | **出声**：报出 `reason`；`rate-limited` 时提示设置 `GITHUB_TOKEN` 或稍后重试 |
| 3 | 全部源 `ok`，但没有"真正解决同一问题"的条目（含 `results: []`） | **静默**：只回一句"未发现可直接复用的开源方案。"然后**立刻继续原任务** |
| 4 | 全部源 `ok`，且有对口条目 | **出声**：五字段清单（项目 / 核心功能 / 可复用点 / 与本方案的差异 / 潜在风险）+ 证据 + a/b 询问，回话以**收束行**结束 |

要点：**`status: "ok"` 且 `results: []` 是唯一代表"真的没有"的信号**；任何其他组合都必须出声。"没查成"永远不等于"没有"。只对"有没有先例"发言，不替用户选 a/b。

**触发 / 不触发**（`SKILL.md` 的判定表）：要新建项目 / 仓库 / skill / plugin / 子系统 / 工具时触发；改 bug、重构、改现有代码、给现有项目加依赖、纯知识提问不触发。用户显式声明"跳过先例检索"后，本次会话不再触发。同一次"新建"只查一次，用户选完 a 或 b 后不再重复检索。

### 5. 安装：三种宿主，同一份文件

| 宿主 | skill 目录 | 安装命令（把 `skills/wheel-check/` 整个拷进去） |
|---|---|---|
| **DSH** | `~/.dsh/skills/` | `cp -R skills/wheel-check ~/.dsh/skills/` |
| **Claude Code** | `~/.claude/skills/` | `cp -R skills/wheel-check ~/.claude/skills/` |
| **Codex CLI / Gemini CLI / Copilot CLI** | `~/.agents/skills/` | `cp -R skills/wheel-check ~/.agents/skills/` |

Windows（PowerShell，`~` 即 `$env:USERPROFILE`）：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.dsh\skills" | Out-Null
Copy-Item -Recurse -Force .\skills\wheel-check "$env:USERPROFILE\.dsh\skills\"
# 想跟随仓库更新，也可以改用目录联接（本例即本机实测用的方式）：
# New-Item -ItemType Junction -Path "$env:USERPROFILE\.dsh\skills\wheel-check" -Target .\skills\wheel-check
```

- 要拷的就是 `SKILL.md` + `references/*.md` + `scripts/probe.py` —— 无构建步骤、无 pip 依赖（纯 stdlib，Python ≥3.8 即可）。
- 同一份 `SKILL.md` 在三种宿主通用（design.md §5.1）：差异只在"能不能取到硬事实"，由上面那条能力阶梯兜住。
- 本机实测：DSH 侧用目录联接装入 `~/.dsh/skills/wheel-check`；另两处是各宿主文档中的 skill 目录，未在本机实测。
- 装完自检（从副本目录里跑，确认零依赖可用）：

```bash
python <skills>/wheel-check/scripts/probe.py health
python <skills>/wheel-check/scripts/probe.py search --terms "markdown viewer" --limit 3
```

### 6. `GITHUB_TOKEN`：可选，但建议设置

| 情况 | GitHub 搜索配额 |
|---|---|
| 不设置 | **10 次/分钟**（未认证） |
| 设置 `GITHUB_TOKEN` | **30 次/分钟** |

- 读法：环境变量 `GITHUB_TOKEN`；未设置时回退到 `GH_TOKEN`（`gh` CLI 用的名字）。空值视为未设置。
- **只发给 `api.github.com`**：Hacker News（Algolia）与 Hugging Face Hub 是无密钥第三方，不会收到这个凭据。
- **脚本从不打印、不回显 token**：stdout 是纯 JSON，诊断只走 stderr，两处都不含 token（有回归测试盯着）。
- 设置方式：POSIX `export GITHUB_TOKEN=...`；PowerShell `$env:GITHUB_TOKEN = "..."`；或在宿主的进程环境里持久化。
- token 无效 / 过期时，GitHub 回 401，脚本报 `degraded`（`reason: null`，原始状态码在 stderr）——它**不会**悄悄退回匿名，也不会假装查到了空结果。
- 一次检索的请求预算：≤3 个过滤档位（每档 1 次请求）+ 最多 1 次 403/429 重试。

隐私：不采集遥测；脚本本身不写任何文件，只向上面三个公开端点发出你这次检索所需的请求。

### 7. 调用方式与输出契约

```bash
python <path>/probe.py health                        # 逐源探活 + 鉴权 + 配额（不花搜索配额）
python <path>/probe.py search --terms "test selection" \
        [--source auto|github|hn|hf] [--strict] [--limit 8]   # --limit 取值范围 1..100
python <path>/probe.py repo <owner/name>             # 单个仓库的硬事实（用于补全 HN 命中）
```

- **用 `python <path>/probe.py` 这种形式调用**：文件以 `100644`（无执行位）提交，POSIX 上 `./probe.py` 会失败。解释器按 `python` → `py` → `python3` 顺序探测。
- **`--terms` 一次只给一个概念名词。** `|` 组**不是并集**：多个词会被空格连接成一次查询，由 GitHub 按 AND 处理（实测：多词长短语 + 强过滤会把真答案杀光，而 `test selection` 能命中 526★ 在位者）。重试阶梯是**换一个更宽的概念名词**，由调用方逐个尝试。
- 退出码：`0` = `ok`；`2` = `degraded`；`3` = 参数错误（`bad-args`）。stdout 永远是纯 JSON，诊断在 stderr。
- 源策略（`--source auto`，默认）：**GitHub 主力（总是查）→ 返回空结果时追加 HN（概念信号，补 GitHub 的语义鸿沟）→ 需求含 AI/ML 语义时追加 HF**（模型条目与代码仓库分列，downloads/likes 不是项目成熟度）。`--source github|hn|hf` 为人工强制单源（强制 HF 时不加 AI 门槛）；数据层的约定是：**任何没有实现检索器的源名一律降级（退出码 2），绝不悄悄换成 GitHub 的数据**。
- 信封形状：

```json
{ "status": "ok | degraded",
  "reason": "rate-limited | timeout | network | bad-args | null",
  "sources": [{ "source": "github", "tier": "strict|relaxed|plain|none", "total": 11, "status": "ok" }],
  "results": [{ "kind": "repo", "full_name": "Tinder/bazel-diff", "url": "https://github.com/Tinder/bazel-diff",
                "stars": 526, "pushed_at": "2026-10-06", "license": "Apache-2.0", "archived": false,
                "language": "Java", "description": "..." }] }
```

  `kind` 取值 `repo`（GitHub）/ `discussion`（HN：标题、points、时间、外链，**没有** stars / license）/ `model`（HF：model_id、downloads、likes）。`sources[i].status ∈ {ok, failed, degraded}`：`failed` 表示该源失败但整体操作仍然成立（典型：`auto` 下 HN 补充源挂了而 GitHub 已作答），`degraded` 表示被显式指定的那个源失败了。`total` 是该源自己报告的匹配数——HF 例外，它只表示"Hub 返回了 N 条"。
- 本仓库**不提供** `package` 子命令：design.md §4.2 曾列出它，但它没有落到任何任务上，且 npm / crates.io 的关键词搜索实测为噪音（只支持精确包名）。要查某个具体包时，走 L2/L3 直接看注册表页面。详见 §13 实现偏差记录。

### 8. 已知限制（诚实清单）

1. **重试退避只有 1.0 秒，而 GitHub 的搜索窗口是 60 秒。** 真遇到配额耗尽时，这一次重试基本只是再确认一遍 403，还多花掉 1 次/分钟的配额。真正管用的补救是设置 `GITHUB_TOKEN`，不是重试。（数值来自 §6，属 brief 指定值。）
2. **非传输类的 5xx 会显示成 `timeout`。** 504 确实是超时；500 也归到 `timeout`——`reason` 词汇表里没有 `server-error`，加它属于改 spec。行为一致，只是措辞不够精确。
3. **`limit` 的两条路径刻意不对称：** 程序化调用 `github_search()` / `hn_search()` / `hf_search()` 时，超范围的 `limit` 会被静默夹到 1..100；命令行 `--limit` 超范围则直接以退出码 3 拒绝（那是"调用错误"，不是"检索失败"）。
4. **含义不明的 API 响应体会降级，而不是被读成"没有结果"。** 200 但 body 是 `{}` 或只有 `message` 时，脚本报 `degraded`（`reason: null`）并在 stderr 给出原因；真正的 `{"total_count": 0, "items": []}` 仍然是 `ok` + `results: []`。三个源都走这条检查：HN 返回的 `{}` 同样降级，`health` 与检索器用的是同一个形状检查。
5. **eval 结果是人记录的，不是自动化评测**（§1.3 明确不把 eval 平台化）：`evals/cases.jsonl` 10 条用例（5 出声 / 2 静默 / 2 不触发 / 1 失败路径），结果见 `evals/results-RED.md` 与 `evals/results-GREEN.md`。**RED 基线不是"无 skill 对照"**：本轮实测的宿主能看到会话 skill 目录，部分基线 agent 因此加载了别的 skill 并停在审批门——这个有效性限制已如实记录在结果文件里。
6. **"补充源失败"只能从 `sources` 读出来。** 因为 HN 只在 GitHub 返回空时才被查询，它失败时信封恰好是"真的没有"的形状（`ok` + `[]`，`reason: null`），只有 HN 那一条 `status: "failed"` 与 stderr 一行诊断能区分二者。所以契约强制调用方检查 `any(sources[i].status != "ok")`。
7. **HN 命中没有 stars / license**，必须用 `probe.py repo <owner/name>` 补全，补不到就标"证据不全"；**HN 的 points 不是仓库成熟度**。HF 条目是模型不是代码库，必须与仓库分列。
8. **触发是概率性的**：skill 靠 `description` 由宿主自行决定是否加载，做不到 100%。
9. **"想法已作为某个大产品的一个功能存在"查不出来**——明确列为已知限制，不假装能解决。
10. **只有三个源**：GitLab 公开搜索（前三名均 1★）、Product Hunt（API 无文本搜索）、Sourcegraph / Stack Overflow（问的是"怎么写"）经实测或前车之鉴排除；npm / crates.io 只支持精确包名。

### 9. 测试与行为证据

```bash
python -m unittest discover -s tests -t .        # 173 tests, 0 skipped

# 屏蔽所有 socket 后重跑同一套用例（须先 import ssl / unittest.mock：ssl 导入时要用到 socket 类）
python -c "import ssl, unittest.mock, socket, unittest, sys; socket.socket=socket.create_connection=socket.getaddrinfo=lambda *a, **k: (_ for _ in ()).throw(OSError('blocked')); sys.exit(0 if unittest.TextTestRunner().run(unittest.TestLoader().discover('tests', top_level_dir='.')).wasSuccessful() else 1)"
```

- 测试全部注入假 `fetch`，不碰真实 API；上面第二条命令在全网不可达的条件下同样全绿——即"测试不会偷偷联网"是被验证过的，不是被假设的。（约定：任何测试都不得命中 `api.github.com`，未认证配额只有 10 次/分钟。）
- 本机只在 **Python 3.13.3** 上实跑过（Windows PowerShell 5.1 环境）；spec §5.2 声明的最低版本是 3.8，本机没有 3.8 解释器可验，这条差异已记录在案。
- 行为证据（用例 → 预期 → 观测）在 [`evals/`](evals/)：`cases.jsonl` 是用例集，`results-RED.md` 是基线，`results-GREEN.md` 是 skill 就位后的实测记录（含未覆盖的缺口）。
- 决策记录（每条裁决的理由与代价）：[`docs/rulings.md`](docs/rulings.md)。原始会话台账 `.superpowers/sdd/wheel-check/progress.md` 是本次构建的草稿纸，**不入库**；结论已全部提炼进 `docs/rulings.md`。

### 10. 许可

MIT，见 [`LICENSE`](LICENSE)（2026）。

---

## English

### 1. What it is

When the user is about to **build something new** — a project, repo, skill, plugin, subsystem or tool — and no code has been written yet, `wheel-check` runs one real search and then says exactly one of three things: **there is a real precedent** (a five-field list, evidence, and an a/b question), **there genuinely is none** (one line, then straight back to the original task), or **the search did not work** (speak up, name the source that failed).

The division of labour is deliberate: **the script guarantees the numbers are real, the model guarantees the judgement is sound.** `probe.py` returns hard facts only (stars, last push, license, archived, links) plus the filter tier it used — it never ranks, scores or judges relevance. "Is this actually the same problem?" is the model's call, per `references/triage-rubric.md`.

### 2. What it solves: four differences from comparable tools

The problem: nothing forces an agent to check whether someone has already solved the thing properly, so wheels get reinvented (design.md §1.1). Most comparable tools do the opposite — they let a model guess star counts, let keywords guess relevance, and blur "the search failed" into "nothing was found". The differences come from design.md §1.3's explicit non-goals:

1. **No scoring.** No 0–100 score, scorecard, trend or pivot advice. §1.3's reason: scoring is exactly what makes comparable tools interrupt people. This one gives facts plus a same-problem-or-not verdict.
2. **No blocking, no doing the work for you.** It never vetoes the user's decision and never rewrites code or installs dependencies. The list is *material*: the turn ends at the a/b question, and building starts after the user answers.
3. **No registry fan-out.** It does not query 10–19 package registries (measured as noise). Three sources only; breadth comes from filter tiers and query wording, not from the number of sources.
4. **Not a different problem.** No code-plagiarism detection (a different thing from precedent discovery), no multi-turn conversational market research (that is idea-reality-mcp's commercial niche). The remaining §1.3 non-goals: automatic code rewriting/adoption, an eval platform, and a resident DSH plugin prompt.

### 3. How it works: the capability ladder (degrade, but visibly)

| Tier | Means | Works when | Trustworthiness of hard facts |
|---|---|---|---|
| **L1** | The bundled `scripts/probe.py` (interpreter probed as `python` → `py` → `python3`) | the host has a shell and an interpreter | **fully trustworthy** |
| **L2** | The host's own HTTP tool hitting `api.github.com` | L1 unavailable | trustworthy (the model carries the orchestration) |
| **L3** | The host's `web_search` / `web_fetch` on web pages | L1 and L2 both unavailable | **NOT trustworthy — every star count and date must be labelled unverified** |

- Prefer L1, fall back to L2, then L3; **every degradation is stated where the user can see it** — never silent.
- Budget: at most 3 searches and ~45 seconds per "new build". A budget-exhausted answer must be labelled incomplete — never turned into "there is no precedent".

### 4. The behaviour contract: when to speak, when to stay quiet

Judge in this order (the order is load-bearing: a failed supplement source produces an envelope shaped exactly like "nothing exists"):

| # | Observed shape | What to say |
|---|---|---|
| 1 | any `sources[i].status` is not `"ok"` | **Speak**: this search was incomplete — name the source that did not answer; repeat the tool's reason if it gave one, never guess one |
| 2 | envelope `status` is `"degraded"` | **Speak**: report `reason`; on `rate-limited`, tell the user to set `GITHUB_TOKEN` or retry later |
| 3 | every source `ok`, but nothing genuinely solves the same problem (including `results: []`) | **Stay silent**: reply with one line — "未发现可直接复用的开源方案。" ("no directly reusable open-source project found") — and continue the original task immediately |
| 4 | every source `ok` and there is a real match | **Speak**: five-field list (project / core function / reusable parts / differences from this plan / risks) + evidence + an a/b question, ending the turn at that question |

Key point: **`status: "ok"` with `results: []` is the only signal that means "there is genuinely nothing"**; every other combination must be spoken. "The search did not happen" is never "nothing exists". It only ever speaks about precedent — it never picks a/b for the user.

**Triggers / non-triggers** (`SKILL.md`'s table): building a new project, repo, skill, plugin, subsystem or tool triggers one search; bug fixes, refactors, edits to existing code, adding a dependency to an existing project, and pure knowledge questions do not. An explicit "skip the prior-art check" from the user disables it for the session. One "new build" is searched once; after the user picks a or b it does not search again.

### 5. Install: three hosts, one file

| Host | Skill directory | Install (copy `skills/wheel-check/` into it) |
|---|---|---|
| **DSH** | `~/.dsh/skills/` | `cp -R skills/wheel-check ~/.dsh/skills/` |
| **Claude Code** | `~/.claude/skills/` | `cp -R skills/wheel-check ~/.claude/skills/` |
| **Codex CLI / Gemini CLI / Copilot CLI** | `~/.agents/skills/` | `cp -R skills/wheel-check ~/.agents/skills/` |

Windows (PowerShell; `~` is `$env:USERPROFILE`):

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.dsh\skills" | Out-Null
Copy-Item -Recurse -Force .\skills\wheel-check "$env:USERPROFILE\.dsh\skills\"
# To track the repo instead, use a junction (this is how it was verified locally):
# New-Item -ItemType Junction -Path "$env:USERPROFILE\.dsh\skills\wheel-check" -Target .\skills\wheel-check
```

- What you copy is `SKILL.md` + `references/*.md` + `scripts/probe.py`: no build step, no pip dependency (pure stdlib, Python ≥3.8).
- The same `SKILL.md` works in all three hosts (design.md §5.1); the only difference is whether hard facts are reachable, which the capability ladder above absorbs.
- Verified locally: DSH, installed as a directory junction at `~/.dsh/skills/wheel-check`. The other two are the hosts' documented skill directories and were **not** exercised on this machine.
- Post-install check (run it from the copy, proving zero-dependency operation):

```bash
python <skills>/wheel-check/scripts/probe.py health
python <skills>/wheel-check/scripts/probe.py search --terms "markdown viewer" --limit 3
```

### 6. `GITHUB_TOKEN` (optional, recommended)

| Situation | GitHub search quota |
|---|---|
| No token | **10 requests/minute** (unauthenticated) |
| `GITHUB_TOKEN` set | **30 requests/minute** |

- Read from the `GITHUB_TOKEN` environment variable, with `GH_TOKEN` (the `gh` CLI's name) as a fallback. A blank value counts as unset.
- **Sent to `api.github.com` only**: Hacker News (Algolia) and the Hugging Face Hub are keyless third parties and never receive the credential.
- **The token is never printed or echoed**: stdout is pure JSON, diagnostics go to stderr, and neither contains it (regression-tested).
- How to set it: `export GITHUB_TOKEN=...` (POSIX) or `$env:GITHUB_TOKEN = "..."` (PowerShell), or persist it in the host's process environment.
- An invalid or expired token makes GitHub answer 401, which surfaces as `degraded` (`reason: null`, raw status on stderr). It does **not** silently fall back to anonymous, and it never pretends an empty result.
- Request budget per search: ≤3 filter tiers (one request each) plus at most one 403/429 retry.

Privacy: no telemetry; the script itself writes no files and only sends the requests this search needs to the three public endpoints above.

### 7. Invocation and output contract

```bash
python <path>/probe.py health                        # per-source liveness + auth + quota (spends no search quota)
python <path>/probe.py search --terms "test selection" \
        [--source auto|github|hn|hf] [--strict] [--limit 8]   # --limit is 1..100
python <path>/probe.py repo <owner/name>             # hard facts for one repo (backfills HN hits)
```

- **Invoke it as `python <path>/probe.py`**: the file is committed as `100644` (no exec bit), so `./probe.py` fails on POSIX. The interpreter is probed as `python` → `py` → `python3`.
- **Pass one concept noun per `--terms`.** A `|` group is **not a union**: multiple words are space-joined into a single query and GitHub ANDs them (measured: a multi-word long phrase plus strong filters kills the real answer, while `test selection` surfaces the 526★ incumbent). The retry ladder is *change the noun*, walked one at a time by the caller.
- Exit codes: `0` = `ok`, `2` = `degraded`, `3` = bad arguments. stdout is always pure JSON; diagnostics go to stderr.
- Source strategy (`--source auto`, the default): **GitHub primary (always) → HN appended when GitHub came back empty (concept signals, covering GitHub's semantic gap) → HF appended when the requirement carries AI/ML semantics** (models are listed apart from code repos; downloads/likes are not project maturity). `--source github|hn|hf` forces one source (a forced HF search is not AI-gated). The data layer's rule: **any source name without a retriever degrades (exit 2); it never quietly answers with GitHub's data instead.**
- Envelope shape: see the JSON example in the Chinese section above. `kind` is `repo` (GitHub) / `discussion` (HN: title, points, date, external link — **no** stars, **no** license) / `model` (HF: model_id, downloads, likes). `sources[i].status ∈ {ok, failed, degraded}`: `failed` means that source failed while the operation still stands (typically the HN supplement under `auto` with GitHub already answering); `degraded` means the explicitly requested source failed. `total` is the source's own match count — except HF, where it only means "the Hub returned N".
- There is **no** `package` subcommand: design.md §4.2 listed one, but no task owned it and npm / crates.io keyword search measured as noise (exact package names only). To inspect a named package, use L2/L3 against the registry page. See §13, the implementation-deviation record.

### 8. Known limitations (an honest list)

1. **The retry backoff is 1.0 s against GitHub's 60 s search window.** On genuine quota exhaustion the retry mostly re-confirms the 403 while spending one more of the 10 requests/minute. The remedy that works is `GITHUB_TOKEN`, not the retry.
2. **A non-transport 5xx surfaces as `timeout`.** A 504 genuinely is one; a 500 also reports `timeout`, because the `reason` vocabulary has no `server-error` value and adding one would change the spec. Same behaviour, imprecise wording.
3. **`limit` behaves asymmetrically on purpose:** programmatic calls to `github_search()` / `hn_search()` / `hf_search()` silently clamp out-of-range limits to 1..100, while the CLI's `--limit` rejects them with exit 3 (that is a malformed *call*, not a failed *search*).
4. **An ambiguous API body degrades instead of being read as "no results".** A 200 whose body is `{}` or only a `message` reports `degraded` (`reason: null`) with the cause on stderr; a genuine `{"total_count": 0, "items": []}` stays `ok` + `results: []`. All three sources run that check: HN's `{}` degrades too, and `health` and the retriever share the same shape check.
5. **Eval results are recorded by hand, not by an automated harness** (§1.3 declines to build an eval platform): `evals/cases.jsonl` holds 10 cases (5 speak / 2 silent / 2 no-trigger / 1 failure path), with `evals/results-RED.md` and `evals/results-GREEN.md`. **The RED baseline is not a skill-free control**: in this harness agents can see the session's skill catalog, so some baseline agents loaded another skill and stopped at its approval gate — recorded honestly in the results file.
6. **A failed supplement is visible only through `sources`.** Because HN is queried only when GitHub returned nothing, its failure lands on the exact shape that means "genuinely nothing" (`ok` + `[]`, `reason: null`); only HN's `status: "failed"` entry and one stderr line distinguish the two. The contract therefore requires the caller to check `any(sources[i].status != "ok")`.
7. **HN hits carry no stars and no license** — backfill with `probe.py repo <owner/name>` or label the row "incomplete evidence"; **HN points are not repository maturity**. HF entries are models, not code libraries, and must be listed separately.
8. **Triggering is probabilistic**: the host decides whether to load the skill from its `description`; 100% is not achievable.
9. **"The idea already exists as a feature of a big product" is not findable** — listed as a known limitation rather than pretended away.
10. **Three sources only**: GitLab's public search (top three hits had 1★), Product Hunt (no text search in its API) and Sourcegraph / Stack Overflow (those answer "how do I write it") were excluded by measurement or prior experience; npm / crates.io support exact package names only.

### 9. Tests and behavioural evidence

```bash
python -m unittest discover -s tests -t .        # 173 tests, 0 skipped

# The same suite with every socket blocked (import ssl/unittest.mock first: ssl needs the socket class)
python -c "import ssl, unittest.mock, socket, unittest, sys; socket.socket=socket.create_connection=socket.getaddrinfo=lambda *a, **k: (_ for _ in ()).throw(OSError('blocked')); sys.exit(0 if unittest.TextTestRunner().run(unittest.TestLoader().discover('tests', top_level_dir='.')).wasSuccessful() else 1)"
```

- Every test injects a fake `fetch` and none of them touches a live API; the second command above stays green with the whole network unreachable, so "the suite never phones home" is verified rather than assumed. (House rule: no test may hit `api.github.com` — the unauthenticated quota is 10 requests/minute.)
- The suite has only ever been run on **Python 3.13.3** (Windows PowerShell 5.1). spec §5.2 declares a 3.8 floor; no 3.8 interpreter was available here, so that claim remains unverified and is recorded as such.
- Behavioural evidence (case → expectation → observation) lives in [`evals/`](evals/): `cases.jsonl` is the case set, `results-RED.md` the baseline, `results-GREEN.md` the observed runs with the gaps left visible.
- The ruling record (every decision, its one-line rationale and its cost-if-wrong): [`docs/rulings.md`](docs/rulings.md). The raw session ledger `.superpowers/sdd/wheel-check/progress.md` is this build's scratch pad and is **not committed**; every conclusion was distilled into `docs/rulings.md`.

### 10. License

MIT — see [`LICENSE`](LICENSE) (2026).
