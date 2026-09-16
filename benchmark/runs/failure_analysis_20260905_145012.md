
### M08 medium — 2026 年一季度各产品一级分类的买入金额分别是多少?
- 类型: 结果不一致
- agent_sql: `SELECT dim_product.up_prdt_type_id AS 产品一级分类, SUM(dwd_cust_tran_d.buy_amt) AS 买入金额合计 FROM dwd_cust_tran_d JOIN dim_product ON dwd_cust_tran_d.prdt_id = dim_product.prdt_id WHERE dwd_cust_tran_d.data_dt BETWEEN '20260101' AND '20260331' GROUP BY dim_product.up_prdt_type_id ORDER BY 买入金额合计 DESC`
- gold_sql: `SELECT d.up_prdt_type_id AS 一级分类ID, d.up_prdt_type_name AS 一级分类, ROUND(SUM(t.buy_amt),4) AS 买入金额 FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260101' AND '20260331' GROUP BY d.up_prdt_type_id, d.up_prdt_type_name ORDER BY 买入金额 DESC`
- gold_rows(前5): [['PT040000', '股票', 1381320301.622], ['PT050000', '开放式基金', 929399333.4812], ['PT070000', '理财产品', 7581000.0], ['PT100000', '证券投资类私募', 5250000.0], ['PT030000', '债券', 2563851.77]]
- agent_rows(前5): [['PT040000', 1381320301.622], ['PT050000', 929399333.4812], ['PT070000', 7581000.0], ['PT100000', 5250000.0], ['PT030000', 2563851.77]]
- 口径: 一季度=20260101~20260331;一级分类用 up_prdt_type_id+up_prdt_type_name 分组(存在同名多义)。

### M14 medium — 2026-03-31 持仓市值最高的 10 个产品二级分类是哪些?
- 类型: 结果不一致
- agent_sql: `SELECT dp.prdt_type_id AS 产品二级分类ID, dp.prdt_type_name AS 产品二级分类名称, SUM(dh.mkt_val) AS 持仓市值 FROM dwd_cust_hold_d dh JOIN dim_product dp ON dh.prdt_id = dp.prdt_id WHERE dh.data_dt = '20260331' GROUP BY dp.prdt_type_id, dp.prdt_type_name ORDER BY 持仓市值 DESC LIMIT 10`
- gold_sql: `SELECT d.prdt_type_id AS 二级分类ID, d.prdt_type_name AS 二级分类, ROUND(SUM(h.mkt_val),4) AS 持仓市值 FROM dwd_cust_hold_d h JOIN dim_product d ON h.prdt_id=d.prdt_id WHERE h.data_dt='20260331' GROUP BY d.prdt_type_id, d.prdt_type_name ORDER BY 持仓市值 DESC LIMIT 10`
- gold_rows(前5): [['PT040100', 'A股', 317373893.69], ['PT040700', '创业板', 97685253.74], ['PT030100', '普通型国债', 38184000.0], ['PT041100', '科创板', 36674226.08], ['PT040300', '沪港通', 35381902.0]]
- agent_rows(前5): [['PT040100', 'A股', 317373893.69], ['PT040700', '创业板', 97685253.74], ['PT030100', '普通型国债', 38184000.0], ['PT041100', '科创板', 36674226.08], ['PT040300', '沪港通', 35381902.0]]
- 口径: 持仓取 20260331 快照;二级分类用 prdt_type_id+prdt_type_name 分组;取前10。

