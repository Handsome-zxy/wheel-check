# wheel-check — 开发前开源先例检索 Skill · 设计文档

**日期**：2026-10-06
**状态**：已获用户批准（2026-10-06 会话，v2 扩源已批准），待审阅文字稿
**版本**：v2.2（v2.1 基础上补齐 per-source 状态词汇与"补充源失败可见性"契约）
**蓝本**：仓库外的 `pre-build-prior-art-research.md`（未随仓库分发）的 B 方案
**关系**：本文档**取代**仓库外的早期草案 `docs/superpowers/specs/2026-10-06-prior-art-design.md`（用户指示弃用）。早期草案的调研结论中被本文档采用的部分，均在文中标注来源。

---

## 1. 背景与目标

### 1.1 问题

在开始开发一个新项目 / 新模块 / 新 skill 之前，没有任何机制会强制 agent 先去查"这件事别人是不是已经成熟地做完了"。结果是重复造轮子。

### 1.2 目标

在用户提出"要新建某个东西"时：

- 查到**真正解决同一问题**的开源项目 → 结构化返回，并让用户选 **a) 沿用这些项目继续开发** / **b) 不沿用，纯自研**；
- 没查到 → **静默跳过**，不输出任何检索痕迹，直接把控制权交回主模型；
- 检索失败 → **出声说明失败原因**（绝不假装"查过了，没有"）。

### 1.3 非目标（明确不做）

| 不做 | 原因 |
|---|---|
| ❌ 0–100 评分 / 评分卡 / 趋势 / pivot 建议 | 评分正是现有同类工具"打断人"的根源；本 skill 只给事实与判定 |
| ❌ 阻断或否决用户的开发决定 | 清单是**供料**，永不阻断 |
| ❌ 扇出 10–19 个包注册表 | 实测为噪音（见 §2.2），广度来自单源多分面而非多源 |
| ❌ 代码抄袭检测 | 与"先例发现"是不同的东西 |
| ❌ 多轮对话式市场调研 | 那是 idea-reality-mcp 的商业定位 |
| ❌ 代码自动改写 / 自动采纳 | 用户选 a 也只是"继续开发时参考"，不自动改代码 |
| ❌ eval 平台化（run.py、CI 接入） | 本次只留用例集与人工记录的结果文件；自动化评测留待后续 |
| ❌ DSH 插件层的常驻 system prompt | skill 零安装即可被发现；插件层仅在触发率被证明不足时再加（§9） |

---

## 2. 实测依据（全部为本机真实调用，非推测）

### 2.1 网络与解释器事实

| 客户端 | 结果 | 影响 |
|---|---|---|
| `python`（3.13.3）/ `py`（启动器） | ✅ 均可调用 | **L1 主路径** |
| `python3`（本机） | ❌ Microsoft Store 别名，空输出 | 启动器阶梯中排最后 |
| Python `urllib` → `api.github.com` | ✅ HTTP 200 | **数据层用 Python** |
| Node `fetch` → `api.github.com` | ⚠️ 本轮 200，上一轮同样代码 `fetch failed` | 抖动，不作唯一主路径 |
| `curl.exe` → `api.github.com` | ❌ `http_code=000`（TLS 握手失败） | **禁用 curl** |

### 2.2 检索质量（同一探针，真实查询）

