# reuse-map —— 借鉴映射（设计文档 §8.1 原文）

本 skill 自己的判定与措辞不是凭空定的，来源如下。表内文字为设计文档 §8.1 的原文。

| 来源项目 | 借鉴的模块 | 适配改动点 |
|---|---|---|
| `aradar46/reuse-before-generate` | "多角度检索 + 双箱证据（可复用 / 需竞争）"的思路；"不做额外 LLM 调用"的分工原则 | **只保留 GitHub + HN + 条件 HF**（实测多注册表是噪音）；术语由模型给，不由参数长文案规定 |
| `skyzhao1223/wheel-hub` | `adopt / adapt / inspect / avoid` 四档判定措辞 | 融合进"可复用点 / 差异"两列，不单独打分 |
| `rjmurillo/ai-agents` 的 `programming-advisor` | 触发语表、常见重复造轮子清单（auth / 日期 / 表单校验 / CLI 解析等） | 进 `references/triage-rubric.md` 的辅助清单，**不作主判定** |
| `shimo4228/search-first` | `Adopt / Extend / Compose / Build` 决策矩阵、Quick/Full 双模式 | 决策矩阵进 `verdicts.md` 作 a/b 选项的措辞来源 |
| `mnemox-ai/idea-reality-mcp` | `duplicate_likelihood` 字段语义（"是否已被做过"） | 只借语义，**不借评分**；其"Product Hunt 源无效"的教训直接采纳（不接 PH） |

## 本 skill 自己遵守同一流程（设计文档 §8.2）

开发 `wheel-check` 时执行同一动作：先查、有则结构化呈现并询问 a/b、无则静默继续。RED 阶段的基线运行就是它的第一次真实自检（见 `evals/results-RED.md`）。

## 边界（源自上表的适配改动点，非新增规则）

- 不做评分、不做趋势、不做 pivot 建议——只给事实与判定。
- 不接 Product Hunt、GitLab、Sourcegraph、Stack Overflow 等源（实测或前车之鉴排除，理由见设计文档 §2.2b）。
- 借鉴的是**判定与措辞**，不是别的项目的实现代码。
