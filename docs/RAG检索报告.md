# RAG Schema Linking 检索报告

> 问题数: 46; 全库 schema: 1618 字符(含 8 张表); 检索: BM25 + 中文 bigram; 注入=候选表完整结构 + 其余表名/列名目录。
> 召回判定: 金标 expect_tables 全部出现在召回的 top-k 表内(宁多勿漏)。

| top-k | 命中金标表(题数) | 召回率 | 平均注入 schema 字符(混合) | 相对全库 |
|---|---|---|---|---|
| 1 | 15/46 | 32.6% | 892 | 55.1% |
| 3 | 39/46 | 84.8% | 1118 | 69.1% |
| 4 | 43/46 | 93.5% | 1204 | 74.4% |
| 6 | 44/46 | 95.7% | 1305 | 80.7% |

## 未被 top-6 召回覆盖的题

| ID | 金标表 | top-k 召回 |
|---|---|---|
| C02 | ads_cust_info_d, dim_product, dim_public, dwd_cust_hold_d, dws_cust_aset_d, dws_cust_fin_d | dim_public, ads_cust_info_d, dim_product, dwd_cust_hold_d, dws_cust_fin_d, dwd_cust_tran_d |
| C03 | dim_product, dwd_cust_tran_d, dws_cust_aset_d | dwd_cust_tran_d, dws_cust_aset_d, dwd_cust_hold_d, dws_cust_fin_d, ads_cust_info_d, dim_public |

## 结论

- top-6 对金标表的召回率 95.7%; 平均 schema 字符 1305, 为全库的 80.7%。
- 当前 8 表库上缩减有限; schema 越大(表数越多), RAG 的收益近似线性放大。
- 引擎默认 retrieve_mode=full(不改变既有评测); retrieve_mode=rag 时才注入召回短 schema。

## 端到端(全量 46 问, retrieve_mode=rag, 真实 LLM)

> 来源: `run_20261008_024128.json`(全量运行 `--no-report`, 未覆盖 docs/评测报告-基线.md); 模型/环境与基线一致。

| 指标 | 全部 | 简单 | 中等 | 复杂 |
|---|---|---|---|---|
| SQL 可执行率 | 100.0% | 100.0% | 100.0% | 100.0% |
| 结果正确率 | 100.0% | 100.0% | 100.0% | 100.0% |
| 口径正确率 | 100.0% | 100.0% | 100.0% | 100.0% |
| 端到端准确率 | 100.0% | 100.0% | 100.0% | 100.0% |
| 幻觉率 | 0.0% | 0.0% | 0.0% | 0.0% |

| 耗时 | 全部 | 简单 | 中等 | 复杂 |
|---|---|---|---|---|
| P50 | 8.1s | 5.7s | 8.8s | 9.3s |
| P90 | 10.3s | 8.1s | 10.1s | 11.9s |

- RAG 召回: 平均 4.7 表/题; 金标表全覆盖 44/46(与离线 top-6 的 44/46 一致)。
- 对照 full 基线(同代码默认模式, `docs/评测报告-基线.md`): e2e 100.0%, P50 7.1s / P90 9.1s。
- 注: 未召回的表仍以「表名/列名目录」进 prompt, 所以 C02/C03 漏召回未导致答错; 单轮数字不构成统计显著结论。