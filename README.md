# ppt-builder

把需求分析、技术方案、培训文档、开题报告、工作材料等提炼成**讲得全、对得上口、凝练且可溯源**的 PPT 内容，并自动排成可编辑的 PPT（模型选版式、程序画图，原生形状，42 个版式，同一版式自动换外观）。

**这是 dsh 适配版（0.8.0-dsh.1）**：不需要 API Key、不联网——每一次模型调用都由运行它的智能体（agent）按 job 提示词作答，流水线负责解析、校验、版式与渲染。原版的 `provider: deepseek`（自带 Key 走 OpenAI 兼容接口）仍然保留。

- 给智能体看的说明：`SKILL.md`（重点看「二、运行 / 作答协议」）
- 上游原版：https://github.com/wendaokeliu-sys/ppt-builder ；本 fork：https://github.com/kaijia323/ppt-builder

## 1. 安装

```bash
cd <技能目录>
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
bin/run ping                            # agent 模式：不联网、不需要 Key，直接返回就绪
```

依赖装在技能目录的 `.venv` 里（避开 Debian/Ubuntu 的 PEP 668 限制，也不碰系统 Python）；`bin/run` 就是 `.venv/bin/python run.py`。
可选：安装 LibreOffice（`soffice`）以支持 `.doc/.ppt/.xls/.wps` 等旧格式解析。

## 2. 快速开始

```bash
bin/run init --workdir work/demo --scenario requirements_review \
  --inputs 需求分析说明书.docx --audience "甲方组织的需求论证专家" \
  --purpose "需求分析说明书通过专家论证" --duration 20

bin/run run --workdir work/demo          # 退出码 4：按 job 作答后重跑；退出码 2：大纲确认 → 写 answers.json
bin/run run --workdir work/demo          # 继续：写作、审查、核验、排版、讲稿，停在成品确认 → 写 visual_answers.json
bin/run run --workdir work/demo          # 确认后完成 → work/demo/<标题>.pptx + 注意事项.md
```

`provider: agent` 的循环：`run` 每次走到需要模型输出时，就以退出码 4 结束并给出 job 文件（提示词 + schema + 输出路径）；
智能体把答案写成 JSON 放到 `answer_path`，再重跑同一条命令。已答的 job 自动复用，答案不合格会生成带问题清单的新 job。
详见 `SKILL.md` 的「作答协议」。

不给 `--workdir` 时，成果放在第一个材料所在的文件夹里新建的 `PPT_<材料文件名>`（已有同名文件夹则用 `_2`、`_3`……），完整路径见 `init` 输出的 `summary.workdir`。

## 3. 设计思路：像专家一样自上而下

| 步骤 | 做什么 | 档位 | 关键设计 |
|---|---|---|---|
| ① 通读理解 | 读完全文，产出"理解卡"：文档类型与用途、论证主线、逐章作用/摘要/要点/对听众的重要性、关键事项（待确认、指标、做与不做、交付、约束）、文档自身问题 | flash 深度思考 | 先给目录再给全文，任务放在最后；强制逐章输出，防止中间读漏；重要性按"听众要做什么判断"来定 |
| ② 内容规划 | 先搭大纲骨架与追问清单（frame），再定章、按章展开到页（三步规划） | flash 深度思考 | 页数按时长与篇幅给出；要点须是原文事实；问题须落页；归纳页每条出自原文 |
| 大纲确认 | 用户看大纲表（章节、内容、页数）确认或用一句话调整 | — | 修改意见和补充内容会自动纳入，重新规划；详细规划见 _过程文件/规划说明.md |
| ③ 逐页写作 | 每页只拿对应章节的完整原文（带章节路径）来写 | flash | 正反例约束凝练度；数字、出处、语气由脚本校验，不合格带问题清单重写 |
| ④ 编辑审查 | 以评审专家视角审查整套：完整性、结构、重复、标题、要点、关键信息是否被放进备注、概念、语气 | flash 深度思考 | 有问题的页带审查意见重写一轮，再终审一次 |
| ⑤ 核验 | 脚本检查（出处、数字、程度词、语气、篇幅）＋ 标题蕴含核验 | flash 思考 | 问题按默认意见自动修正（按原文改写/删重复），处理不了的汇总到《注意事项.md》 |
| ⑥ 导出 | 输出 content.json / outline.md / sources.md | — | 自动修正与用户改写的内容可追溯 |
| ⑦ 排版与讲稿 | 选版式、填槽位、对着成品页写讲稿 | flash | 成品确认时可按页改内容（edit）或版面（feedback/layout），只重做点名页 |

「档位」列在 `provider: agent` 下表示 job 的 `thinking` 提示（`flash 深度思考` = `thinking: enabled`，应当深想再答）；
原来的 `flash` 是 DeepSeek 模型名，走 `provider: deepseek` 时才真的选模型。

场景文件（`scenarios/*.yaml`）不再规定固定槽位，而是给出这类汇报的**组织原则**和**听众关注点清单**。新增场景只需复制一个 yaml 修改即可。