| 查询形态 | 结果 | 判定 |
|---|---|---|
| `file watcher in:name,description stars:>50 pushed:>2025-01-01 fork:false` | 60 条，**第 1 名即 `paulmillr/chokidar`（12244★）** | ✅ 唯一主力源 |
| 同上但去掉 `stars:` / `pushed:` | 8921 条，假阳性 `tcgoetz/GarminDB` 排第 2 | ⚠️ 无强过滤不可用 |
| `(markdown viewer OR terminal markdown OR markdown renderer) …` | 78 条，**第 1 名 `charmbracelet/glow`（27596★）** | ✅ 括号 OR 有效 |
| `(incremental test selection OR test impact analysis OR affected tests) …` | **total=0** | ❌ **强过滤会漏掉正确答案** |
| `test impact analysis in:name,description stars:>50 fork:false` | total=3，命中全是垃圾（`-L-` / `Verified-Tweaks` / `neurons`） | ❌ **多词长短语 + 强过滤 = 杀光真答案** |
| `test selection in:name,description stars:>50 fork:false` | total=11，命中 **`Tinder/bazel-diff`（526★，2026-10-06 更新）**、`pluff/crystalball`（353★） | ✅ **概念名词比长短语有效得多** |
| npm `/-/v1/search` 关键词搜索 | 搜不到 `chokidar` 本身 | ❌ 仅支持精确包名 |
| crates.io `?q=` | 第 2 名是无关的 `ratkit` | ❌ 仅支持精确包名 |

### 2.2b 补充源实测（v2 新增）

| 源 | 可用性 | 检索质量实测 | 判定 |
|---|---|---|---|
| **Hacker News（Algolia）** | ✅ 免密钥、无配额压力 | `incremental test selection` → 命中 **`Show HN: Testrim: Intelligent Test Impact Analysis`**（GitHub 对同一句返回 total=0）；`affected tests` → 命中 `cargo-affect`、Bazel 受影响测试 | ✅ **纳入为补充源**：概念句召回，补 GitHub 的语义鸿沟 |
| **Hugging Face Hub** | ✅ 免密钥，models/datasets 均可用，带 downloads / likes | `speech to text` → 前 3 名为 1125/676/612 下载、4/1/0 likes 的小模型 | ⚠️ **仅 AI/ML 需求时启用**（条件源），否则是噪音 |
| **GitLab 公开项目搜索** | ✅ 免密钥 | `test impact` → 前三名均 1★（最后活动 2026-04 / 2025-06 / 2023-05） | ❌ **排除**：公开实例信号≈0 |
| Product Hunt | ❌ API 无文本搜索 | 前车之鉴：idea-reality-mcp 为此专门下架该源 | ❌ 排除 |
| Sourcegraph / grep.app | 可用 | 回答"代码里有没有这段实现" | ❌ 排除：非"先例发现"问题（属代码抄袭检测邻域） |
| Gitee / Bitbucket / Codeberg / SourceForge | 未实测（需 token 或已停更） | — | ❌ 本次不纳入 |
| Stack Overflow | 可用 | 问的是"怎么写"，不是"有没有现成的" | ❌ 排除 |

**三条硬结论**：① 强过滤是让信号浮出来的唯一机制；② **强过滤会漏，必须配降过滤重试层**，否则会出现"明明有却静默跳过"——这恰是本项目最不能犯的错（§5.3）；③ **检索词要用概念名词而非多词长短语**，实测 `test selection` 有效而 `test impact analysis` 全灭。

> **源数量不是瓶颈，检索词与过滤强度才是。** 因此 v2 只新增 1 个无条件源（HN）+ 1 个条件源（HF），把主要工程量放在 §5.3 的检索词与重试策略上。

### 2.3 配额

未认证 GitHub search **10 次/分钟**（本轮实测触发 403）；带 `GITHUB_TOKEN` 提升至 30 次/分钟。→ 决定了"每层一次查询、层间退避"的设计，而非"N 个词各查一次"。

---

## 3. 交付物与文件结构

```
wheel-check\                                ← 独立 git 仓库（已 git init）
├── README.md                               ← M4；中英双语
├── LICENSE                                 ← M4；MIT
├── docs/
│   ├── wheel-check-design.md               ← 本文档
│   └── superpowers/plans/<date>-wheel-check.md  ← 实施计划（writing-plans 产出）
├── skills/wheel-check/                     ← 可分发载体（拷贝/软链即可安装）
│   ├── SKILL.md                            ← 契约层
│   ├── references/
│   │   ├── triage-rubric.md                ← "真正解决同一问题"判定 + 弱召回重试策略
│   │   ├── query-patterns.md               ← 去品牌化模板 + 中→英术语映射
│   │   ├── verdicts.md                     ← 终态措辞模板
│   │   └── reuse-map.md                    ← 借鉴来源、可复用模块、适配改动点
│   └── scripts/probe.py                    ← 数据层（纯 stdlib）
└── evals/
    ├── cases.jsonl                         ← 用例集（含"故意没有对应项目"的假需求）
    └── results-RED.md / results-GREEN.md   ← TDD 证据
```

