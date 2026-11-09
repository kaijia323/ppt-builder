# ppt-builder 0.7（原名 ppt-content-builder）

把需求分析、技术方案、培训文档、开题报告、工作材料等提炼成**讲得全、对得上口、凝练且可溯源**的 PPT 内容，并自动排成可编辑的 PPT（模型选版式、程序画图，原生形状，42 个版式，同一版式自动换外观）。所有模型调用走 DeepSeek（OpenAI 兼容接口）；智能体只负责与用户沟通和运行命令，因此可以移植到任何能执行命令的智能体上。

- 给智能体看的说明：`SKILL.md`
- 版本变化与设计原因：`CHANGELOG.md`

## 1. 安装

```bash
pip install -r requirements.txt          # 可选：安装 LibreOffice 以支持 .doc/.ppt/.xls/.wps 等旧格式
export DEEPSEEK_API=sk-...               # 只从环境变量读取，不会写入任何文件或日志（旧名 DEEPSEEK_API_KEY 仍可用）
python run.py ping                       # 测试连通性，显示每个任务使用的模型
```

## 2. 快速开始

```bash
python run.py init --workdir work/demo --scenario requirements_review \
  --inputs 需求分析说明书.docx --audience "甲方组织的需求论证专家" \
  --purpose "需求分析说明书通过专家论证" --duration 20
python run.py run --workdir work/demo    # 停在大纲确认（大纲表 + 提醒）→ 写 answers.json
python run.py run --workdir work/demo    # 写作、审查、核验（问题自动修正）、排版、讲稿，停在成品确认 → 写 visual_answers.json
python run.py run --workdir work/demo    # 确认后完成 → work/demo/<标题>.pptx + 注意事项.md
```

不给 `--workdir` 时，成果放在第一个材料所在的文件夹里新建的 `PPT_<材料文件名>`（已有同名文件夹则用 `_2`、`_3`……），完整路径见 `init` 输出的 `summary.workdir`。

## 3. 设计思路：像专家一样自上而下

| 步骤 | 做什么 | 模型 | 关键设计 |
|---|---|---|---|
| ① 通读理解 | 读完全文，产出"理解卡"：文档类型与用途、论证主线、逐章作用/摘要/要点/对听众的重要性、关键事项（待确认、指标、做与不做、交付、约束）、文档自身问题 | flash 深度思考 | 先给目录再给全文，任务放在最后；强制逐章输出，防止中间读漏；重要性按"听众要做什么判断"来定 |
| ② 内容规划 | 先搭大纲骨架与追问清单（frame），再定章、按章展开到页（三步规划） | flash 深度思考 | 页数按时长与篇幅给出；要点须是原文事实；问题须落页；归纳页每条出自原文 |
| 大纲确认 | 用户看大纲表（章节、内容、页数）确认或用一句话调整 | — | 修改意见和补充内容会自动纳入，重新规划；详细规划见 _过程文件/规划说明.md |
| ③ 逐页写作 | 每页只拿对应章节的完整原文（带章节路径）来写 | flash | 正反例约束凝练度；数字、出处、语气由脚本校验，不合格带问题清单重写 |
| ④ 编辑审查 | 以评审专家视角审查整套：完整性、结构、重复、标题、要点、关键信息是否被放进备注、概念、语气 | flash 深度思考 | 有问题的页带审查意见重写一轮，再终审一次 |
| ⑤ 核验 | 脚本检查（出处、数字、程度词、语气、篇幅）＋ 标题蕴含核验 | flash 思考 | 问题按默认意见自动修正（按原文改写/删重复），处理不了的汇总到《注意事项.md》 |
| ⑥ 导出 | 输出 content.json / outline.md / sources.md | — | 自动修正与用户改写的内容可追溯 |
| ⑦ 排版与讲稿 | 选版式、填槽位、对着成品页写讲稿 | flash | 成品确认时可按页改内容（edit）或版面（feedback/layout），只重做点名页 |

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

- `llm.profiles`：
  - `deep`（deepseek-flash，深度思考）：通读、规划
  - `review`（deepseek-flash，深度思考）：编辑审查
  - `write`（deepseek-flash，不思考）：写页面
  - `verify`（flash，思考）：标题核验
- 思考内容计入输出额度，所以 `deep` 档位的 `max_tokens` 设为 65536。
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

顶层只放成果，其余全部在 `_过程文件/`（0.7.0-dev7 起；旧工作目录过程文件在顶层，照常可用）：

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
  checkpoint3.md  visual_answers.json
  logs/
```

## 6. 日志

| 文件 | 内容 |
|---|---|
| `logs/llm/<call_id>.json` | 每次模型调用的完整记录：完整提示词、请求参数、原始输出、思考内容、解析结果、token、耗时、HTTP 尝试、校验问题 |
| `logs/llm_calls.jsonl` | 每次调用的摘要 |
| `logs/gates.jsonl` | 各质量关卡的判定，例如章节是否被理解、关注点是否覆盖、页面是否一次写成、审查问题、核验结果 |
| `logs/events.jsonl`、`run.log` | 事件与文本日志 |
| `logs/metrics.json` | 各阶段耗时、调用数、token |
| `logs/environment.json` | 版本、配置、提示词与场景文件哈希 |
| `logs/run_report.md` | 自动生成的运行报告 |

通读和规划的结果分别以 `understanding.md`、`checkpoint1.md` 的形式保存，可以直接看出模型对文档的理解和取舍是否合理，这是定位问题最快的入口。

## 7. 评测与反馈

```bash
python run.py eval --workdir work/demo --gold gold.json
python run.py eval-summary --workdirs work/a work/b --out summary.md
python run.py bundle --workdir work/demo --include-inputs
```

## 8. 已知限制

- 不读取图片和图表中的文字；扫描件需先做 OCR。
- 无边框的 PDF 表格只能按行保留。
- `PPTC_LLM_PROVIDER=mock` 只验证流程是否通畅，输出内容没有意义。
