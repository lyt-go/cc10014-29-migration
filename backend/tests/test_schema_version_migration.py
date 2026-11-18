"""结构定义版本表迁移测试。

覆盖需求：
- 旧类型映射（column_mapping）与新类型版本（schema_registry）在同一批次一起迁移；
- 类型别名注册表的归一化与兼容比较（旧 STRING vs 新 VARCHAR 不再报错）；
- 版本表写入超时自动重试；
- 同字段旧/新写法版本冲突的优先级裁决与片段保留；
- 合并视图仍只呈现一份完整结构定义版本表；
- 统一切换批次 + 整批/单表回滚点，回滚不丢失结构版本；
- 逐表迁移进度；字段映射跨模块类型核对；表级比对的归一化类型比较。
"""
from __future__ import annotations

from app.seed import SEED_ROWS
from app.services.column_mapping import ColumnMappingService
from app.services.schema_registry import SchemaRegistryService
from app.services.schema_version import (
    CONFLICT_ALIAS,
    MIGRATION_DONE,
    MIGRATION_FAILED,
    SchemaVersionService,
    SchemaVersionTimeout,
    SWITCH_DONE,
    SWITCH_ROLLED,
)
from app.services.table_compare import TableCompareService
from app.store import store
from app.type_catalog import TYPE_CATALOG_VERSION, canonical_type, types_compatible


def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    SchemaVersionService.TIMEOUT_REMAINING.clear()


class TestTypeCatalog:
    def test_legacy_alias_maps_to_canonical(self):
        assert canonical_type("STRING") == "VARCHAR"
        assert canonical_type(" string ") == "VARCHAR"  # 存量脏写法
        assert canonical_type("INT") == "INTEGER"
        assert canonical_type("VARCHAR(255)") == "VARCHAR"  # 带长度声明
        assert canonical_type("DECIMAL(10,2)") == "DECIMAL"
        assert canonical_type("未知类型X") == "UNKNOWN"

    def test_legacy_string_compatible_with_new_varchar(self):
        # 核心场景：同一字段旧 STRING 与新 VARCHAR 不得再报类型不一致
        assert types_compatible("STRING", "VARCHAR") is True
        assert types_compatible("INT", "BIGINT") is True  # 同族数值
        assert types_compatible("STRING", "TIMESTAMP") is False
        assert types_compatible("未知类型X", "VARCHAR") is False  # 未知不静默放行