**职责分离原则**

- **SKILL.md（契约层）**：只决定"何时开口 / 何时闭嘴 / 说什么"，不含 API 细节 → 将来替换数据层不用改。
- **probe.py（数据层）**：只做检索、强过滤、字段裁剪、限流退避、编码处理。**绝不做**语义判断、打分、排名决策。
- **references/（知识层）**：按需读取，渐进披露。

> 关键分工：**脚本保证数字是真的，模型保证判断是准的。** 调研到的同类工具大多反着来——用模型猜 star 数，用关键词猜相关性。

---

## 4. 接口定义

### 4.1 SKILL.md 契约

**frontmatter（草稿，最终以 GREEN 阶段测试结果迭代）**

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

**触发 / 不触发**

| | 内容 |
|---|---|
| 触发 | 用户表示要**新建**：项目、仓库、skill、plugin、子系统、目录树；进入 plan mode 做新东西；"我想做一个 X" |
| 不触发 | 改 bug、重构、改现有代码、给现有项目加依赖、纯知识提问、只是让我解释概念 |
| 输入 | 用户的项目描述（口语中文/英文皆可） |
| 输出 | 有相关 → 结构化清单 + 询问 a/b；无相关 → **零输出**；失败 → 出声报原因 |

**调用边界**（谁能调、能不能被跳过、越界即失效）：

| 边界 | 规则 |
|---|---|
| 调用者 | 由宿主模型按 description 自动触发；也可被用户显式要求（"先查一下有没有现成的"）；可被计划类 skill 在进入 plan 阶段时调用 |
| 权威范围 | 只对"有没有先例"这件事发言。**不**决定要不要做、不评估用户技术能力、不替用户选 a/b |
| 可被跳过 | 允许用户在会话中显式声明"跳过先例检索"；声明后本 skill 不再触发 |
| 幂等 | 同一次"新建"只查一次；用户已选 a 或 b 之后不重复查询 |
| 硬性越界行为 | 阻断开发、自动改代码、自动安装依赖、把未核实数字当事实陈述——任一发生即视为违反契约 |
| 边界 | 永不阻断原任务；不评分；不自动改代码；绝不编造 star / 日期 |

### 4.2 probe.py CLI

```bash
probe.py health                                   # 探活 + 鉴权 + 配额（逐源报告）
probe.py search --terms "test selection|affected tests" \
                [--source auto|github|hn|hf]      # 默认 auto，见 §5.6
                [--strict] [--limit 8]
probe.py repo <owner/name>                        # 单个仓库硬事实（加深 / 补全 HN 命中）
probe.py package --registry npm --name chokidar   # 精确包名查询（非关键词搜索）
```

`--terms` 为 `|` 分隔的**概念名词**组（§2.2b 硬结论③）。**实现层每个检索档位只发一次请求，且该请求只携带一个概念名词**：实测表明把多个名词合并进一次查询会让 GitHub 按 AND 处理，召回从 11 条（含 526★ 在位者）塌到 3 条垃圾；而用引号做精确短语匹配更糟（同一组词返回 0 条，直接触发静默跳过）。因此 `|` 组表达的是"候选名词集合"，由调用方按 §5.3 的重试阶梯**逐个尝试**，而不是在单次请求里做并集。

