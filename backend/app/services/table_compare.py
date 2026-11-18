"""表级比对 时间线服务：支持比对两个表的数据差异，触发补数任务。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store
from app.type_catalog import canonical_type, split_type_list, types_compatible

MODULE = "table_compare"
REQUIRED_FIELDS = ["任务编号", "左表名称", "右表名称"]
STATUS_ORDER = ["草稿", "比对中", "已完成", "已取消"]
ACTION_RULES = {"启动比对": "比对中", "确认完成": "已完成", "取消比对": "已取消"}
NEGATIVE_ACTIONS = []


class TableCompareService:
    def get_timeline(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get("任务编号", "")),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for k in ["比对字段", "比对方式", "差异阈值", "采样比例", "比对批次"]:
            if k in values:
                entry[k] = values[k]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"比对任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表级比对可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        if action == "启动比对":
            result = self._do_compare(entry)
            entry["_compare_result"] = result
            # 如果有差异，自动触发补数任务
            if result.get("diff_count", 0) > 0:
                self._trigger_backfill(entry, result)
        return entry, f"比对任务已{action}"

    def _do_compare(self, entry: dict[str, Any]) -> dict[str, Any]:
        left = entry.get("左表名称", "")
        right = entry.get("右表名称", "")
        fields_str = entry.get("比对字段", "")
        fields = [f.strip() for f in fields_str.split(",") if f.strip()] if fields_str else []
        left_rows = store.table_rows(left)
        right_rows = store.table_rows(right)
        if not left_rows and not right_rows:
            # 尝试从ODS/DWD读取
            from app.services.ods_layer import OdsLayerService
            ods = OdsLayerService()
            left_rows = ods.get_table_data(left) or left_rows
            right_rows = ods.get_table_data(right) or right_rows
        # 简化比对：按第一个比对字段做键值集合差
        if not fields:
            fields = list(left_rows[0].keys()) if left_rows else []
        key_field = fields[0] if fields else "_id"
        left_keys = {str(r.get(key_field, "")) for r in left_rows}
        right_keys = {str(r.get(key_field, "")) for r in right_rows}
        only_left = left_keys - right_keys
        only_right = right_keys - left_keys
        diff_count = len(only_left) + len(only_right)
        # 结构级类型比对同步产出，类型比较走归一化，兼容旧简写与新类型混写
        schema_check = self.compare_schema_types(left, right)
        return {
            "left_count": len(left_rows),
            "right_count": len(right_rows),
            "diff_count": diff_count,
            "only_left": list(only_left)[:10],
            "only_right": list(only_right)[:10],
            "key_field": key_field,
            "schema_type_check": schema_check,
        }

    def _trigger_backfill(self, entry: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        from app.services.backfill_job import BackfillJobService
        bf = BackfillJobService()
        job_no = f"BF-{entry.get('任务编号', '')}"
        bf.create_entry({
            "任务编号": job_no,
            "目标表名": entry.get("右表名称", ""),
            "起始日期": date.today().isoformat(),
            "截止日期": date.today().isoformat(),
            "并行度": "2",
            "覆盖策略": "覆盖写入",
            "完成水位": "",
            "失败区间": "",
        })
        return {"ok": True, "job_no": job_no}

    def compare_schema_types(self, left_table: str, right_table: str) -> dict[str, Any]:
        """结构级类型比对：两侧字段类型先经 type_catalog 归一化再比较。

        旧 STRING 与新 VARCHAR 归一化后同族视为兼容，避免结构表保留简写、
        字段映射已切新类型时持续误报。
        """
        def schema_index(table_name: str) -> dict[str, str]:
            index: dict[str, str] = {}
            for definition in store.find_all_by_field("schema_registry", "表名称", table_name):
                fields = split_type_list(definition.get("字段名称"))
                raw_types = split_type_list(definition.get("字段类型"))
                for i, field in enumerate(fields):
                    index[field] = raw_types[i] if i < len(raw_types) else ""
            return index

        left_index = schema_index(left_table)
        right_index = schema_index(right_table)
        checks = []
        for field in sorted(set(left_index) | set(right_index)):
            left_raw = left_index.get(field, "")
            right_raw = right_index.get(field, "")
            checks.append({
                "字段名称": field,
                "左表写法": left_raw,
                "右表写法": right_raw,
                "左表规范类型": canonical_type(left_raw),
                "右表规范类型": canonical_type(right_raw),
                "类型一致": types_compatible(left_raw, right_raw)
                if left_raw and right_raw else False,
            })
        return {
            "左表名称": left_table,
            "右表名称": right_table,
            "全部一致": all(item["类型一致"] for item in checks) if checks else False,
            "字段核对": checks,
        }
