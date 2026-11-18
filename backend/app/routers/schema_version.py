"""结构定义版本表接口。

对外暴露迁移、切换、回滚、冲突裁决与合并视图；版本表详情页先经结构注册入口
（schema_registry）核对结构定义，再切换到本路由核对迁移结果。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult, PageResult
from app.services.schema_version import (
    CONFLICT_NONE,
    DEFAULT_MAX_RETRIES,
    SchemaVersionService,
)

router = APIRouter(prefix="/api/schema_version", tags=["结构定义版本表"])

service = SchemaVersionService()

LIST_FIELDS = [
    "版本记录编号", "表名称", "字段名称", "旧类型写法", "规范类型", "类型版本",
    "旧类型别名", "来源模块", "来源编号", "定义版本", "迁移批次", "迁移状态",
    "冲突标识", "裁决结果", "切换批次", "切换状态",
]


def _filter_rows(rows: list[dict], table_name: str | None,
                 conflict: str | None, status: str | None) -> list[dict]:
    if table_name:
        rows = [r for r in rows if table_name.casefold() in str(r.get("表名称", "")).casefold()]
    if conflict:
        if conflict == "有冲突":
            rows = [r for r in rows if r.get("冲突标识") != CONFLICT_NONE]
        else:
            rows = [r for r in rows if r.get("冲突标识") == conflict]
    if status:
        rows = [r for r in rows if r.get("迁移状态") == status]
    return rows


@router.get("", response_model=PageResult[dict])
def list_versions(
    table_name: str | None = Query(default=None, description="按表名称过滤"),
    conflict: str | None = Query(default=None, description="有冲突/别名兼容/待裁决/无"),
    status: str | None = Query(default=None, description="迁移状态过滤"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=500),
) -> PageResult[dict]:
    """结构定义版本表明细：保留每个来源片段（含冲突片段），支持逐表核对。"""
    rows = _filter_rows(service.version_rows(), table_name, conflict, status)
    total = len(rows)
    start = (page - 1) * size
    return PageResult(items=rows[start:start + size], total=total, page=page, size=size)


@router.get("/consolidated")
def consolidated(table_name: str | None = Query(default=None)) -> dict:
    """合并视图：同一表+字段的冲突片段折叠成一行，用户只看到一份完整结构定义版本表。"""
    items = service.consolidated_view(table_name)
    return {"items": items, "total": len(items)}


@router.get("/progress")
def progress() -> dict:
    """逐表迁移进度。"""
    return {"items": service.migration_progress()}


@router.get("/conflicts")
def conflicts() -> dict:
    """全部冲突片段，便于比较结构定义谁优先、哪个先处理。"""
    return {"items": service.list_conflicts(), "total": len(service.list_conflicts())}


@router.get("/batches")
def batches() -> dict:
    """迁移批次、切换批次与整批回滚点台账。"""
    return {"items": service.list_batches()}


@router.post("/migrate", response_model=ActionResult)
def migrate(table_name: str | None = Query(default=None),
            max_retries: int = Query(default=DEFAULT_MAX_RETRIES, ge=0, le=5)) -> ActionResult:
    """发起迁移：字段映射旧类型与结构定义新类型版本在同一批次内一起迁移；超时自动重试。"""
    result = service.migrate(table_name=table_name, max_retries=max_retries)
    failed = [r for r in result["结果"] if r["状态"] == "迁移失败"]
    if failed:
        return ActionResult(ok=False,
                            message=f"迁移批次 {result['迁移批次']} 存在失败表，已按重试策略处理，可再次发起补齐",
                            entry=result)
    return ActionResult(ok=True, message=f"迁移批次 {result['迁移批次']} 完成", entry=result)


@router.post("/switch", response_model=ActionResult)
def switch(table_name: str | None = Query(default=None)) -> ActionResult:
    """统一切换批次：保留整批回滚点后，把结构定义与字段映射存量类型切到规范类型。"""
    result = service.create_switch_batch(table_name=table_name)
    if not result.get("ok"):
        return ActionResult(ok=False, message=result["message"])
    return ActionResult(ok=True,
                        message=f"切换批次 {result['切换批次']} 已生效，回滚点已保留",
                        entry=result)


@router.post("/rollback", response_model=ActionResult)
def rollback(switch_batch: str = Query(..., description="要回滚的切换批次编号"),
             table_name: str | None = Query(default=None)) -> ActionResult:
    """整批或单表回滚；结构定义版本表记录保留不删除。"""
    result = service.rollback_switch(switch_batch, table_name=table_name)
    return ActionResult(ok=bool(result.get("ok")), message=result.get("message", ""),
                        entry=result if result.get("ok") else None)


@router.post("/conflicts/{row_id}/resolve", response_model=ActionResult)
def resolve_conflict(row_id: int, payload: dict | None = None) -> ActionResult:
    """人工裁决冲突字段的最终规范类型。"""
    payload = payload or {}
    row = service.resolve_conflict(row_id, canonical=payload.get("canonical"),
                                   note=payload.get("note", ""))
    if row is None:
        return ActionResult(ok=False, message=f"版本记录 {row_id} 不存在")
    return ActionResult(ok=True, message="冲突已裁决", entry=row)


@router.get("/export")
def export_versions(table_name: str | None = Query(default=None),
                    conflict: str | None = Query(default=None),
                    status: str | None = Query(default=None)) -> StreamingResponse:
    """导出当前筛选下的结构定义版本表 CSV。"""
    rows = _filter_rows(service.version_rows(), table_name, conflict, status)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(LIST_FIELDS)
    for row in rows:
        cells = []
        for field in LIST_FIELDS:
            text = "" if row.get(field) is None else str(row.get(field))
            cells.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(cells)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="schema_version.csv"'})