### C02 complex — 紫金理财钻石卡、男性、40 岁以上、且 2026-03-31 持有比亚迪相关产品超过 1000 的客户,2026 年一季度盈亏合计是多少?
- 类型: 结果不一致
- agent_sql: `WITH filtered_customers AS (
    SELECT DISTINCT a.pty_id
    FROM ads_cust_info_d a
    JOIN dim_public dl ON a.cust_lvl_cd = dl.code AND dl.code_type_id = '100' AND dl.describe = '紫金理财钻石卡'
    JOIN dim_public dg ON a.gender_cd = dg.code AND dg.code_type_id = '500' AND dg.describe = '男'
    WHERE a.data_dt = '20260531'
      AND a.cust_age > 40
      AND EXISTS (
          SELECT 1
          FROM dwd_cust_hold_d h
          JOIN dim_product p ON h.prdt_id = p.prdt_id
          WHERE h.pty_id = a.pty_id
            AND h.data_dt = '20260331'
            AND p.prdt_name LIKE '%比亚迪%'
            AND h.mkt_val > 1000
      )
),
cust_aset AS (
    SELECT pty_id,
           MAX(CASE WHEN data_dt = '20260331' THEN nm_tot_aset + fc_pur_aset END) AS end_aset,
           MIN(CASE WHEN data_dt = '20260101' THEN nm_tot_aset + fc_pur_aset END) AS start_aset
    FROM dws_cust_aset_d
    WHERE data_dt BETWEEN '20260101' AND '20260331'
      AND pty_id IN (SELECT pty_id FROM filtered_customers)
    GROUP BY pty_id
),
cust_fin AS (
    SELECT pty_id,
           SUM(cash_out + tran_out + assign_out) AS outflow,
           SUM(cash_in + tran_in + assign_in) AS inflow
    FROM dws_cust_fin_d
    WHERE data_dt BETWEEN '20260101' AND '20260331'
      AND pty_id IN (SELECT pty_id FROM filtered_customers)
    GROUP BY pty_id
)
SELECT SUM((ca.end_aset - ca.start_aset) + (cf.outflow - cf.inflow)) AS 盈亏合计
FROM filtered_customers fc
JOIN cust_aset ca ON fc.pty_id = ca.pty_id
JOIN cust_fin cf ON fc.pty_id = cf.pty_id`
- gold_sql: `WITH seg AS (SELECT DISTINCT a.pty_id FROM ads_cust_info_d a JOIN dim_public l ON a.cust_lvl_cd=l.code AND l.code_type_id='100' JOIN dim_public g ON a.gender_cd=g.code AND g.code_type_id='500' WHERE a.data_dt='20260531' AND l.describe='紫金理财钻石卡客户' AND g.describe='男' AND a.cust_age>40 AND a.pty_id IN (SELECT h.pty_id FROM dwd_cust_hold_d h JOIN dim_product d ON h.prdt_id=d.prdt_id WHERE h.data_dt='20260331' AND d.prdt_name LIKE '%比亚迪%' GROUP BY h.pty_id HAVING SUM(h.hold_cnt)>1000)), s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id), f AS (SELECT pty_id, SUM(cash_out+tran_out+assign_out) AS o, SUM(cash_in+tran_in+assign_in) AS i FROM dws_cust_fin_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id) SELECT COUNT(*) AS 客户数, ROUND(SUM(COALESCE(e.a,0)-COALESCE(s.a,0)+COALESCE(f.o,0)-COALESCE(f.i,0)),4) AS 盈亏合计 FROM seg LEFT JOIN e ON seg.pty_id=e.pty_id LEFT JOIN s ON seg.pty_id=s.pty_id LEFT JOIN f ON seg.pty_id=f.pty_id`
- gold_rows(前5): [[1, -661039.389]]
- agent_rows(前5): [[None]]
- 口径: 客群=钻石卡(100 解码)+男(500 解码)+age>40+期末持有比亚迪>1000 份;盈亏口径同 C01。

### C04 complex — 2026-03-31 相对 2026-01-01,总资产增幅最大的 10 个客户是哪些?
- 类型: 结果不一致
- agent_sql: `SELECT 
    a.pty_id AS 客户ID,
    c.name AS 客户名称,
    MAX(CASE WHEN a.data_dt = '20260101' THEN a.nm_tot_aset + a.fc_pur_aset END) AS 期初总资产,
    MAX(CASE WHEN a.data_dt = '20260331' THEN a.nm_tot_aset + a.fc_pur_aset END) AS 期末总资产,
    (MAX(CASE WHEN a.data_dt = '20260331' THEN a.nm_tot_aset + a.fc_pur_aset END) - MAX(CASE WHEN a.data_dt = '20260101' THEN a.nm_tot_aset + a.fc_pur_aset END)) / MAX(CASE WHEN a.data_dt = '20260101' THEN a.nm_tot_aset + a.fc_pur_aset END) AS 总资产增幅
FROM dws_cust_aset_d a
JOIN ads_cust_info_d c ON a.pty_id = c.pty_id
WHERE a.data_dt BETWEEN '20260101' AND '20260331'
GROUP BY a.pty_id, c.name
HAVING MAX(CASE WHEN a.data_dt = '20260101' THEN a.nm_tot_aset + a.fc_pur_aset END) > 0
ORDER BY 总资产增幅 DESC
LIMIT 10`
- gold_sql: `WITH s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) AS a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id) SELECT e.pty_id AS 客户号, ROUND(e.a-COALESCE(s.a,0),4) AS 资产增幅 FROM e LEFT JOIN s ON e.pty_id=s.pty_id ORDER BY 资产增幅 DESC LIMIT 10`
- gold_rows(前5): [['C000000000000102', 6061179.89], ['C000000000000007', 4020828.39], ['C000000000000010', 2640611.298], ['C000000000000169', 1944799.92], ['C000000000000132', 1552473.356]]
- agent_rows(前5): [['C000000000000361', '正***', 2.59, 13423.98, 5182.003861003861], ['C000000000000048', '正***', 0.98, 2988.59, 3048.581632653061], ['C000000000000018', '祖***', 336.03, 403714.87, 1200.42508109395], ['C000000000000431', '玲***', 1005.74, 284367.42, 281.74446676079305], ['C000000000000167', '萦***', 4560.4, 1112351.31, 242.9152947109903]]
- 口径: 增幅=期末-期初(期初缺记录按 0);资产=nm_tot_aset+fc_pur_aset;取前10。

