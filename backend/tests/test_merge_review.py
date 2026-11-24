"""归并复审端到端场景：代表点漂移定性、出处回溯、挂起补点与换点补采。

业务叙事：
- 一个终端在短时间内上报三个近邻点 P1/P2/P3，首轮归并保留 P1 为代表点；
- 随后质检更正确认 P1 是漂移坏点，P2 仍在等待质量复核，P3 更正获准、位置合法；
- 旧合并关系可回查，新一轮归并不再让坏点当代表；
- 复审支持「仅撤坏点 / 挂起整组补点」与「换用 P3 继续补采」两条处置路径，
  且发起复审时冻结的质检依据在处置后仍可查回。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="merge_review_test_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["geo_points", "dedup_merged", "dedup_groups", "quality_reports",
                "point_verdicts", "merge_reviews", "checkpoints"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


def _write_three_neighbor_points(batch_key: str, terminal: str = "TERM-N1") -> dict:
    """构造同一终端短时间内上报的三个近邻点，返回点号映射。"""
    from app.services.base import BaseModuleService
    svc = BaseModuleService()
    base_ts = 1_700_000_000.0
    points = [
        {"point_id": 101, "lat": 39.904200, "lon": 116.407400,
         "timestamp": base_ts, "terminal": terminal, "accuracy": 8.0,
         "batch_key": batch_key},
        {"point_id": 102, "lat": 39.904210, "lon": 116.407410,
         "timestamp": base_ts + 20, "terminal": terminal, "accuracy": 9.0,
         "batch_key": batch_key},
        {"point_id": 103, "lat": 39.904195, "lon": 116.407390,
         "timestamp": base_ts + 40, "terminal": terminal, "accuracy": 7.0,
         "batch_key": batch_key},
    ]
    path = svc._data_dir("geo_points") / f"{batch_key}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for p in points:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return {"p1": 101, "p2": 102, "p3": 103}


class TestMergeReviewLifecycle:
    """归并组出处 → 质检定性 → 复审挂起/换点的完整生命周期。"""

    def test_first_merge_links_representative_to_absorbed_sources(self, setup_data_dir):
        """首轮归并生成归并组明细，代表点与被吸收点的原始出处可互相回溯。"""
        from app.services.dedup_merge import DedupMergeService

        ids = _write_three_neighbor_points("MR-BATCH-1")
        dedup = DedupMergeService()
        result = dedup.dedup_batch("MR-BATCH-1", time_tolerance=120, dist_tolerance=100)

        assert result["original_count"] == 3
        assert result["merged_count"] == 1  # 三个近邻点归成一组
        groups = dedup.get_merge_groups("MR-BATCH-1")
        assert len(groups) == 1
        group = groups[0]
        assert group["representative_point_id"] == ids["p1"]
        assert group["absorbed_point_ids"] == [ids["p2"], ids["p3"]]
        assert group["orig_point_ids"] == [101, 102, 103]

        # 从代表点可追溯全部原始上报点
        traced = dedup.trace_original_points("MR-BATCH-1", point_id=ids["p1"])
        assert len(traced) == 1 and traced[0]["is_representative"] is True
        # 按归并组定位全部原始点
        all_sources = dedup.trace_original_points(
            "MR-BATCH-1", group_key=group["group_key"])
        assert {row["point_id"] for row in all_sources} == {101, 102, 103}

    def test_bad_representative_blocks_and_review_hold_path(self, setup_data_dir):
        """代表点被确认漂移后：新归并规避坏点；复审可挂起整组仅撤坏点，证据可查回。"""
        from app.services.dedup_merge import DedupMergeService
        from app.services.geo_quality import GeoQualityService
        from app.services.merge_review import MergeReviewService

        ids = _write_three_neighbor_points("MR-BATCH-2")
        dedup = DedupMergeService()
        quality = GeoQualityService()
        review_svc = MergeReviewService()

        dedup.dedup_batch("MR-BATCH-2", time_tolerance=120, dist_tolerance=100)
        group_key = dedup.get_merge_groups("MR-BATCH-2")[0]["group_key"]

        # P1（当前代表点）被质检更正确认为漂移点；P2 也仍在等待质量复核
        quality.apply_quality_correction(
            "MR-BATCH-2", ids["p1"], "确认漂移", remark="人工复核确认位置漂移")
        quality.register_drift_suspect("MR-BATCH-2", ids["p2"])
        assert quality.point_is_blocked("MR-BATCH-2", ids["p1"]) is True
        assert quality.point_is_blocked("MR-BATCH-2", ids["p3"]) is False

        # 新一轮归并不再把坏点当代表：待复核的 P2 不具代表资格，自动落到合法的 P3
        rerun = dedup.dedup_batch("MR-BATCH-2", time_tolerance=120, dist_tolerance=100)
        rerun_group = dedup.get_merge_groups("MR-BATCH-2")[0]
        assert rerun_group["representative_point_id"] == ids["p3"]
        assert rerun_group["group_status"] == "代表点已规避坏点"
        assert rerun["blocked_group_count"] == 0

        # 针对旧归并关系发起复审（旧组明细仍可回查）
        entry = review_svc.open_review(
            "MR-BATCH-2", group_key, ids["p1"], remark="代表点漂移定性复审")
        review_id = entry["id"]
        assert entry["status"] == "复审中"

        # 发起时冻结的质检依据：P1 确认漂移、P2 待复核
        workspace = review_svc._load_workspace(review_id)
        basis = {v["point_id"]: v["conclusion"]
                 for v in workspace["basis_snapshot"]["point_verdicts"]}
        assert basis[ids["p1"]] == "确认漂移"
        assert basis[ids["p2"]] == "待复核"

        # P3 无结论、P2 待复核，其余点证据均未确认 → 评估结论应为挂起整组
        decision = review_svc.evaluate(review_id)
        assert decision["decision"] == "挂起整组待补点"
        assert set(decision["pending_point_ids"]) == {ids["p2"], ids["p3"]}

        # 提交「挂起整组」：仅撤下坏点，整组转待补点，归并数据集暂时移除该组
        submitted = review_svc.submit(review_id, "挂起整组")
        assert submitted["entry"]["status"] == "已挂起补点"
        held_group = dedup.find_group("MR-BATCH-2", group_key)
        assert held_group["group_status"] == "待补点"
        assert held_group["withdrawn_point_id"] == ids["p1"]
        merged_points, _ = dedup.get_merged_points("MR-BATCH-2")
        assert all(m["group_key"] != group_key for m in merged_points)

        # 旧合并关系与冻结依据仍可查回
        detail = review_svc.get_review_detail(review_id)
        assert detail["basis_snapshot"]["bad_point_basis"]["conclusion"] == "确认漂移"
        assert {p["point_id"] for p in detail["current_original_points"]} == {101, 102, 103}
        assert any(e["event"] == "发起复审" for e in detail["workspace"]["timeline"])

        # 已提交的复审不可重复提交
        with pytest.raises(ValueError):
            review_svc.submit(review_id, "挂起整组")

    def test_review_switch_to_legal_point_after_correction_approved(self, setup_data_dir):
        """复审执行中第三点更正获准：同一复审可改判为立即换用 P3 继续补采。"""
        from app.services.dedup_merge import DedupMergeService
        from app.services.geo_quality import GeoQualityService
        from app.services.merge_review import MergeReviewService

        ids = _write_three_neighbor_points("MR-BATCH-3")
        dedup = DedupMergeService()
        quality = GeoQualityService()
        review_svc = MergeReviewService()

        dedup.dedup_batch("MR-BATCH-3", time_tolerance=120, dist_tolerance=100)
        group_key = dedup.get_merge_groups("MR-BATCH-3")[0]["group_key"]

        quality.apply_quality_correction("MR-BATCH-3", ids["p1"], "确认漂移")
        quality.register_drift_suspect("MR-BATCH-3", ids["p2"])  # P2 等待复核

        entry = review_svc.open_review("MR-BATCH-3", group_key, ids["p1"])
        review_id = entry["id"]

        # 先评估：无合法候选 → 挂起整组
        first = review_svc.evaluate(review_id)
        assert first["decision"] == "挂起整组待补点"

        # 执行中第三点质检更正获准并接入本轮复审
        correction = review_svc.attach_quality_correction(
            review_id, ids["p3"], "复核通过",
            remark="复核确认 P3 为可代替坏点的合法位置",
            evidence={"source": "manual_recheck"})
        assert correction["conclusion"] == "复核通过"
        assert correction["revision"] == 1
        # 台账中 P3 最新结论已更新
        assert quality.get_point_verdict("MR-BATCH-3", ids["p3"])["conclusion"] == "复核通过"

        # 同一复审重新评估：具备换点条件
        second = review_svc.evaluate(review_id)
        assert second["decision"] == "可立即换用合法代表点"
        assert second["replacement_point_id"] == ids["p3"]

        # 完成同一个复审申请：换用 P3 继续补采
        submitted = review_svc.submit(review_id, "换点补采")
        assert submitted["entry"]["status"] == "已换点补采"
        updated = dedup.find_group("MR-BATCH-3", group_key)
        assert updated["representative_point_id"] == ids["p3"]
        assert updated["replaced_from"] == ids["p1"]
        # 坏点未被删除，仍保留在组内出处中，仅失去代表身份
        assert ids["p1"] in updated["orig_point_ids"]
        assert updated["absorbed_point_ids"][0:]  # 非空
        merged_points, _ = dedup.get_merged_points("MR-BATCH-3")
        assert len(merged_points) == 1
        assert merged_points[0]["representative_point_id"] == ids["p3"]

        # 冻结快照仍是发起时依据（P3 当时证据未确认），证明原处置证据可查回
        detail = review_svc.get_review_detail(review_id)
        basis_p3 = next(v for v in detail["basis_snapshot"]["point_verdicts"]
                        if v["point_id"] == ids["p3"])
        assert basis_p3["conclusion"] == "证据未确认"
        # 更正历史完整保留
        assert len(detail["attached_corrections"]) == 1
        assert detail["attached_corrections"][0]["point_id"] == ids["p3"]

    def test_only_group_members_can_attach_and_replace(self, setup_data_dir):
        """组外点不能接入复审；未复核通过的点不能作为替代代表点。"""
        from app.services.dedup_merge import DedupMergeService
        from app.services.geo_quality import GeoQualityService
        from app.services.merge_review import MergeReviewService

        ids = _write_three_neighbor_points("MR-BATCH-4")
        dedup = DedupMergeService()
        quality = GeoQualityService()
        review_svc = MergeReviewService()

        dedup.dedup_batch("MR-BATCH-4", time_tolerance=120, dist_tolerance=100)
        group_key = dedup.get_merge_groups("MR-BATCH-4")[0]["group_key"]
        quality.apply_quality_correction("MR-BATCH-4", ids["p1"], "确认漂移")
        quality.register_drift_suspect("MR-BATCH-4", ids["p2"])  # P2 等待质量复核
        entry = review_svc.open_review("MR-BATCH-4", group_key, ids["p1"])

        with pytest.raises(ValueError):
            review_svc.attach_quality_correction(entry["id"], 999, "复核通过")
        with pytest.raises(ValueError):
            review_svc.attach_quality_correction(
                entry["id"], ids["p3"], "非法结论")
        # P2 仍在等待质量复核，不具代表资格，直接换点应被拒绝
        with pytest.raises(ValueError):
            review_svc.submit(entry["id"], "换点补采", replacement_point_id=ids["p2"])
        # 无任何复核通过的候选点时，无候选自动换点同样不允许
        with pytest.raises(ValueError):
            review_svc.submit(entry["id"], "换点补采")

    def test_withdraw_only_bad_point_keeps_group_traceable(self, setup_data_dir):
        """仅撤坏点处置：坏点失去代表身份，组转待补点，出处与旧关系全部可回查。"""
        from app.services.dedup_merge import DedupMergeService
        from app.services.geo_quality import GeoQualityService
        from app.services.merge_review import MergeReviewService

        ids = _write_three_neighbor_points("MR-BATCH-5")
        dedup = DedupMergeService()
        quality = GeoQualityService()
        review_svc = MergeReviewService()

        dedup.dedup_batch("MR-BATCH-5", time_tolerance=120, dist_tolerance=100)
        group_key = dedup.get_merge_groups("MR-BATCH-5")[0]["group_key"]
        quality.apply_quality_correction("MR-BATCH-5", ids["p1"], "确认漂移")

        entry = review_svc.open_review("MR-BATCH-5", group_key, ids["p1"])
        review_svc.evaluate(entry["id"])
        result = review_svc.submit(entry["id"], "仅撤坏点")

        assert result["entry"]["status"] == "已撤点"
        group = dedup.find_group("MR-BATCH-5", group_key)
        assert group["group_status"] == "待补点"
        assert group["withdrawn_point_id"] == ids["p1"]
        # 三个原始点出处一个不少
        assert group["orig_point_ids"] == [101, 102, 103]
        # 复审列表可按组回查到这条旧复审
        rows = review_svc.list_reviews(batch_key="MR-BATCH-5", group_key=group_key)
        assert len(rows) == 1 and rows[0]["id"] == entry["id"]
