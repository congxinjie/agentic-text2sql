# -*- coding: utf-8 -*-
"""RAG schema linking 消融: 对 46 问用 BM25 召回 top-k 表, 统计命中金标 expect_tables 的召回率
与 schema 字符缩减; 产出 docs/RAG检索报告.md。无需 LLM/密钥。"""
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


def main():
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
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT)
    for k in ks:
        print(f"top-{k}: {hit[k]}/{n} ({100.0 * hit[k] / n:.1f}%)")


if __name__ == "__main__":
    main()
