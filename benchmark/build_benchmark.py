# -*- coding: utf-8 -*-
"""自建 40 问基准集 + 程序化闸门(gold_sql 必须真实跑通且非空)。

用法:
    python benchmark/build_benchmark.py
输出:
    benchmark/benchmark.json(40 问, gold_answer 为真实运行结果)
铁律: 一切答案来自库内真实运行, 禁止估算。
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db"
OUT = Path(__file__).resolve().parent / "benchmark.json"


def connect():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def probe():
    """预探产品名/板块可用性, 供出题校准。"""
    c = connect()
    for kw in ("比亚迪", "招商银行", "中国平安"):
        n = c.execute("SELECT COUNT(*) FROM dim_product WHERE prdt_name LIKE ?", (f"%{kw}%",)).fetchone()[0]
        print(f"[probe] prdt_name LIKE %{kw}% -> {n}")
    n1 = c.execute("SELECT COUNT(*) FROM dim_product WHERE prdt_type_name LIKE '%科创%'").fetchone()[0]
    n2 = c.execute("SELECT COUNT(*) FROM dim_product WHERE prdt_name LIKE '%科创%'").fetchone()[0]
    print(f"[probe] prdt_type_name LIKE %科创% -> {n1}; prdt_name LIKE %科创% -> {n2}")
    rows = c.execute("SELECT DISTINCT prdt_type_name FROM dim_product WHERE prdt_type_name LIKE '%科创%' LIMIT 10").fetchall()
    print("        科创类型样例:", [r[0] for r in rows])
    c.close()


SIMPLE = [
    ("S01", "客户主档中共有多少客户?", "客户筛选", ["ads_cust_info_d"],
     "SELECT COUNT(*) AS c FROM ads_cust_info_d WHERE data_dt='20260531'",
     "客户表只有 20260531 一个快照。", ["20260531"], ["ads_cust_info_d"]),
    ("S02", "客户的平均年龄是多少?", "客户筛选", ["ads_cust_info_d"],
     "SELECT ROUND(AVG(cust_age),2) AS 平均年龄 FROM ads_cust_info_d WHERE data_dt='20260531'",
     "年龄直接取 cust_age;客户表单快照 20260531。", ["20260531"], ["ads_cust_info_d"]),
    ("S03", "客户中年龄最大的是多少岁?", "客户筛选", ["ads_cust_info_d"],
     "SELECT MAX(cust_age) AS 最大年龄 FROM ads_cust_info_d WHERE data_dt='20260531'",
     "年龄直接取 cust_age;客户表单快照 20260531。", ["20260531"], ["ads_cust_info_d"]),
    ("S04", "各省份分别有多少客户?", "客户筛选", ["ads_cust_info_d"],
     "SELECT prov_name AS 省份, COUNT(*) AS 客户数 FROM ads_cust_info_d WHERE data_dt='20260531' GROUP BY prov_name ORDER BY 客户数 DESC",
     "按 prov_name 分组,时间=客户快照 20260531。", ["20260531"], ["ads_cust_info_d"]),
    ("S05", "各城市分别有多少客户?", "客户筛选", ["ads_cust_info_d"],
     "SELECT city_name AS 城市, COUNT(*) AS 客户数 FROM ads_cust_info_d WHERE data_dt='20260531' GROUP BY city_name ORDER BY 客户数 DESC",
     "按 city_name 分组,时间=客户快照 20260531。", ["20260531"], ["ads_cust_info_d"]),
    ("S06", "共有多少个营业部?", "维度查询", ["dim_branch"],
     "SELECT COUNT(DISTINCT org_id) AS 营业部数 FROM dim_branch",
     "营业部以 dim_branch.org_id 去重。", [], ["dim_branch"]),
    ("S07", "产品维表里共有多少个产品?", "维度查询", ["dim_product"],
     "SELECT COUNT(DISTINCT prdt_id) AS 产品数 FROM dim_product",
     "产品以 prdt_id 去重。", [], ["dim_product"]),
    ("S08", "产品共分为多少个二级分类?", "维度查询", ["dim_product"],
     "SELECT COUNT(DISTINCT prdt_type_id) AS 二级分类数 FROM dim_product",
     "二级分类以 prdt_type_id 去重。", [], ["dim_product"]),
    ("S09", "2026-03-31 全库客户总资产是多少?", "指标查询", ["dws_cust_aset_d"],
     "SELECT ROUND(SUM(nm_tot_aset+fc_pur_aset),4) AS 总资产 FROM dws_cust_aset_d WHERE data_dt='20260331'",
     "总资产=nm_tot_aset+fc_pur_aset;取 20260331 快照。", ["20260331"], ["dws_cust_aset_d"]),
    ("S10", "2026-03-31 全库客户现金资产合计是多少?", "指标查询", ["dws_cust_aset_d"],
     "SELECT ROUND(SUM(nm_bal+fc_bal),4) AS 现金资产 FROM dws_cust_aset_d WHERE data_dt='20260331'",
     "现金资产=nm_bal+fc_bal;取 20260331 快照。", ["20260331"], ["dws_cust_aset_d"]),
    ("S11", "2026 年一季度全库客户买入金额合计是多少?", "指标查询", ["dwd_cust_tran_d"],
     "SELECT ROUND(SUM(buy_amt),4) AS 买入金额 FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331'",
     "一季度=20260101~20260331;买入金额=buy_amt。", ["20260101", "20260331"], ["dwd_cust_tran_d"]),
    ("S12", "2026 年一季度全库客户卖出金额合计是多少?", "指标查询", ["dwd_cust_tran_d"],
     "SELECT ROUND(SUM(sell_amt),4) AS 卖出金额 FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331'",
     "一季度=20260101~20260331;卖出金额=sell_amt。", ["20260101", "20260331"], ["dwd_cust_tran_d"]),
    ("S13", "2026 年一季度客户交易笔数(买入+卖出)合计是多少?", "指标查询", ["dwd_cust_tran_d"],
     "SELECT ROUND(SUM(buy_cnt+sell_cnt),0) AS 交易笔数 FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331'",
     "一季度=20260101~20260331;交易笔数=buy_cnt+sell_cnt。", ["20260101", "20260331"], ["dwd_cust_tran_d"]),
    ("S14", "2026-03-31 全库客户持仓市值合计是多少?", "指标查询", ["dwd_cust_hold_d"],
     "SELECT ROUND(SUM(mkt_val),4) AS 持仓市值 FROM dwd_cust_hold_d WHERE data_dt='20260331'",
     "持仓市值=mkt_val;取 20260331 快照。", ["20260331"], ["dwd_cust_hold_d"]),
]

MEDIUM = [
    ("M01", "各客户等级分别有多少客户?", "客户筛选", ["ads_cust_info_d", "dim_public"],
     "SELECT p.describe AS 客户等级, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE a.data_dt='20260531' GROUP BY p.describe ORDER BY 客户数 DESC",
     "客户等级=dim_public code_type_id=100 解码 cust_lvl_cd;客户表单快照。", ["20260531"], ["ads_cust_info_d", "dim_public"]),
    ("M02", "男性、女性客户各有多少人?", "客户筛选", ["ads_cust_info_d", "dim_public"],
     "SELECT p.describe AS 性别, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public p ON a.gender_cd=p.code AND p.code_type_id='500' WHERE a.data_dt='20260531' GROUP BY p.describe ORDER BY 客户数 DESC",
     "性别=dim_public code_type_id=500 解码 gender_cd。", ["20260531"], ["ads_cust_info_d", "dim_public"]),
    ("M03", "各学历客户分别有多少人?", "客户筛选", ["ads_cust_info_d", "dim_public"],
     "SELECT p.describe AS 学历, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public p ON a.edu_cd=p.code AND p.code_type_id='600' WHERE a.data_dt='20260531' GROUP BY p.describe ORDER BY 客户数 DESC",
     "学历=dim_public code_type_id=600 解码 edu_cd。", ["20260531"], ["ads_cust_info_d", "dim_public"]),
    ("M04", "各职业客户分别有多少人(前 10)?", "客户筛选", ["ads_cust_info_d", "dim_public"],
     "SELECT p.describe AS 职业, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public p ON a.prof_cd=p.code AND p.code_type_id='700' WHERE a.data_dt='20260531' GROUP BY p.describe ORDER BY 客户数 DESC LIMIT 10",
     "职业=dim_public code_type_id=700 解码 prof_cd;取前10。", ["20260531"], ["ads_cust_info_d", "dim_public"]),
    ("M05", "按年龄段(30 岁以下/30-50/50-60/60 以上)客户分别有多少人?", "客户筛选", ["ads_cust_info_d"],
     "SELECT CASE WHEN cust_age<30 THEN '30岁以下' WHEN cust_age<50 THEN '30-50岁' WHEN cust_age<60 THEN '50-60岁' ELSE '60岁以上' END AS 年龄段, COUNT(*) AS 客户数 FROM ads_cust_info_d WHERE data_dt='20260531' GROUP BY 年龄段 ORDER BY 年龄段",
     "年龄分桶边界: [0,30) [30,50) [50,60) [60,∞)。", ["20260531"], ["ads_cust_info_d"]),
    ("M06", "硕士及以上学历的女性客户有多少人?", "客户筛选", ["ads_cust_info_d", "dim_public"],
     "SELECT COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public e ON a.edu_cd=e.code AND e.code_type_id='600' JOIN dim_public g ON a.gender_cd=g.code AND g.code_type_id='500' WHERE a.data_dt='20260531' AND g.describe='女' AND e.describe IN ('硕士','博士')",
     "硕士及以上=硕士/博士;女性经 500 解码。", ["20260531"], ["ads_cust_info_d", "dim_public"]),
    ("M07", "各分公司下分别有多少营业部?", "维度查询", ["dim_branch"],
     "SELECT up_org_name AS 分公司, COUNT(*) AS 营业部数 FROM dim_branch GROUP BY up_org_name ORDER BY 营业部数 DESC",
     "分公司=dim_branch.up_org_name 分组。", [], ["dim_branch"]),
    ("M08", "2026 年一季度各产品一级分类的买入金额分别是多少?", "指标查询", ["dwd_cust_tran_d", "dim_product"],
     "SELECT d.up_prdt_type_id AS 一级分类ID, d.up_prdt_type_name AS 一级分类, ROUND(SUM(t.buy_amt),4) AS 买入金额 FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260101' AND '20260331' GROUP BY d.up_prdt_type_id, d.up_prdt_type_name ORDER BY 买入金额 DESC",
     "一季度=20260101~20260331;一级分类用 up_prdt_type_id+up_prdt_type_name 分组(存在同名多义)。", ["20260101", "20260331"], ["dwd_cust_tran_d", "dim_product"]),
    ("M09", "各分公司分别有多少客户?", "客户筛选", ["ads_cust_info_d", "dim_branch"],
     "SELECT b.up_org_name AS 分公司, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_branch b ON a.org_id=b.org_id WHERE a.data_dt='20260531' GROUP BY b.up_org_name ORDER BY 客户数 DESC",
     "客户归属营业部 org_id, 经 dim_branch 上卷到分公司。", ["20260531"], ["ads_cust_info_d", "dim_branch"]),
    ("M10", "2026 年一季度普通账户和信用账户的交易金额分别是多少?", "指标查询", ["dwd_cust_tran_d"],
     "SELECT sys_source AS 账户, ROUND(SUM(buy_amt+sell_amt),4) AS 交易金额 FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY sys_source ORDER BY sys_source",
     "nm=普通,fc=信用;交易金额=buy_amt+sell_amt。", ["20260101", "20260331"], ["dwd_cust_tran_d"]),
    ("M11", "2026-03-31 各客户等级客户的总资产分别是多少?", "指标查询", ["dws_cust_aset_d", "ads_cust_info_d", "dim_public"],
     "SELECT p.describe AS 客户等级, ROUND(SUM(s.nm_tot_aset+s.fc_pur_aset),4) AS 总资产 FROM dws_cust_aset_d s JOIN ads_cust_info_d a ON s.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE s.data_dt='20260331' GROUP BY p.describe ORDER BY 总资产 DESC",
     "资产取 20260331 快照;等级经 100 解码;总资产=nm_tot_aset+fc_pur_aset。", ["20260331", "20260531"], ["dws_cust_aset_d", "ads_cust_info_d", "dim_public"]),
    ("M12", "2026 年一季度交易额(买入+卖出)前 10 的客户是哪些?", "数据分析", ["dwd_cust_tran_d"],
     "SELECT pty_id AS 客户号, ROUND(SUM(buy_amt+sell_amt),4) AS 交易额 FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id ORDER BY 交易额 DESC LIMIT 10",
     "一季度=20260101~20260331;交易额=buy_amt+sell_amt;取前10。", ["20260101", "20260331"], ["dwd_cust_tran_d"]),
    ("M13", "2026 年一季度交易过招商银行相关产品的客户有多少人?", "客户筛选", ["dwd_cust_tran_d", "dim_product"],
     "SELECT COUNT(DISTINCT t.pty_id) AS 客户数 FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260101' AND '20260331' AND d.prdt_name LIKE '%招商银行%'",
     "招商银行产品按 prdt_name LIKE 匹配;客户按 pty_id 去重。", ["20260101", "20260331"], ["dwd_cust_tran_d", "dim_product"]),
    ("M14", "2026-03-31 持仓市值最高的 10 个产品二级分类是哪些?", "数据分析", ["dwd_cust_hold_d", "dim_product"],
     "SELECT d.prdt_type_id AS 二级分类ID, d.prdt_type_name AS 二级分类, ROUND(SUM(h.mkt_val),4) AS 持仓市值 FROM dwd_cust_hold_d h JOIN dim_product d ON h.prdt_id=d.prdt_id WHERE h.data_dt='20260331' GROUP BY d.prdt_type_id, d.prdt_type_name ORDER BY 持仓市值 DESC LIMIT 10",
     "持仓取 20260331 快照;二级分类用 prdt_type_id+prdt_type_name 分组;取前10。", ["20260331"], ["dwd_cust_hold_d", "dim_product"]),
]

COMPLEX = [
    ("C01", "2026 年一季度全库客户的盈亏合计是多少?(盈亏=期末总资产-期初总资产+资金流出-资金流入)",
     "数据分析", ["dws_cust_aset_d", "dws_cust_fin_d"],
     "WITH s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id), f AS (SELECT pty_id, SUM(cash_out+tran_out+assign_out) AS o, SUM(cash_in+tran_in+assign_in) AS i FROM dws_cust_fin_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id) SELECT ROUND(SUM(COALESCE(e.a,0)-COALESCE(s.a,0)+COALESCE(f.o,0)-COALESCE(f.i,0)),4) AS 盈亏合计 FROM e LEFT JOIN s ON e.pty_id=s.pty_id LEFT JOIN f ON e.pty_id=f.pty_id",
     "盈亏=期末资产-期初资产+资金流出-资金流入(队伍口径约定);资产=nm_tot_aset+fc_pur_aset;资金流=cash/tran/assign 三列。",
     ["20260101", "20260331"], ["dws_cust_aset_d", "dws_cust_fin_d"]),
    ("C02", "紫金理财钻石卡、男性、40 岁以上、且 2026-03-31 持有比亚迪相关产品超过 1000 的客户,2026 年一季度盈亏合计是多少?",
     "数据分析", ["ads_cust_info_d", "dim_public", "dwd_cust_hold_d", "dim_product", "dws_cust_aset_d", "dws_cust_fin_d"],
     "WITH seg AS (SELECT DISTINCT a.pty_id FROM ads_cust_info_d a JOIN dim_public l ON a.cust_lvl_cd=l.code AND l.code_type_id='100' JOIN dim_public g ON a.gender_cd=g.code AND g.code_type_id='500' WHERE a.data_dt='20260531' AND l.describe='紫金理财钻石卡客户' AND g.describe='男' AND a.cust_age>40 AND a.pty_id IN (SELECT h.pty_id FROM dwd_cust_hold_d h JOIN dim_product d ON h.prdt_id=d.prdt_id WHERE h.data_dt='20260331' AND d.prdt_name LIKE '%比亚迪%' GROUP BY h.pty_id HAVING SUM(h.hold_cnt)>1000)), s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id), f AS (SELECT pty_id, SUM(cash_out+tran_out+assign_out) AS o, SUM(cash_in+tran_in+assign_in) AS i FROM dws_cust_fin_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id) SELECT COUNT(*) AS 客户数, ROUND(SUM(COALESCE(e.a,0)-COALESCE(s.a,0)+COALESCE(f.o,0)-COALESCE(f.i,0)),4) AS 盈亏合计 FROM seg LEFT JOIN e ON seg.pty_id=e.pty_id LEFT JOIN s ON seg.pty_id=s.pty_id LEFT JOIN f ON seg.pty_id=f.pty_id",
     "客群=钻石卡(100 解码)+男(500 解码)+age>40+期末持有比亚迪>1000 份;盈亏口径同 C01。",
     ["20260101", "20260331", "20260531"], ["ads_cust_info_d", "dim_public", "dwd_cust_hold_d", "dim_product", "dws_cust_aset_d", "dws_cust_fin_d"]),
    ("C03", "2026 年一季度日均总资产超过 30 万、且股票交易额超过 10 万的客户有多少人?",
     "客户筛选", ["dws_cust_aset_d", "dwd_cust_tran_d", "dim_product"],
     "WITH a AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset)/90.0 AS avg_aset FROM dws_cust_aset_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id), t AS (SELECT t.pty_id, SUM(t.buy_amt+t.sell_amt) AS amt FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260101' AND '20260331' AND d.up_prdt_type_id='PT040000' GROUP BY t.pty_id) SELECT COUNT(*) AS 客户数 FROM a JOIN t ON a.pty_id=t.pty_id WHERE a.avg_aset>300000 AND t.amt>100000",
     "日均=区间合计/90(资产表 90 天);股票=up_prdt_type_id='PT040000';交易额=buy_amt+sell_amt。",
     ["20260101", "20260331"], ["dws_cust_aset_d", "dwd_cust_tran_d", "dim_product"]),
    ("C04", "2026-03-31 相对 2026-01-01,总资产增幅最大的 10 个客户是哪些?",
     "数据分析", ["dws_cust_aset_d"],
     "WITH s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id) SELECT e.pty_id AS 客户号, ROUND(e.a-COALESCE(s.a,0),4) AS 资产增幅 FROM e LEFT JOIN s ON e.pty_id=s.pty_id ORDER BY 资产增幅 DESC LIMIT 10",
     "增幅=期末-期初(期初缺记录按 0);资产=nm_tot_aset+fc_pur_aset;取前10。",
     ["20260101", "20260331"], ["dws_cust_aset_d"]),
    ("C05", "2026 年一季度各客户等级客户的资金净流入(流入-流出)分别是多少?",
     "数据分析", ["dws_cust_fin_d", "ads_cust_info_d", "dim_public"],
     "WITH f AS (SELECT pty_id, SUM(cash_in+tran_in+assign_in) AS i, SUM(cash_out+tran_out+assign_out) AS o FROM dws_cust_fin_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id) SELECT p.describe AS 客户等级, ROUND(SUM(f.i-f.o),4) AS 净流入 FROM f JOIN ads_cust_info_d a ON f.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' GROUP BY p.describe ORDER BY 净流入 DESC",
     "净流入=流入-流出;流入/流出=cash/tran/assign 三列合计;等级经 100 解码。",
     ["20260101", "20260331", "20260531"], ["dws_cust_fin_d", "ads_cust_info_d", "dim_public"]),
    ("C06", "2026 年一季度交易过招商银行相关产品、且 2026-03-31 持有中国平安相关产品的客户有多少人?",
     "客户筛选", ["dwd_cust_tran_d", "dwd_cust_hold_d", "dim_product"],
     "SELECT COUNT(*) AS 客户数 FROM (SELECT DISTINCT t.pty_id FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260101' AND '20260331' AND d.prdt_name LIKE '%招商银行%' INTERSECT SELECT DISTINCT h.pty_id FROM dwd_cust_hold_d h JOIN dim_product d ON h.prdt_id=d.prdt_id WHERE h.data_dt='20260331' AND d.prdt_name LIKE '%中国平安%')",
     "两客群交集(INTERSECT);产品按 prdt_name LIKE 匹配。", ["20260101", "20260331"], ["dwd_cust_tran_d", "dwd_cust_hold_d", "dim_product"]),
    ("C07", "2026-01-10 至 2026-02-15,科创板产品交易额超过 25 万的客户,按营业部统计交易额?",
     "数据分析", ["dwd_cust_tran_d", "dim_product", "ads_cust_info_d", "dim_branch"],
     "WITH t AS (SELECT t.pty_id, SUM(t.buy_amt+t.sell_amt) AS amt FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260110' AND '20260215' AND d.prdt_type_name LIKE '%科创%' GROUP BY t.pty_id HAVING SUM(t.buy_amt+t.sell_amt)>250000) SELECT b.up_org_name AS 分公司, b.org_name AS 营业部, COUNT(*) AS 客户数, ROUND(SUM(t.amt),4) AS 交易额 FROM t JOIN ads_cust_info_d a ON t.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_branch b ON a.org_id=b.org_id GROUP BY b.up_org_name, b.org_name ORDER BY 交易额 DESC",
     "交易量按金额口径(队伍约定)=buy_amt+sell_amt;科创板按 prdt_type_name LIKE;区间=20260110~20260215。",
     ["20260110", "20260215"], ["dwd_cust_tran_d", "dim_product", "ads_cust_info_d", "dim_branch"]),
    ("C08", "2026-03-31 各客户等级中总资产最高的客户及其资产是多少?",
     "数据分析", ["dws_cust_aset_d", "ads_cust_info_d", "dim_public"],
     "WITH r AS (SELECT s.pty_id, s.nm_tot_aset+s.fc_pur_aset AS total, p.describe AS 等级, ROW_NUMBER() OVER (PARTITION BY p.describe ORDER BY s.nm_tot_aset+s.fc_pur_aset DESC) AS rn FROM dws_cust_aset_d s JOIN ads_cust_info_d a ON s.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE s.data_dt='20260331') SELECT 等级, pty_id AS 客户号, ROUND(total,4) AS 总资产 FROM r WHERE rn=1 ORDER BY 总资产 DESC",
     "资产取 20260331 快照;窗口按等级分组取最高。", ["20260331", "20260531"], ["dws_cust_aset_d", "ads_cust_info_d", "dim_public"]),
    ("C09", "2026 年一季度交易天数最多的 5 个客户是哪些?", "数据分析", ["dwd_cust_tran_d"],
     "SELECT pty_id AS 客户号, COUNT(DISTINCT data_dt) AS 交易天数 FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id ORDER BY 交易天数 DESC LIMIT 5",
     "交易天数=COUNT(DISTINCT data_dt);取前5。", ["20260101", "20260331"], ["dwd_cust_tran_d"]),
    ("C10", "2026 年一季度交易天数超过 20 天的客户有多少人?", "客户筛选", ["dwd_cust_tran_d"],
     "SELECT COUNT(*) AS 客户数 FROM (SELECT pty_id FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id HAVING COUNT(DISTINCT data_dt)>20)",
     "高频客户=交易天数>20;交易天数=COUNT(DISTINCT data_dt)。", ["20260101", "20260331"], ["dwd_cust_tran_d"]),
    ("C11", "按年龄段和客户等级交叉统计客户人数?", "数据分析", ["ads_cust_info_d", "dim_public"],
     "SELECT p.describe AS 客户等级, CASE WHEN cust_age<30 THEN '30岁以下' WHEN cust_age<50 THEN '30-50岁' WHEN cust_age<60 THEN '50-60岁' ELSE '60岁以上' END AS 年龄段, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE a.data_dt='20260531' GROUP BY p.describe, 年龄段 ORDER BY 客户等级, 年龄段",
     "年龄分桶与 M05 同;等级经 100 解码;交叉分组。", ["20260531"], ["ads_cust_info_d", "dim_public"]),
    ("C12", "2026-03-31 各营业部客户总资产合计最高的 10 个营业部是哪些?",
     "数据分析", ["dws_cust_aset_d", "ads_cust_info_d", "dim_branch"],
     "SELECT b.up_org_name AS 分公司, b.org_name AS 营业部, ROUND(SUM(s.nm_tot_aset+s.fc_pur_aset),4) AS 总资产 FROM dws_cust_aset_d s JOIN ads_cust_info_d a ON s.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_branch b ON a.org_id=b.org_id WHERE s.data_dt='20260331' GROUP BY b.up_org_name, b.org_name ORDER BY 总资产 DESC LIMIT 10",
     "资产取 20260331 快照;客户归属营业部经 org_id 上卷;取前10。", ["20260331", "20260531"], ["dws_cust_aset_d", "ads_cust_info_d", "dim_branch"]),
]


def build_entries():
    entries = []
    for group, diff in ((SIMPLE, "simple"), (MEDIUM, "medium"), (COMPLEX, "complex")):
        for qid, question, category, tables, gold_sql, caliber_notes, expect_time, expect_tables in group:
            entries.append({
                "id": qid,
                "question": question,
                "difficulty": diff,
                "category": category,
                "tables": tables,
                "gold_sql": gold_sql,
                "caliber_notes": caliber_notes,
                "expect_time": expect_time,
                "expect_tables": expect_tables,
                "scenario": "normal",
            })
    return entries


def gate(entries):
    c = connect()
    failed = []
    for e in entries:
        try:
            cur = c.execute(e["gold_sql"])
            headers = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if not rows:
                failed.append((e["id"], "空结果(不合规:基准题必须非空)"))
                continue
            e["gold_answer"] = {
                "type": "scalar" if (len(rows) == 1 and len(headers) == 1) else "grouped",
                "headers": headers,
                "rows": rows,
                "tolerance": 1e-6,
            }
        except Exception as ex:
            failed.append((e["id"], f"{type(ex).__name__}: {ex}"))
    c.close()
    return failed


def main():
    probe()
    entries = build_entries()
    n_s = sum(1 for e in entries if e["difficulty"] == "simple")
    n_m = sum(1 for e in entries if e["difficulty"] == "medium")
    n_c = sum(1 for e in entries if e["difficulty"] == "complex")
    print(f"\n共 {len(entries)} 题: 简单 {n_s} / 中等 {n_m} / 复杂 {n_c}")
    failed = gate(entries)
    if failed:
        print("\n[闸门] 以下题未通过(必须修复后重跑):")
        for qid, msg in failed:
            print(f"  {qid}: {msg}")
        sys.exit(1)
    payload = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "db": str(DB),
            "count": len(entries),
            "note": "40 问自建基准集; gold_sql 已全部真实跑通且非空; Q&A.xlsx 空壳不依赖",
        },
        "items": entries,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[OK] 40 问全部通过闸门, 已写入 {OUT}")


if __name__ == "__main__":
    main()
