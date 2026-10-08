# query-patterns —— 检索词怎么造

目标：把一句中文或英文需求，变成 **2–3 个英文概念名词**（每个 1–2 个词），一次只发一个。

## 去品牌化三步

1. **剥掉品牌与专名**：产品名、库名、公司名、"我们内部的 X"、以及用户举例说的那个 App，全部去掉。
   品牌名会把检索锚死在那个产品上——"做个像 Notion 的编辑器" 查出的是 Notion 的集成项目，不是编辑器。
2. **抽象成"领域 + 动作"**：先用一句话重写需求（谁，对什么东西，做什么），再从这句话里取最短的领域名词。
   例：「改完文件后只跑受影响的测试」→ 领域 = 测试执行，动作 = 挑出受影响的 → `test selection`。
   例：「把我们公司内部的报销审批流程搬到网页上」→ 领域 = 审批流，动作 = 走表单流转 → `approval workflow`。
3. **转英文并给出 2–3 个候选**：中文查询在 GitHub 上等于没查（中文写的仓库名/描述占比极低，实测基本无效）。
   候选按"越贴题越先试"排序，**逐个试，一次只发一个**。候选里可以留一个稍长的概念句，专门交给 HN（见下）。

## 概念名词规则（全部来自实测）

- **一次请求只带一个概念名词。** 多个名词塞进一次查询会被 GitHub 按 AND 处理，召回崩塌。
- **不要用引号。** 引号强制精确短语匹配，会把结果打到 0——而 0 会触发静默跳过，是本项目最严重的失败。
- **不要超过 2 个词。** 3 个词以上按长短语处理（见下表第一行）。

| 查询形态 | 实测结果 | 结论 |
|---|---|---|
| `test impact analysis` + 强过滤 | total=3，全是垃圾（`-L-` / `Verified-Tweaks` / `neurons`） | 多词长短语杀光召回 |
| `test selection` + 强过滤 | total=11，命中 `Tinder/bazel-diff`（526★，2026-10-06 更新）、`pluff/crystalball`（353★） | **概念名词有效** |
| `("test selection" OR "affected tests")` | **0 条** | 引号短语 = 静默跳过，最严重的失败 |
| `test selection affected tests`（AND） | 3 条全垃圾 | 多名词合并同样是杀召回 |
| `(markdown viewer OR terminal markdown)` | 130 条，`charmbracelet/glow` 第 2 | 括号 OR 只在**具体词**上有效；对**通用名词**会退化成 11108 条噪声。所以规则是"一次一名词"，不是"永不用 OR" |
| `file watcher in:name,description stars:>50 pushed:>2025-01-01 fork:false` | 60 条，第 1 名 `paulmillr/chokidar`（12244★） | 强过滤是让信号浮出来的机制，但必须配降过滤重试层 |

**HN 是概念句的补集。** GitHub 对 `incremental test selection` 返回 total=0，HN 对同一句命中 `Show HN: Testrim: Intelligent Test Impact Analysis`。所以当某个概念名词在 GitHub 全灭时，同一句话交给 HN 仍可能有收获——这是候选里可以留一个稍长概念句给 HN 的原因。

## 中 → 英术语映射（起步方向，不是实测结论；用重试链验证）

| 中文需求 | 候选英文概念名词（按顺序试） |
|---|---|
| 改完文件只跑受影响的测试 | `test selection` → `affected tests` → `incremental test selection`（概念句，留给 HN） |
| 终端里的 markdown 阅读器 | `markdown viewer` → `markdown renderer` |
| 提交前扫描密钥泄露 | `secret scanning` → `secret detection` → `credential scanning` |
| 本地语音转文字 | `speech recognition` → `speech to text` |
| 照片按人脸分组 | `face clustering` → `face recognition` |
| 内部报销审批搬到网页 | `approval workflow` → `expense approval` |
| 定时任务调度 | `job scheduler` → `cron` |
| 日志聚合 | `log aggregation` → `log collector` |
| 限流 | `rate limiting` → `rate limit` |
| 表单校验 | `form validation` |
| 日期时间处理 | `date parsing` |
| CLI 参数解析 | `argument parsing` → `cli parser` |

## 重试链（顺序不可换）

1. **换下一个概念名词**（一次一个）——先换词，不要先放宽过滤。
   实测依据：`test impact analysis` 全灭 → `test selection` 命中 526★ 在位者。
2. **去掉 `pushed:`**（时间过滤）
3. **去掉 `stars:`**（星标过滤）
4. **纯关键词**

- 在 **L1** 下，`probe.py search` 一次调用内自动走完第 2–4 步（严格 → 放宽 → 纯关键词三档），所以你只需要重复第 1 步。
- 在 **L2 / L3** 下，这四步要你自己按顺序做。
- 默认**不加** `--strict`：它只跑强过滤那一档，等于关掉降过滤重试层，会漏掉只在中档出现的答案。
- GitHub 命中了但**全是噪音**（同行不同问题）时，同样回到第 1 步换词——数据层不判断相关性，这个判断是你的（设计文档 §5.5 的"空**或全噪音**"）。换词无效时，显式把补充源问一遍（`--source hn`）——注意 `auto` 模式只在主力源**返回空**时才自动问 HN，全噪音不会自动触发它。
- 上限：**≤3 次 `probe.py search` 调用**、总耗时 ≤45 秒。这个数字的单位是**调用次数，不是请求数**：一次调用最多花 4 次 GitHub 请求（3 个档位 + 最多 1 次 403/429 重试），通常 1–2 次（命中即停）。配额后果：未认证 10 次/分钟 ≈ 2 次完整调用/分钟；设了 `GITHUB_TOKEN` 是 30 次/分钟。到上限就用现有证据出结论并标明"未查全"，不要为了凑满次数继续烧配额。

## L1 调用

```bash
# 一次一个概念名词（<skill-dir> 是含 SKILL.md 的目录）
python <skill-dir>/scripts/probe.py search --terms "test selection"
python <skill-dir>/scripts/probe.py search --terms "affected tests"
python <skill-dir>/scripts/probe.py repo Tinder/bazel-diff      # 单个仓库的硬事实
python <skill-dir>/scripts/probe.py health                      # 探活 + 鉴权 + 配额
```

- 解释器按 `python` → `py` → `python3` 顺序探测（实测本机 `python3` 是 Microsoft Store 空壳，返回空输出，故排最后）。`--help` 列出全部参数。
- **`health` 不是一次检索结果。** 它只回答"源是否活着 / 鉴权与配额还剩多少"；它的信封同样长成 `ok` + `results: []`，但那是"**没有检索过**"，不是"检索过、确实没有先例"。判断有没有先例，只能看 `search` 的返回。
- stdout 是一个 JSON 信封（`status` / `reason` / `sources[]` / `results[]`），诊断信息在 stderr。退出码：`0` = ok，`2` = degraded，`3` = 参数错误。
- 未认证的 GitHub 搜索配额是 10 次/分钟；`rate-limited` 就是撞上它了。
