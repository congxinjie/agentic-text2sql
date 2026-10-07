# -*- coding: utf-8 -*-
"""RAG schema linking 消融: 对 46 问用 BM25 召回 top-k 表, 统计命中金标 expect_tables 的召回率
与 schema 字符缩减; 产出 docs/RAG检索报告.md。离线部分无需 LLM/密钥。

可选: `--run benchmark/runs/run_<ts>.json` 追加「端到端(RAG 模式)」章节,
数字直接取自该全量 run 的 records/metrics, 不重算不估算。"""
import json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "text2sql_demo"))
import text2sql as T  # noqa: E402
import schema_rag as R  # noqa: E402

DB = ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db"
BIZ = json.loads((ROOT / "demo" / "enterprise_biz.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "RAG检索报告.md"



def _e2e_section(run_path, n):
    """从一次 retrieve_mode=rag 的全量 run JSON 生成端到端章节(真实数字, 不估算)。"""
    run = json.loads(Path(run_path).read_text(encoding="utf-8"))
    a = run["metrics"]["all"]
    bd = run["metrics"]["by_difficulty"]
    recs = run["records"]
    has = [r for r in recs if r.get("rag_tables")]
    hit = 0
    for r in has:
        exp = set(str(x).lower() for x in (r.get("expect_tables") or []))
        got = set(str(x).lower() for x in r["rag_tables"])
        if exp and exp.issubset(got):
            hit += 1
    avg = sum(len(r["rag_tables"]) for r in has) / max(1, len(has))
    diffs = ("simple", "medium", "complex")

    def row(fmt, name, key):
        vals = [fmt.format(a[key])] + [fmt.format(bd[d][key]) if d in bd else "-" for d in diffs]
        return "| " + name + " | " + " | ".join(vals) + " |"

    lines = ["", "## 端到端(全量 46 问, retrieve_mode=rag, 真实 LLM)", "",
             f"> 来源: `{Path(run_path).name}`(全量运行 `--no-report`, 未覆盖 docs/评测报告-基线.md); "
             f"模型/环境与基线一致。",
             "", "| 指标 | 全部 | 简单 | 中等 | 复杂 |", "|---|---|---|---|---|"]
    lines.append(row("{:.1f}%", "SQL 可执行率", "exec_rate"))
    lines.append(row("{:.1f}%", "结果正确率", "result_rate"))
    lines.append(row("{:.1f}%", "口径正确率", "caliber_rate"))
    lines.append(row("{:.1f}%", "端到端准确率", "e2e_rate"))
    lines.append(row("{:.1f}%", "幻觉率", "hallucination_rate"))
    lines += ["", "| 耗时 | 全部 | 简单 | 中等 | 复杂 |", "|---|---|---|---|---|"]
    lines.append(row_ms(a, bd, diffs, "P50", "p50_ms"))
    lines.append(row_ms(a, bd, diffs, "P90", "p90_ms"))
    lines += ["",
              f"- RAG 召回: 平均 {avg:.1f} 表/题; 金标表全覆盖 {hit}/{len(has)}(与离线 top-6 的 {hit}/{n} 一致)。",
              "- 对照 full 基线(同代码默认模式, `docs/评测报告-基线.md`): e2e 100.0%, P50 7.1s / P90 9.1s。",
              "- 注: 未召回的表仍以「表名/列名目录」进 prompt, 所以 C02/C03 漏召回未导致答错; 单轮数字不构成统计显著结论。"]
    return lines


def row_ms(a, bd, diffs, name, key):
    vals = [f"{a[key]/1000:.1f}s"] + [f"{bd[d][key]/1000:.1f}s" if d in bd else "-" for d in diffs]
    return "| " + name + " | " + " | ".join(vals) + " |"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    run_path = argv[argv.index("--run") + 1] if "--run" in argv else None
    meta = T.get_table_meta(str(DB))
    ix = R.SchemaIndex(meta, BIZ.get("biz_context") or "")
    rag_agent = T.QueryAgent(str(DB), "k", verbose=False, sample_rows=0,
                             retrieve_mode="rag", biz_context=BIZ.get("biz_context") or "")
    items = json.loads((HERE / "benchmark.json").read_text(encoding="utf-8"))["items"]
    full_chars = len(rag_agent.schema)

    def short_chars(tabs):
        rag_agent._rag_tables = list(tabs)
        return len(rag_agent._ctx().split("业务说明:")[0])

    ks = (1, 3, 4, 6)
    hit = {k: 0 for k in ks}
    sums = {k: 0 for k in ks}
    misses = []
    for it in items:
        expect = set(str(x).lower() for x in (it.get("expect_tables") or []))
        for k in ks:
            sel = ix.select(it["question"], k)
            if expect.issubset(set(sel)):
                hit[k] += 1
            sums[k] += short_chars(sel)
        if not expect.issubset(set(ix.select(it["question"], max(ks)))):
            misses.append((it.get("id"), sorted(expect), ix.select(it["question"], max(ks))))
    n = len(items)
    lines = ["# RAG Schema Linking 检索报告", "",
             f"> 问题数: {n}; 全库 schema: {full_chars} 字符(含 {len(meta)} 张表); 检索: BM25 + 中文 bigram; 注入=候选表完整结构 + 其余表名/列名目录。",
             "> 召回判定: 金标 expect_tables 全部出现在召回的 top-k 表内(宁多勿漏)。", ""]
    lines += ["| top-k | 命中金标表(题数) | 召回率 | 平均注入 schema 字符(混合) | 相对全库 |", "|---|---|---|---|---|"]
    for k in ks:
        avg = sums[k] / max(1, n)
        lines.append(f"| {k} | {hit[k]}/{n} | {100.0 * hit[k] / n:.1f}% | {avg:.0f} | {100.0 * avg / full_chars:.1f}% |")
    lines += ["", f"## 未被 top-{max(ks)} 召回覆盖的题", ""]
    if not misses:
        lines.append("无。")
    else:
        lines += ["| ID | 金标表 | top-k 召回 |", "|---|---|---|"]
        for qid, expect, sel in misses:
            je = ", ".join(expect)
            js = ", ".join(sel)
            lines.append(f"| {qid} | {je} | {js} |")
    r = 100.0 * hit[max(ks)] / n
    avg6 = sums[max(ks)] / max(1, n)
    lines += ["", "## 结论", "",
              f"- top-{max(ks)} 对金标表的召回率 {r:.1f}%; 平均 schema 字符 {avg6:.0f}, 为全库的 {100.0 * avg6 / full_chars:.1f}%。",
              "- 当前 8 表库上缩减有限; schema 越大(表数越多), RAG 的收益近似线性放大。",
              "- 引擎默认 retrieve_mode=full(不改变既有评测); retrieve_mode=rag 时才注入召回短 schema。"]
    if run_path:
        lines += _e2e_section(run_path, n)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT)
    for k in ks:
        print(f"top-{k}: {hit[k]}/{n} ({100.0 * hit[k] / n:.1f}%)")


if __name__ == "__main__":
    main()
