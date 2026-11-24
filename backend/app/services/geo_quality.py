"""轨迹质量领域服务：对采集批次定位点执行漂移检测、重复率和缺失率校验，生成质量报告数据集。

除批次级报告外，还维护「点位质检结论台账」(point_verdicts)：
- 漂移检测命中的点会登记为 待复核 结论，供人工质检跟进；
- 复核结论可被更正确认为 确认漂移（坏点，禁止再作归并代表点）或 复核通过（合法位置，可作替代代表点）；
- 点位归并与归并复审都通过本台账取数，确保坏点不会继续充当代表点。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 质检结论取值：待复核 / 确认漂移 / 复核通过
VERDICT_PENDING = "待复核"
VERDICT_DRIFT = "确认漂移"
VERDICT_PASS = "复核通过"
# 供归并侧判断「坏点」的结论集合
BLOCKED_VERDICTS = {VERDICT_DRIFT}


class GeoQualityService(BaseModuleService):
    MODULE = "geo_quality"
    ENTITY = "质量报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "批次编号", "漂移点数", "重复点数", "缺失率", "异常时段", "校验规则", "质检结论"]
    REQUIRED_FIELDS = ["报告编号", "批次编号", "漂移点数"]
    STATUS_ORDER = ["草稿", "待质检", "质检中", "已出报告"]
    ACTION_RULES = {"启动质检": "待质检", "出具报告": "已出报告", "标记驳回": "已出报告"}
    NEGATIVE_ACTIONS = []

    # -- 点位级质检结论台账 --

    def _verdicts_path(self, batch_key: str):
        return self._data_dir("point_verdicts") / f"{batch_key}_verdicts.jsonl"

    def _load_verdicts(self, batch_key: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self._verdicts_path(batch_key))

    def _save_verdicts(self, batch_key: str, verdicts: list[dict[str, Any]]) -> int:
        path = self._verdicts_path(batch_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for rec in verdicts:
                f.write(self._json_line(rec))
        return len(verdicts)

    @staticmethod
    def _json_line(rec: dict[str, Any]) -> str:
        import json
        return json.dumps(rec, ensure_ascii=False, default=str) + "\n"

    def list_point_verdicts(self, batch_key: str, point_id: int | None = None,
                            conclusion: str | None = None) -> list[dict[str, Any]]:
        """按批次（可再按原始点号、质检结论）读取点位质检结论台账。"""
        rows = self._load_verdicts(batch_key)
        if point_id is not None:
            rows = [r for r in rows if int(r.get("point_id", -1)) == int(point_id)]
        if conclusion:
            rows = [r for r in rows if r.get("conclusion") == conclusion]
        return rows

    def get_point_verdict(self, batch_key: str, point_id: int) -> dict[str, Any] | None:
        """读取单个原始上报点的最新质检结论，无结论时返回 None（视为证据未确认）。"""
        rows = self.list_point_verdicts(batch_key, point_id=point_id)
        return rows[-1] if rows else None

    def register_drift_suspect(self, batch_key: str, point_id: int,
                               evidence: dict[str, Any] | None = None,
                               source_report: str = "") -> dict[str, Any]:
        """漂移检测命中后登记一条 待复核 结论；已存在结论的点不重复登记。"""
        existing = self.get_point_verdict(batch_key, point_id)
        if existing is not None:
            return existing
        rec = {
            "batch_key": batch_key,
            "point_id": point_id,
            "conclusion": VERDICT_PENDING,
            "source_report": source_report or f"QUAL-{batch_key}",
            "evidence": evidence or {},
            "remark": "漂移检测命中，等待人工质量复核",
            "created_at": time.time(),
            "updated_at": time.time(),
            "revision": 1,
        }
        path = self._verdicts_path(batch_key)
        self._write_jsonl(path, [rec])
        return rec

    def apply_quality_correction(self, batch_key: str, point_id: int, conclusion: str,
                                 remark: str = "", evidence: dict[str, Any] | None = None,
                                 source_review_id: int | None = None) -> dict[str, Any]:
        """对单个原始点提交质检更正，追加一条带修订号的新结论，保留完整更正历史。

        conclusion 仅允许 确认漂移 / 复核通过；待复核 由漂移检测登记，不在此接口受理。
        """
        if conclusion not in (VERDICT_DRIFT, VERDICT_PASS):
            raise ValueError(f"质检更正结论仅支持 {VERDICT_DRIFT} 或 {VERDICT_PASS}")
        rows = self._load_verdicts(batch_key)
        history = [r for r in rows if int(r.get("point_id", -1)) == int(point_id)]
        prev = history[-1] if history else None
        now = time.time()
        rec = {
            "batch_key": batch_key,
            "point_id": point_id,
            "conclusion": conclusion,
            "source_report": (prev or {}).get("source_report", f"QUAL-{batch_key}"),
            "evidence": evidence or (prev or {}).get("evidence", {}),
            "remark": remark or ("人工质检更正：确认漂移点" if conclusion == VERDICT_DRIFT
                                 else "人工质检更正：复核通过，位置合法"),
            "previous_conclusion": (prev or {}).get("conclusion"),
            "source_review_id": source_review_id,
            "created_at": (prev or {}).get("created_at", now),
            "updated_at": now,
            "revision": len(history) + 1,
        }
        self._write_jsonl(self._verdicts_path(batch_key), [rec])
        return rec

    def point_is_blocked(self, batch_key: str, point_id: int) -> bool:
        """坏点判定：最新结论为 确认漂移 的点禁止作为归并代表点。"""
        latest = self.get_point_verdict(batch_key, point_id)
        return bool(latest and latest.get("conclusion") in BLOCKED_VERDICTS)

    def point_is_eligible_representative(self, batch_key: str, point_id: int) -> bool:
        """代表点资格：确认漂移为坏点、待复核为证据未确认，二者均不得当代表；

        无结论（证据未确认但未被质疑）与复核通过的点具备资格。
        """
        latest = self.get_point_verdict(batch_key, point_id)
        if latest is None:
            return True
        return latest.get("conclusion") not in BLOCKED_VERDICTS | {VERDICT_PENDING}

    def check_quality(self, batch_key: str, max_speed_mps: float = 50.0,
                       max_gap_sec: float = 300.0) -> dict[str, Any]:
        """读取采集批次定位点数据文件，执行漂移检测、重复率统计和缺失率判定，生成质量校验报告数据集。"""
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")

        points = self._read_jsonl(points_file)
        if not points:
            return {"batch_key": batch_key, "total_points": 0}

        total = len(points)
        # 时间排序
        points.sort(key=lambda p: p.get("timestamp", 0))

        drift_count = 0
        duplicate_count = 0
        gap_count = 0
        anomaly_periods = []

        for i in range(1, total):
            prev = points[i - 1]
            curr = points[i]
            time_diff = abs(curr.get("timestamp", 0) - prev.get("timestamp", 0))
            dist = self._haversine(
                prev.get("lat", 0), prev.get("lon", 0),
                curr.get("lat", 0), curr.get("lon", 0)
            )

            # 漂移检测：速度超过阈值
            if time_diff > 0:
                speed = dist / time_diff
                if speed > max_speed_mps:
                    drift_count += 1
                    anomaly_periods.append({
                        "point_id": curr.get("point_id"),
                        "speed_mps": round(speed, 2),
                        "time": time.strftime("%H:%M:%S", time.localtime(curr["timestamp"])),
                        "type": "drift",
                    })
                    # 漂移命中即登记待复核结论，供归并与复审取数
                    self.register_drift_suspect(
                        batch_key, curr["point_id"],
                        evidence={"speed_mps": round(speed, 2), "max_speed_mps": max_speed_mps},
                        source_report=f"QUAL-{batch_key}",
                    )

            # 重复点检测：同一终端时间和距离相近
            if (prev.get("terminal") == curr.get("terminal") and
                time_diff < 5 and dist < 1.0):
                duplicate_count += 1

            # 缺失检测：时间间隔过大
            if time_diff > max_gap_sec:
                gap_count += 1
                anomaly_periods.append({
                    "time": time.strftime("%H:%M:%S", time.localtime(prev["timestamp"])),
                    "gap_sec": round(time_diff, 1),
                    "type": "gap",
                })

        missing_rate = round(gap_count / max(total, 1), 4)
        drift_rate = round(drift_count / max(total, 1), 4)
        dup_rate = round(duplicate_count / max(total, 1), 4)

        # 质检结论
        if drift_rate < 0.05 and missing_rate < 0.1:
            conclusion = "合格"
        elif drift_rate < 0.2 and missing_rate < 0.3:
            conclusion = "待复核"
        else:
            conclusion = "不合格"

        report = {
            "batch_key": batch_key,
            "total_points": total,
            "drift_points": drift_count,
            "duplicate_points": duplicate_count,
            "gap_points": gap_count,
            "drift_rate": drift_rate,
            "duplicate_rate": dup_rate,
            "missing_rate": missing_rate,
            "max_speed_mps": max_speed_mps,
            "max_gap_sec": max_gap_sec,
            "anomaly_periods": anomaly_periods[:10],  # 只保留前10个异常
            "conclusion": conclusion,
            "checked_at": time.time(),
        }

        output_file = self._data_dir("quality_reports") / f"{batch_key}_quality.jsonl"
        written = self._write_jsonl(output_file, [report])

        # 创建质检报告记录
        entry, _ = self.create_entry({
            "报告编号": f"QUAL-{batch_key}",
            "批次编号": batch_key,
            "漂移点数": str(drift_count),
            "重复点数": str(duplicate_count),
            "缺失率": f"{missing_rate:.1%}",
            "异常时段": f"间隙{gap_count}段/漂移{drift_count}点",
            "校验规则": f"最大速度{max_speed_mps}m/s,最大间隙{max_gap_sec}s",
            "质检结论": conclusion,
        })
        if entry:
            self.run_action(entry["id"], "出具报告")

        return {
            "batch_key": batch_key,
            "total_points": total,
            "drift_count": drift_count,
            "duplicate_count": duplicate_count,
            "gap_count": gap_count,
            "missing_rate": f"{missing_rate:.1%}",
            "conclusion": conclusion,
            "point_verdicts": len(self.list_point_verdicts(batch_key)),
            "output_file": str(output_file),
        }

    def get_quality_report(self, batch_key: str) -> dict[str, Any]:
        """读取批次的轨迹质量报告数据集。"""
        file_path = self._data_dir("quality_reports") / f"{batch_key}_quality.jsonl"
        reports = self._read_jsonl(file_path)
        return reports[0] if reports else {}