> v2.1 修正：v2 曾写"内部合并为一次括号 OR 查询"。实测评测推翻了它——括号 OR 在**通用名词**上会退化成近义噪声（`test selection OR affected tests` → 11108 条），引号短语则直接归零。`build_query` 因此改为：单词直传，多词空格连接（不做并集承诺）。详见 §12 决策记录。

### 4.3 probe.py 输出契约

```json
{
  "status": "ok | degraded",
  "reason": "rate-limited | timeout | network | bad-args | null",
  "sources": [
    { "source": "github", "tier": "strict|relaxed|plain|none", "total": 11, "status": "ok" },
    { "source": "hn",     "tier": "plain",  "total": 3,  "status": "ok" }
  ],
  "results": [
    { "kind": "repo",
      "full_name": "Tinder/bazel-diff", "url": "https://github.com/Tinder/bazel-diff",
      "stars": 526, "pushed_at": "2026-10-06", "license": "Apache-2.0",
      "archived": false, "language": "Java", "description": "..." },
    { "kind": "discussion",
      "title": "Show HN: Testrim – Intelligent Test Impact Analysis",
      "url": "https://news.ycombinator.com/item?id=...", "points": 42,
      "created_at": "2026-03-11", "external_url": "https://github.com/..." },
    { "kind": "model",
      "model_id": "openai/whisper-large-v3", "downloads": 2400000, "likes": 4100,
      "url": "https://huggingface.co/openai/whisper-large-v3" }
  ]
}
```

- **退出码**：`0` = `ok`；`2` = `degraded`；`3` = 参数错误（`bad-args`）。
- `status: "ok"` **且** `results: []` 是**唯一**代表"真的没有"的信号。任何其他组合，模型都必须出声。
- **per-source 状态词汇（v2.2 补齐）**：`sources[i].status ∈ {"ok", "failed", "degraded"}`。
  - `"ok"`：该源被查询且成功。
  - `"failed"`：该源被查询但失败，且**整体操作仍然成立**（典型：`auto` 模式下作为补充源的 HN 失败，而主力 GitHub 已作答）。此时信封保持 `status: "ok"`、`reason: null`、退出码 0。
  - `"degraded"`：该源是**被显式指定**的单源（`--source X`）且失败 → 整体降级，退出码 2。
  
  > **为什么这条必须写进契约**：HN 只在 GitHub 返回空结果时才被查询（§5.3 ⑤）。也就是说，HN 补充源失败时，信封恰好落在 §4.3 定义的"真的没有"形状上（`ok` + `[]`）。如果调用方只看 `status`/`reason`，就会把"HN 挂了"读成"确实没有先例"——正是 §2.2b 里 Testrim 那一幕，也正是本项目存在的理由。因此：
  >
  > **契约要求：调用方必须检查 `any(s["status"] != "ok" for s in sources)`，并把任何非 ok 的源视为"这次没查全"，出声说明，而不是静默跳过。** 数据层同时必须把该源的失败原因打到 stderr（stdout 仍为纯 JSON），使人与机器都有可见通道。

- **证据字段随 `kind` 而不同**：`discussion` 条目**没有** stars / license / archived。模型必须对 `discussion` 里明显对口的项目用 `probe.py repo <owner/name>` 补全，或在该条目标注"证据不全（HN 命中，未核实仓库）"——**不得把 HN 的热度当作仓库成熟度**。
- 脚本**不判断相关性**，只回硬事实与所用过滤档位（`tier`）。

---

## 5. 架构

### 5.1 能力阶梯（泛用性的全部实现）

同一份 `SKILL.md` 在 DSH / Claude Code / Codex / Gemini CLI / Copilot CLI 通用。差异只在"能不能取到硬事实"，因此把差异显式建模为三级阶梯，逐级降级：

| 层 | 手段 | 生效条件 | 硬事实可信度 |
|---|---|---|---|
| **L1** | `python` → `py` → `python3 scripts/probe.py …`（按序探测） | 宿主有 shell 且解释器可用 | **完全可信** |
| **L2** | 宿主原生 HTTP 工具抓 `api.github.com`（如 web_fetch） | L1 不可用 | 可信（编排由模型承担） |
| **L3** | 宿主 `web_search` / `web_fetch` 查网页 | L1+L2 都不可用 | **不可信 → 必须标注"数字未核实"** |

