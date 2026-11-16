"""流告警规则接口：维护告警规则，覆盖启用告警、暂停告警、关闭告警等动作。

告警判定数据结果走同一组接口：/evaluate 触发判定，/determinations 读取判定
结果，/notifications 是通知链路出口；三者与规则列表、规则详情读取同一份数据。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alarm import AlarmService

router = APIRouter(prefix="/api/alarm", tags=["流告警规则"])

service = AlarmService()

LIST_FIELDS = ["规则编号", "监控指标", "告警条件", "告警级别", "通知渠道", "静默周期", "升级策略", "规则状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤流告警规则列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="告警规则已登记", entry=entry)


@router.post("/evaluate", response_model=ActionResult)
def evaluate_metric(payload: EntryPayload) -> ActionResult:
    """对同一监控指标做多规则优先级判定：冲突校验后只写一份告警判定数据结果。"""
    judgment, message = service.evaluate_metric(payload.values)
    if judgment is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=judgment)


@router.get("/determinations", response_model=PageResult[dict])
def list_determinations(
    source: str | None = Query(default=None, description="按来源标识检索，如 JOBO-0002"),
    metric: str | None = Query(default=None, description="按监控指标检索"),
    rule: str | None = Query(default=None, description="按规则编号检索命中或生效的判定"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取告警判定数据结果列表；与规则列表、规则详情是同一份数据。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_determinations(source=source, metric=metric, rule=rule, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/determinations/{judge_id}", response_model=dict)
def get_determination(judge_id: str) -> dict:
    """按判定编号读取单条告警判定数据结果，跨系统链路凭判定编号追溯。"""
    judgment = service.get_determination(judge_id)
    if judgment is None:
        raise HTTPException(status_code=404, detail=f"告警判定 {judge_id} 不存在或已归档")
    return judgment


@router.get("/notifications", response_model=dict)
def list_notifications() -> dict[str, Any]:
    """通知链路出口：只接收判定结果这一版本，静默期内的判定不下发通知。"""
    items = service.list_notifications()
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出流告警规则清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alarm", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条告警规则执行启用告警、暂停告警、关闭告警；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
