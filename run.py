#!/usr/bin/env python3
"""ppt-builder 命令行入口。

退出码：0 成功；1 运行错误；2 需要用户决定（检查点）；3 配置/环境/输入错误
每条命令最后在 stdout 输出一行 JSON：{"ok", "exit_code", "command", "summary", "next", "files"}，方便任何智能体解析。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认 GBK，部分符号无法输出：统一改为 UTF-8，无法编码的字符替换而不报错
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ppt_content import VERSION  # noqa: E402
from ppt_content.core import (EXIT_CONFIG, EXIT_ERROR, EXIT_NEED_USER, EXIT_OK, STAGES, NeedUser,  # noqa: E402
                              PipelineError, RunLogger, Workdir, format_exc, load_config, load_scenario, now_iso,
                              read_json, write_environment, write_json)

NEXT_HINT = {
    "ingest": "understand", "understand": "plan",
    "plan": "检查点1：把 checkpoint1.md 的页面规划交给用户确认，回答写入 answers.json，然后运行 run",
    "write": "review", "review": "check",
    "check": "export（核验与终审问题按默认修改意见自动处理，处理不了的汇总到工作目录顶层的《注意事项.md》）",
    "export": "design",
    "design": "render",
    "render": "成品确认：把 checkpoint3.md 和成品 PPT、预览图交给用户确认，回答写入 visual_answers.json，然后运行 run",
    "done": "完成。成果 PPT 和《注意事项.md》在工作目录顶层；过程文件在 _过程文件/（不需要打开）",
    "done_clean": "完成。成品已确认，过程文件已清理，工作目录只剩成果 PPT 和《注意事项.md》；把两个文件的完整路径告诉用户",
}


def emit(ok, code, command, summary=None, nxt=None, files=None, error=None):
    out = {"ok": ok, "exit_code": code, "command": command, "summary": summary or {}, "next": nxt, "files": files or []}
    if error:
        out["error"] = error
    print(json.dumps(out, ensure_ascii=False))


def default_workdir(a):
    """没给 --workdir 时（0.7.0-dev21）：放在第一个材料所在的文件夹里，新建 PPT_<材料文件名>；
    同名文件夹已有内容时依次用 PPT_<材料文件名>_2、_3……，不覆盖已有成果。"""
    inputs = a.inputs or ((read_json(a.brief) or {}).get("inputs") if a.brief else None)
    if not inputs:
        raise PipelineError("必须指定 --inputs", EXIT_CONFIG)
    first = os.path.abspath(inputs[0])
    base = os.path.join(os.path.dirname(first), "PPT_" + os.path.splitext(os.path.basename(first))[0].strip())
    cand, n = base, 1
    while os.path.isdir(cand) and os.listdir(cand):
        n += 1
        cand = "%s_%d" % (base, n)
    return cand


def cmd_init(a, cfg):
    if not a.workdir:
        a.workdir = default_workdir(a)
    wd = Workdir(a.workdir)
    if a.brief:
        brief = read_json(a.brief)
    else:
        brief = {}
    for k, v in [("scenario", a.scenario), ("audience", a.audience), ("purpose", a.purpose), ("duration_min", a.duration),
                 ("max_pages", a.max_pages), ("title", a.title), ("subtitle", a.subtitle), ("must_include", a.must_include),
                 ("format_rules", a.format_rules), ("density", a.density)]:
        if v is not None:
            brief[k] = v
    for k, v in [("template", a.template), ("logo", a.logo)]:
        if v:
            if not os.path.exists(v):
                raise PipelineError("%s 文件不存在：%s" % ("模板" if k == "template" else "Logo 图片", v), EXIT_CONFIG)
            if k == "template" and not v.lower().endswith((".pptx", ".potx")):
                raise PipelineError("模板只支持 .pptx / .potx：%s" % v, EXIT_CONFIG)
            brief[k] = os.path.abspath(v)
    if a.logo_pos:
        brief["logo_pos"] = a.logo_pos
    if a.theme:                       # 内置色调（0.7.0-dev15）：中文名或英文键
        from ppt_content.visual.prim import THEME_NAMES, theme_key
        k = theme_key(a.theme)
        if not k or k == "template":
            raise PipelineError("没有这个色调：%s（可选：%s）" % (a.theme, "、".join(THEME_NAMES.values())), EXIT_CONFIG)
        brief["theme"] = k
    if brief.get("format_rules"):
        # --format-rules 以前只记录不使用（0.7.0-dev9）：并入规划提示词已有的“用户要求必须包含”，不改提示词
        extra = "其他要求：%s" % brief["format_rules"]
        if extra not in (brief.get("must_include") or ""):
            brief["must_include"] = "；".join(x for x in (brief.get("must_include"), extra) if x)
    if a.inputs:
        brief["inputs"] = a.inputs
    brief["inputs"] = [os.path.abspath(x) for x in brief.get("inputs", [])]
    if not brief.get("scenario"):
        raise PipelineError("必须指定 --scenario", EXIT_CONFIG)
    if not brief["inputs"]:
        raise PipelineError("必须指定 --inputs", EXIT_CONFIG)
    miss = [x for x in brief["inputs"] if not os.path.exists(x)]
    if miss:
        raise PipelineError("输入文件不存在：%s" % miss, EXIT_CONFIG)
    if brief["scenario"] == "auto":            # 0.7：通读后按文档类型自动选场景，没有贴合的用通用（自适应骨架）
        brief["scenario_name"] = "自动判断"
        brief.setdefault("duration_min", 20)
    else:
        scen = load_scenario(brief["scenario"])
        brief["scenario_name"] = scen.get("display_name", brief["scenario"])
        brief.setdefault("duration_min", scen.get("default_duration_min", 15))
    brief["created_at"] = now_iso()
    brief["skill_version"] = VERSION
    if os.path.exists(wd.p("brief.json")) and not a.force:
        raise PipelineError("工作目录已初始化，如需覆盖请加 --force（会清空阶段状态）", EXIT_CONFIG)
    os.makedirs(wd.root, exist_ok=True)
    write_json(wd.p("brief.json"), brief)
    write_json(wd.p("state.json"), {"stages": {}})
    for f in ("answers.json", "review_decisions.json", "user_blocks.json"):
        if os.path.exists(wd.p(f)):
            os.rename(wd.p(f), wd.p(f + ".old"))
    return wd, {"scenario": brief["scenario"], "inputs": len(brief["inputs"]), "duration_min": brief["duration_min"],
                "workdir": os.path.abspath(wd.top)}


def run_stage(stage, wd, cfg, log, a, feedback=None, revise=None):
    from ppt_content import check, export, ingest, plan, review, understand, write
    deps = {"understand": ["ingest"], "plan": ["understand"], "write": ["plan"], "review": ["write"],
            "check": ["review"], "export": ["check"], "design": ["export"], "render": ["design"]}
    wd.require(*deps.get(stage, []))
    visual_on = cfg.get("visual", {}).get("enabled", True)
    if stage in ("design", "render") and not visual_on:
        wd.set_stage(stage, "done", summary={"skipped": True})
        return {"skipped": "visual.enabled=false，只输出内容稿"}
    llm = None
    if stage not in ("ingest", "render"):          # export 也要：审查问题的默认处理需要按原文改写（0.7）
        from ppt_content.llm import LLM
        llm = LLM(cfg, log)
    if stage == "write":
        # 检查点 1：已确认则继续；有修改意见/补充内容则自动重新规划并再次停在检查点 1
        res = plan.resolve_checkpoint(wd, cfg, log, llm)
        if res is not True:
            run_stage("plan", wd, cfg, log, a, feedback=res)
    log.begin_stage(stage)
    wd.set_stage(stage, "running", invocation=log.invocation_id)
    need = None
    try:
        if stage == "ingest":
            summary = ingest.run(wd, cfg, log, allow_low_text=getattr(a, "allow_low_text", False))
        elif stage == "understand":
            summary = understand.run(wd, cfg, log, llm)
        elif stage == "plan":
            summary = plan.run(wd, cfg, log, llm, feedback=feedback)
            need = NeedUser("检查点 1：请把 checkpoint1.md 的页面规划（第 %d 版）交给用户确认；确认写 {\"decision\": \"approve\"}，"
                            "修改写 {\"decision\": \"revise\", \"feedback\": \"……\"}" % summary["plan_version"],
                            ["checkpoint1.md", "answers_template.json"])
        elif stage == "write":
            summary = write.run(wd, cfg, log, llm)
        elif stage == "review":
            summary = review.run(wd, cfg, log, llm)
        elif stage == "check":
            summary = check.run(wd, cfg, log, llm)
            if not cfg.get("check", {}).get("auto_decisions", True):      # 0.7 默认不停：问题按默认意见自动处理（见 autofix）
                need = NeedUser("检查点 2：%s" % ("存在必须处理的问题，需用户逐项决定" if not summary["passed"] else "请用户确认成稿"),
                                ["checkpoint2.md", "review_template.json", "06_check/check_report.json"])
        elif stage == "export":
            summary = export.run(wd, cfg, log, llm)
        elif stage == "design":
            from ppt_content.visual import design
            summary = design.run(wd, cfg, log, llm, revise=revise)
        elif stage == "render":
            from ppt_content.visual import render
            summary = render.run(wd, cfg, log)
            need = NeedUser("检查点 3：请把 checkpoint3.md 的版面清单%s交给用户确认；确认写 {\"decision\": \"approve\"}，"
                            "修改写 {\"decision\": \"revise\", \"pages\": {\"页号\": {\"layout\": \"L26\"}}}" % (
                                "和预览图" if summary.get("preview") else ""),
                            ["checkpoint3.md", "visual_answers_template.json", "08_visual/visual_report.json"])
    except NeedUser as e:
        wd.set_stage(stage, "waiting_user", message=str(e))
        log.end_stage("waiting_user")
        raise
    except Exception:
        wd.set_stage(stage, "failed")
        log.end_stage("failed")
        raise
    wd.set_stage(stage, "done", summary=summary)
    log.end_stage("done", summary=summary)
    # 0.7.0-dev7：成果 PPT 复制到工作目录顶层并写《注意事项.md》（只读已有结果，失败不影响流程）
    if stage == "render" or (stage == "export" and not visual_on):
        try:
            from ppt_content import notice
            delivered = notice.deliver(wd, log, with_pptx=(stage == "render"))
            if need:
                need.files = delivered + list(need.files)
        except Exception as e:      # noqa
            log.warn("deliver_failed", "交付整理失败（成果仍在 _过程文件/08_visual/）：%r" % e, trace=format_exc()[-2000:])
    if need:
        raise need
    return summary


def adopt_top_answers(wd):
    """回答文件应写在 _过程文件/ 里；智能体按旧说明写到了工作目录顶层时，移进去（0.7.0-dev7）。"""
    if wd.legacy or not os.path.isdir(wd.top):
        return
    for f in ("answers.json", "visual_answers.json", "review_decisions.json"):
        if os.path.exists(wd.deliver(f)):
            os.makedirs(wd.root, exist_ok=True)
            os.replace(wd.deliver(f), wd.p(f))


def cfg_auto_decisions():
    try:
        from ppt_content.core import load_config
        return load_config().get("check", {}).get("auto_decisions", True)
    except Exception:      # noqa
        return True


def cmd_status(wd):
    st = wd.state()["stages"]
    rows = {s: st.get(s, {}).get("status", "pending") for s in STAGES}
    nxt = None
    for s in STAGES:
        if rows[s] != "done":
            nxt = s
            break
    pl = read_json(wd.stage("plan", "plan.json"), {}) or {}
    info = {"stages": rows, "next_stage": nxt,
            "plan_version": pl.get("version"),
            "answers_present": os.path.exists(wd.p("answers.json")),
            "review_decisions_present": os.path.exists(wd.p("review_decisions.json"))}
    if nxt == "write" and not info["answers_present"]:
        info["hint"] = NEXT_HINT["plan"]
    elif nxt == "export" and not info["review_decisions_present"] and not cfg_auto_decisions():
        info["hint"] = NEXT_HINT["check"]
    else:
        info["hint"] = "运行 %s" % nxt if nxt else "全部完成"
    return info


def disable_quick_edit():
    """Windows 控制台的“快速编辑”模式下，鼠标点一下窗口就会进入“选择”状态，程序写输出时被挂起，直到按键才继续
    （实测一次规划因此停了 14 分钟）。运行期间关闭该模式；非 Windows 或无控制台时什么也不做。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)                 # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)   # 去掉 ENABLE_QUICK_EDIT_MODE，保留 EXTENDED_FLAGS
    except Exception:
        pass


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    disable_quick_edit()
    ap = argparse.ArgumentParser(description="ppt-builder v%s：把材料提炼为可溯源、经核验的 PPT 内容" % VERSION)
    ap.add_argument("--config", help="配置文件路径（默认技能目录下 config.yaml）")
    ap.add_argument("--version", action="version", version="ppt-builder %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="初始化工作目录与需求简报")
    p.add_argument("--workdir", help="工作目录（成果放在这里）；不给时在第一个材料所在文件夹新建 PPT_<材料文件名>")
    p.add_argument("--scenario", help="auto（通读后自动判断）/ requirements_review / review / approval / progress_report / acceptance / proposal / research_midterm / solution_pitch / training / system_training / work_report / generic")
    p.add_argument("--inputs", nargs="+")
    p.add_argument("--audience")
    p.add_argument("--purpose")
    p.add_argument("--duration", type=float, help="汇报时长（分钟）")
    p.add_argument("--max-pages", type=int, dest="max_pages")
    p.add_argument("--density", choices=["standard", "dense"], help="内容密度：standard 讲解型 / dense 评审型（评审类场景默认）")
    p.add_argument("--title")
    p.add_argument("--subtitle")
    p.add_argument("--must-include", dest="must_include")
    p.add_argument("--format-rules", dest="format_rules")
    p.add_argument("--brief", help="直接提供 brief.json")
    p.add_argument("--template", help="套用的 PPT 模板（.pptx/.potx）：取主色、字体、Logo，版式不变（0.7.0-dev9）")
    p.add_argument("--logo", help="Logo 图片（png/jpg）；不给时尝试从模板里找")
    p.add_argument("--theme", help="内置色调：湛蓝（默认）/ 青碧 / 中国红 / 墨蓝 / 松绿 / 黛紫 / 琥珀（也可写 blue/teal/red/slate/green/purple/amber）")
    p.add_argument("--logo-pos", dest="logo_pos", choices=["cover", "all", "none"],
                   help="Logo 位置：cover 只放封面和结束页（默认）/ all 内容页右上角（放在母版里，封面、过渡页、结束页不放）/ none 不放")
    p.add_argument("--force", action="store_true")
    for s in STAGES:
        sp = sub.add_parser(s, help="运行阶段 %s" % s)
        sp.add_argument("--workdir", required=True)
        if s == "ingest":
            sp.add_argument("--allow-low-text", action="store_true")
    p = sub.add_parser("run", help="自动依次运行未完成的阶段，直到遇到检查点或完成")
    p.add_argument("--workdir", required=True)
    p.add_argument("--allow-low-text", action="store_true")
    for name, h in [("status", "查看进度与下一步"), ("report", "生成运行报告 logs/run_report.md"), ("ping", "检查配置并测试模型连通性")]:
        p = sub.add_parser(name, help=h)
        p.add_argument("--workdir", required=(name != "ping"))
    p = sub.add_parser("speech", help="只重写讲稿（0.6，需已完成版式设计；之后运行 run 生成 PPT）")
    p.add_argument("--workdir", required=True)
    p = sub.add_parser("frame", help="生成大纲骨架与追问清单（0.6，规划的第一小步；需已完成通读）")
    p.add_argument("--workdir", required=True)
    p = sub.add_parser("replan", help="带修改意见回到内容规划（检查点 1），下游阶段将重新生成")
    p.add_argument("--workdir", required=True)
    p.add_argument("--feedback", required=True)
    p = sub.add_parser("bundle", help="打包工作目录、日志与技能快照，用于问题分析")
    p.add_argument("--workdir", required=True)
    p.add_argument("--out")
    p.add_argument("--include-inputs", action="store_true", help="同时打包原始输入文件")
    p = sub.add_parser("eval", help="对照标准答案评测本次结果")
    p.add_argument("--workdir", required=True)
    p.add_argument("--gold", required=True)
    p = sub.add_parser("eval-summary", help="汇总多个工作目录的评测结果")
    p.add_argument("--workdirs", nargs="+", required=True)
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    t0 = now_iso()
    log = None
    try:
        cfg = load_config(a.config)
    except PipelineError as e:
        emit(False, e.code, a.cmd, error=str(e))
        return e.code
    try:
        if a.cmd == "ping":
            from ppt_content.llm import LLM
            wd = Workdir(a.workdir or os.path.join(os.getcwd(), ".pptc_check"))
            os.makedirs(wd.root, exist_ok=True)
            log = RunLogger(wd, cfg, "ping")
            log.stage = "ping"
            write_environment(wd, cfg)
            llm = LLM(cfg, log)
            r = llm.call_json("ping", {}, target="ping")
            prof = {t: llm.profile_for(t)["model"] for t in cfg["llm"]["tasks"]}
            emit(r["ok"], EXIT_OK if r["ok"] else EXIT_ERROR, "ping",
                 {"provider": cfg["llm"]["provider"], "models_by_task": prof, "reply": r["data"], "issues": r["issues"]},
                 files=[wd.logs("llm_calls.jsonl")])
            return EXIT_OK if r["ok"] else EXIT_ERROR
        if a.cmd == "eval-summary":
            from ppt_content.reports import eval_summary
            out = eval_summary(a.workdirs, a.out)
            emit(bool(out), EXIT_OK if out else EXIT_ERROR, a.cmd, files=[out] if out else [])
            return EXIT_OK if out else EXIT_ERROR
        if a.cmd == "init":
            wd, summary = cmd_init(a, cfg)
            log = RunLogger(wd, cfg, "init")
            write_environment(wd, cfg)
            log.info("init", "初始化完成", brief=read_json(wd.p("brief.json")))
            log.invocation(argv, EXIT_OK, t0)
            emit(True, EXIT_OK, "init", summary, "run --workdir \"%s\"" % summary["workdir"], [wd.p("brief.json")])
            return EXIT_OK
        wd = Workdir(a.workdir)
        if not os.path.exists(wd.p("brief.json")):
            if os.path.exists(os.path.join(wd.top, "注意事项.md")) and \
                    any(f.lower().endswith(".pptx") for f in os.listdir(wd.top)):
                raise PipelineError("这份 PPT 已确认完成，过程文件已清理（%s 只保留成果）。需要修改时可直接在 PowerPoint 里改，"
                                    "或重新运行 init 生成" % wd.top, EXIT_CONFIG)
            # 路径写错或还没 init：直接说明，不去创建目录（0.7.0-dev7）
            raise PipelineError("工作目录不对：%s 下没有找到本技能的运行记录（brief.json）。请检查路径是否正确；"
                                "新项目请先运行 init" % wd.top, EXIT_CONFIG)
        adopt_top_answers(wd)
        log = RunLogger(wd, cfg, a.cmd)
        if a.cmd == "status":
            info = cmd_status(wd)
            emit(True, EXIT_OK, "status", info, info.get("hint"))
            return EXIT_OK
        if a.cmd == "report":
            from ppt_content.reports import run_report
            f = run_report(wd)
            emit(True, EXIT_OK, "report", files=[f])
            return EXIT_OK
        if a.cmd == "bundle":
            from ppt_content.reports import bundle
            f = bundle(wd, a.out, a.include_inputs)
            emit(True, EXIT_OK, "bundle", {"bytes": os.path.getsize(f)}, files=[f])
            return EXIT_OK
        if a.cmd == "eval":
            from ppt_content.llm import LLM
            from ppt_content.reports import evaluate
            log.stage = "eval"
            r = evaluate(wd, a.gold, LLM(cfg, log), log)
            emit(True, EXIT_OK, "eval", r, files=[wd.p("eval_result.json")])
            return EXIT_OK
        wd.brief()
        write_environment(wd, cfg)
        if a.cmd == "speech":
            from ppt_content.visual import design as vdesign
            from ppt_content.llm import LLM
            wd.require("design")
            log.stage = "speech"
            summary = vdesign.run_speech(wd, cfg, log, LLM(cfg, log))
            st = wd.state()
            if st["stages"].get("render", {}).get("status") == "done":
                wd.set_stage("design", "done", summary=st["stages"]["design"].get("summary"))   # 让 render 过期，run 时重新生成 PPT
            log.invocation(argv, EXIT_OK, t0)
            emit(True, EXIT_OK, "speech", summary, "运行 run 重新生成 PPT", [wd.stage("design", "design.json")])
            return EXIT_OK
        if a.cmd == "frame":
            from ppt_content import frame
            from ppt_content.llm import LLM
            wd.require("understand")
            log.stage = "frame"
            summary = frame.run(wd, cfg, log, LLM(cfg, log))
            log.invocation(argv, EXIT_OK, t0)
            emit(True, EXIT_OK, "frame", summary, "把 03_plan/frame.md 交给用户查看", [wd.stage("plan", "frame.md"),
                                                                                 wd.stage("plan", "frame.json")])
            return EXIT_OK
        if a.cmd == "replan":
            wd.require("understand")
            run_stage("plan", wd, cfg, log, a, feedback=a.feedback)
        if a.cmd == "run":
            summaries = {}
            approved = False
            from ppt_content.visual import render as vrender
            cs = vrender.checkpoint_state(wd)
            st = wd.state()["stages"]
            if cs and st.get("render", {}).get("status") == "done":
                decision, ans, h = cs
                if decision == "revise":
                    summaries["design"] = run_stage("design", wd, cfg, log, a, revise=ans)
                    vrender.mark_applied(wd, h, decision)
                elif decision == "approve":
                    vrender.mark_applied(wd, h, decision)
                    log.info("visual_approved", "检查点 3 已确认")
                    approved = True
            st = wd.state()["stages"]
            todo = [s for s in STAGES if st.get(s, {}).get("status") != "done"]
            for s in todo:
                summaries[s] = run_stage(s, wd, cfg, log, a)
            from ppt_content.reports import run_report
            run_report(wd)
            log.invocation(argv, EXIT_OK, t0)
            cleaned = False
            if approved and not todo:      # 成品已确认、流程结束（0.7.0-dev25）：只留成果 PPT 和《注意事项.md》
                from ppt_content import notice
                try:
                    cleaned = notice.finalize(wd, log, cfg)
                except Exception as e:      # noqa  清理失败不影响成果
                    log.warn("cleanup_failed", "过程文件清理失败：%r" % e)
            files = [os.path.join(wd.top, f) for f in sorted(os.listdir(wd.top))
                     if f.endswith(".pptx") or f == "注意事项.md"] if os.path.isdir(wd.top) else []
            if not files:          # 只做内容稿（visual.enabled=false）或交付整理失败
                rep = read_json(wd.stage("render", "visual_report.json")) or {}
                files = [f for f in [rep.get("pptx"), wd.stage("export", "content.json")] if f]
            emit(True, EXIT_OK, "run", summaries, NEXT_HINT["done_clean"] if cleaned else NEXT_HINT["done"], files)
            return EXIT_OK
        summary = run_stage(a.cmd, wd, cfg, log, a)
        from ppt_content.reports import run_report
        run_report(wd)
        log.invocation(argv, EXIT_OK, t0)
        emit(True, EXIT_OK, a.cmd, summary, NEXT_HINT.get(a.cmd))
        return EXIT_OK
    except NeedUser as e:
        if log:
            log.info("need_user", str(e), files=e.files)
            log.invocation(argv, EXIT_NEED_USER, t0)
            try:
                from ppt_content.reports import run_report
                run_report(Workdir(a.workdir))
            except Exception:
                pass
        w0 = Workdir(a.workdir)
        emit(False, EXIT_NEED_USER, a.cmd, nxt=str(e), files=[f if os.path.isabs(f) else w0.p(f) for f in e.files])
        return EXIT_NEED_USER
    except PipelineError as e:
        if log:
            log.error("pipeline_error", str(e), trace=format_exc()[-4000:])
            log.invocation(argv, e.code, t0, err=str(e))
        emit(False, e.code, a.cmd, error=str(e))
        return e.code
    except Exception as e:
        tb = format_exc()
        if log:
            log.error("unexpected_error", repr(e), trace=tb[-6000:])
            log.invocation(argv, EXIT_ERROR, t0, err=tb[-6000:])
        print(tb, file=sys.stderr)
        emit(False, EXIT_ERROR, a.cmd, error=repr(e))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
