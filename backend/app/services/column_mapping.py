"""字段映射业务规则：支持从入库批次提取源字段，映射到目标字段，并驱动ODS表结构创建。

迁移约定：旧类型映射与结构定义新类型版本必须一起迁移。本模块在保留「数据类型」
原始写法（旧简写）的同时登记「规范数据类型」，类型比较一律走 type_catalog，
保证结构表尚未切换简写时也能稳定比对。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.type_catalog import canonical_type, split_type_list, type_version

MODULE = "column_mapping"
LIST_FIELDS = ["规则编号", "源字段名", "目标字段名", "数据类型", "默认值", "转换函数", "规则版本", "生效日期"]
REQUIRED_FIELDS = ["规则编号", "源字段名", "目标字段名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class ColumnMappingService:
    def _enrich_canonical(self, row: dict[str, Any]) -> dict[str, Any]:
        """存量映射记录读取时懒回填规范类型，旧数据类型写法保持不变。"""
        if "规范数据类型" not in row:
            raw_types = split_type_list(row.get("数据类型", ""))
            row["规范数据类型"] = ",".join(canonical_type(token) for token in raw_types)
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
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
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 关联来源批次
        if values.get("来源批次"):
            entry["来源批次"] = values["来源批次"]
        # 旧类型写法原样保留，同时解析规范类型与类型版本，两侧同步迁移
        entry["规范数据类型"] = ",".join(
            canonical_type(token) for token in split_type_list(entry.get("数据类型", "")))
        entry["类型体系版本"] = (
            type_version(split_type_list(entry.get("数据类型", ""))[0])
            if split_type_list(entry.get("数据类型", "")) else "")
        rows.append(entry)
        return entry, []

    def resolve_canonical_types(self, raw_types: Any) -> str:
        """供跨模块调用：把映射侧的混合类型串解析为规范类型串。"""
        return ",".join(canonical_type(token) for token in split_type_list(raw_types))

    def check_type_against_schema(self, mapping_id: int) -> dict[str, Any] | None:
        """跨模块编排：委托结构注册服务沿调用链完成字段类型核对。

        字段映射不自行实现类型比较，统一以结构注册为结构定义权威入口，
        确保字段映射切换与结构表简写并存期间不会持续报类型错误。
        """
        if store.find(MODULE, mapping_id) is None:
            return None
        from app.services.schema_registry import SchemaRegistryService
        return SchemaRegistryService().check_mapping_type_consistency(mapping_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于字段映射可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"映射规则已{action}"

    def get_mappings_by_batch(self, batch_no: str) -> list[dict[str, Any]]:
        return store.find_all_by_field(MODULE, "来源批次", batch_no)
