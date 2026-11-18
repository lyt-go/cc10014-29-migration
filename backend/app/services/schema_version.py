"""结构定义版本表服务。

承担这次类型体系迁移的主流程，所有规则集中在本模块：

1. 旧类型映射与新类型版本一起迁移：同时扫描 schema_registry（结构定义）与
   column_mapping（字段映射），按「表 + 字段」生成结构定义版本表记录，旧简写、
   规范类型、类型版本、别名标记落在同一行，杜绝两边切换不一致导致的类型比较报错。
2. 超时即重试：单表迁移失败（含模拟超时）按重试策略自动重试，超限标记迁移失败，
   已完成表不受影响，可再次发起增量迁移补齐。
3. 版本冲突裁决：同一字段旧 STRING 与新 VARCHAR 存量冲突时，按固定优先级裁决
   （结构定义优先于字段映射；已生效优先；高版本优先；早登记先处理），别名同族
   判定为兼容，冲突片段全部保留，同时提供合并视图，用户仍只看到一份完整版本表。
4. 统一切换批次 + 整批回滚点：切换前对结构定义、字段映射、版本表做快照，切换把
   存量类型改写为规范类型；任一层回滚都只还原快照，版本表记录始终保留不删除。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store
from app.type_catalog import (
    canonical_type,
    is_legacy_notation,
    split_type_list,
    type_version,
    types_compatible,
    TYPE_CATALOG_VERSION,
    UNKNOWN_TYPE,
)

VERSION_MODULE = "schema_version"
BATCH_MODULE = "schema_version_batch"

# 迁移状态机
MIGRATION_PENDING = "待迁移"
MIGRATION_RUNNING = "迁移中"
MIGRATION_DONE = "已迁移"
MIGRATION_FAILED = "迁移失败"
MIGRATION_ROLLED = "已回滚"

# 切换状态机
SWITCH_NONE = "未切换"
SWITCH_DONE = "已切换"
SWITCH_ROLLED = "已回滚"

CONFLICT_NONE = "无"
CONFLICT_ALIAS = "别名兼容"
CONFLICT_MANUAL = "待裁决"

# 冲突优先级：结构定义优先于字段映射（结构注册是结构定义的权威入口）
MODULE_RANK = {"schema_registry": 0, "column_mapping": 1}
STATUS_RANK = {"已生效": 0, "已上线": 0, "待校验": 1, "校验中": 1, "比对中": 1, "草稿": 2}

DEFAULT_MAX_RETRIES = 2  # 首次 + 2 次重试


class SchemaVersionTimeout(Exception):
    """结构定义版本表写入超时（真实项目对应数据库/网络超时）。"""


class SchemaVersionService:
    # 测试注入：表名 -> 剩余强制超时次数。类级别共享，路由单例与测试实例行为一致。
    TIMEOUT_REMAINING: dict[str, int] = {}

    # ---- 基础存取 ----

    def version_rows(self) -> list[dict[str, Any]]:
        return store.rows(VERSION_MODULE)

    def batch_rows(self) -> list[dict[str, Any]]:
        return store.rows(BATCH_MODULE)

    def _next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---- 来源采集：结构定义 + 字段映射必须一起迁移 ----

    def _mapping_table_lookup(self) -> dict[str, str]:
        """字段映射不直接挂表名，通过来源批次 -> ODS 表名称补齐。"""
        lookup: dict[str, str] = {}
        for ods in store.rows("ods_layer"):
            batch = str(ods.get("创建批次", "")).strip()
            table = str(ods.get("表名称", "")).strip()
            if batch and table:
                lookup[batch] = table
        return lookup

    def collect_sources(self) -> list[dict[str, Any]]:
        """汇总两个模块的逐字段来源记录，每条都带表名、字段、原始类型与优先级素材。"""
        sources: list[dict[str, Any]] = []
        # 1) 结构定义：字段名称/字段类型是等长逗号串，逐字段拆开
        for entry in store.rows("schema_registry"):
            fields = split_type_list(entry.get("字段名称"))
            raw_types = split_type_list(entry.get("字段类型"))
            for index, field in enumerate(fields):
                raw_type = raw_types[index] if index < len(raw_types) else ""
                sources.append({
                    "表名称": entry.get("表名称", ""),
                    "字段名称": field,
                    "旧类型写法": raw_type,
                    "来源模块": "schema_registry",
                    "来源记录id": int(entry.get("id", 0)),
                    "来源编号": entry.get("定义编号", ""),
                    "来源状态": entry.get("status", ""),
                    "定义版本": entry.get("定义版本", ""),
                    "生效批次": entry.get("生效批次", ""),
                })
        # 2) 字段映射：目标字段名/数据类型逐字段拆开，表名经来源批次反查
        table_lookup = self._mapping_table_lookup()
        for entry in store.rows("column_mapping"):
            fields = split_type_list(entry.get("目标字段名"))
            raw_types = split_type_list(entry.get("数据类型"))
            table_name = table_lookup.get(str(entry.get("来源批次", "")).strip(), "")
            for index, field in enumerate(fields):
                raw_type = raw_types[index] if index < len(raw_types) else ""
                sources.append({
                    "表名称": table_name,
                    "字段名称": field,
                    "旧类型写法": raw_type,
                    "来源模块": "column_mapping",
                    "来源记录id": int(entry.get("id", 0)),
                    "来源编号": entry.get("规则编号", ""),
                    "来源状态": entry.get("status", ""),
                    "定义版本": entry.get("规则版本", ""),
                    "生效批次": entry.get("生效日期", ""),
                })
        return sources

    @staticmethod
    def _fingerprint(source: dict[str, Any]) -> str:
        return "{}:{}:{}:{}".format(
            source["来源模块"], source["来源记录id"], source["字段名称"], source["旧类型写法"])

    @staticmethod
    def _version_number(define_version: Any) -> int:
        try:
            return int(str(define_version or "v0").replace("v", ""))
        except ValueError:
            return 0

    def _priority_key(self, source: dict[str, Any]) -> tuple[int, int, int, str, int]:
        """裁决优先级（越小越优先）：模块 → 状态 → 定义版本(高优先，取负) → 生效批次 → 记录id。"""
        return (
            MODULE_RANK.get(source["来源模块"], 9),
            STATUS_RANK.get(source["来源状态"], 9),
            -self._version_number(source["定义版本"]),
            str(source["生效批次"]),
            int(source["来源记录id"]),
        )

    # ---- 超时重试 ----

    def _write_version_row(self, table_name: str, row: dict[str, Any]) -> None:
        """写版本表的单点操作；测试可通过 TIMEOUT_REMAINING 注入前几次超时。"""
        remaining = self.TIMEOUT_REMAINING.get(table_name, 0)
        if remaining > 0:
            self.TIMEOUT_REMAINING[table_name] = remaining - 1
            raise SchemaVersionTimeout(f"结构定义版本表写入超时：{table_name}")
        store.rows(VERSION_MODULE).append(row)

    def _migrate_table(
        self, table_name: str, sources: list[dict[str, Any]], migration_batch: str,
        max_retries: int,
    ) -> dict[str, Any]:
        """按表迁移：冲突裁决在表内完成，整表共享超时重试预算。"""
        existing = {
            self._fingerprint(row): row
            for row in self.version_rows()
            if row.get("表名称") == table_name
        }
        # 增量迁移：已落版本表的来源片段不重复生成
        pending = [s for s in sources if self._fingerprint(s) not in existing]
        if not pending:
            return {"表名称": table_name, "状态": MIGRATION_DONE, "新增": 0,
                    "重试": 0, "说明": "无增量来源，跳过"}

        # 冲突裁决：按表+字段分组（包含已迁移片段，保证存量冲突也能被识别）
        groups: dict[str, list[dict[str, Any]]] = {}
        for source in [*sources, *[self._row_to_source(r) for r in existing.values()]]:
            groups.setdefault(source["字段名称"], []).append(source)

        attempt = 0
        last_error = ""
        while attempt <= max_retries:
            try:
                for source in pending:
                    row = self._build_version_row(source, groups[source["字段名称"]],
                                                  migration_batch)
                    self._write_version_row(table_name, row)
                return {"表名称": table_name, "状态": MIGRATION_DONE,
                        "新增": len(pending), "重试": attempt, "说明": "迁移成功"}
            except SchemaVersionTimeout as exc:
                last_error = str(exc)
                attempt += 1
                # 超时后本批已写入的行回滚到表迁移前，下一轮整体重试，避免半截数据
                for row in [r for r in self.version_rows()
                            if r.get("迁移批次") == migration_batch and r.get("表名称") == table_name]:
                    self.version_rows().remove(row)
        # 重试用尽：表级标记失败，等待下次增量迁移补齐
        for source in pending:
            failed = self._build_version_row(source, groups[source["字段名称"]], migration_batch)
            failed["迁移状态"] = MIGRATION_FAILED
            failed["冲突说明"] = (failed.get("冲突说明", "") + f"；迁移失败：{last_error}").strip("；")
            self.version_rows().append(failed)
        return {"表名称": table_name, "状态": MIGRATION_FAILED, "新增": 0,
                "重试": max_retries, "说明": last_error}

    def _row_to_source(self, row: dict[str, Any]) -> dict[str, Any]:
        """版本表记录反解为来源形态，用于重跑迁移时的冲突分组。"""
        return {
            "表名称": row.get("表名称", ""),
            "字段名称": row.get("字段名称", ""),
            "旧类型写法": row.get("旧类型写法", ""),
            "来源模块": row.get("来源模块", ""),
            "来源记录id": int(row.get("来源记录id", 0)),
            "来源编号": row.get("来源编号", ""),
            "来源状态": row.get("来源状态", ""),
            "定义版本": row.get("定义版本", ""),
            "生效批次": row.get("生效批次", ""),
        }

    def _build_version_row(
        self, source: dict[str, Any], field_sources: list[dict[str, Any]],
        migration_batch: str,
    ) -> dict[str, Any]:
        raw_type = source["旧类型写法"]
        canonical = canonical_type(raw_type)
        winner = min(field_sources, key=self._priority_key)
        winner_canonical = canonical_type(winner["旧类型写法"])

        raw_spellings = sorted({s["旧类型写法"] for s in field_sources if s["旧类型写法"]})
        canonical_set = {canonical_type(s["旧类型写法"]) for s in field_sources}
        conflict, conflict_note = CONFLICT_NONE, ""
        if len(raw_spellings) > 1 or len(canonical_set) > 1:
            if UNKNOWN_TYPE in canonical_set or any(
                not types_compatible(a, b)
                for a in canonical_set for b in canonical_set
            ):
                conflict = CONFLICT_MANUAL
                conflict_note = (
                    f"字段存在不可自动兼容的类型写法：{'/'.join(raw_spellings)}，"
                    f"按优先级暂取 {winner['来源编号']} 的 {winner_canonical}，需人工裁决")
            else:
                conflict = CONFLICT_ALIAS
                conflict_note = (
                    f"旧简写与新类型版本冲突（{'/'.join(raw_spellings)}），"
                    f"归一化后同为 {winner_canonical}；优先采用 {winner['来源编号']}"
                    f"（{winner['来源模块']}/{winner['来源状态']}）")

        return {
            "id": self._next_id(VERSION_MODULE),
            "版本记录编号": f"SV-{source['表名称'] or 'UNBOUND'}-{source['字段名称']}-{source['来源记录id']}",
            "表名称": source["表名称"],
            "字段名称": source["字段名称"],
            "旧类型写法": raw_type,
            "规范类型": canonical,
            "类型版本": type_version(raw_type),
            "旧类型别名": "是" if is_legacy_notation(raw_type) else "否",
            "来源模块": source["来源模块"],
            "来源记录id": source["来源记录id"],
            "来源编号": source["来源编号"],
            "来源状态": source["来源状态"],
            "定义版本": source["定义版本"],
            "生效批次": source["生效批次"],
            "迁移批次": migration_batch,
            "迁移状态": MIGRATION_PENDING,
            "冲突标识": conflict,
            "冲突说明": conflict_note,
            "裁决结果": winner_canonical if conflict != CONFLICT_NONE else "",
            "裁决优先来源": winner["来源编号"] if conflict != CONFLICT_NONE else "",
            "切换批次": "",
            "切换状态": SWITCH_NONE,
            "status": "草稿",
            "pending": True,
            "abnormal": conflict == CONFLICT_MANUAL,
        }

    # ---- 迁移入口 ----

    def migrate(self, *, table_name: str | None = None,
                max_retries: int = DEFAULT_MAX_RETRIES) -> dict[str, Any]:
        """执行迁移；旧类型映射（字段映射）与新类型版本（结构定义）在同一批次内完成。"""
        sources = self.collect_sources()
        if table_name:
            sources = [s for s in sources if s["表名称"] == table_name]
        tables: list[str] = sorted({s["表名称"] for s in sources})
        migration_batch = f"MG-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        results = []
        for current in tables:
            table_sources = [s for s in sources if s["表名称"] == current]
            results.append(self._migrate_table(
                current or "未挂表", table_sources, migration_batch, max_retries))

        # 成功写入的行把迁移状态翻成已迁移（失败行在 _migrate_table 内已标记）
        for row in self.version_rows():
            if row.get("迁移批次") == migration_batch and row.get("迁移状态") == MIGRATION_PENDING:
                row["迁移状态"] = MIGRATION_DONE
                row["status"] = "已生效"
                row["pending"] = False

        failed = [r for r in results if r["状态"] == MIGRATION_FAILED]
        batch_row = {
            "id": self._next_id(BATCH_MODULE),
            "批次编号": migration_batch,
            "批次类型": "迁移批次",
            "状态": "部分失败" if failed else "已完成",
            "覆盖表": ",".join(tables),
            "统计": {"表数": len(tables), "失败表数": len(failed),
                    "明细": results, "类型体系版本": TYPE_CATALOG_VERSION},
            "回滚点": None,
            "创建时间": self._now(),
            "status": "已生效" if not failed else "待处理",
            "pending": bool(failed),
            "abnormal": bool(failed),
        }
        self.batch_rows().append(batch_row)
        store.save_version(BATCH_MODULE, batch_row["id"], batch_row)
        return {"迁移批次": migration_batch, "结果": results,
                "进度": self.migration_progress(), "批次": batch_row}

    def migration_progress(self) -> list[dict[str, Any]]:
        """逐表迁移进度：版本表冲突片段按表展示迁移状态分布。"""
        progress: dict[str, dict[str, Any]] = {}
        for row in self.version_rows():
            table = row.get("表名称") or "未挂表"
            item = progress.setdefault(table, {
                "表名称": table, "总片段": 0, "已迁移": 0, "迁移失败": 0,
                "待迁移": 0, "已回滚": 0, "冲突片段": 0})
            item["总片段"] += 1
            state = row.get("迁移状态", "")
            if state == MIGRATION_DONE:
                item["已迁移"] += 1
            elif state == MIGRATION_FAILED:
                item["迁移失败"] += 1
            elif state == MIGRATION_ROLLED:
                item["已回滚"] += 1
            else:
                item["待迁移"] += 1
            if row.get("冲突标识") != CONFLICT_NONE:
                item["冲突片段"] += 1
        return [progress[name] for name in sorted(progress)]

    # ---- 冲突管理 ----

    def list_conflicts(self) -> list[dict[str, Any]]:
        return [row for row in self.version_rows() if row.get("冲突标识") != CONFLICT_NONE]

    def resolve_conflict(self, row_id: int, canonical: str | None = None,
                         note: str = "") -> dict[str, Any] | None:
        row = store.find(VERSION_MODULE, row_id)
        if row is None:
            return None
        decision = canonical or row.get("裁决结果") or canonical_type(row.get("旧类型写法"))
        row["冲突标识"] = CONFLICT_ALIAS if row.get("冲突标识") == CONFLICT_MANUAL else row["冲突标识"]
        row["裁决结果"] = decision
        row["冲突说明"] = (note or "人工裁决完成") + f"，裁决类型：{decision}"
        row["abnormal"] = False
        store.save_version(VERSION_MODULE, row_id, row)
        return row

    # ---- 合并视图：不同片段保留，但用户只看到一份完整结构定义版本表 ----

    def consolidated_view(self, table_name: str | None = None) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.version_rows():
            if table_name and row.get("表名称") != table_name:
                continue
            key = (row.get("表名称", ""), row.get("字段名称", ""))
            item = merged.setdefault(key, {
                "表名称": key[0], "字段名称": key[1],
                "旧类型写法集合": [], "规范类型集合": [], "来源片段": [],
                "冲突标识": CONFLICT_NONE, "冲突说明": "", "裁决结果": "",
                "迁移状态": MIGRATION_DONE, "切换状态": SWITCH_NONE,
                "切换批次": "", "类型版本": TYPE_CATALOG_VERSION,
            })
            if row.get("旧类型写法") not in item["旧类型写法集合"]:
                item["旧类型写法集合"].append(row.get("旧类型写法"))
            if row.get("规范类型") not in item["规范类型集合"]:
                item["规范类型集合"].append(row.get("规范类型"))
            item["来源片段"].append(row.get("版本记录编号"))
            if row.get("冲突标识") != CONFLICT_NONE:
                item["冲突标识"] = row["冲突标识"]
                item["冲突说明"] = row.get("冲突说明", "")
                item["裁决结果"] = row.get("裁决结果", "")
            if row.get("迁移状态") == MIGRATION_FAILED:
                item["迁移状态"] = MIGRATION_FAILED
            if row.get("切换状态") == SWITCH_ROLLED:
                item["切换状态"] = SWITCH_ROLLED
            elif row.get("切换状态") == SWITCH_DONE and item["切换状态"] != SWITCH_ROLLED:
                item["切换状态"] = SWITCH_DONE
            if row.get("切换批次"):
                item["切换批次"] = row["切换批次"]
        # 集合转逗号串便于表格直接渲染
        result = []
        for item in merged.values():
            item["旧类型写法集合"] = ",".join(item["旧类型写法集合"])
            item["规范类型集合"] = ",".join(item["规范类型集合"])
            item["来源片段数"] = len(item["来源片段"])
            item["来源片段"] = ",".join(item["来源片段"])
            result.append(item)
        return sorted(result, key=lambda x: (x["表名称"], x["字段名称"]))

    # ---- 统一切换批次与整批回滚点 ----

    def _snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "schema_registry": deepcopy(store.rows("schema_registry")),
            "column_mapping": deepcopy(store.rows("column_mapping")),
            VERSION_MODULE: deepcopy(self.version_rows()),
        }

    def create_switch_batch(self, *, table_name: str | None = None) -> dict[str, Any]:
        targets = [row for row in self.version_rows()
                   if row.get("迁移状态") == MIGRATION_DONE
                   and (not table_name or row.get("表名称") == table_name)
                   and row.get("切换状态") != SWITCH_DONE]
        if not targets:
            return {"ok": False, "message": "没有可切换的已迁移版本记录"}
        switch_batch = f"SW-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        snapshot = self._snapshot()
        batch_row = {
            "id": self._next_id(BATCH_MODULE),
            "批次编号": switch_batch,
            "批次类型": "切换批次",
            "状态": "已生效",
            "覆盖表": ",".join(sorted({r["表名称"] for r in targets if r.get("表名称")})),
            "统计": {"切换片段": len(targets)},
            "回滚点": snapshot,
            "创建时间": self._now(),
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self.batch_rows().append(batch_row)

        # 沿调用链切换：结构定义与字段映射里的存量类型改写为规范类型，
        # 旧写法仍保留在版本表与回滚点中，任一环节都能还原。
        target_keys = {(r["来源模块"], r["来源记录id"], r["字段名称"]) for r in targets}
        self._apply_canonical_to_sources(target_keys)
        for row in targets:
            row["切换批次"] = switch_batch
            row["切换状态"] = SWITCH_DONE
        store.save_version(BATCH_MODULE, batch_row["id"], batch_row)
        return {"ok": True, "切换批次": switch_batch, "切换片段": len(targets),
                "回滚点": "已保留", "批次": batch_row}

    def _apply_canonical_to_sources(
        self, target_keys: set[tuple[str, int, str]]) -> None:
        for entry in store.rows("schema_registry"):
            fields = split_type_list(entry.get("字段名称"))
            raw_types = split_type_list(entry.get("字段类型"))
            new_types = []
            for index, field in enumerate(fields):
                raw = raw_types[index] if index < len(raw_types) else ""
                if ("schema_registry", int(entry.get("id", 0)), field) in target_keys:
                    new_types.append(canonical_type(raw))
                else:
                    new_types.append(raw)
            if new_types:
                entry["字段类型"] = ",".join(new_types)
        for entry in store.rows("column_mapping"):
            fields = split_type_list(entry.get("目标字段名"))
            raw_types = split_type_list(entry.get("数据类型"))
            new_types = []
            for index, field in enumerate(fields):
                raw = raw_types[index] if index < len(raw_types) else ""
                if ("column_mapping", int(entry.get("id", 0)), field) in target_keys:
                    new_types.append(canonical_type(raw))
                else:
                    new_types.append(raw)
            if new_types:
                entry["数据类型"] = ",".join(new_types)

    def active_switch_batch(self) -> dict[str, Any] | None:
        for batch in reversed(self.batch_rows()):
            if batch.get("批次类型") == "切换批次" and batch.get("状态") == "已生效":
                return batch
        return None

    def rollback_switch(self, switch_batch: str, *,
                        table_name: str | None = None) -> dict[str, Any]:
        """整批或单表回滚：只还原快照与切换状态，版本表记录一条不删，不丢已有结构版本。"""
        batch = next((b for b in self.batch_rows()
                      if b.get("批次编号") == switch_batch and b.get("批次类型") == "切换批次"),
                     None)
        if batch is None:
            return {"ok": False, "message": f"切换批次 {switch_batch} 不存在"}
        snapshot = batch.get("回滚点")
        if not snapshot:
            return {"ok": False, "message": "回滚点缺失，无法回滚"}

        def restore(module: str, keep_predicate) -> None:
            snap_rows = snapshot.get(module, [])
            if table_name:
                snap_by_id = {int(r.get("id", 0)): r for r in snap_rows
                              if keep_predicate(r)}
                current = store.rows(module)
                for index, row in enumerate(list(current)):
                    if int(row.get("id", 0)) in snap_by_id:
                        current[index] = deepcopy(snap_by_id[int(row.get("id", 0))])
            else:
                store._tables[module] = deepcopy(snap_rows)  # noqa: SLF001

        restore("schema_registry", lambda r: r.get("表名称") == table_name)
        # 字段映射行不带表名，借助版本表片段反查属于该表的映射记录
        if table_name:
            mapping_ids = {r.get("来源记录id") for r in self.version_rows()
                           if r.get("表名称") == table_name and r.get("来源模块") == "column_mapping"}
            restore("column_mapping", lambda r: int(r.get("id", 0)) in mapping_ids)
        else:
            restore("column_mapping", lambda r: True)

        for row in self.version_rows():
            if row.get("切换批次") != switch_batch:
                continue
            if table_name and row.get("表名称") != table_name:
                continue
            row["切换状态"] = SWITCH_ROLLED
            if table_name is None:
                row["迁移状态"] = MIGRATION_ROLLED
        if table_name is None:
            batch["状态"] = "已回滚"
            batch["status"] = "已废弃"
        store.save_version(BATCH_MODULE, batch["id"], batch)
        scope = f"表 {table_name}" if table_name else "整批"
        return {"ok": True, "message": f"{scope}已回滚到切换前快照，结构版本记录全部保留",
                "切换批次": switch_batch}

    def list_batches(self) -> list[dict[str, Any]]:
        """批次台账：迁移批次与切换批次（含回滚点）统一展示。"""
        return [dict(b, 回滚点="已保留" if b.get("回滚点") else "无") for b in self.batch_rows()]