### C05 complex — 2026 年一季度各客户等级客户的资金净流入(流入-流出)分别是多少?
- 类型: 结果不一致
- agent_sql: `SELECT dim_public.describe AS 客户等级, SUM(dws_cust_fin_d.cash_in + dws_cust_fin_d.tran_in + dws_cust_fin_d.assign_in - (dws_cust_fin_d.cash_out + dws_cust_fin_d.tran_out + dws_cust_fin_d.assign_out)) AS 资金净流入
FROM dws_cust_fin_d
JOIN ads_cust_info_d ON dws_cust_fin_d.pty_id = ads_cust_info_d.pty_id
JOIN dim_public ON ads_cust_info_d.cust_lvl_cd = dim_public.code AND dim_public.code_type_id = '100'
WHERE dws_cust_fin_d.data_dt BETWEEN '20260101' AND '20260331'
GROUP BY dim_public.describe
ORDER BY 资金净流入 DESC`
- gold_sql: `WITH f AS (SELECT pty_id, SUM(cash_in+tran_in+assign_in) AS i, SUM(cash_out+tran_out+assign_out) AS o FROM dws_cust_fin_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id) SELECT p.describe AS 客户等级, ROUND(SUM(f.i-f.o),4) AS 净流入 FROM f JOIN ads_cust_info_d a ON f.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' GROUP BY p.describe ORDER BY 净流入 DESC`
- gold_rows(前5): [['紫金理财金卡客户', 1646533.84], ['紫金理财银卡客户', 1206013.36], ['紫金理财钻石卡客户', 1021769.84], ['紫金理财白金卡客户', 749385.46], ['空', 66261.64]]
- agent_rows(前5): [['紫金理财金卡客户', 1646533.8399999999], ['紫金理财银卡客户', 1206013.36], ['紫金理财钻石卡客户', 1021769.84], ['紫金理财白金卡客户', 749385.4600000004], ['空', 66261.64]]
- 口径: 净流入=流入-流出;流入/流出=cash/tran/assign 三列合计;等级经 100 解码。

### C07 complex — 2026-01-10 至 2026-02-15,科创板产品交易额超过 25 万的客户,按营业部统计交易额?
- 类型: 结果不一致
- agent_sql: `SELECT 
  b.org_name AS 营业部,
  SUM(t.buy_amt + t.sell_amt) AS 交易额
FROM dwd_cust_tran_d t
JOIN dim_product p ON t.prdt_id = p.prdt_id
JOIN ads_cust_info_d c ON t.pty_id = c.pty_id
JOIN dim_branch b ON c.org_id = b.org_id
WHERE t.data_dt BETWEEN '20260110' AND '20260215'
  AND p.prdt_type_name = '科创板'
GROUP BY t.pty_id, b.org_name
HAVING SUM(t.buy_amt + t.sell_amt) > 250000
ORDER BY 交易额 DESC`
- gold_sql: `WITH t AS (SELECT t.pty_id, SUM(t.buy_amt+t.sell_amt) AS amt FROM dwd_cust_tran_d t JOIN dim_product d ON t.prdt_id=d.prdt_id WHERE t.data_dt BETWEEN '20260110' AND '20260215' AND d.prdt_type_name LIKE '%科创%' GROUP BY t.pty_id HAVING SUM(t.buy_amt+t.sell_amt)>250000) SELECT b.up_org_name AS 分公司, b.org_name AS 营业部, COUNT(*) AS 客户数, ROUND(SUM(t.amt),4) AS 交易额 FROM t JOIN ads_cust_info_d a ON t.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_branch b ON a.org_id=b.org_id GROUP BY b.up_org_name, b.org_name ORDER BY 交易额 DESC`
- gold_rows(前5): [['苏州分公司', '张家******营业部', 44, 172005739.27], ['无锡分公司', '无锡******营业部', 1, 5114499.16], ['南京分公司', '南京******营业部', 1, 625231.01]]
- agent_rows(前5): [['张家******营业部', 47328084.93], ['张家******营业部', 45786573.71], ['张家******营业部', 24875521.72], ['张家******营业部', 5811196.01], ['无锡******营业部', 5114499.16]]
- 口径: 交易量按金额口径(队伍约定)=buy_amt+sell_amt;科创板按 prdt_type_name LIKE;区间=20260110~20260215。