**降级必须可见**：任何一层降级都要让用户看到，绝不静默降级。L3 场景下清单里的 star / 日期必须标注为未核实，不得当作事实陈述。

### 5.2 组件与依赖

| 组件 | 职责 | 依赖 |
|---|---|---|
| `SKILL.md` | 触发判定、流程编排、静默/出声决策、能力阶梯、Stop-and-Ask 规则 | 宿主的 skill 机制 + shell + 网络 |
| `scripts/probe.py` | GitHub 检索、强过滤、字段裁剪、限流退避、UTF-8 输出 | Python ≥3.8 标准库（`urllib`/`json`/`argparse`），零 pip |
| `references/*.md` | 判定标准、查询改写模板、措辞模板、借鉴映射 | 无 |

**依赖方向**：`SKILL.md → probe.py → GitHub REST API`。`references` 被 `SKILL.md` 单向引用。`probe.py` **不读** `references`，**不做**任何语义判断。

### 5.3 数据流（v2）

```
用户提示词
  → ① 适用性判定 ──不适用──→ 静默结束（不消耗任何检索）
  → ② 去品牌化 + 术语扩展：中文 → 英文，产出 2–3 个**概念名词**（非长短语）
  → ③ 源策略（§5.5）：GitHub 强过滤（默认必查）；AI 类需求追加 HF
  → ④ GitHub 结果分流
       ├─ 命中且非全噪音 → 语义重排
       └─ 空 / 全噪音 → 重试：**换下一个概念名词（一次只发一个）** → 去 pushed: → 去 stars: → 纯关键词
                        （≤3 次，≤45s）
  → ⑤ 仍无 → 追加查询 HN（概念信号源），命中的对口条目用 probe.py repo 补全硬事实
  → ⑥ 判定"是否真正解决同一问题"
       ├─ 有 → 结构化清单 + 询问 a/b ──→ 交回主模型
       └─ 无 → 静默结束 ─────────────→ 交回主模型
  → 任何一环失败 → degraded 出声（绝不假装"查过了没有"）
```

> 重试顺序的依据来自实测：**换更宽的概念名词比放宽 star 阈值更有效**（`test impact analysis` 全灭 → `test selection` 命中 526★ 在位者）。

### 5.4 结构化清单格式（有相关时）

必含字段（用户要求）：**项目名称 / 核心功能 / 可复用点 / 与本方案的差异 / 潜在风险**。附加证据字段：stars、最后提交、license、archived、链接。

```markdown
### 开源先例：<一句话需求>

| 项目 | 核心功能 | 可复用点 | 与本方案的差异 | 潜在风险 | 证据 |
|---|---|---|---|---|---|
| owner/name | ... | ... | ... | ... | 12,244★ · 2026-10-04 · MIT |

**建议**：a) 沿用上面 N 个项目继续开发（可复用 X、需改造 Y） / b) 不沿用，纯自研
请选择 a 或 b。
```

未检出相关项目时：**不输出以上任何内容**，直接继续原任务。

### 5.5 源策略（v2）

| 源 | 角色 | 何时查 | 提供 | 不具备 |
|---|---|---|---|---|
| **GitHub 仓库搜索** | 主力（默认必查） | 总是 | stars / pushed_at / license / archived / language / description | — |
| **Hacker News (Algolia)** | 补充（概念信号） | GitHub 强过滤**空或全噪音**时追加；也可与 GitHub 并查 | 标题、points、创建时间、外链 | **无 stars / 无 license**，须用 `probe.py repo` 补全 |
| **Hugging Face Hub** | 条件源 | 仅当需求含 AI/ML 语义（模型、微调、embedding、语音、视觉、LLM 等） | model_id / downloads / likes / 数据集 | 与"可复用代码库"是两种东西，不得混为一列 |
| npm / crates.io | 精确包名 | 用户点名包名时 | 版本、发布时间 | **关键词搜索不可用**（实测噪音） |
| GitLab / Product Hunt / Sourcegraph / Stack Overflow | 不纳入 | — | — | 见 §2.2b 排除理由 |

