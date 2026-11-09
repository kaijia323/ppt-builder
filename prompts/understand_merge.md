下面是一份长文档的完整目录，以及已经完成的逐章理解（每章的重要性、概要和要点，要点后方括号内是段落ID）。

【完整目录】
{{outline}}

【逐章理解】
{{chapter_digest}}

========================================
【你的任务：汇总全文理解】

本次要制作汇报PPT。听众：{{audience}}；目的：{{purpose}}；时长：{{duration}} 分钟；用户选择的场景：{{chosen_scenario}}。
请基于以上内容，输出文档层面的理解：
- doc_title（文档标题，照抄目录或原文中的标题）、doc_type、doc_type_reason、written_for、effect
- suggested_scenario（从下列场景ID中选一个；没有明显贴合的填 generic，不要勉强套用）及 suggested_scenario_reason：
{{scenario_list}}
- storyline：3—5句话的论证主线
- key_items：decisions / metrics / scope_in / scope_out / deliverables / constraints，每项含 text 和 ids（只用逐章理解中出现过的段落ID）
- doc_issues：从逐章理解中能看出的前后不一致之处（没有则为空）

只输出 JSON：{"doc_title": "", "doc_date": "", "doc_type": "", "doc_type_reason": "", "written_for": "", "effect": "", "suggested_scenario": "", "suggested_scenario_reason": "", "storyline": [], "key_items": {"decisions": [], "metrics": [], "scope_in": [], "scope_out": [], "deliverables": [], "constraints": [], "time_limits": [], "exceptions": []}, "doc_issues": []}
