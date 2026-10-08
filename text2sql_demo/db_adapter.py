# -*- coding: utf-8 -*-
"""数据库适配层(纯标准库): 引擎只依赖统一接口, 默认 SQLite; 其它库由使用方注入 adapter。

最小接口: table_meta / schema_text / run_query; 可选: explain_plan / list_indexes / analyze_plan。
安全约定: 适配器必须提供只读连接; 引擎不执行任何写操作。零第三方依赖(非 SQLite 由使用方注入驱动)。
"""


class BaseAdapter:
    name = "base"

    def table_meta(self):
        raise NotImplementedError

    def schema_text(self, sample_rows=2, sensitive_columns=None, tables=None):
        raise NotImplementedError

    def run_query(self, sql, max_rows=100):
        raise NotImplementedError

    def explain_plan(self, sql):
        return []

    def list_indexes(self):
        return {}

    def analyze_plan(self, sql):
        return {}


class SqliteAdapter(BaseAdapter):
    name = "sqlite"

    def __init__(self, db_path):
        self.db_path = str(db_path)

    def table_meta(self):
        import text2sql
        return text2sql.get_table_meta(self.db_path)

    def schema_text(self, sample_rows=2, sensitive_columns=None, tables=None):
        import text2sql
        return text2sql.build_schema(self.db_path, sample_rows, sensitive_columns, tables)

    def run_query(self, sql, max_rows=100):
        import text2sql
        return text2sql.run_query(self.db_path, sql)

    def explain_plan(self, sql):
        import sql_plan
        return sql_plan.explain_plan(self.db_path, sql)

    def list_indexes(self):
        import sql_plan
        return sql_plan.list_indexes(self.db_path)

    def analyze_plan(self, sql):
        import sql_plan
        try:
            return sql_plan.analyze(self.db_path, sql)
        except Exception:
            return {}


def make_adapter(db_path=None, adapter=None):
    if adapter is not None:
        return adapter
    if not db_path:
        raise ValueError("需要 db_path 或自定义 db_adapter")
    if "://" in str(db_path):
        raise ValueError("非 SQLite 连接串请注入自定义 db_adapter(实现 table_meta/schema_text/run_query)")
    return SqliteAdapter(db_path)