### C08 complex — 2026-03-31 各客户等级中总资产最高的客户及其资产是多少?
- 类型: 结果不一致
- agent_sql: `SELECT d.describe AS 客户等级, MAX(a.nm_tot_aset + a.fc_pur_aset) AS 最高总资产
FROM dws_cust_aset_d a
JOIN ads_cust_info_d c ON a.pty_id = c.pty_id
JOIN dim_public d ON c.cust_lvl_cd = d.code AND d.code_type_id = '100'
WHERE a.data_dt = '20260331'
GROUP BY d.describe
ORDER BY 最高总资产 DESC`
- gold_sql: `WITH r AS (SELECT s.pty_id, s.nm_tot_aset+s.fc_pur_aset AS total, p.describe AS 等级, ROW_NUMBER() OVER (PARTITION BY p.describe ORDER BY s.nm_tot_aset+s.fc_pur_aset DESC) AS rn FROM dws_cust_aset_d s JOIN ads_cust_info_d a ON s.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE s.data_dt='20260331') SELECT 等级, pty_id AS 客户号, ROUND(total,4) AS 总资产 FROM r WHERE rn=1 ORDER BY 总资产 DESC`
- gold_rows(前5): [['紫金理财钻石卡客户', 'C000000000000004', 59662230.459], ['紫金理财白金卡客户', 'C000000000000150', 4775602.76], ['紫金理财金卡客户', 'C000000000000461', 961747.26], ['紫金理财银卡客户', 'C000000000000057', 536115.296], ['紫金理财卡客户', 'C000000000000439', 114624.22]]
- agent_rows(前5): [['紫金理财钻石卡客户', 59662230.459], ['紫金理财白金卡客户', 4775602.76], ['紫金理财金卡客户', 961747.26], ['紫金理财银卡客户', 536115.296], ['紫金理财卡客户', 114624.22]]
- 口径: 资产取 20260331 快照;窗口按等级分组取最高。

### C11 complex — 按年龄段和客户等级交叉统计客户人数?
- 类型: 结果不一致
- agent_sql: `SELECT 
  CASE 
    WHEN cust_age < 30 THEN '30岁以下'
    WHEN cust_age < 40 THEN '30-39岁'
    WHEN cust_age < 50 THEN '40-49岁'
    WHEN cust_age < 60 THEN '50-59岁'
    ELSE '60岁及以上'
  END AS 年龄段,
  dim_public.describe AS 客户等级,
  COUNT(ads_cust_info_d.pty_id) AS 客户人数
FROM ads_cust_info_d
JOIN dim_public ON ads_cust_info_d.cust_lvl_cd = dim_public.code AND dim_public.code_type_id = '100'
WHERE ads_cust_info_d.data_dt = '20260531'
GROUP BY 年龄段, 客户等级
ORDER BY 年龄段 ASC, 客户等级 ASC`
- gold_sql: `SELECT p.describe AS 客户等级, CASE WHEN cust_age<30 THEN '30岁以下' WHEN cust_age<50 THEN '30-50岁' WHEN cust_age<60 THEN '50-60岁' ELSE '60岁以上' END AS 年龄段, COUNT(*) AS 客户数 FROM ads_cust_info_d a JOIN dim_public p ON a.cust_lvl_cd=p.code AND p.code_type_id='100' WHERE a.data_dt='20260531' GROUP BY p.describe, 年龄段 ORDER BY 客户等级, 年龄段`
- gold_rows(前5): [['空', '30-50岁', 3], ['空', '50-60岁', 28], ['空', '60岁以上', 34], ['紫金理财卡客户', '30-50岁', 4], ['紫金理财卡客户', '50-60岁', 25]]
- agent_rows(前5): [['40-49岁', '空', 3], ['40-49岁', '紫金理财卡客户', 4], ['40-49岁', '紫金理财白金卡客户', 2], ['40-49岁', '紫金理财金卡客户', 9], ['40-49岁', '紫金理财钻石卡客户', 2]]
- 口径: 年龄分桶与 M05 同;等级经 100 解码;交叉分组。