class TestMigration:
    def setup_method(self):
        reset_store()
        self.service = SchemaVersionService()

    def test_migrate_covers_schema_and_mapping_together(self):
        result = self.service.migrate()
        # 三个 ODS 表均完成迁移
        tables = {item["表名称"] for item in result["进度"]}
        assert tables == {"ods_orders", "ods_payments", "ods_users"}
        rows = self.service.version_rows()
        # 结构定义片段 + 字段映射片段都进了同一张版本表
        modules = {row["来源模块"] for row in rows}
        assert modules == {"schema_registry", "column_mapping"}
        # 旧写法保留，规范类型与类型版本同时记录
        order_id = next(r for r in rows if r["字段名称"] == "order_id"
                        and r["来源模块"] == "schema_registry")
        assert order_id["旧类型写法"] == "STRING"
        assert order_id["规范类型"] == "VARCHAR"
        assert order_id["类型版本"] == TYPE_CATALOG_VERSION
        assert order_id["旧类型别名"] == "是"
        assert order_id["迁移状态"] == MIGRATION_DONE

    def test_incremental_migrate_skips_existing_fingerprints(self):
        first = self.service.migrate()
        first_count = len(self.service.version_rows())
        second = self.service.migrate()
        assert len(self.service.version_rows()) == first_count  # 无重复片段
        assert all(r["状态"] == MIGRATION_DONE and "跳过" in r["说明"]
                   for r in second["结果"])

    def test_timeout_retried_until_success(self):
        # ods_orders 前两次写入强制超时，第三次成功
        SchemaVersionService.TIMEOUT_REMAINING["ods_orders"] = 2
        result = self.service.migrate(table_name="ods_orders", max_retries=2)
        table_result = next(r for r in result["结果"] if r["表名称"] == "ods_orders")
        assert table_result["状态"] == MIGRATION_DONE
        assert table_result["重试"] == 2
        # 重试不产生半截重复数据
        orders_rows = [r for r in self.service.version_rows() if r["表名称"] == "ods_orders"]
        fingerprints = {(r["来源模块"], r["来源记录id"], r["字段名称"], r["旧类型写法"])
                        for r in orders_rows}
        assert len(fingerprints) == len(orders_rows)

    def test_timeout_exhausted_marks_table_failed_and_other_tables_unaffected(self):
        SchemaVersionService.TIMEOUT_REMAINING["ods_orders"] = 5  # 超过重试预算
        result = self.service.migrate(max_retries=1)
        failed = [r for r in result["结果"] if r["状态"] == MIGRATION_FAILED]
        assert failed and failed[0]["表名称"] == "ods_orders"
        # 其他表正常完成
        assert all(r["状态"] == MIGRATION_DONE
                   for r in result["结果"] if r["表名称"] != "ods_orders")
        # 补齐：清掉注入后再次迁移，失败表增量补齐
        SchemaVersionService.TIMEOUT_REMAINING.clear()
        retry = self.service.migrate(table_name="ods_orders")
        assert all(r["状态"] == MIGRATION_DONE for r in retry["结果"])

    def test_alias_conflict_priority_and_fragment_retention(self):
        # 制造冲突：ods_orders.order_id 新增一份新 VARCHAR 写法的结构定义（草稿 v2）
        sch = SchemaRegistryService()
        created, missing = sch.create_entry({
            "定义编号": "SCH-ORD-002", "表名称": "ods_orders",
            "字段名称": "order_id,order_amount", "字段类型": "VARCHAR,DECIMAL",
            "可空标识": "N,N", "默认值": ",", "定义版本": "v2", "生效批次": "RAW-20261104",
        })
        assert not missing
        self.service.migrate()
        conflicts = self.service.list_conflicts()
        order_conflicts = [c for c in conflicts if c["字段名称"] == "order_id"]
        assert order_conflicts, "旧 STRING 与新 VARCHAR 应识别为别名兼容冲突"
        assert all(c["冲突标识"] == CONFLICT_ALIAS for c in order_conflicts)
        # 裁决优先级：已生效的 v1 结构定义优先于草稿 v2；结构定义优先于字段映射
        winner = order_conflicts[0]["裁决优先来源"]
        assert winner == "SCH-ORD-001"
        # 冲突片段全部保留
        spellings = {c["旧类型写法"] for c in self.service.version_rows()
                     if c["字段名称"] == "order_id"}
        assert {"STRING", "VARCHAR"} <= spellings

    def test_manual_conflict_on_incompatible_types(self):
        sch = SchemaRegistryService()
        sch.create_entry({
            "定义编号": "SCH-ORD-003", "表名称": "ods_orders",
            "字段名称": "order_status", "字段类型": "TIMESTAMP",
            "可空标识": "N", "默认值": "", "定义版本": "v2", "生效批次": "RAW-20261104",
        })
        self.service.migrate()
        manual = [c for c in self.service.list_conflicts()
                  if c["字段名称"] == "order_status" and c["冲突标识"] == "待裁决"]
        assert manual
        # 人工裁决后异常消除
        resolved = self.service.resolve_conflict(manual[0]["id"], canonical="VARCHAR",
                                                 note="业务确认保持字符类型")
        assert resolved["冲突标识"] == CONFLICT_ALIAS
        assert resolved["裁决结果"] == "VARCHAR"
        assert resolved["abnormal"] is False

    def test_consolidated_view_is_single_complete_table(self):
        sch = SchemaRegistryService()
        sch.create_entry({
            "定义编号": "SCH-ORD-002", "表名称": "ods_orders",
            "字段名称": "order_id", "字段类型": "VARCHAR",
            "可空标识": "N", "默认值": "", "定义版本": "v2", "生效批次": "RAW-20261104",
        })
        self.service.migrate()
        view = self.service.consolidated_view(table_name="ods_orders")
        order_item = next(item for item in view if item["字段名称"] == "order_id")
        # 多个片段折叠成一行，旧写法与规范类型集合都在
        assert "STRING" in order_item["旧类型写法集合"]
        assert "VARCHAR" in order_item["旧类型写法集合"]
        assert order_item["来源片段数"] >= 3  # 结构定义 v1/v2 + 字段映射
        assert order_item["冲突标识"] == CONFLICT_ALIAS

    def test_progress_is_per_table(self):
        self.service.migrate()
        progress = {item["表名称"]: item for item in self.service.migration_progress()}
        assert set(progress) == {"ods_orders", "ods_payments", "ods_users"}
        assert all(item["迁移失败"] == 0 for item in progress.values())
        assert all(item["已迁移"] == item["总片段"] for item in progress.values())