| 场景 | `--scenario` |
|---|---|
| 需求评审汇报 | `requirements_review` |
| 方案评审汇报 | `review` |
| 知识与规范培训 | `training` |
| 开题报告 | `proposal`（不生成任何参考文献）|
| 工作汇报/总结 | `work_report` |
| 通用 | `generic` |

## 4. 配置（config.yaml）

- `llm.provider`：`agent`（默认，不联网、不需要 Key，模型活由会话智能体按 job 作答）/ `deepseek`（自带 Key 走 OpenAI 兼容接口）/ `mock`（离线规则模拟，只验证流程）。
- `llm.profiles`（`provider: agent` 时只取其中的 `thinking` 作为 job 提示；`deepseek` 时才真正选模型/参数）：
  - `deep`（深度思考）：通读、规划
  - `review`（深度思考）：编辑审查
  - `write`（不思考）：写页面
  - `verify`（思考）：标题核验
- `llm.max_semantic_retries`：答案不合格时带问题清单重写的次数上限（agent 模式下表现为生成新的 job）。
- `understand.max_single_chars`：不超过这个字数（默认 30 万字）时一次读完全文。
- `write`：标题、要点、备注的字数上限。
- `review.revise`、`review.final_pass`：是否带意见重写、是否终审。
- `check.entail`：`none` / `titles` / `all`。
- `visual.number_check` / `visual.geom_check`：数字核对、成品几何质检（默认开，只提示不改动）。
- 内置 7 种色调：湛蓝（默认）/ 青碧 / 中国红 / 墨蓝 / 松绿 / 黛紫 / 琥珀。选默认模板时开始前会列出来让你选；也可以 `init --theme 中国红`，或成品确认时说“整体换成中国红”。
- 套用户模板：`init` 时加 `--template 模板.pptx`（可加 `--logo Logo.png`、`--logo-pos cover|all|none`）。只取主色、字体、Logo，版式不变；
  不加时与默认样式完全相同。
- 服务端不支持某些参数组合时，会自动去掉相应参数，并在日志中记录 `param_adapt`。

## 5. 工作目录

顶层只放成果，其余全部在 `_过程文件/`：

```
<workdir>/
  <标题>.pptx       成品（讲稿在备注里）
  注意事项.md       汇报前需要留意的地方：请核实、原文有但没讲、版面问题、已自动修改（位置用幻灯片序号）
  _过程文件/
  brief.json  state.json  user_blocks.json  规划说明.md  待核实清单.md
  checkpoint1.md  answers_template.json  answers.json  answers_history/
  checkpoint2.md  review_template.json  review_decisions.json
  01_ingest/      blocks.jsonl  chunks.json  docs.json  D1.md（解析结果，可人工检查）
  02_understand/  understanding.json  understanding.md（理解卡，可直接阅读）
  03_plan/        plan.json  plan_v1.json…  feedback_history.json  approved.json
  04_write/       pages.json  draft.md
  05_review/      review_round1.json  review_final.json  pages.json  reviewed.md
  06_check/       check_report.json
  07_export/      content.json  outline.md  sources.md  decisions_applied.json  pending.json
  08_visual/      design.json  <标题>.pptx  preview.pdf  sheet_*.png  visual_report.json  讲稿.md
  agent/          jobs/<job_id>.json（题目：提示词+schema+输出路径） answers/<job_id>.json（你的作答）
  checkpoint3.md  visual_answers.json
  logs/
```

## 6. 日志

| 文件 | 内容 |
|---|---|
| `logs/llm/<call_id>.json` | 每次模型调用的完整记录：完整提示词、请求参数、输出（HTTP provider 为模型原始输出，agent 为你的答案）、解析结果、token、耗时、请求/作答尝试、校验问题 |
| `logs/llm_calls.jsonl` | 每次调用的摘要 |
| `logs/gates.jsonl` | 各质量关卡的判定，例如章节是否被理解、关注点是否覆盖、页面是否一次写成、审查问题、核验结果 |
| `logs/events.jsonl`、`run.log` | 事件与文本日志 |
| `logs/metrics.json` | 各阶段耗时、调用数、token |
| `logs/environment.json` | 版本、配置、提示词与场景文件哈希 |
| `logs/run_report.md` | 自动生成的运行报告 |

通读和规划的结果分别以 `understanding.md`、`checkpoint1.md` 的形式保存，可以直接看出模型对文档的理解和取舍是否合理，这是定位问题最快的入口。

## 7. 评测与反馈

```bash
bin/run eval --workdir work/demo --gold gold.json
bin/run eval-summary --workdirs work/a work/b --out summary.md
bin/run bundle --workdir work/demo --include-inputs
```

Windows 上用 `.venv\Scripts\python.exe run.py ...`（`bin/run` 是 POSIX sh 脚本）。

## 8. 已知限制

- 不读取图片和图表中的文字；扫描件需先做 OCR。
- 无边框的 PDF 表格只能按行保留。
- `PPTC_LLM_PROVIDER=mock` 只验证流程是否通畅，输出内容没有意义。
