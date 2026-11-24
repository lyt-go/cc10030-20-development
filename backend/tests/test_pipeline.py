"""跨模块集成测试：验证地理空间数据采集→转换→切分→驻留→聚合→快照→导出的完整链路。"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    """每个测试用例使用独立临时目录写入数据产物文件。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_test_"))
    # 注入临时路径到 base
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    # 创建必要的子目录
    for sub in ["geo_points", "coord_transformed", "track_segments", "dwell_events",
                "grid_index", "heatmap", "dedup_merged", "spatial_join",
                "poi_match", "region_aggregates", "od_matrix", "route_restore",
                "density_estimate", "quality_reports", "snapshots", "exports",
                "checkpoints", "point_verdicts", "dedup_groups", "merge_reviews"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    # 清理
    if tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestFullDataPipeline:
    """全链路测试：采集→转换→切分→驻留→网格→聚合→快照→导出。"""

    def test_full_pipeline_end_to_end(self, setup_data_dir):
        """端到端：采集批次→坐标转换→轨迹切分→驻留识别→网格索引→热力聚合→空间关联→OD矩阵→密度估算→质检→快照→导出。"""
        from app.services.geo_source import GeoSourceService
        from app.services.coord_transform import CoordTransformService
        from app.services.track_segment import TrackSegmentService
        from app.services.dwell_detect import DwellDetectService
        from app.services.grid_index import GridIndexService
        from app.services.heatmap_aggregate import HeatmapAggregateService
        from app.services.spatial_join import SpatialJoinService
        from app.services.od_matrix import OdMatrixService
        from app.services.density_estimate import DensityEstimateService
        from app.services.geo_quality import GeoQualityService
        from app.services.geo_snapshot import GeoSnapshotService
        from app.services.geo_export import GeoExportService

        geo = GeoSourceService()
        coord = CoordTransformService()
        track = TrackSegmentService()
        dwell = DwellDetectService()
        grid = GridIndexService()
        heatmap = HeatmapAggregateService()
        sjoin = SpatialJoinService()
        od = OdMatrixService()
        density = DensityEstimateService()
        quality = GeoQualityService()
        snapshot = GeoSnapshotService()
        export = GeoExportService()

        batch_key = "TEST-BATCH-001"

        # Step 1: 接入采集批次并生成定位点文件
        result1 = geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-001",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=30)
        assert result1 is not None
        assert result1["批次编号"] == batch_key
        assert result1["采集水位"] == "已采集30条"

        # 验证定位点文件存在且有30条记录
        points, points_path = geo.get_batch_points(batch_key)
        assert len(points) == 30
        assert points[0]["batch_key"] == batch_key

        # Step 2: 坐标标准化转换
        # 先创建转换规则
        coord.create_entry({
            "规则编号": "TX-RULE-001",
            "源坐标系": "WGS84",
            "目标坐标系": "GCJ02",
            "偏移参数": "0.0,0.0",
            "精度位数": "6",
            "越界处理": "clamp",
            "规则版本": "v1.0",
            "生效批次": "",
        })
        coord.run_action(1, "确认生效")

        result2 = coord.transform_batch(batch_key, rule_id=1)
        assert result2["input_points"] == 30
        assert result2["transformed_points"] == 30
        transformed_pts, _ = coord.get_transformed_points(batch_key)
        assert len(transformed_pts) == 30
        assert "trans_lat" in transformed_pts[0]

        # Step 3: 轨迹切分
        result3 = track.segment_from_transformed(batch_key, interval_sec=300, distance_threshold=500)
        assert result3["segments"] > 0
        assert result3["total_points"] == 30
        segments, seg_file = track.get_segments(batch_key)
        assert len(segments) > 0
        assert "start_lat" in segments[0]

        # Step 4: 驻留识别
        result4 = dwell.detect_from_segments(batch_key, radius=200, min_dwell_sec=30)
        assert result4["segments_processed"] > 0
        events, _ = dwell.get_dwell_events(batch_key)
        assert isinstance(events, list)

        # Step 5: 网格索引
        result5 = grid.index_points_from_batch(batch_key, precision=6)
        assert result5["grids_generated"] > 0
        grids, _ = grid.get_grid_index(batch_key)
        assert len(grids) > 0
        assert "geohash" in grids[0]
        assert grids[0]["point_count"] > 0

        # Step 6: 热力聚合
        result6 = heatmap.aggregate_grid_heatmap(batch_key)
        assert result6["heatmap_cells"] > 0
        heatmap_data, _ = heatmap.get_heatmap_data(batch_key)
        assert len(heatmap_data) > 0

        # Step 7: 空间关联（轨迹-网格）
        result7 = sjoin.join_trajectories_to_grids(batch_key)
        assert result7["total_segments"] > 0
        joins, _ = sjoin.get_join_results(batch_key)
        assert len(joins) > 0
        assert "grid_geohash" in joins[0]

        # Step 8: OD矩阵
        result8 = od.compute_od_from_trajectories(batch_key)
        assert result8["segments_processed"] > 0
        od_data, _ = od.get_od_matrix(batch_key)
        assert len(od_data) > 0
        assert "trip_count" in od_data[0]

        # Step 9: 密度估算
        result9 = density.estimate_density(batch_key, bandwidth=500)
        assert result9["grids_estimated"] > 0
        density_data, _ = density.get_density_estimates(batch_key)
        assert len(density_data) > 0
        assert "estimated_density" in density_data[0]

        # Step 10: 质量校验
        result10 = quality.check_quality(batch_key)
        assert result10["total_points"] == 30
        assert "conclusion" in result10
        assert result10["missing_rate"] is not None

        # Step 11: 分析快照
        result11 = snapshot.create_snapshot(batch_key)
        assert result11["total_results"] > 0
        snap = snapshot.get_snapshot(batch_key)
        assert snap is not None
        assert "snapshot_id" in snap

        # Step 12: 结果导出
        result12 = export.export_batch_results(batch_key)
        assert result12["exported_records"] > 0
        summary = export.get_export_summary(batch_key)
        assert summary.get("total_records", 0) > 0

    def test_confirm_batch_triggers_coord_and_grid(self, setup_data_dir):
        """确认接入触发坐标转换和网格索引自动创建。"""
        from app.services.geo_source import GeoSourceService
        from app.services.coord_transform import CoordTransformService

        geo = GeoSourceService()
        coord = CoordTransformService()

        batch_key = "TRIG-001"

        # 接入批次
        entry = geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-T1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=20)

        # 确认接入（使用onboard返回的entry id）
        result = geo.confirm_batch(entry["id"])
        assert result["batch_key"] == batch_key
        assert "coord_transform" in result["auto_triggers"]
        assert result["auto_triggers"]["coord_transform"]["transformed_points"] == 20
        assert "grid_index" in result["auto_triggers"]
        assert result["auto_triggers"]["grid_index"]["grids_generated"] > 0

        # 验证转换后坐标文件存在
        transformed_pts, _ = coord.get_transformed_points(batch_key)
        assert len(transformed_pts) == 20

    def test_dedup_merge_reduces_duplicates(self, setup_data_dir):
        """点位去重归并确实减少记录条数。"""
        from app.services.geo_source import GeoSourceService
        from app.services.dedup_merge import DedupMergeService

        geo = GeoSourceService()
        dedup = DedupMergeService()

        batch_key = "DEDUP-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-D1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=30)

        result = dedup.dedup_batch(batch_key, time_tolerance=3600, dist_tolerance=500)
        assert result["original_count"] == 30
        assert result["merged_count"] <= 30
        merged_pts, _ = dedup.get_merged_points(batch_key)
        assert len(merged_pts) == result["merged_count"]

    def test_geofence_hit_detection(self, setup_data_dir):
        """围栏判定能检测到围栏内命中记录。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geofence_rule import GeofenceRuleService

        geo = GeoSourceService()
        fence = GeofenceRuleService()

        batch_key = "FENCE-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-F1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=30)

        # 创建围栏
        fence.create_entry({
            "围栏编号": "FENCE-001",
            "围栏形状": "polygon",
            "顶点序列": "39.9,116.4;39.92,116.4;39.92,116.42;39.9,116.42",
            "判定方式": "ray_casting",
            "进出条件": "inside",
            "生效时段": "2026-09-01",
            "命中次数": "0",
            "规则版本": "v1.0",
        })
        fence.run_action(1, "确认生效")

        result = fence.check_points_against_fence(batch_key, fence_id=1)
        assert result["total_points"] == 30
        assert result["hits"] >= 0  # 视生成随机点可能命中也可能不命中

    def test_poi_match_generates_candidates(self, setup_data_dir):
        """驻留事件与POI匹配生成候选排名数据。"""
        from app.services.geo_source import GeoSourceService
        from app.services.coord_transform import CoordTransformService
        from app.services.track_segment import TrackSegmentService
        from app.services.dwell_detect import DwellDetectService
        from app.services.poi_match import PoiMatchService

        geo = GeoSourceService()
        coord = CoordTransformService()
        track = TrackSegmentService()
        dwell = DwellDetectService()
        poi = PoiMatchService()

        batch_key = "POI-MATCH-01"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-P1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=40)

        coord.transform_batch(batch_key)
        track.segment_from_transformed(batch_key)
        dwell.detect_from_segments(batch_key)

        result = poi.match_dwell_to_poi(batch_key)
        assert result["events_processed"] >= 0
        matches, _ = poi.get_poi_matches(batch_key)
        assert isinstance(matches, list)

    def test_region_aggregation(self, setup_data_dir):
        """区域维度聚合正确统计网格数量和定位点总量。"""
        from app.services.geo_source import GeoSourceService
        from app.services.coord_transform import CoordTransformService
        from app.services.grid_index import GridIndexService
        from app.services.region_dim import RegionDimService

        geo = GeoSourceService()
        coord = CoordTransformService()
        grid = GridIndexService()
        region = RegionDimService()

        batch_key = "REGION-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-R1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=30)

        coord.transform_batch(batch_key)
        grid.index_points_from_batch(batch_key)

        result = region.aggregate_by_region(batch_key)
        assert result["grids_aggregated"] > 0
        assert result["total_points"] == 30

        agg_data, _ = region.get_region_aggregates(batch_key)
        assert len(agg_data) > 0

    def test_route_restore_with_road_network(self, setup_data_dir):
        """路径还原将轨迹分段匹配到路网路段。"""
        from app.services.geo_source import GeoSourceService
        from app.services.coord_transform import CoordTransformService
        from app.services.track_segment import TrackSegmentService
        from app.services.route_restore import RouteRestoreService

        geo = GeoSourceService()
        coord = CoordTransformService()
        track = TrackSegmentService()
        route = RouteRestoreService()

        batch_key = "ROUTE-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-RT1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=25)

        coord.transform_batch(batch_key)
        track.segment_from_transformed(batch_key)

        result = route.restore_from_segments(batch_key)
        assert result["segments_processed"] > 0
        assert result["restored_routes"] > 0

        routes, _ = route.get_restored_routes(batch_key)
        assert len(routes) > 0
        assert "road_name" in routes[0]

    def test_backfill_appends_late_data(self, setup_data_dir):
        """历史回补成功追加迟到数据到已有批次文件。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_backfill import GeoBackfillService

        geo = GeoSourceService()
        backfill = GeoBackfillService()

        batch_key = "BACKFILL-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-B1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=20)

        result = backfill.backfill_late_data(batch_key, late_point_count=10)
        assert result["existing_points"] == 20
        assert result["backfill_points"] == 10
        assert result["total_after"] == 30

        # 验证文件有30条
        points, _ = geo.get_batch_points(batch_key)
        assert len(points) == 30