class TestSwitchAndRollback:
    def setup_method(self):
        reset_store()
        self.service = SchemaVersionService()
        self.service.migrate()

    def test_switch_rewrites_both_modules_and_keeps_legacy_in_version_table(self):
        switch = self.service.create_switch_batch()
        assert switch["ok"] is True
        assert switch["回滚点"] == "已保留"
        schema = store.find_by_field("schema_registry", "定义编号", "SCH-ORD-001")
        mapping = store.find_by_field("column_mapping", "规则编号", "MAP-ORD-001")
        # 存量类型已切到规范类型
        assert "VARCHAR" in schema["字段类型"].split(",")
        assert "STRING" not in schema["字段类型"].split(",")
        assert mapping["数据类型"].split(",")[0] == "VARCHAR"
        # 版本表旧写法仍在，切换状态已更新
        version_row = next(r for r in self.service.version_rows()
                           if r["来源编号"] == "SCH-ORD-001" and r["字段名称"] == "order_id")
        assert version_row["旧类型写法"] == "STRING"
        assert version_row["切换状态"] == SWITCH_DONE

    def test_full_batch_rollback_restores_legacy_and_keeps_versions(self):
        switch = self.service.create_switch_batch()
        before_count = len(self.service.version_rows())
        result = self.service.rollback_switch(switch["切换批次"])
        assert result["ok"] is True
        schema = store.find_by_field("schema_registry", "定义编号", "SCH-ORD-001")
        mapping = store.find_by_field("column_mapping", "规则编号", "MAP-ORD-001")
        # 存量类型还原为旧简写
        assert schema["字段类型"] == "STRING,DECIMAL,STRING,STRING"
        assert mapping["数据类型"].split(",")[0] == "STRING"
        # 结构版本一条不丢，状态标记已回滚
        assert len(self.service.version_rows()) == before_count
        assert all(r["切换状态"] == SWITCH_ROLLED for r in self.service.version_rows())

    def test_single_table_rollback_keeps_other_tables_switched(self):
        switch = self.service.create_switch_batch()
        self.service.rollback_switch(switch["切换批次"], table_name="ods_orders")
        orders = store.find_by_field("schema_registry", "定义编号", "SCH-ORD-001")
        payments = store.find_by_field("schema_registry", "定义编号", "SCH-PAY-001")
        assert "STRING" in orders["字段类型"].split(",")  # 已回滚
        assert "STRING" not in payments["字段类型"].split(",")  # 仍是切换后
        payments_rows = [r for r in self.service.version_rows() if r["表名称"] == "ods_payments"]
        assert all(r["切换状态"] == SWITCH_DONE for r in payments_rows)

    def test_batches_ledger_contains_migration_and_switch(self):
        self.service.create_switch_batch()
        kinds = {b["批次类型"] for b in self.service.list_batches()}
        assert kinds == {"迁移批次", "切换批次"}
        switch_batch = next(b for b in self.service.list_batches()
                            if b["批次类型"] == "切换批次")
        assert switch_batch["回滚点"] == "已保留"


class TestCrossModuleTypeCheck:
    def setup_method(self):
        reset_store()

    def test_mapping_type_check_passes_with_mixed_notation(self):
        # 结构定义保留旧 STRING 简写，字段映射切到新 VARCHAR，归一化后仍一致
        mapping = store.find_by_field("column_mapping", "规则编号", "MAP-ORD-001")
        mapping["数据类型"] = "VARCHAR,DECIMAL,VARCHAR"
        cms = ColumnMappingService()
        result = cms.check_type_against_schema(mapping["id"])
        assert result is not None
        assert result["全部一致"] is True
        check = next(c for c in result["字段核对"] if c["字段名称"] == "order_id")
        assert check["映射类型写法"] == "VARCHAR"
        assert check["结构定义写法"] == "STRING"

    def test_table_compare_uses_canonical_types(self):
        # 两侧结构定义写法不同但逻辑类型相同，比对不再报错
        left = store.find_by_field("schema_registry", "定义编号", "SCH-ORD-001")
        left["表名称"] = "cmp_left"
        right = store.find_by_field("schema_registry", "定义编号", "SCH-PAY-001")
        right["表名称"] = "cmp_right"
        right["字段名称"] = "order_id,order_amount,order_status,dt"
        right["字段类型"] = "VARCHAR,DECIMAL,VARCHAR,VARCHAR"
        cmp_service = TableCompareService()
        result = cmp_service.compare_schema_types("cmp_left", "cmp_right")
        assert result["全部一致"] is True

    def test_new_mapping_entry_carries_canonical_types(self):
        cms = ColumnMappingService()
        entry, missing = cms.create_entry({
            "规则编号": "MAP-NEW-001", "源字段名": "a,b", "目标字段名": "a,b",
            "数据类型": "STRING,INT", "规则版本": "v1", "来源批次": "RAW-20261101",
        })
        assert not missing
        assert entry["规范数据类型"] == "VARCHAR,INTEGER"
        assert entry["类型体系版本"] == TYPE_CATALOG_VERSION
        # 旧写法未被覆盖
        assert entry["数据类型"] == "STRING,INT"
