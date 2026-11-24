"""归并复审专用路由：归并组出处回溯、点位质检结论、复审工作区与处置提交。

路由统一挂 /api/merge-review 前缀，与通用资源路由 /api/{module} 互不冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.dedup_merge import DedupMergeService
from app.services.geo_quality import VERDICT_DRIFT, VERDICT_PASS, GeoQualityService
from app.services.merge_review import MergeReviewService

router = APIRouter(prefix="/api/merge-review", tags=["归并复审"])

_merge_review = MergeReviewService()
_dedup = DedupMergeService()
_quality = GeoQualityService()


class OpenReviewPayload(BaseModel):
    batch_key: str
    group_key: str
    bad_point_id: int
    remark: str | None = None


class CorrectionPayload(BaseModel):
    point_id: int
    conclusion: str = Field(description="确认漂移 或 复核通过")
    remark: str | None = None
    evidence: dict[str, Any] | None = None


class SubmitPayload(BaseModel):
    action: str = Field(description="挂起整组 / 换点补采 / 仅撤坏点")
    replacement_point_id: int | None = None


@router.get("/batches/{batch_key}/groups")
def get_groups(batch_key: str) -> dict[str, Any]:
    """按批次读取归并组明细：代表点、被吸收点与各点质检结论。"""
    groups_file = _dedup._groups_file(batch_key)
    if not groups_file.exists():
        raise HTTPException(status_code=404,
                            detail=f"批次 {batch_key} 尚无归并组明细，请先执行点位归并")
    groups = _dedup.get_merge_groups(batch_key)
    return {"batch_key": batch_key, "total": len(groups), "groups": groups}


@router.get("/batches/{batch_key}/original-points")
def trace_points(batch_key: str, group_key: str | None = None,
                 point_id: int | None = None,
                 conclusion: str | None = None) -> dict[str, Any]:
    """按归并组明细和质检结果定位原始上报点。"""
    if not _dedup._groups_file(batch_key).exists():
        raise HTTPException(status_code=404,
                            detail=f"批次 {batch_key} 尚无归并组明细，无法定位原始上报点")
    items = _dedup.trace_original_points(
        batch_key, point_id=point_id, group_key=group_key, conclusion=conclusion)
    return {"batch_key": batch_key, "total": len(items), "items": items}


@router.get("/batches/{batch_key}/point-verdicts")
def point_verdicts(batch_key: str, conclusion: str | None = None) -> dict[str, Any]:
    """读取批次点位质检结论台账，供复核取数。"""
    rows = _quality.list_point_verdicts(batch_key, conclusion=conclusion)
    return {"batch_key": batch_key, "total": len(rows), "items": rows}


@router.post("/reviews")
def open_review(payload: OpenReviewPayload) -> dict[str, Any]:
    """发起归并复审，冻结当次质检结论快照。"""
    try:
        entry = _merge_review.open_review(
            payload.batch_key, payload.group_key, payload.bad_point_id,
            remark=payload.remark or "")
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "entry": entry}


@router.get("/reviews")
def list_reviews(batch_key: str | None = None, group_key: str | None = None) -> dict[str, Any]:
    """回查复审记录（含旧复审、已处置复审）。"""
    rows = _merge_review.list_reviews(batch_key=batch_key, group_key=group_key)
    return {"total": len(rows), "items": rows}


@router.get("/reviews/{review_id}")
def review_detail(review_id: int) -> dict[str, Any]:
    """查回复审全量证据：快照、更正、评估、处置与时间线。"""
    try:
        return _merge_review.get_review_detail(review_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/reviews/{review_id}/corrections")
def attach_correction(review_id: int, payload: CorrectionPayload) -> dict[str, Any]:
    """把获准的质检更正接入本轮复审（如第三点复核通过）。"""
    if payload.conclusion not in (VERDICT_DRIFT, VERDICT_PASS):
        raise HTTPException(status_code=400,
                            detail=f"结论仅支持 {VERDICT_DRIFT} 或 {VERDICT_PASS}")
    try:
        correction = _merge_review.attach_quality_correction(
            review_id, payload.point_id, payload.conclusion,
            remark=payload.remark or "", evidence=payload.evidence)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "correction": correction}


@router.post("/reviews/{review_id}/evaluate")
def evaluate(review_id: int) -> dict[str, Any]:
    """核对证据：立即换点还是挂起整组补点。"""
    try:
        return _merge_review.evaluate(review_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/reviews/{review_id}/submit")
def submit(review_id: int, payload: SubmitPayload) -> dict[str, Any]:
    """完成同一个复审申请：挂起整组（仅撤坏点）或换用合法点继续补采。"""
    try:
        return _merge_review.submit(
            review_id, payload.action,
            replacement_point_id=payload.replacement_point_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
