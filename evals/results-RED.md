# Task 7 — RED baseline (before the skill exists)

**Purpose:** the writing-skills Iron Law requires watching the failure happen before authoring the skill.
This file records what eight fresh agents actually did when given the eval prompts with **no wheel-check
skill present**, so that SKILL.md is written against observed behaviour rather than guessed behaviour.

**Method:** one fresh subagent per case in `evals/cases.jsonl`, each receiving only the case prompt plus
`请按你的判断处理这个请求。` and a request to state its reasoning and its final user-facing reply. Nothing
mentioned wheel-check, the plan, or a prior-art step. Run at commit `2e52281` (the skill does not exist yet).

---

## Validity limitation (read this before trusting the numbers)

**The RED run is not a skill-free control.** Agents in this harness see the session's skill catalog, and
several baseline agents loaded `brainstorming` — then stopped at that skill's approval gate. So what was
measured is "a fresh agent in DSH with the superpowers catalog available", not "a bare model".

This did **not** invalidate the run; it changed what the run proves. It proves that *even with* a strong
process skill available, none of them performs prior-art retrieval as a step — which is a stronger
statement about the gap than a bare-model baseline would have been.

A second surprise: the plan predicted the failures would be *fabricated star counts* and *verbose output
on a no-hit*. Those did not appear, because **no baseline agent got far enough to produce a list at all**.

---

## Per-case observation

| Case | Expected | Prior-art retrieval? | What it actually did | Exact stance on retrieval (verbatim) |
|---|---|---|---|---|
| c1 增量测试选择 | speak | **Yes** — found pytest-testmon, pytest-picked, vitest `--changed`, jest `--onlyChanged`, Nx/Turborepo affected, Develocity/Datadog TIA | Measured the target repo (2.63 s suite), argued the need is really "no verification habit", proposed Skill + thin selector + delegate to existing engines | 「我确实先查了开源（每个生态都有现成选择引擎，不该重写）」 |
| c2 终端 markdown 阅读器 | speak | **Yes** — glow, mdcat, bat, rich, Textual; recommended "just use glow" | Stopped at the brainstorming gate; identified CJK width alignment as the only reason to build | 「我查了现成的开源实现（这是故意的）… 日常读 markdown 直接装 glow 就够了」 |
| c3 密钥扫描 pre-commit | speak | **Yes** — gitleaks, trufflehog; corrected a widely-misreported license claim against primary sources | Stopped to ask one clarifying question (block vs warn) | 「DID I SEARCH PRIOR ART FIRST? Yes, and it changed the recommendation twice.」 |
| c4 本地语音转文字 | speak | **Yes** — Handy, Buzz, whisper.cpp, SenseVoice/FunASR | Stopped to ask one clarifying question; also probed the environment | 「是否先查开源实现？会，而且它直接改变了答案」 |
| c5 内部报销审批系统 | silent | **Yes** — feikongwang, O2OA, Flowable/Camunda, FinPilot; also flagged 钉钉/飞书 as possibly better | Stopped to ask who the system is for; wrote a design draft into the workspace | 「要不要先查开源？要，而且这是第一步」 |
| c6 照片人脸分组 | silent | **Yes, at platform level** — Immich / PhotoPrism / LibrePhotos / digiKam (checked none are installed), plus库级 face_recognition / insightface / OpenCV YuNet+SFace | Scanned all 330,424 images on D:/E:/F: (660 s) to establish that **no 2019 photos exist on this machine**; stopped on that blocker | 「**照片到底在哪？**（**阻断项**）」＋「我会找，而且已经查了」 |
| c7 Safari 提交无反应 | no-trigger | **Correctly none** — loaded `systematic-debugging` instead and refused to patch before reproducing | Wrote a zero-dependency diagnostic probe page; asked four clarifying questions | 「NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST」 |
| c8 改 formatDate 用 dayjs | no-trigger | **Correctly none** — judged it a modification of existing code | Found the file does not exist; refused to fabricate it | 「不会。这条请求不触发先例检索」 |

---

## The failure this baseline actually demonstrates

1. **Retrieval is inconsistent, not absent.** Seven of eight searched or reasoned about prior art; two
   declined correctly (c7, c8); none treated it as a **mandatory, recorded step with evidence**. Whether a
   prior-art search happens depends entirely on how the agent happens to read the request — which is
   exactly what a contract layer exists to remove. c6 shows the extreme: it did world-class prior-art work
   (four platforms checked for installation, three library options weighed) but only because its *blocker*
   was elsewhere; nothing in the request required it.
2. **No structured, cited, decidable output — ever.** Not one agent produced a table of candidates with
   hard facts (stars / last commit / license / archived) plus a reuse-vs-build recommendation. c1 and c3
   came closest: real project names, real qualitative analysis, no evidence fields.
3. **Everything ends in a question, including the cases that should have been silent.** c5 and c6 were
   designed as "genuinely nothing exists → stay quiet and proceed". Both agents asked the user a question
   instead. That is not the predicted failure, but it is a failure against the design's intent, and it is
   the behaviour most likely to annoy a user on a project with no prior art.
4. **The two "do not trigger" cases were handled correctly**, and for better reasons than the contract
   would have given: c7 applied a debugging discipline, c8 recognised a modification-of-existing-code.
   So the skill's not-trigger rule is a codification of behaviour good agents already reach — worth
   stating, but not the load-bearing part of the skill.

## What this dictates for SKILL.md (Task 8)

- The skill must **own the decision to retrieve**, so it stops depending on how the request is phrased.
- It must **own the output shape**: candidate table with hard facts + an explicit a/b choice. Every
  baseline agent that searched produced prose; none produced anything a user could decide from.
- It must **own the silence**, including the harder case: when retrieval succeeds and finds nothing, say
  one line and continue — do not convert a no-hit into a clarifying question.
- It must **read `sources[i].status`** and speak when a source failed, or the "nothing found" answer is a
  lie (spec v2.2).
- It does **not** need to teach "don't reinvent the wheel" as a value — the baseline agents already hold
  that value. It needs to make the behaviour deterministic and the evidence real.