`--source auto`（默认）按上表决策；`--source github|hn|hf` 为人工强制单源（调试与 eval 用）。

### 5.6 扩展点

| 扩展点 | 做法 | 触发条件 |
|---|---|---|
| 换数据层 | 用 MCP server 替换 `probe.py`，`SKILL.md` 不动（契约层与数据层已分离） | 需要远程/团队共享检索时 |
| 提升触发确定性 | 加 DSH 插件薄壳注入常驻规则 | 仅当触发率被证明不足时（§9） |
| 本地先例优先 | 新增一源：先查当前仓库/组织内是否已有实现 | 用户提出时 |
| 私有源 | `probe.py` 增加 `--source gitlab\|gitea` | 有自建实例时 |

---

## 6. 异常处理与降级链

| 情况 | 行为 |
|---|---|
| 限流 403/429 | 退避重试 1 次 → 仍失败：`degraded / rate-limited`，提示设置 `GITHUB_TOKEN` |
| 存在 `GITHUB_TOKEN` | 自动使用（10 → 30 次/分钟） |
| 网络完全不通 | `health` 阶段即发现 → `degraded / network` → 退 L2/L3 |
| 无 Python 解释器 | 退 L2 → L3，并**明说精度下降**（硬事实不可靠） |
| 非 ASCII 输出（控制台 GBK） | `sys.stdout.reconfigure(encoding="utf-8")` + `PYTHONIOENCODING=utf-8` |
| 单请求超时 | 15 秒，计入预算，不重试超过 2 次 |
| 总预算耗尽 | 用现有证据出结论，标 `degraded`，不出结论性"没有" |
| 参数错误 | 退出码 3，`bad-args`，不静默 |

**降级链**：`probe.py(GitHub API)` → `L2 宿主 HTTP 工具` → `L3 web_search` → 全不可用则 `degraded` 出声。

---

## 7. Stop-and-Ask 规则（用户显式要求）

遇到以下情形**必须停下来问用户**，不得自行假设或跳过：

1. 需求存在多种等价术语，且不同术语会显著改变检索结果，无法择优；
2. 某候选落在"是否真正解决同一问题"的边界上，而该判断会改变 a/b 建议；
3. ≥2 个候选都声称解决同一问题，且**推荐哪个会实质影响**用户的复用决策；
4. 用户给的需求描述本身不足以提炼出任何技术术语（例：只给了产品名）。

提问时须给出：**具体问题 + 可选项 + 我的建议**，等待确认后再继续。

---

## 8. 借鉴机制（用户显式要求）

### 8.1 借鉴映射（写入 `references/reuse-map.md`）

| 来源项目 | 借鉴的模块 | 适配改动点 |
|---|---|---|
| `aradar46/reuse-before-generate` | "多角度检索 + 双箱证据（可复用 / 需竞争）"的思路；"不做额外 LLM 调用"的分工原则 | **只保留 GitHub + HN + 条件 HF**（实测多注册表是噪音）；术语由模型给，不由参数长文案规定 |
| `skyzhao1223/wheel-hub` | `adopt / adapt / inspect / avoid` 四档判定措辞 | 融合进"可复用点 / 差异"两列，不单独打分 |
| `rjmurillo/ai-agents` 的 `programming-advisor` | 触发语表、常见重复造轮子清单（auth / 日期 / 表单校验 / CLI 解析等） | 进 `references/triage-rubric.md` 的辅助清单，**不作主判定** |
| `shimo4228/search-first` | `Adopt / Extend / Compose / Build` 决策矩阵、Quick/Full 双模式 | 决策矩阵进 `verdicts.md` 作 a/b 选项的措辞来源 |
| `mnemox-ai/idea-reality-mcp` | `duplicate_likelihood` 字段语义（"是否已被做过"） | 只借语义，**不借评分**；其"Product Hunt 源无效"的教训直接采纳（不接 PH） |

