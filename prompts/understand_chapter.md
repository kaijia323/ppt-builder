下面是一份长文档的完整目录，以及其中若干章节的全文（每段开头的方括号内是段落ID）。

【完整目录】
{{outline}}

【本次阅读的章节全文】
{{full_text}}

========================================
【你的任务：逐章理解】

本次要根据整份文档制作汇报PPT。听众：{{audience}}；目的：{{purpose}}；时长：{{duration}} 分钟；场景：{{chosen_scenario}}。
请结合完整目录理解这些章节在全文中的位置，对以下每个章节输出一项理解，不能遗漏：
{{required_sections}}

每项包含：
- section：章节键（与目录写法一致）
- role：这一章在全文中的作用（10字以内）
- summary：80字以内的内容概要，必须写出关键事实（数字、名称、范围）
- key_points：2—5个要点，每个包含 text（不超过50字，数字照抄原文）和 ids（段落ID）
- importance：对本次听众的重要程度 high / medium / low（取决于对听众的价值，不取决于篇幅）
- importance_reason：一句话理由

规则：只依据原文；段落ID必须真实存在；注意上下文，不把只适用于特定情形的句子当作整体结论。

只输出 JSON：{"chapters": [{"section": "", "role": "", "summary": "", "key_points": [{"text": "", "ids": []}], "importance": "", "importance_reason": ""}]}
