"""点位归并领域服务：按去重键、时间容差和距离容差对重复定位点执行归并去重，生成归并后的数据集。

归并产物分两层：
- 归并组明细 (dedup_groups/{batch}_groups.jsonl)：每个归并组记录代表点、被吸收点及全部原始上报点出处，
  并关联每个点当前的质检结论，支持从代表点回溯原始观测；
- 归并数据集 (dedup_merged/{batch}_merged.jsonl)：每组输出一条代表记录，orig_point_ids 保留全组出处。

代表点选择受点位质检结论约束：确认漂移的坏点与待复核（证据未确认）的点都不得作为代表点；
若一组内没有任何合格代表，该组标记为 待补点 且不产出归并记录，等待复审处置。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.geo_quality import GeoQualityService
from app.store import store


class DedupMergeService(BaseModuleService):
    MODULE = "dedup_merge"
    ENTITY = "归并批次"
    OPERATION_TERM = "去重"
    LIST_FIELDS = ["归并编号", "批次编号", "去重键列", "时间容差", "距离容差", "原始条数", "归并条数", "归并水位"]
    REQUIRED_FIELDS = ["归并编号", "批次编号", "去重键列"]
    STATUS_ORDER = ["草稿", "待归并", "已归并", "已回滚"]
    ACTION_RULES = {"启动归并": "待归并", "确认归并": "已归并", "执行回滚": "已回滚"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._quality = GeoQualityService()

    # -- 产物路径 --

    def _groups_file(self, batch_key: str):
        return self._data_dir("dedup_groups") / f"{batch_key}_groups.jsonl"

    def _merged_file(self, batch_key: str):
        return self._data_dir("dedup_merged") / f"{batch_key}_merged.jsonl"

    # -- 归并执行 --

    def dedup_batch(self, batch_key: str, time_tolerance: float = 60.0,
                     dist_tolerance: float = 10.0) -> dict[str, Any]:
        """读取采集批次定位点数据集文件，按时间和距离容差执行去重归并，生成归并组明细与归并数据集。"""
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")

        points = self._read_jsonl(points_file)
        if not points:
            return {"batch_key": batch_key, "original": 0, "merged": 0}

        original_count = len(points)
        # 按终端和时间排序
        points.sort(key=lambda p: (p.get("terminal", ""), p.get("timestamp", 0)))

        groups: list[dict[str, Any]] = []
        for p in points:
            target_group: dict[str, Any] | None = None
            for grp in reversed(groups[-10:]):  # 只看最近10组提高性能
                if grp.get("terminal") != p.get("terminal", ""):
                    continue
                rep = grp["members"][0]
                time_diff = abs(p.get("timestamp", 0) - rep.get("timestamp", 0))
                dist = self._haversine(
                    p.get("lat", 0), p.get("lon", 0),
                    rep.get("lat", 0), rep.get("lon", 0),
                )
                if time_diff <= time_tolerance and dist <= dist_tolerance:
                    target_group = grp
                    break
            if target_group is None:
                groups.append({
                    "group_key": f"GRP-{batch_key}-{len(groups) + 1:04d}",
                    "batch_key": batch_key,
                    "terminal": p.get("terminal", ""),
                    "time_tolerance": time_tolerance,
                    "dist_tolerance": dist_tolerance,
                    "members": [p],
                })
            else:
                target_group["members"].append(p)

        group_records, merged_records, blocked_groups = self._build_group_records(
            batch_key, groups)

        # 覆盖写：新一轮归并不能继续沿用已被质检否定的旧代表点
        import json
        groups_file = self._groups_file(batch_key)
        groups_file.parent.mkdir(parents=True, exist_ok=True)
        with open(groups_file, "w", encoding="utf-8") as f:
            for rec in group_records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        merged_file = self._merged_file(batch_key)
        merged_file.parent.mkdir(parents=True, exist_ok=True)
        with open(merged_file, "w", encoding="utf-8") as f:
            for rec in merged_records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        written_groups, written = len(group_records), len(merged_records)

        # 创建归并记录
        entry, _ = self.create_entry({
            "归并编号": f"DEDUP-{batch_key}", "批次编号": batch_key,
            "去重键列": "terminal,timestamp",
            "时间容差": f"{time_tolerance}s",
            "距离容差": f"{dist_tolerance}m",
            "原始条数": str(original_count),
            "归并条数": str(written),
            "归并水位": f"已归并{written}/{original_count}条,待补点组{len(blocked_groups)}个",
        })
        if entry:
            self.run_action(entry["id"], "确认归并")

        return {
            "batch_key": batch_key,
            "original_count": original_count,
            "merged_count": written,
            "group_count": written_groups,
            "blocked_group_count": len(blocked_groups),
            "removed_duplicates": original_count - written,
            "groups_file": str(self._groups_file(batch_key)),
            "output_file": str(self._merged_file(batch_key)),
        }

    def _verdict_view(self, batch_key: str, point_id: int) -> dict[str, Any]:
        """汇总单个原始点的质检结论，供归并组明细与复审取数。"""
        verdict = self._quality.get_point_verdict(batch_key, point_id)
        if verdict is None:
            return {"point_id": point_id, "conclusion": "证据未确认", "blocked": False,
                    "verdict_revision": 0}
        return {
            "point_id": point_id,
            "conclusion": verdict.get("conclusion"),
            "blocked": verdict.get("conclusion") in {"确认漂移"},
            "verdict_revision": verdict.get("revision", 1),
            "verdict_updated_at": verdict.get("updated_at"),
            "verdict_remark": verdict.get("remark", ""),
        }

    def _build_group_records(self, batch_key: str,
                             groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """把归并组展开为可落盘的组明细与归并记录，代表点选择跳过已确认漂移的坏点。"""
        group_records: list[dict[str, Any]] = []
        merged_records: list[dict[str, Any]] = []
        blocked_groups: list[str] = []

        for grp in groups:
            members = grp["members"]
            member_views = [
                {**self._verdict_view(batch_key, m["point_id"]),
                 "lat": m.get("lat"), "lon": m.get("lon"),
                 "timestamp": m.get("timestamp"), "accuracy": m.get("accuracy")}
                for m in members
            ]
            eligible = [m for m in members
                        if self._quality.point_is_eligible_representative(
                            batch_key, m["point_id"])]
            group_status = "已归并"
            representative = None
            if eligible:
                representative = eligible[0]
                if members[0]["point_id"] != representative["point_id"]:
                    group_status = "代表点已规避坏点"
            else:
                group_status = "待补点"
                blocked_groups.append(grp["group_key"])
                # 无合法代表点时仅保留原始首点作占位，不产出归并记录
                representative = members[0]

            absorbed_ids = [m["point_id"] for m in members
                            if m["point_id"] != representative["point_id"]]
            record = {
                "group_key": grp["group_key"],
                "batch_key": batch_key,
                "terminal": grp.get("terminal"),
                "representative_point_id": representative["point_id"],
                "absorbed_point_ids": absorbed_ids,
                "orig_point_ids": [m["point_id"] for m in members],
                "member_count": len(members),
                "group_status": group_status,
                "member_verdicts": member_views,
                "time_tolerance": grp.get("time_tolerance"),
                "dist_tolerance": grp.get("dist_tolerance"),
            }
            group_records.append(record)

            if group_status != "待补点":
                merged_records.append({
                    "point_id": len(merged_records) + 1,
                    "group_key": grp["group_key"],
                    "orig_point_ids": record["orig_point_ids"],
                    "representative_point_id": representative["point_id"],
                    "lat": representative["lat"],
                    "lon": representative["lon"],
                    "timestamp": representative["timestamp"],
                    "terminal": representative.get("terminal"),
                    "accuracy": representative.get("accuracy"),
                    "batch_key": batch_key,
                    "dedup_time_tolerance": grp.get("time_tolerance"),
                    "dedup_dist_tolerance": grp.get("dist_tolerance"),
                })
        return group_records, merged_records, blocked_groups

    # -- 出处回溯检索 --

    def get_merge_groups(self, batch_key: str) -> list[dict[str, Any]]:
        """读取批次的归并组明细（代表点与被吸收点出处 + 各组质检结论）。"""
        return self._read_jsonl(self._groups_file(batch_key))

    def find_group(self, batch_key: str, group_key: str) -> dict[str, Any] | None:
        """按归并组编号定位单个归并组明细。"""
        for grp in self.get_merge_groups(batch_key):
            if grp.get("group_key") == group_key:
                return grp
        return None

    def trace_original_points(self, batch_key: str, point_id: int | None = None,
                              group_key: str | None = None,
                              conclusion: str | None = None) -> list[dict[str, Any]]:
        """按归并组明细和质检结果定位原始上报点，返回每个原始点的出处与质检结论。"""
        groups = self.get_merge_groups(batch_key)
        if group_key:
            groups = [g for g in groups if g.get("group_key") == group_key]
        points = {p["point_id"]: p for p in self._read_jsonl(
            self._data_dir("geo_points") / f"{batch_key}.jsonl")}
        result: list[dict[str, Any]] = []
        for grp in groups:
            for member in grp.get("member_verdicts", []):
                if point_id is not None and int(member.get("point_id", -1)) != int(point_id):
                    continue
                if conclusion and member.get("conclusion") != conclusion:
                    continue
                raw = points.get(member["point_id"], {})
                result.append({
                    "group_key": grp["group_key"],
                    "group_status": grp.get("group_status"),
                    "is_representative": grp.get("representative_point_id") == member["point_id"],
                    "point_id": member["point_id"],
                    "lat": raw.get("lat", member.get("lat")),
                    "lon": raw.get("lon", member.get("lon")),
                    "timestamp": raw.get("timestamp", member.get("timestamp")),
                    "terminal": raw.get("terminal", grp.get("terminal")),
                    "accuracy": raw.get("accuracy", member.get("accuracy")),
                    "conclusion": member.get("conclusion"),
                    "blocked": bool(member.get("blocked")),
                    "verdict_revision": member.get("verdict_revision"),
                })
        return result

    def _rewrite_products(self, batch_key: str,
                          groups: list[dict[str, Any]]) -> tuple[int, int]:
        """按当前归并组明细重写组明细文件与归并数据集（待补点组不产出归并记录）。"""
        import json

        groups_file = self._groups_file(batch_key)
        groups_file.parent.mkdir(parents=True, exist_ok=True)
        with open(groups_file, "w", encoding="utf-8") as f:
            for rec in groups:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

        points = {p["point_id"]: p for p in self._read_jsonl(
            self._data_dir("geo_points") / f"{batch_key}.jsonl")}
        merged: list[dict[str, Any]] = []
        for grp in groups:
            if grp.get("group_status") == "待补点":
                continue
            rep_id = grp["representative_point_id"]
            rep = points.get(rep_id, {})
            merged.append({
                "point_id": len(merged) + 1,
                "group_key": grp["group_key"],
                "orig_point_ids": grp["orig_point_ids"],
                "representative_point_id": rep_id,
                "lat": rep.get("lat"), "lon": rep.get("lon"),
                "timestamp": rep.get("timestamp"),
                "terminal": rep.get("terminal"), "accuracy": rep.get("accuracy"),
                "batch_key": batch_key,
                "dedup_time_tolerance": grp.get("time_tolerance"),
                "dedup_dist_tolerance": grp.get("dist_tolerance"),
            })
        merged_file = self._merged_file(batch_key)
        merged_file.parent.mkdir(parents=True, exist_ok=True)
        with open(merged_file, "w", encoding="utf-8") as f:
            for rec in merged:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return len(groups), len(merged)

    def withdraw_representative(self, batch_key: str, group_key: str,
                                bad_point_id: int | None = None) -> dict[str, Any]:
        """复审处置：仅撤下坏点的代表身份，整组转为待补点，归并数据集暂时移除该组记录。

        组内成员与出处全部保留，旧合并关系仍可回查，等待后续补点或换用合法代表点。
        """
        groups = self.get_merge_groups(batch_key)
        target = next((g for g in groups if g.get("group_key") == group_key), None)
        if target is None:
            raise FileNotFoundError(f"批次 {batch_key} 中不存在归并组 {group_key}")
        withdrawn = bad_point_id or target.get("representative_point_id")
        target["withdrawn_point_id"] = withdrawn
        target["group_status"] = "待补点"
        target["absorbed_point_ids"] = list(target.get("orig_point_ids", []))
        self._rewrite_products(batch_key, groups)
        return target

    def replace_representative(self, batch_key: str, group_key: str,
                               new_point_id: int) -> dict[str, Any]:
        """复审处置：把归并组代表点换用为已复核通过的合法点，重写组明细与归并数据集。

        新代表点必须是组内成员且未被确认漂移；被替换的坏点保留在成员与出处中，仅撤下代表身份。
        """
        groups = self.get_merge_groups(batch_key)
        target = next((g for g in groups if g.get("group_key") == group_key), None)
        if target is None:
            raise FileNotFoundError(f"批次 {batch_key} 中不存在归并组 {group_key}")
        if new_point_id not in target.get("orig_point_ids", []):
            raise ValueError(f"点 {new_point_id} 不属于归并组 {group_key}，不能接入该组复审")
        if not self._quality.point_is_eligible_representative(batch_key, new_point_id):
            verdict = self._quality.get_point_verdict(batch_key, new_point_id)
            reason = verdict.get("conclusion") if verdict else "证据未确认"
            raise ValueError(f"点 {new_point_id} 当前结论为 {reason}，不能作为代表点")

        old_rep = target.get("representative_point_id")
        target["representative_point_id"] = new_point_id
        target["absorbed_point_ids"] = [pid for pid in target["orig_point_ids"]
                                        if pid != new_point_id]
        target["group_status"] = "已换用合法代表点"
        target["replaced_from"] = old_rep
        # 刷新成员质检视图
        target["member_verdicts"] = [
            {**self._verdict_view(batch_key, m["point_id"]),
             **{k: m[k] for k in ("lat", "lon", "timestamp", "accuracy") if k in m}}
            for m in target["member_verdicts"]
        ]
        self._rewrite_products(batch_key, groups)
        return target

    def get_merged_points(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定批次的去重归并后的定位点数据集文件。"""
        file_path = self._merged_file(batch_key)
        if not file_path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的归并数据集不存在")
        data = self._read_jsonl(file_path)
        return data, str(file_path)
