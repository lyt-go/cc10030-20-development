<template>
  <section class="page" data-module="merge_review">
    <header class="page-head">
      <div>
        <h2>归并复审工作台</h2>
        <p class="page-desc">
          从归并组明细与质检结论回溯原始上报点，对漂移坏点发起复审：挂起整组补点，或换用复核通过的合法点继续补采。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">归并组</span>
        <strong class="stat-value">{{ groups.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待补点组</span>
        <strong class="stat-value">{{ pendingGroups }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">复审中</span>
        <strong class="stat-value">{{ activeReviews }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="loadGroups">
      <label class="filter-item">
        <span>批次编号</span>
        <input v-model="batchKey" data-testid="batch-input" placeholder="如 MR-BATCH-1" required />
      </label>
      <button class="btn primary" data-testid="load-batch" type="submit">载入归并组</button>
      <button class="btn ghost" type="button" data-testid="load-reviews" @click="loadReviews">
        回查复审记录
      </button>
    </form>

    <!-- 归并组明细 + 原始点出处 -->
    <h3 class="section-title">归并组明细与出处回溯</h3>
    <table class="data-table" data-testid="group-table">
      <thead>
        <tr>
          <th>归并组</th>
          <th>代表点</th>
          <th>被吸收点</th>
          <th>组状态</th>
          <th>成员质检结论</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="group in groups" :key="group.group_key" :data-testid="`group-${group.group_key}`">
          <td>{{ group.group_key }}</td>
          <td>
            <strong :class="{ 'error-text': isBlocked(group, group.representative_point_id) }">
              {{ group.representative_point_id }}
            </strong>
          </td>
          <td>{{ group.absorbed_point_ids.join('、') || '—' }}</td>
          <td>{{ group.group_status }}</td>
          <td>
            <span
              v-for="member in group.member_verdicts"
              :key="member.point_id"
              class="verdict-tag"
              :data-testid="`verdict-${member.point_id}`"
              :class="verdictClass(member.conclusion)"
            >
              {{ member.point_id }}:{{ member.conclusion }}
            </span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" data-testid="trace-group" @click="traceGroup(group)">
              原始出处
            </button>
            <button class="link" type="button" data-testid="open-review" @click="startReview(group)">
              发起复审
            </button>
          </td>
        </tr>
        <tr v-if="!groups.length">
          <td colspan="6" class="empty-state">先载入一个已归并批次</td>
        </tr>
      </tbody>
    </table>

    <!-- 原始上报点检索结果 -->
    <div v-if="tracedPoints.length" class="trace-panel" data-testid="trace-panel">
      <h3 class="section-title">原始上报点（{{ traceTitle }}）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>原始点号</th>
            <th>归属归并组</th>
            <th>是否代表点</th>
            <th>坐标</th>
            <th>质检结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="point in tracedPoints" :key="`${point.group_key}-${point.point_id}`">
            <td>{{ point.point_id }}</td>
            <td>{{ point.group_key }}</td>
            <td>{{ point.is_representative ? '代表点' : '被吸收点' }}</td>
            <td>{{ point.lat }}, {{ point.lon }}</td>
            <td :class="verdictClass(point.conclusion)">{{ point.conclusion }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 复审工作区 -->
    <div v-if="activeDetail" class="review-panel" data-testid="review-panel">
      <h3 class="section-title">
        复审 {{ activeDetail.entry.复审编号 }}
        <span class="verdict-tag" :class="statusClass(activeDetail.entry.status)">
          {{ activeDetail.entry.status }}
        </span>
      </h3>

      <div class="basis-box">
        <h4>发起时冻结的质检依据（快照 {{ activeDetail.basis_snapshot.snapshot_version }}）</h4>
        <ul>
          <li v-for="v in activeDetail.basis_snapshot.point_verdicts" :key="v.point_id">
            点 {{ v.point_id }}：{{ v.conclusion }}（修订 {{ v.revision }}）
          </li>
        </ul>
        <p class="page-desc">
          坏点 {{ activeDetail.workspace.bad_point_id }} 当次依据：
          {{ activeDetail.basis_snapshot.bad_point_basis.conclusion }}
          —— {{ activeDetail.basis_snapshot.bad_point_basis.remark }}
        </p>
      </div>

      <div v-if="activeDetail.attached_corrections.length" class="basis-box">
        <h4>已接入的质检更正</h4>
        <ul>
          <li v-for="c in activeDetail.attached_corrections" :key="`${c.point_id}-${c.revision}`">
            点 {{ c.point_id }} → {{ c.conclusion }}（修订 {{ c.revision }}）：{{ c.remark }}
          </li>
        </ul>
      </div>

      <div v-if="activeDetail.workspace.decision" class="basis-box" data-testid="decision-box">
        <h4>复审判定：{{ activeDetail.workspace.decision }}</h4>
        <p>{{ activeDetail.workspace.decision_detail }}</p>
      </div>

      <div v-if="isReviewOpen" class="action-row">
        <form class="inline-form" @submit.prevent="onAttachCorrection">
          <select v-model="correctionPointId" data-testid="correction-point">
            <option v-for="pid in attachablePointIds" :key="pid" :value="pid">点 {{ pid }}</option>
          </select>
          <select v-model="correctionConclusion" data-testid="correction-conclusion">
            <option value="复核通过">复核通过</option>
            <option value="确认漂移">确认漂移</option>
          </select>
          <button class="btn" type="submit" data-testid="attach-correction">接入质检更正</button>
        </form>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="evaluate" @click="onEvaluate">
            核对挂起/换点
          </button>
          <button class="btn ghost" type="button" data-testid="submit-hold" @click="onSubmit('挂起整组')">
            挂起整组（仅撤坏点）
          </button>
          <button class="btn primary" type="button" data-testid="submit-replace" @click="onSubmit('换点补采')">
            换用合法点继续补采
          </button>
        </div>
      </div>

      <h4>处置与时间线</h4>
      <ol class="timeline" data-testid="timeline">
        <li v-for="(item, idx) in activeDetail.workspace.timeline" :key="idx">
          {{ item.event }} —— {{ item.detail }}
        </li>
      </ol>
    </div>

    <!-- 复审记录回查 -->
    <div v-if="reviews.length" class="trace-panel">
      <h3 class="section-title">复审记录（含已处置旧复审）</h3>
      <table class="data-table" data-testid="review-table">
        <thead>
          <tr>
            <th>复审编号</th>
            <th>归并组</th>
            <th>坏点</th>
            <th>状态</th>
            <th>判定</th>
            <th>处置</th>
            <th>证据快照</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in reviews" :key="row.id" :data-testid="`review-${row.id}`">
            <td>{{ row.复审编号 }}</td>
            <td>{{ row.归并组编号 }}</td>
            <td>{{ row.坏点编号 }}</td>
            <td>{{ row.status }}</td>
            <td>{{ row.复审判定 }}</td>
            <td>{{ row.处置动作 }}</td>
            <td>{{ row.证据快照版本 }}</td>
            <td class="row-actions">
              <button class="link" type="button" data-testid="review-detail" @click="loadDetail(row.id)">
                查回证据
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  attachCorrection,
  evaluateReview,
  fetchGroups,
  fetchReviewDetail,
  listReviews,
  openReview,
  submitReview,
  traceOriginalPoints,
  type MergeGroup,
  type OriginalPoint,
  type ReviewDetail,
  type ReviewRow,
} from '@/api/merge-review'

const batchKey = ref('')
const groups = ref<MergeGroup[]>([])
const tracedPoints = ref<OriginalPoint[]>([])
const traceTitle = ref('')
const reviews = ref<ReviewRow[]>([])
const activeDetail = ref<ReviewDetail | null>(null)
const errorMessage = ref('')
const correctionPointId = ref<number | null>(null)
const correctionConclusion = ref<'复核通过' | '确认漂移'>('复核通过')

const pendingGroups = computed(
  () => groups.value.filter((group) => group.group_status === '待补点').length,
)
const activeReviews = computed(
  () => reviews.value.filter((row) => row.status === '复审中').length,
)
const isReviewOpen = computed(() => activeDetail.value?.entry.status === '复审中')
const attachablePointIds = computed(() => {
  if (!activeDetail.value) return []
  const bad = activeDetail.value.workspace.bad_point_id
  return activeDetail.value.current_group.orig_point_ids.filter((pid) => pid !== bad)
})

function verdictClass(conclusion: string): string {
  if (conclusion === '确认漂移') return 'verdict-drift'
  if (conclusion === '待复核') return 'verdict-pending'
  if (conclusion === '复核通过') return 'verdict-pass'
  return 'verdict-unknown'
}

function statusClass(status: string): string {
  if (status.includes('挂起') || status.includes('撤')) return 'verdict-drift'
  if (status.includes('换点')) return 'verdict-pass'
  return 'verdict-pending'
}

function isBlocked(group: MergeGroup, pointId: number): boolean {
  return group.member_verdicts.find((m) => m.point_id === pointId)?.blocked ?? false
}

function fail(error: unknown) {
  errorMessage.value = error instanceof Error ? error.message : '操作失败'
}

async function loadGroups() {
  errorMessage.value = ''
  try {
    const payload = await fetchGroups(batchKey.value.trim())
    groups.value = payload.groups
    await loadReviews()
  } catch (error) {
    fail(error)
  }
}

async function loadReviews() {
  if (!batchKey.value.trim()) return
  errorMessage.value = ''
  try {
    const payload = await listReviews(batchKey.value.trim())
    reviews.value = payload.items
  } catch (error) {
    fail(error)
  }
}

async function traceGroup(group: MergeGroup) {
  errorMessage.value = ''
  try {
    const payload = await traceOriginalPoints(batchKey.value.trim(), { groupKey: group.group_key })
    tracedPoints.value = payload.items
    traceTitle.value = group.group_key
  } catch (error) {
    fail(error)
  }
}

async function startReview(group: MergeGroup) {
  errorMessage.value = ''
  try {
    const bad = group.representative_point_id
    const result = await openReview(batchKey.value.trim(), group.group_key, bad)
    await loadDetail(result.entry.id)
    await loadGroups()
  } catch (error) {
    fail(error)
  }
}

async function loadDetail(reviewId: number) {
  errorMessage.value = ''
  try {
    activeDetail.value = await fetchReviewDetail(reviewId)
    correctionPointId.value = attachablePointIds.value[0] ?? null
  } catch (error) {
    fail(error)
  }
}

async function onAttachCorrection() {
  if (!activeDetail.value || correctionPointId.value === null) return
  errorMessage.value = ''
  try {
    await attachCorrection(
      activeDetail.value.entry.id,
      correctionPointId.value,
      correctionConclusion.value,
    )
    await loadDetail(activeDetail.value.entry.id)
    await loadGroups()
  } catch (error) {
    fail(error)
  }
}

async function onEvaluate() {
  if (!activeDetail.value) return
  errorMessage.value = ''
  try {
    await evaluateReview(activeDetail.value.entry.id)
    await loadDetail(activeDetail.value.entry.id)
  } catch (error) {
    fail(error)
  }
}

async function onSubmit(action: '挂起整组' | '换点补采') {
  if (!activeDetail.value) return
  errorMessage.value = ''
  try {
    await submitReview(activeDetail.value.entry.id, action)
    await loadDetail(activeDetail.value.entry.id)
    await loadGroups()
    await loadReviews()
  } catch (error) {
    fail(error)
  }
}
</script>

<style scoped>
.section-title {
  margin: 20px 0 8px;
  font-size: 15px;
}
.verdict-tag {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #eef2f7;
}
.verdict-drift {
  color: #b42318;
  background: #fee4e2;
}
.verdict-pending {
  color: #b54708;
  background: #fef0c7;
}
.verdict-pass {
  color: #067647;
  background: #d1fadf;
}
.verdict-unknown {
  color: #475467;
  background: #f2f4f7;
}
.trace-panel,
.review-panel,
.basis-box {
  border: 1px solid #e4e7ec;
  border-radius: 8px;
  padding: 12px 16px;
  margin: 12px 0;
  background: #fcfcfd;
}
.inline-form {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.action-row {
  margin: 12px 0;
}
.timeline {
  padding-left: 20px;
  line-height: 1.9;
  color: #344054;
}
</style>
