下面是一份完整的文档。先给出目录（方括号内是章节键，括号内是字数），再给出全文（每段开头的方括号内是段落ID）。

【目录】
{{outline}}

【全文】
{{full_text}}

========================================
【你的任务：通读理解】

本次要根据这份文档制作一个汇报PPT。
- 听众：{{audience}}
- 汇报目的：{{purpose}}
- 汇报时长：{{duration}} 分钟
- 用户选择的场景：{{chosen_scenario}}

请完整阅读全文后，输出这份文档的"理解卡"。这一步只做理解，不写PPT内容。

1. 文档定位
   - doc_title：文档标题（照抄原文标题）
   - doc_type：文档类型（如需求分析说明书、技术方案、开题报告、培训材料、工作总结）
   - doc_type_reason：判断依据（一句话）
   - written_for：写给谁看
   - doc_date：文档的落款日期或编制日期（照抄原文，如"2026.10.30"；没有就写"原文未说明"）
   - effect：这份文档通过或发布后起什么作用（原文有写就照写，没写就写"原文未说明"）
   - suggested_scenario：从下列场景中选出最适合的一个（只填场景ID），并在 suggested_scenario_reason 中说明理由；
     没有明显贴合的场景时填 generic（通用：按文档自身结构和汇报目的组织），不要勉强套用：
{{scenario_list}}

2. storyline：用3—5句话说清这份文档的论证主线，即文档是按什么逻辑展开的。
   例如"现状存在什么问题 → 要建设什么 → 各部分做到什么程度 → 范围边界在哪 → 如何保障与验收"。

3. chapters：逐章理解。以下每个章节都必须输出一项，不能遗漏：
   {{required_sections}}
   每项包含：
   - section：章节键（如 D1-S2.4），必须与目录中的写法一致
   - role：这一章在全文中的作用（10字以内，如"界定建设范围""提出性能指标"）
   - summary：80字以内的内容概要，必须写出该章最关键的事实（数字、名称、范围），不要写成"本章介绍了……"
   - key_points：该章最重要的2—5个要点。每个要点包含 text（不超过50字，数字照抄原文）和 ids（支撑该要点的段落ID）
   - importance：对本次听众的重要程度，取 high / medium / low
   - importance_reason：一句话说明理由

   判断重要性时请想清楚：这些听众要做什么判断？他们最关心什么？
   重要性取决于对听众的价值，不取决于章节篇幅。
   例如对需求评审专家来说，功能需求、建设范围、验收指标是 high；编写目的、参考文件、术语表通常是 low。

4. key_items：关键事项清单。每项包含 text 和 ids，只列原文明确写出的内容。
   - decisions：需要听众决定、确认或补充的事项（例如原文写明"建议由甲方在评审时明确"的内容）
   - metrics：量化指标和验收标准
   - scope_in：要做什么（建设或工作内容）
   - scope_out：明确不做什么
   - deliverables：交付什么
   - constraints：前提条件、假设和约束
   - time_limits：时限类承诺（如"N日内完成""N小时内响应""7×12小时服务"）
   - exceptions：例外与兜底处理（如"无法自动判定的标注待人工核实""数据延迟则工期顺延"）

5. doc_issues：文档自身的问题，如前后不一致、总数与分项之和不符、段落重复、编号错乱、疑似笔误、表述含糊。只记录，不纠正；没有就输出空数组。

【规则】
- 只依据原文。数字、名称照抄；段落ID必须是原文中真实存在的。
- 注意上下文：某句话如果只针对特定情形，不能当作整体结论。
- summary、text 中只写结论，不要出现"？""实际应为"之类的自我修正；拿不准的地方记入 doc_issues。
- 每个要求的章节都要输出，宁可简短也不要遗漏；较长的章节不要因为篇幅长就多写，较短的章节如果重要也要写清楚。

【输出格式】只输出如下结构的 JSON：
{"doc_title": "", "doc_date": "", "doc_type": "", "doc_type_reason": "", "written_for": "", "effect": "", "suggested_scenario": "", "suggested_scenario_reason": "",
 "storyline": ["", ""],
 "chapters": [{"section": "D1-S2.1", "role": "", "summary": "", "key_points": [{"text": "", "ids": ["D1-S2.1-P04"]}], "importance": "high", "importance_reason": ""}],
 "key_items": {"decisions": [{"text": "", "ids": []}], "metrics": [], "scope_in": [], "scope_out": [], "deliverables": [], "constraints": [], "time_limits": [], "exceptions": []},
 "doc_issues": [{"text": "", "ids": []}]}
