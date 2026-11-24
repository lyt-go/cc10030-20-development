"""归并复审领域服务：针对点位归并代表点的质检问题发起复审，串联归并组明细与点位质检结论。

核心能力：
- 发起复审时冻结当次依据的质检结论快照（basis_snapshot），复核取数不受后续更正覆盖；
- 复审执行中可接入新获准的质检更正（如第三点复核通过），并给出「立即换用合法点」或「挂起整组补点」的判定；
- 支持两类处置：仅撤下坏点（整组转待补点，旧合并关系可回查）与换用合法代表点继续补采；
- 证据可全程查回：归并组出处、原始上报点、历次质检结论与复审时间线。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.dedup_merge import DedupMergeService
from app.services.geo_quality import (
    BLOCKED_VERDICTS,
    VERDICT_DRIFT,
    VERDICT_PASS,
    VERDICT_PENDING,
    GeoQualityService,
)
from app.store import store

# 复审判定取值
DECISION_HOLD = "挂起整组待补点"
DECISION_REPLACE = "可立即换用合法代表点"
DECISION_WITHDRAW = "仅撤下坏点"


class MergeReviewService(BaseModuleService):
    MODULE = "merge_review"
    ENTITY = "归并复审"
    OPERATION_TERM = "复审"
    LIST_FIELDS = ["复审编号", "批次编号", "归并组编号", "坏点编号", "复审状态",
                   "复审判定", "代表点编号", "候选证据", "处置动作", "申请时间",
                   "处置时间", "证据快照版本"]
    REQUIRED_FIELDS = ["复审编号", "批次编号", "归并组编号", "坏点编号"]
    STATUS_ORDER = ["草稿", "复审中", "已挂起补点", "已换点补采", "已撤点", "已关闭"]
    ACTION_RULES = {
        "发起复审": "复审中",
        "挂起整组": "已挂起补点",
        "换点补采": "已换点补采",
        "仅撤坏点": "已撤点",
        "关闭复审": "已关闭",
    }
    NEGATIVE_ACTIONS = ["挂起整组", "仅撤坏点"]

    def __init__(self) -> None:
        self._dedup = DedupMergeService()
        self._quality = GeoQualityService()

    # -- 复审工作区快照持久化 --

    def _review_file(self, review_id: int):
        return self._data_dir("merge_reviews") / f"review_{review_id}.json"

    def _save_workspace(self, review_id: int, workspace: dict[str, Any]) -> None:
        path = self._review_file(review_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json
        workspace["_updated_at"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(workspace, f, ensure_ascii=False, indent=2, default=str)

    def _load_workspace(self, review_id: int) -> dict[str, Any]:
        path = self._review_file(review_id)
        if not path.exists():
            raise FileNotFoundError(f"归并复审 {review_id} 的工作区不存在")
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # -- 发起复审 --

    def open_review(self, batch_key: str, group_key: str, bad_point_id: int,
                    remark: str = "") -> dict[str, Any]:
        """发起一项归并复审：冻结归并组出处与当次质检结论快照，进入复审中状态。"""
        group = self._dedup.find_group(batch_key, group_key)
        if group is None:
            raise FileNotFoundError(f"批次 {batch_key} 中不存在归并组 {group_key}")
        if bad_point_id not in group.get("orig_point_ids", []):
            raise ValueError(f"坏点 {bad_point_id} 不属于归并组 {group_key}")

        # 冻结当次依据的质检结论（后续更正不影响本快照，复核取数可查）
        basis_verdicts = []
        for pid in group["orig_point_ids"]:
            verdict = self._quality.get_point_verdict(batch_key, pid)
            basis_verdicts.append({
                "point_id": pid,
                "conclusion": verdict["conclusion"] if verdict else "证据未确认",
                "revision": verdict.get("revision", 0) if verdict else 0,
                "remark": verdict.get("remark", "") if verdict else "",
                "snapshot_at": time.time(),
            })
        bad_basis = next(v for v in basis_verdicts if v["point_id"] == bad_point_id)

        rows = store.rows(self.MODULE)
        review_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        review_no = f"REV-{batch_key}-{review_id:04d}"
        now_text = time.strftime("%Y-%m-%d %H:%M:%S")

        workspace = {
            "review_id": review_id,
            "review_no": review_no,
            "batch_key": batch_key,
            "group_key": group_key,
            "bad_point_id": bad_point_id,
            "opened_at": time.time(),
            "basis_snapshot": {
                "group": dict(group),
                "point_verdicts": basis_verdicts,
                "bad_point_basis": bad_basis,
                "snapshot_version": f"v{len(basis_verdicts)}-{int(time.time())}",
            },
            "attached_corrections": [],
            "timeline": [{
                "at": time.time(), "event": "发起复审",
                "detail": f"坏点 {bad_point_id} 当次依据结论：{bad_basis['conclusion']}",
            }],
            "decision": None,
            "decision_detail": None,
            "disposition": None,
        }

        entry, missing = self.create_entry({
            "复审编号": review_no,
            "批次编号": batch_key,
            "归并组编号": group_key,
            "坏点编号": str(bad_point_id),
            "复审状态": "复审中",
            "复审判定": "待评估",
            "代表点编号": str(group.get("representative_point_id")),
            "候选证据": f"组内{len(group['orig_point_ids'])}点/坏点依据{bad_basis['conclusion']}",
            "处置动作": "未处置",
            "申请时间": now_text,
            "处置时间": "—",
            "证据快照版本": workspace["basis_snapshot"]["snapshot_version"],
        })
        if missing:
            raise ValueError(f"缺少必填字段: {missing}")
        # create_entry 落在草稿态，发起复审即进入复审中
        self.run_action(entry["id"], "发起复审")
        self._save_workspace(review_id, workspace)
        entry["workspace"] = workspace
        return entry

    # -- 接入质检更正 --

    def attach_quality_correction(self, review_id: int, point_id: int,
                                  conclusion: str, remark: str = "",
                                  evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """把一条质检更正写入点位台账并接入本轮复审；更正历史保留，旧复审快照不被覆盖。"""
        workspace = self._load_workspace(review_id)
        batch_key = workspace["batch_key"]
        group_key = workspace["group_key"]
        group = self._dedup.find_group(batch_key, group_key)
        if group is None or point_id not in group.get("orig_point_ids", []):
            raise ValueError(f"点 {point_id} 不属于复审归并组 {group_key}，不具备接入本轮复审的条件")
        if conclusion not in (VERDICT_DRIFT, VERDICT_PASS):
            raise ValueError(f"质检更正结论仅支持 {VERDICT_DRIFT} 或 {VERDICT_PASS}")

        correction = self._quality.apply_quality_correction(
            batch_key, point_id, conclusion, remark=remark,
            evidence=evidence, source_review_id=review_id)
        workspace["attached_corrections"].append(correction)
        workspace["timeline"].append({
            "at": time.time(), "event": "接入质检更正",
            "detail": f"点 {point_id} 更正为 {conclusion}（修订{correction['revision']}）",
        })
        self._save_workspace(review_id, workspace)
        return correction

    # -- 评估判定 --

    def evaluate(self, review_id: int) -> dict[str, Any]:
        """核对当前证据：有复核通过的同组成员即可立即换点，否则挂起整组等待补点。"""
        workspace = self._load_workspace(review_id)
        batch_key = workspace["batch_key"]
        group = self._dedup.find_group(batch_key, workspace["group_key"])
        if group is None:
            raise FileNotFoundError("归并组已不存在")
        bad_point_id = workspace["bad_point_id"]

        members: list[dict[str, Any]] = []
        replacement: dict[str, Any] | None = None
        pending_members: list[int] = []
        for pid in group["orig_point_ids"]:
            verdict = self._quality.get_point_verdict(batch_key, pid)
            conclusion = verdict["conclusion"] if verdict else "证据未确认"
            blocked = conclusion in BLOCKED_VERDICTS
            member = {"point_id": pid, "conclusion": conclusion,
                      "blocked": blocked, "is_bad_point": pid == bad_point_id}
            members.append(member)
            if pid == bad_point_id:
                continue
            if conclusion == VERDICT_PASS and replacement is None:
                replacement = member
            if conclusion in (VERDICT_PENDING, "证据未确认"):
                pending_members.append(pid)

        if replacement is not None:
            decision = DECISION_REPLACE
            detail = (f"点 {replacement['point_id']} 质检更正已复核通过，"
                      f"可立即换用该合法位置继续补采，坏点 {bad_point_id} 仅撤下代表身份")
        else:
            decision = DECISION_HOLD
            if pending_members:
                detail = (f"其余点（{ '、'.join(map(str, pending_members)) }）证据尚未确认，"
                          f"挂起整组等待补点，坏点 {bad_point_id} 先行撤下")
            else:
                detail = f"组内无可用合法代表点，挂起整组等待补点，坏点 {bad_point_id} 先行撤下"

        workspace["decision"] = decision
        workspace["decision_detail"] = detail
        workspace["evaluation"] = {
            "members": members,
            "replacement_point_id": replacement["point_id"] if replacement else None,
            "pending_point_ids": pending_members,
            "evaluated_at": time.time(),
        }
        workspace["timeline"].append({"at": time.time(), "event": "复审评估", "detail": detail})
        self._save_workspace(review_id, workspace)

        entry = store.find(self.MODULE, review_id)
        if entry is not None:
            entry["复审判定"] = decision
            entry["候选证据"] = (
                f"换用候选点{replacement['point_id']}" if replacement
                else f"待确认点{len(pending_members)}个"
            )
        return {"decision": decision, "detail": detail,
                "replacement_point_id": replacement["point_id"] if replacement else None,
                "pending_point_ids": pending_members, "members": members}

    # -- 提交处置 --

    def submit(self, review_id: int, action: str,
               replacement_point_id: int | None = None) -> dict[str, Any]:
        """完成同一个复审申请的处置提交：挂起整组（仅撤坏点）或换点补采。"""
        if action not in ("挂起整组", "换点补采", "仅撤坏点"):
            raise ValueError("复审处置仅支持 挂起整组 / 换点补采 / 仅撤坏点")
        workspace = self._load_workspace(review_id)
        batch_key = workspace["batch_key"]
        group_key = workspace["group_key"]
        bad_point_id = workspace["bad_point_id"]

        if workspace.get("decision") is None:
            self.evaluate(review_id)
            workspace = self._load_workspace(review_id)

        entry = store.find(self.MODULE, review_id)
        if entry is None:
            raise FileNotFoundError(f"归并复审 {review_id} 不存在")
        if entry.get("status") not in ("复审中",):
            raise ValueError(f"复审当前状态为 {entry.get('status')}，不可重复提交")

        if action == "换点补采":
            target_id = replacement_point_id or workspace.get("evaluation", {}).get(
                "replacement_point_id")
            if target_id is None:
                raise ValueError("无复核通过的合法候选点，不能换点补采；应挂起整组等待补点")
            updated_group = self._dedup.replace_representative(batch_key, group_key, int(target_id))
            disposition = {"action": action, "replacement_point_id": int(target_id),
                           "withdrawn_point_id": bad_point_id,
                           "group_status": updated_group["group_status"]}
            self.run_action(review_id, "换点补采")
        else:
            # 挂起整组 与 仅撤坏点 都先把坏点撤下代表身份，整组转待补点
            updated_group = self._dedup.withdraw_representative(
                batch_key, group_key, bad_point_id=bad_point_id)
            disposition = {"action": action, "withdrawn_point_id": bad_point_id,
                           "group_status": updated_group["group_status"]}
            self.run_action(review_id, "挂起整组" if action == "挂起整组" else "仅撤坏点")

        workspace["disposition"] = disposition
        workspace["closed_at"] = time.time()
        workspace["timeline"].append({
            "at": time.time(), "event": f"处置提交：{action}",
            "detail": str(disposition),
        })
        self._save_workspace(review_id, workspace)

        entry = store.find(self.MODULE, review_id)
        entry["处置动作"] = action
        entry["处置时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if action == "换点补采":
            entry["代表点编号"] = str(disposition["replacement_point_id"])
        return {"ok": True, "entry": entry, "disposition": disposition,
                "decision": workspace.get("decision"),
                "decision_detail": workspace.get("decision_detail")}

    # -- 证据查回 --

    def get_review_detail(self, review_id: int) -> dict[str, Any]:
        """查回复审全量证据：冻结快照、更正历史、评估结论、处置记录与时间线。"""
        workspace = self._load_workspace(review_id)
        entry = store.find(self.MODULE, review_id)
        batch_key = workspace["batch_key"]
        # 原始上报点与当前最新质检结论（与冻结快照对照，证明旧处置依据可查回）
        current_points = self._dedup.trace_original_points(
            batch_key, group_key=workspace["group_key"])
        return {
            "entry": entry,
            "workspace": workspace,
            "basis_snapshot": workspace["basis_snapshot"],
            "attached_corrections": workspace["attached_corrections"],
            "current_group": self._dedup.find_group(batch_key, workspace["group_key"]),
            "current_original_points": current_points,
        }

    def list_reviews(self, batch_key: str | None = None,
                     group_key: str | None = None) -> list[dict[str, Any]]:
        """按批次/归并组回查复审记录（含已处置的旧复审）。"""
        rows = list(store.rows(self.MODULE))
        if batch_key:
            rows = [r for r in rows if r.get("批次编号") == batch_key]
        if group_key:
            rows = [r for r in rows if r.get("归并组编号") == group_key]
        return rows