### 8.2 本 skill 自身也遵守同一流程

开发 wheel-check 时执行同一动作：先查、有则结构化呈现并询问 a/b、无则静默继续。**RED 阶段的基线运行即为它的第一次真实自检**（见 `evals/results-RED.md`）。

---

## 9. 风险与已知限制

| 风险 | 说明 | 缓解 |
|---|---|---|
| **强过滤导致漏检** | 实测 `incremental test selection` 强过滤后 `total=0`，而正确答案存在（HN 对同一句反而命中 Testrim） | §5.3 的"换概念名词 + 降过滤"重试链 + HN 补充源；宁可多查一层，不可该找到却没找到 |
| **多词长短语杀光召回** | `test impact analysis` 强过滤 total=3 全垃圾；`test selection` 命中 526★ 在位者 | `query-patterns.md` 强制"概念名词优先"；重试第一步是换词而非放宽阈值 |
| **HN 命中缺乏硬事实** | HN 只有标题/热度/外链，**没有 stars 与 license** | 对口条目必须 `probe.py repo` 补全，否则标注"证据不全"；**禁止把 HN 热度当仓库成熟度** |
| **精度是真正的难点** | 检索是几天的事，精度是几个月的事；实测 top 5 内仍有假阳性（`GarminDB`） | triage-rubric 显式判定 + 静默契约 + 用例集驱动迭代 |
| **触发是概率性的** | skill 靠 description 让宿主自行决定是否加载，做不到 100% | description 迭代 + RED/GREEN 实测触发；必要时加插件层 |
| **语义鸿沟** | 提示词词汇 ≠ 仓库词汇（"改完文件只跑受影响的测试" vs test impact analysis） | 去品牌化 + 术语扩展（§5.3②）；中文必须先转英文检索式 |
| **"想法已作为大产品的一个功能存在"** | 找不到对应仓库 | 明确列为已知限制，不假装能解决 |
| **GitHub 限流** | 未认证 10 次/分钟 | 每层一次查询 + 退避 + `GITHUB_TOKEN` 指引 |
| **平台吸收风险** | 主流 agent 均已有 web search，但**无人内建"动手前查先例 + 静默跳过"的行为** | 只做该行为差异；接受未来可能被吸收 |
| **同名不同功能** | `prior art` 关键词大量命中**专利**检索工具 | triage-rubric 明确排除专利/代码抄袭类 |

---

## 10. 验证方案

### 10.1 TDD（writing-skills 的 Iron Law）

| 阶段 | 动作 | 证据 |
|---|---|---|
| **RED** | 派**干净 subagent**（无 skill）执行 8 个真实场景提示，记录其自然行为 | `evals/results-RED.md`（含原话摘录） |
| **GREEN** | 把 skill 软链进 `~/.dsh/skills/`，同一批提示再跑一遍 | `evals/results-GREEN.md` |
| **REFACTOR** | 提取新的规避话术 → 补入 SKILL.md 的反规避条目 → 重跑 | 结果文件追加 |

场景覆盖：4 个"有明确先例"（应出声并问 a/b，其中 1 个必须是**只有 HN 命中而 GitHub 全灭**的概念句）+ 1 个 AI/ML 需求（验证 HF 条件源启用）+ 2 个"故意无先例"（应静默）+ 1 个"不适用"（改 bug，应完全不触发）+ 1 个"失败路径"（断网/坏参数，应出声）。

### 10.2 指标（本次人工记录，自动化留待后续）

