# -*- coding: utf-8 -*-
"""Schema Linking 检索器(纯标准库, 零依赖, 类 RAG)。

把"表/列 + biz_context 中文片段"切成语料, 用 BM25 + 中文 bigram 召回与问题最相关的表,
供引擎只注入 top-k 表结构(而不是全库 schema)。零第三方依赖。
"""
import math
import re

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


def tokens(text):
    """英文/列名按词切分, 中文按字符 bigram 切分(免分词)。"""
    text = str(text or "").lower()
    out = [w.lower() for w in _WORD.findall(text)]
    for seg in _CJK.findall(text):
        if len(seg) == 1:
            out.append(seg)
        else:
            for i in range(len(seg) - 1):
                out.append(seg[i:i + 2])
    return out


class SchemaIndex:
    def __init__(self, meta, biz_context="", extra_docs=None):
        self.meta = {str(t).lower(): list(cols) for t, cols in (meta or {}).items()}
        _cnt = {}
        for _cols in self.meta.values():
            for _c in _cols:
                _cnt[_c] = _cnt.get(_c, 0) + 1
        _n = max(1, len(self.meta))
        self.generic_cols = {c for c, n in _cnt.items() if n >= max(3, _n // 2)}
        self.docs = []
        self._add_table_docs()
        self._add_context_docs(biz_context or "")
        for d in (extra_docs or []):
            self._add_doc(d.get("text", ""), d.get("tables") or [], d.get("cols") or [])
        self._build()

    def _add_doc(self, text, tables, cols):
        self.docs.append({"text": str(text), "tables": set(tables), "cols": set(cols)})

    def _add_table_docs(self):
        for t, cols in self.meta.items():
            self._add_doc("表 " + t + " " + " ".join(cols), [t], cols)

    def _add_context_docs(self, ctx):
        for part in re.split(r"[\n;。；]+", ctx):
            part = part.strip()
            if not part:
                continue
            low = part.lower()
            tabs, cols = [], []
            for t, cs in self.meta.items():
                hit = [c for c in cs if c not in self.generic_cols and c in low]
                if t in low or hit:
                    tabs.append(t)
                    cols.extend(hit)
            self._add_doc(part, tabs, cols)


    def _build(self):
        self.df = {}
        self.doc_tf = []
        for d in self.docs:
            tf = {}
            for tok in tokens(d["text"]):
                tf[tok] = tf.get(tok, 0) + 1
                self.df[tok] = self.df.get(tok, 0) + 1
            self.doc_tf.append(tf)
        total = sum(sum(tf.values()) for tf in self.doc_tf)
        self.avg_len = total / max(1, len(self.doc_tf))

    def score(self, question):
        qt = tokens(question)
        if not qt:
            return {}
        n = len(self.docs)
        k1, b = 1.5, 0.75
        scores = []
        for i, tf in enumerate(self.doc_tf):
            dl = sum(tf.values()) or 1
            s = 0.0
            for tok in qt:
                f = tf.get(tok, 0)
                if not f:
                    continue
                df = self.df.get(tok, 0) or 1
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / self.avg_len))
            if s > 0:
                scores.append((s, i))
        table_score = {}
        for s, i in scores:
            for t in self.docs[i]["tables"]:
                table_score[t] = table_score.get(t, 0.0) + s
        lowq = question.lower()
        for t, cs in self.meta.items():
            if t in lowq:
                table_score[t] = table_score.get(t, 0.0) + 100.0
            elif any(c not in self.generic_cols and c in lowq for c in cs):
                table_score[t] = table_score.get(t, 0.0) + 30.0
        return table_score

    def select(self, question, top_k=6):
        ts = self.score(question)
        if not ts:
            return []
        return [t for t, _ in sorted(ts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]]
