
### M05 medium — 按年龄段(30 岁以下/30-50/50-60/60 以上)客户分别有多少人?
- 类型: 结果不一致
- agent_sql: `SELECT 
  CASE 
    WHEN cust_age < 30 THEN '<30' 
    WHEN cust_age >= 30 AND cust_age < 50 THEN '[30,50)' 
    WHEN cust_age >= 50 AND cust_age < 60 THEN '[50,60)' 
    ELSE '>=60' 
  END AS 年龄段,
  COUNT(pty_id) AS 客户人数
FROM ads_cust_info_d
GROUP BY 年龄段
ORDER BY 年龄段`
- gold_sql: `SELECT CASE WHEN cust_age<30 THEN '30岁以下' WHEN cust_age<50 THEN '30-50岁' WHEN cust_age<60 THEN '50-60岁' ELSE '60岁以上' END AS 年龄段, COUNT(*) AS 客户数 FROM ads_cust_info_d WHERE data_dt='20260531' GROUP BY 年龄段 ORDER BY 年龄段`
- gold_rows(前5): [['30-50岁', 34], ['50-60岁', 204], ['60岁以上', 262]]
- agent_rows(前5): [['>=60', 262], ['[30,50)', 34], ['[50,60)', 204]]
- 口径: 年龄分桶边界: [0,30) [30,50) [50,60) [60,∞)。

### M11 medium — 2026-03-31 各客户等级客户的总资产分别是多少?
- 类型: 结果不一致
- agent_sql: `SELECT 
  dim_public.code AS cust_lvl_cd,
  dim_public.describe AS 客户等级,
  SUM(dws_cust_aset_d.nm_tot_aset + dws_cust_aset_d.fc_pur_aset) AS 总资产
FROM ads_cust_info_d
JOIN dim_public 
  ON ads_cust_info_d.cust_lvl_cd = dim_public.code 
  AND dim_public.code_type_id = '100'
JOIN dws_cust_aset_d 
  ON ads_cust_info_d.pty_id = dws_cust_aset_d.pty_id
WHERE dws_cust_aset_d.data_dt = '20260331'
GROUP BY dim_public.code, dim_public.describe
ORDER BY 总资产 DESC`
- gold_sql: `SELECT p.describe AS 客户等级, ROUND(SUM(s.nm_tot_aset+s.fc_pur_aset),4) AS 总资产 FROM dws_cust_aset_d s JOIN ads_cust_info_d a ON s.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE s.data_dt='20260331' GROUP BY p.describe ORDER BY 总资产 DESC`
- gold_rows(前5): [['紫金理财钻石卡客户', 374619378.0222], ['紫金理财白金卡客户', 140762627.2047], ['紫金理财金卡客户', 60061397.7867], ['紫金理财银卡客户', 19356475.8411], ['紫金理财卡客户', 1407726.9494]]
- agent_rows(前5): [['1000001', '紫金理财钻石卡客户', 374619378.0222], ['1000002', '紫金理财白金卡客户', 140762627.2047], ['1000003', '紫金理财金卡客户', 60061397.7867], ['1000004', '紫金理财银卡客户', 19356475.8411], ['1000005', '紫金理财卡客户', 1407726.9494]]
- 口径: 资产取 20260331 快照;等级经 100 解码;总资产=nm_tot_aset+fc_pur_aset。