### C12 complex — 2026-03-31 各营业部客户总资产合计最高的 10 个营业部是哪些?
- 类型: 结果不一致
- agent_sql: `SELECT dim_branch.org_name AS 营业部, SUM(dws_cust_aset_d.nm_tot_aset + dws_cust_aset_d.fc_pur_aset) AS 总资产合计
FROM dws_cust_aset_d
JOIN ads_cust_info_d ON dws_cust_aset_d.pty_id = ads_cust_info_d.pty_id
JOIN dim_branch ON ads_cust_info_d.org_id = dim_branch.org_id
WHERE dws_cust_aset_d.data_dt = '20260331'
GROUP BY dim_branch.org_name
ORDER BY 总资产合计 DESC
LIMIT 10`
- gold_sql: `SELECT b.up_org_name AS 分公司, b.org_name AS 营业部, ROUND(SUM(s.nm_tot_aset+s.fc_pur_aset),4) AS 总资产 FROM dws_cust_aset_d s JOIN ads_cust_info_d a ON s.pty_id=a.pty_id AND a.data_dt='20260531' JOIN dim_branch b ON a.org_id=b.org_id WHERE s.data_dt='20260331' GROUP BY b.up_org_name, b.org_name ORDER BY 总资产 DESC LIMIT 10`
- gold_rows(前5): [['苏州分公司', '张家******营业部', 566467337.8261], ['南京分公司', '南京******营业部', 13465985.473], ['上海分公司', '上海******营业部', 6432304.08], ['无锡分公司', '无锡******营业部', 3320031.62], ['苏州分公司', '苏州******营业部', 1755682.889]]
- agent_rows(前5): [['张家******营业部', 566467337.8261], ['南京******营业部', 13465985.473], ['上海******营业部', 6432304.08], ['无锡******营业部', 3320031.62], ['苏州******营业部', 1755682.889]]
- 口径: 资产取 20260331 快照;客户归属营业部经 org_id 上卷;取前10。

### M12 medium — 2026 年一季度交易额(买入+卖出)前 10 的客户是哪些?
- 类型: 结果不一致
- agent_sql: `SELECT 
    t.pty_id AS 客户ID,
    c.name AS 客户姓名,
    SUM(t.buy_amt) AS 买入金额,
    SUM(t.sell_amt) AS 卖出金额,
    SUM(t.buy_amt + t.sell_amt) AS 交易额
FROM dwd_cust_tran_d t
JOIN ads_cust_info_d c ON t.pty_id = c.pty_id
WHERE t.data_dt BETWEEN '20260101' AND '20260331'
GROUP BY t.pty_id, c.name
ORDER BY 交易额 DESC
LIMIT 10`
- gold_sql: `SELECT pty_id AS 客户号, ROUND(SUM(buy_amt+sell_amt),4) AS 交易额 FROM dwd_cust_tran_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id ORDER BY 交易额 DESC LIMIT 10`
- gold_rows(前5): [['C000000000000336', 490354281.17], ['C000000000000007', 435798052.65], ['C000000000000267', 419949807.5798], ['C000000000000111', 231808750.48], ['C000000000000284', 204330579.22]]
- agent_rows(前5): [['C000000000000336', '祥***', 250908122.41, 239446158.76, 490354281.17], ['C000000000000007', '林***', 36896492.74, 398901559.91, 435798052.65], ['C000000000000267', '正***', 209598483.3699, 210351324.2099, 419949807.5798], ['C000000000000111', '鸿***', 41248330.0, 190560420.48, 231808750.48], ['C000000000000284', '凤***', 102100534.11999999, 102230045.1, 204330579.22]]
- 口径: 一季度=20260101~20260331;交易额=buy_amt+sell_amt;取前10。