| 指标 | 定义 |
|---|---|
| 静默正确率 | 无对应项目的用例中，正确地什么都没说的比例（**本项目最关键的指标**） |
| 假阳性率 | 清单中"其实不解决同一问题"的条目占比 |
| 出声正确率 | 失败路径中正确报告失败（而非静默）的比例 |
| 触发正确率 | 该触发的触发、不该触发的不触发 |

---

## 11. 里程碑与验收

| 阶段 | 产出 | 验收标准 |
|---|---|---|
| **M1** | `probe.py`（health / search / repo / package，含三源） | 对 3 个已知查询返回正确硬事实；GitHub 强过滤为空时 HN 兜底生效；HF 仅在 AI 类需求下被查；限流时正确返回 `degraded`；非 ASCII 输出不崩 |
| **M2** | `SKILL.md` + `references/`（4 个文件） | RED/GREEN 两轮结果文件齐备；静默正确率 100%；出声正确率 100% |
| **M3** | `evals/cases.jsonl` + 结果记录 | 用例集 ≥10 条，含 ≥2 条假需求 |
| **M4** | `README.md`（双语）+ `LICENSE` + 安装说明 | 陌生人可从零装上；`skills/wheel-check/` 拷进任意宿主 skill 目录即可用 |

---

## 12. 术语与决策记录

| 决策 | 结论 | 依据 |
|---|---|---|
| 命名 | `wheel-check` | 与既有 `wheel-hub` 同语汇，GitHub 无同名 skill；`prior-art` 撞名且语义偏专利 |
| 仓库位置 | 独立 git 仓库（本机路径不入库） | 用户选定；可迁移可分发 |
| 安装方式 | 拷贝/软链 `skills/wheel-check/` 到宿主 skill 目录 | 零安装摩擦；DSH 侧用目录联接做实测 |
| 数据层语言 | Python（纯 stdlib） | 本机实测：curl 坏、Node 抖动、Python 稳定；三平台普遍自带 |
| 检索源 | GitHub 仓库搜索（主力、默认必查）+ Hacker News Algolia（补充、概念信号）+ Hugging Face Hub（条件、仅 AI/ML 需求）+ 精确包名查询 | 实测：GitHub 强过滤唯一主力；HN 能补概念句漏检；HF 对 AI 需求有效但通用需求是噪音；GitLab/PH/Sourcegraph/SO 经实测或前车之鉴排除 |
| 分档 | 二分：相关则问 a/b，不相关则静默 | 用户本次指令；旧草案的弱/中/强三档已弃用 |
| 失败处理 | `degraded` 必须出声 | 旧草案的关键洞见：现有同类工具把"没搜到"与"没搜成"混为一谈 |
| v2 扩源（2026-10-06 用户批准） | 新增 HN 为无条件补充源、HF 为 AI 条件源；其余候选社区经实测排除 | 实测：HN 对概念句的召回是 GitHub 的补集（`Testrim` 案例）；GitLab 公开搜索前三名均 1★；HF 对 AI 需求有效但对通用需求是噪音 |
| v2 检索词规则 | 概念名词优先于多词长短语；重试第一步是换词而非放宽阈值 | 实测：`test impact analysis` total=3 全垃圾 vs `test selection` 命中 526★ 在位者 |
| v2.1 查询形态（2026-10-06，Task 3 实现期实测修正） | **一次请求只携带一个概念名词**；单词直传，多词空格连接；**不使用引号、不使用括号 OR**；`\|` 组由调用方按重试阶梯逐个尝试 | 实测：`("test selection" OR "affected tests")` → **0 条**（等于静默跳过，本项目最严重的失败）；`test selection` → 11 条含 526★ 在位者；`test selection affected tests`（AND）→ 3 条全垃圾；括号 OR 在通用名词上 → 11108 条噪声。★ 注意：括号 OR 在**具体词**上确实有效（`markdown viewer OR terminal markdown` → 130 条、glow 第 2），失效的是"通用名词并集"，故规则定为"一次一名词"而非"括号 OR 一律不可用" |