### C02 complex — 紫金理财钻石卡、男性、40 岁以上、且 2026-03-31 持有比亚迪相关产品超过 1000 的客户,2026 年一季度盈亏合计是多少?
- 类型: 结果不一致
- agent_sql: `WITH filtered_customers AS (
    SELECT DISTINCT a.pty_id
    FROM ads_cust_info_d a
    JOIN dim_public lvl ON a.cust_lvl_cd = lvl.code AND lvl.code_type_id = '100'
    JOIN dim_public g ON a.gender_cd = g.code AND g.code_type_id = '500'
    WHERE a.data_dt = '20260531'
      AND lvl.describe = '紫金理财钻石卡客户'
      AND g.describe = '男'
      AND a.cust_age > 40
),
hold_customers AS (
    SELECT DISTINCT h.pty_id
    FROM dwd_cust_hold_d h
    JOIN dim_product p ON h.prdt_id = p.prdt_id
    WHERE h.data_dt = '20260331'
      AND p.prdt_name LIKE '%比亚迪%'
      AND h.mkt_val > 1000
),
target_customers AS (
    SELECT pty_id FROM filtered_customers
    INTERSECT
    SELECT pty_id FROM hold_customers
),
aset_data AS (
    SELECT 
        a.pty_id,
        MAX(CASE WHEN a.data_dt = '20260331' THEN a.nm_tot_aset + a.fc_pur_aset END) AS end_aset,
        MIN(CASE WHEN a.data_dt = '20260101' THEN a.nm_tot_aset + a.fc_pur_aset END) AS start_aset
    FROM dws_cust_aset_d a
    WHERE a.data_dt BETWEEN '20260101' AND '20260331'
      AND a.pty_id IN (SELECT pty_id FROM target_customers)
    GROUP BY a.pty_id
),
fin_data AS (
    SELECT 
        f.pty_id,
        SUM(f.cash_in + f.tran_in + f.assign_in) AS total_in,
        SUM(f.cash_out + f.tran_out + f.assign_out) AS total_out
    FROM dws_cust_fin_d f
    WHERE f.data_dt BETWEEN '20260101' AND '20260331'
      AND f.pty_id IN (SELECT pty_id FROM target_customers)
    GROUP BY f.pty_id
)
SELECT 
    SUM((COALESCE(a.end_aset, 0) - COALESCE(a.start_aset, 0)) + (COALESCE(f.total_out, 0) - COALESCE(f.total_in, 0))) AS 盈亏合计
FROM target_customers tc
LEFT JOIN aset_data a ON tc.pty_id = a.pty_id
LEFT JOIN fin_data f ON tc.pty_id = f.pty_id`
- gold_sql: `WITH seg AS (SELECT DISTINCT a.pty_id FROM ads_cust_info_d a JOIN dim_public l ON a.cust_lvl_cd=l.code AND l.code_type_id='100' JOIN dim_public g ON a.gender_cd=g.code AND g.code_type_id='500' WHERE a.data_dt='20260531' AND l.describe='紫金理财钻石卡客户' AND g.describe='男' AND a.cust_age>40 AND a.pty_id IN (SELECT h.pty_id FROM dwd_cust_hold_d h JOIN dim_product d ON h.prdt_id=d.prdt_id WHERE h.data_dt='20260331' AND d.prdt_name LIKE '%比亚迪%' GROUP BY h.pty_id HAVING SUM(h.hold_cnt)>1000)), s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id), f AS (SELECT pty_id, SUM(cash_out+tran_out+assign_out) AS o, SUM(cash_in+tran_in+assign_in) AS i FROM dws_cust_fin_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id) SELECT COUNT(*) AS 客户数, ROUND(SUM(COALESCE(e.a,0)-COALESCE(s.a,0)+COALESCE(f.o,0)-COALESCE(f.i,0)),4) AS 盈亏合计 FROM seg LEFT JOIN e ON seg.pty_id=e.pty_id LEFT JOIN s ON seg.pty_id=s.pty_id LEFT JOIN f ON seg.pty_id=f.pty_id`
- gold_rows(前5): [[1, -661039.389]]
- agent_rows(前5): [[-661039.3889999986]]
- 口径: 客群=钻石卡(100 解码)+男(500 解码)+age>40+期末持有比亚迪>1000 份;盈亏口径同 C01。

### C04 complex — 2026-03-31 相对 2026-01-01,总资产增幅最大的 10 个客户是哪些?
- 类型: 结果不一致
- agent_sql: `WITH s AS (
    SELECT pty_id, SUM(nm_tot_aset + fc_pur_aset) AS a
    FROM dws_cust_aset_d
    WHERE data_dt = '20260101'
    GROUP BY pty_id
),
e AS (
    SELECT pty_id, SUM(nm_tot_aset + fc_pur_aset) AS a
    FROM dws_cust_aset_d
    WHERE data_dt = '20260331'
    GROUP BY pty_id
)
SELECT e.pty_id, e.a - COALESCE(s.a, 0) AS 总资产增幅
FROM e
LEFT JOIN s ON e.pty_id = s.pty_id
ORDER BY 总资产增幅 DESC
LIMIT 10`
- gold_sql: `WITH s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id) SELECT e.pty_id AS 客户号, ROUND(e.a-COALESCE(s.a,0),4) AS 资产增幅 FROM e LEFT JOIN s ON e.pty_id=s.pty_id ORDER BY 资产增幅 DESC LIMIT 10`
- gold_rows(前5): [['C000000000000102', 6061179.89], ['C000000000000007', 4020828.39], ['C000000000000010', 2640611.298], ['C000000000000169', 1944799.92], ['C000000000000132', 1552473.356]]
- agent_rows(前5): [['C000000000000102', 6061179.890000001], ['C000000000000007', 4020828.3900000006], ['C000000000000010', 2640611.2980000004], ['C000000000000169', 1944799.9200000018], ['C000000000000132', 1552473.356]]
- 口径: 增幅=期末-期初(期初缺记录按 0);资产=nm_tot_aset+fc_pur_aset;取前10。

