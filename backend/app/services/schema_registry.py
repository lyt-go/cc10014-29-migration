"""结构定义业务规则：管理表的字段结构，版本化管理。

类型体系迁移后，本模块同时保留「旧类型写法」（字段类型，兼容存量简写）与
「规范类型」（经 type_catalog 归一化的新类型版本）；任何跨模块类型比较都必须
走 type_catalog，禁止字符串直接比较，避免旧 STRING 与新 VARCHAR 混用时误报。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.type_catalog import (
    canonical_type,
    describe_type,
    split_type_list,
    type_version,
    types_compatible,
)

MODULE = "schema_registry"
LIST_FIELDS = ["定义编号", "表名称", "字段名称", "字段类型", "可空标识", "默认值", "定义版本", "生效批次"]
REQUIRED_FIELDS = ["定义编号", "表名称", "字段名称"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class SchemaRegistryService:
    def _enrich_canonical(self, row: dict[str, Any]) -> dict[str, Any]:
        """存量记录（迁移前已存在的种子数据）读取时懒回填规范类型，旧写法保持不变。"""
        if "规范字段类型" not in row:
            raw_types = split_type_list(row.get("字段类型", ""))
            row["规范字段类型"] = ",".join(canonical_type(token) for token in raw_types)
            row["类型体系版本"] = type_version(raw_types[0]) if raw_types else ""
        return row

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("定义编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = [self._enrich_canonical(row) for row in rows]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._enrich_canonical(entry) if entry else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        # 登记时即解析新类型版本；旧写法原样保留在「字段类型」中，保证存量兼容
        entry["规范字段类型"] = self.resolve_canonical_types(entry.get("字段类型", ""))
        entry["类型体系版本"] = type_version(
            split_type_list(entry.get("字段类型", ""))[0]
        ) if split_type_list(entry.get("字段类型", "")) else ""
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def resolve_canonical_types(self, raw_types: Any) -> str:
        """旧简写/新类型混合串 -> 规范类型串，结构注册详情页据此核对迁移结果。"""
        return ",".join(canonical_type(token) for token in split_type_list(raw_types))

    def describe_field_types(self, table_name: str) -> list[dict[str, Any]]:
        """逐字段返回旧写法、规范类型、类型版本与别名标记。"""
        result: list[dict[str, Any]] = []
        for entry in self.get_schema_for_table(table_name):
            fields = split_type_list(entry.get("字段名称"))
            raw_types = split_type_list(entry.get("字段类型"))
            for index, field in enumerate(fields):
                raw = raw_types[index] if index < len(raw_types) else ""
                item = {"字段名称": field, **describe_type(raw),
                        "定义版本": entry.get("定义版本", ""),
                        "定义编号": entry.get("定义编号", "")}
                result.append(item)
        return result

    def check_mapping_type_consistency(
        self, mapping_id: int,
    ) -> dict[str, Any] | None:
        """字段映射跨模块编排结构定义版本表的核心比对：

        沿「字段映射 -> ODS 表 -> 结构注册」调用链，逐字段比较映射数据类型与
        结构定义字段类型。两侧都先经 type_catalog 归一化，旧 STRING 与新 VARCHAR
        同族即兼容，不再出现一边先切换、一边仍是简写时的持续报错。
        """
        mapping = store.find("column_mapping", mapping_id)
        if mapping is None:
            return None
        table_lookup = {
            str(ods.get("创建批次", "")): ods.get("表名称", "")
            for ods in store.rows("ods_layer") if ods.get("创建批次")
        }
        table_name = table_lookup.get(str(mapping.get("来源批次", "")), "")
        target_fields = split_type_list(mapping.get("目标字段名"))
        mapping_types = split_type_list(mapping.get("数据类型"))
        # 结构注册按表取最新生效定义，逐字段建索引
        schema_index: dict[str, str] = {}
        for entry in self.get_schema_for_table(table_name):
            fields = split_type_list(entry.get("字段名称"))
            raw_types = split_type_list(entry.get("字段类型"))
            for index, field in enumerate(fields):
                schema_index[field] = raw_types[index] if index < len(raw_types) else ""
        checks = []
        for index, field in enumerate(target_fields):
            mapping_raw = mapping_types[index] if index < len(mapping_types) else ""
            schema_raw = schema_index.get(field, "")
            checks.append({
                "字段名称": field,
                "映射类型写法": mapping_raw,
                "结构定义写法": schema_raw,
                "映射规范类型": canonical_type(mapping_raw),
                "结构定义规范类型": canonical_type(schema_raw),
                "类型一致": types_compatible(mapping_raw, schema_raw) if schema_raw else True,
                "结构定义缺失": not bool(schema_raw),
            })
        return {
            "规则编号": mapping.get("规则编号", ""),
            "表名称": table_name,
            "全部一致": all(item["类型一致"] for item in checks),
            "字段核对": checks,
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"结构定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于结构注册可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"结构定义已{action}"

    def get_schema_for_table(self, table_name: str) -> list[dict[str, Any]]:
        return store.find_all_by_field(MODULE, "表名称", table_name)

    def get_latest_schema(self, table_name: str) -> dict[str, Any] | None:
        schemas = self.get_schema_for_table(table_name)
        if not schemas:
            return None
        # 找版本号最大的
        latest = max(schemas, key=lambda s: s.get("定义版本", "v0"))
        return latest

    def derive_schema_from_lineage(self, table_name: str) -> dict[str, Any] | None:
        """从血缘关系推导字段变更：找上游表最新schema并合并。"""
        # 查找血缘中该table_name作为下游的边
        upstream_tables = []
        for edge in store.get_lineage_edges():
            if edge.get("下游表名") == table_name:
                up = edge.get("上游表名", "")
                if up:
                    upstream_tables.append(up)
        if not upstream_tables:
            return None
        # 取第一个上游的最新schema
        up_schema = self.get_latest_schema(upstream_tables[0])
        if not up_schema:
            return None
        latest = self.get_latest_schema(table_name)
        new_version = "v2"
        if latest:
            try:
                vnum = int(latest.get("定义版本", "v1").replace("v", ""))
                new_version = f"v{vnum + 1}"
            except Exception:
                new_version = "v2"
        # 生成新schema定义
        new_entry = {
            "定义编号": f"SCH-DERIVED-{table_name}",
            "表名称": table_name,
            "字段名称": up_schema.get("字段名称", ""),
            "字段类型": up_schema.get("字段类型", ""),
            "可空标识": up_schema.get("可空标识", ""),
            "默认值": up_schema.get("默认值", ""),
            "定义版本": new_version,
            "生效批次": up_schema.get("生效批次", ""),
        }
        created = self.create_entry(new_entry)[0]
        return created
