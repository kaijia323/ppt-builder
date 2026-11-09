任务：评测用。判断标准答案中的每个关键要点，是否在 PPT 大纲中得到体现。

标准关键要点：
{{key_points}}

PPT 大纲：
{{outline}}

对每个要点给出：key（如 K1）、coverage（full：完整体现，含关键数字/结论；partial：部分体现或表述不完整；none：没有体现）、pages（体现在哪几页，页码数组）、reason（一句话理由）。每个要点都必须判定。

输出 json 示例：
{"matches": [{"key": "K1", "coverage": "full", "pages": [3], "reason": "第3页标题完整给出了响应时间降幅"}]}