### C07 complex — 2026-01-10 至 2026-02-15,科创板产品交易额超过 25 万的客户,按营业部统计交易额?
- 类型: 结果不一致
- agent_sql: `SELECT 
  b.up_org_name AS 分公司,
  b.org_name AS 营业部名称,
  SUM(t.buy_amt + t.sell_amt) AS 交易额
FROM dwd_cust_tran_d t
JOIN dim_product p ON t.prdt_id = p.prdt_id
JOIN ads_cust_info_d c ON t.pty_id = c.pty_id
JOIN dim_branch b ON c.org_id = b.org_id
WHERE t.data_dt BETWEEN '20260110' AND '20260215'
  AND p.prdt_type_name LIKE '%科创%'
GROUP BY b.up_org_name, b.org_name
HAVING SUM(t.buy_amt + t.sell_amt) > 250000
ORDER BY 交易额 DESC`
- gold_sql: `WITH t AS (SELECT t.pty_id, SUM(t.buy_amt+t.sell_amt) AS amt FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260110' AND '20260215' AND d.prdt_type_name LIKE '%科创%' GROUP BY t.pty_id HAVING SUM(t.buy_amt+t.sell_amt)>250000) SELECT b.up_org_name AS 分公司, b.org_name AS 营业部, COUNT(*) AS 客户数, ROUND(SUM(t.amt),4) AS 交易额 FROM t JOIN ads_cust_info_d a ON t.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_branch b ON a.org_id=b.org_id GROUP BY b.up_org_name, b.org_name ORDER BY 交易额 DESC`
- gold_rows(前5): [['苏州分公司', '张家******营业部', 44, 172005739.27], ['无锡分公司', '无锡******营业部', 1, 5114499.16], ['南京分公司', '南京******营业部', 1, 625231.01]]
- agent_rows(前5): [['苏州分公司', '张家******营业部', 174227990.91], ['无锡分公司', '无锡******营业部', 5114499.16], ['南京分公司', '南京******营业部', 625231.01]]
- 口径: 交易量按金额口径(队伍约定)=buy_amt+sell_amt;科创板按 prdt_type_name LIKE;区间=20260110~20260215。

### C11 complex — 按年龄段和客户等级交叉统计客户人数?
- 类型: 结果不一致
- agent_sql: `SELECT 
  CASE 
    WHEN a.cust_age < 30 THEN '<30'
    WHEN a.cust_age >= 30 AND a.cust_age < 50 THEN '[30,50)'
    WHEN a.cust_age >= 50 AND a.cust_age < 60 THEN '[50,60)'
    ELSE '>=60'
  END AS 年龄段,
  p.describe AS 客户等级,
  COUNT(a.pty_id) AS 客户人数
FROM ads_cust_info_d a
JOIN dim_public p ON a.cust_lvl_cd = p.code AND p.code_type_id = '100'
WHERE a.data_dt = '20260531'
GROUP BY 年龄段, p.describe
ORDER BY 年龄段, p.describe`
- gold_sql: `SELECT p.describe AS 客户等级, CASE WHEN cust_age<30 THEN '30岁以下' WHEN cust_age<50 THEN '30-50岁' WHEN cust_age<60 THEN '50-60岁' ELSE '60岁以上' END AS 年龄段, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE a.data_dt='20260531' GROUP BY p.describe, 年龄段 ORDER BY 客户等级, 年龄段`
- gold_rows(前5): [['空', '30-50岁', 3], ['空', '50-60岁', 28], ['空', '60岁以上', 34], ['紫金理财卡客户', '30-50岁', 4], ['紫金理财卡客户', '50-60岁', 25]]
- agent_rows(前5): [['>=60', '空', 34], ['>=60', '紫金理财卡客户', 34], ['>=60', '紫金理财白金卡客户', 38], ['>=60', '紫金理财金卡客户', 69], ['>=60', '紫金理财钻石卡客户', 15]]
- 口径: 年龄分桶与 M05 同;等级经 100 解码;交叉分组。