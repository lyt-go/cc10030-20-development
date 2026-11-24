<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">地理空间轨迹与网格聚合分析平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向定位点采集、坐标标准化、网格切分、轨迹分段驻留识别、空间关联聚合与区域维度产出的地理空间数据分析平台。</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [{ label: "运营概览", path: "/" }, { label: "定位源接入", path: "/geo_source" }, { label: "坐标标准化", path: "/coord_transform" }, { label: "轨迹切分", path: "/track_segment" }, { label: "驻留识别", path: "/dwell_detect" }, { label: "网格索引", path: "/grid_index" }, { label: "围栏判定", path: "/geofence_rule" }, { label: "空间关联", path: "/spatial_join" }, { label: "兴趣点匹配", path: "/poi_match" }, { label: "区域维度", path: "/region_dim" }, { label: "热力聚合", path: "/heatmap_aggregate" }, { label: "起止流量", path: "/od_matrix" }, { label: "路径还原", path: "/route_restore" }, { label: "密度估算", path: "/density_estimate" }, { label: "轨迹质量", path: "/geo_quality" }, { label: "点位归并", path: "/dedup_merge" }, { label: "归并复审工作台", path: "/merge-review" }, { label: "分析快照", path: "/geo_snapshot" }, { label: "历史回补", path: "/geo_backfill" }, { label: "结果导出", path: "/geo_export" }]
</script>