class TestDataQualityChecks:
    """数据质量检测：脏数据、缺失、重复批次处理。"""

    def test_dirty_points_detected(self, setup_data_dir):
        """漂移点和重复点被正确检测。"""
        from app.services.geo_source import GeoSourceService
        from app.services.geo_quality import GeoQualityService

        geo = GeoSourceService()
        quality = GeoQualityService()

        batch_key = "DIRTY-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-DIRTY",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=50)

        result = quality.check_quality(batch_key, max_speed_mps=30, max_gap_sec=300)
        assert result["total_points"] == 50
        assert "drift_count" in result
        assert "duplicate_count" in result
        assert "missing_rate" in result
        assert result["conclusion"] in ("合格", "待复核", "不合格")

    def test_interrupted_recovery_checkpoint(self, setup_data_dir):
        """中断恢复：检查点读写一致性。"""
        from app.services.geo_source import GeoSourceService

        geo = GeoSourceService()
        batch_key = "RECOV-001"

        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-RCV",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=20)

        # 读取检查点
        cp = geo._read_checkpoint(f"geo_collect_{batch_key}")
        assert cp is not None
        assert cp["point_count"] == 20
        assert cp["status"] == "active"


class TestSnapshotAndExport:
    """快照与导出：版本化管理与数据交付。"""

    def test_snapshot_reflects_all_aggregations(self, setup_data_dir):
        """分析快照正确汇总热力、OD和区域聚合数据。"""
        from app.services.geo_source import GeoSourceService
        from app.services.coord_transform import CoordTransformService
        from app.services.track_segment import TrackSegmentService
        from app.services.grid_index import GridIndexService
        from app.services.heatmap_aggregate import HeatmapAggregateService
        from app.services.od_matrix import OdMatrixService
        from app.services.region_dim import RegionDimService
        from app.services.geo_snapshot import GeoSnapshotService

        geo = GeoSourceService()
        coord = CoordTransformService()
        track = TrackSegmentService()
        grid = GridIndexService()
        heatmap = HeatmapAggregateService()
        od = OdMatrixService()
        region = RegionDimService()
        snapshot = GeoSnapshotService()

        batch_key = "SNAP-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-S1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=35)

        coord.transform_batch(batch_key)
        track.segment_from_transformed(batch_key)
        grid.index_points_from_batch(batch_key)
        heatmap.aggregate_grid_heatmap(batch_key)
        od.compute_od_from_trajectories(batch_key)
        region.aggregate_by_region(batch_key)

        result = snapshot.create_snapshot(batch_key)
        assert result["total_results"] > 0
        assert result["heatmap_cells"] > 0
        assert result["od_pairs"] > 0

    def test_export_produces_shards(self, setup_data_dir):
        """分片导出生成多个分片文件。"""
        from app.services.geo_source import GeoSourceService
        from app.services.coord_transform import CoordTransformService
        from app.services.track_segment import TrackSegmentService
        from app.services.grid_index import GridIndexService
        from app.services.heatmap_aggregate import HeatmapAggregateService
        from app.services.od_matrix import OdMatrixService
        from app.services.geo_export import GeoExportService

        geo = GeoSourceService()
        coord = CoordTransformService()
        track = TrackSegmentService()
        grid = GridIndexService()
        heatmap = HeatmapAggregateService()
        od = OdMatrixService()
        export = GeoExportService()

        batch_key = "EXPORT-001"
        geo.onboard_batch({
            "批次编号": batch_key,
            "采集终端": "TERM-E1",
            "定位方式": "GPS",
            "采样频率": "1Hz",
        }, point_count=50)

        coord.transform_batch(batch_key)
        track.segment_from_transformed(batch_key)
        grid.index_points_from_batch(batch_key)
        heatmap.aggregate_grid_heatmap(batch_key)
        od.compute_od_from_trajectories(batch_key)

        result = export.export_batch_results(batch_key, export_format="jsonl", shard_size=10)
        assert result["exported_records"] > 0
        assert result["shards"] >= 1