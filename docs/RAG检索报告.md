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