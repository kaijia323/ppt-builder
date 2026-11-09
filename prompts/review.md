【文档理解卡】
{{understanding}}

【PPT 大纲】（共 {{n_pages}} 个内容页；要点后方括号内是引用的段落ID）
{{deck}}

========================================
【你的任务：编辑审查】

你是参加本次汇报的资深评审专家，同时也是经验丰富的汇报教练。
- 听众：{{audience}}；汇报目的：{{purpose}}；时长：{{duration}} 分钟
- 场景：{{scenario}}。组织原则：{{principle}}
- 汇报主线：{{storyline}}
- 这类汇报的听众关注点：
{{focus_checklist}}

【脚本检测到的疑似跨页重复要点】（请判断是否确属重复；属实的按 duplication 列出，并说明保留在哪一页）
{{dup_hints}}

【用户在检查点 1 已确认的意见】（规划已按这些意见调整，审查意见不得与之相反；若你认为其中某条有问题，type 填 other，在 problem 中写明"与用户意见冲突"，由用户决定）
{{user_feedback}}

请对照文档理解卡，逐项检查这套大纲（依据章节后括号内是原文的章节标题，章节标题本身也是原文依据）：

1. 完整性（completeness）：听众关注点有没有漏掉？文档中重要性为"高"的章节有没有缺席或被讲得过于单薄？
2. 结构（structure）：页面顺序是否让听众容易跟上？有没有一页塞了两个不相关的主题？背景页是否讲清了现状问题和目标？
3. 重复（duplication）：有没有两页或两条要点讲同一件事？
4. 标题（title）：能否概括全页？写的是结论还是技术细节？分级、列举是否写全？前提条件有没有被省略？有没有断章取义、以偏概全或说过头？
5. 要点（bullet）：是否一条一事？有没有低价值的名称罗列、程序性内容？有没有过长？
6. 关键信息放进了备注（notes_misplaced）：听众应该看到的关键信息，是否只写在了备注里？
7. 概念（concept）：有没有把一类内容改称另一类（例如把业务痛点称为项目风险）？
8. 语气（tone）：有没有把"需/须/计划"写成已经完成？
9. 准确性（accuracy）：根据理解卡，有没有与原文不符的地方？有没有把不同句子拼成原文没有的新事实、改写原文的专有提法？
   提 accuracy 问题时，必须在 quote 中照抄能证明问题的原文句子，在 ids 中给出段落ID；拿不出原文依据的，不要列为 accuracy。

输出要求：
- overall：score（1—5分，5分为可以直接用于汇报）、summary（一两句话总体评价）、strengths（做得好的地方）。
- issues：只列确实影响汇报效果的问题，不吹毛求疵；同一问题不要重复列。每项包含：
  - page：页码；整套层面的问题（如漏掉某章、页面顺序）填 0
  - type：completeness / structure / duplication / title / bullet / notes_misplaced / concept / tone / accuracy / other
  - severity：high（不改会误导听众或遗漏关键内容）/ medium（明显影响效果）/ low（可以更好）
  - problem：问题是什么（一句话）
  - suggestion：具体怎么改（例如给出改写后的标题或要点），必须依据原文
  - ids：相关的段落ID（形如 D1-S2.4-P03，可以为空；不要填章节键）
  - quote：accuracy 问题必填，照抄原文句子；其他类型可为空

只输出 JSON：
{"overall": {"score": 4, "summary": "", "strengths": [""]},
 "issues": [{"page": 3, "type": "title", "severity": "high", "problem": "", "suggestion": "", "ids": [], "quote": ""}]}
