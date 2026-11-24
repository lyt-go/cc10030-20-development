/**
 * 归并复审工作台接口：归并组出处回溯、点位质检结论、复审工作区与处置提交。
 * 与后端 app/routers/merge_review.py 的 /api/merge-review 前缀对齐。
 */
import { request } from './client'

export type MemberVerdict = {
  point_id: number
  conclusion: string
  blocked: boolean
  lat?: number
  lon?: number
  timestamp?: number
  accuracy?: number
}

export type MergeGroup = {
  group_key: string
  batch_key: string
  terminal?: string
  representative_point_id: number
  absorbed_point_ids: number[]
  orig_point_ids: number[]
  member_count: number
  group_status: string
  member_verdicts: MemberVerdict[]
  withdrawn_point_id?: number
  replaced_from?: number
}

export type OriginalPoint = {
  group_key: string
  group_status: string
  is_representative: boolean
  point_id: number
  lat?: number
  lon?: number
  timestamp?: number
  terminal?: string
  accuracy?: number
  conclusion: string
  blocked: boolean
  verdict_revision: number
}

export type PointVerdict = {
  batch_key: string
  point_id: number
  conclusion: string
  remark?: string
  revision: number
  updated_at?: number
}

export type ReviewRow = {
  id: number
  复审编号: string
  批次编号: string
  归并组编号: string
  坏点编号: string
  status: string
  复审判定: string
  代表点编号: string
  候选证据: string
  处置动作: string
  申请时间: string
  处置时间: string
  证据快照版本: string
}

export type ReviewDetail = {
  entry: ReviewRow
  workspace: {
    review_no: string
    bad_point_id: number
    decision: string | null
    decision_detail: string | null
    disposition: Record<string, unknown> | null
    timeline: { at: number; event: string; detail: string }[]
  }
  basis_snapshot: {
    snapshot_version: string
    bad_point_basis: { point_id: number; conclusion: string; remark: string }
    point_verdicts: { point_id: number; conclusion: string; revision: number }[]
  }
  attached_corrections: PointVerdict[]
  current_group: MergeGroup
  current_original_points: OriginalPoint[]
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error((await response.json()).detail ?? '请求失败')
  return response.json()
}

async function postJson<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error((await response.json()).detail ?? '操作失败')
  return response.json()
}

export function fetchGroups(batchKey: string): Promise<{ total: number; groups: MergeGroup[] }> {
  return getJson(`/api/merge-review/batches/${encodeURIComponent(batchKey)}/groups`)
}

export function traceOriginalPoints(
  batchKey: string,
  params: { groupKey?: string; pointId?: number; conclusion?: string } = {},
): Promise<{ total: number; items: OriginalPoint[] }> {
  const query = new URLSearchParams()
  if (params.groupKey) query.set('group_key', params.groupKey)
  if (params.pointId !== undefined) query.set('point_id', String(params.pointId))
  if (params.conclusion) query.set('conclusion', params.conclusion)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return getJson(`/api/merge-review/batches/${encodeURIComponent(batchKey)}/original-points${suffix}`)
}

export function fetchPointVerdicts(
  batchKey: string,
  conclusion?: string,
): Promise<{ total: number; items: PointVerdict[] }> {
  const suffix = conclusion ? `?conclusion=${encodeURIComponent(conclusion)}` : ''
  return getJson(`/api/merge-review/batches/${encodeURIComponent(batchKey)}/point-verdicts${suffix}`)
}

export function openReview(
  batchKey: string,
  groupKey: string,
  badPointId: number,
  remark = '',
): Promise<{ ok: boolean; entry: ReviewRow }> {
  return postJson('/api/merge-review/reviews', {
    batch_key: batchKey,
    group_key: groupKey,
    bad_point_id: badPointId,
    remark,
  })
}

export function listReviews(
  batchKey?: string,
  groupKey?: string,
): Promise<{ total: number; items: ReviewRow[] }> {
  const query = new URLSearchParams()
  if (batchKey) query.set('batch_key', batchKey)
  if (groupKey) query.set('group_key', groupKey)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return getJson(`/api/merge-review/reviews${suffix}`)
}

export function fetchReviewDetail(reviewId: number): Promise<ReviewDetail> {
  return getJson(`/api/merge-review/reviews/${reviewId}`)
}

export function attachCorrection(
  reviewId: number,
  pointId: number,
  conclusion: '确认漂移' | '复核通过',
  remark = '',
): Promise<{ ok: boolean; correction: PointVerdict }> {
  return postJson(`/api/merge-review/reviews/${reviewId}/corrections`, {
    point_id: pointId,
    conclusion,
    remark,
  })
}

export function evaluateReview(reviewId: number): Promise<{
  decision: string
  detail: string
  replacement_point_id: number | null
  pending_point_ids: number[]
}> {
  return postJson(`/api/merge-review/reviews/${reviewId}/evaluate`, {})
}

export function submitReview(
  reviewId: number,
  action: '挂起整组' | '换点补采' | '仅撤坏点',
  replacementPointId?: number,
): Promise<{ ok: boolean; entry: ReviewRow; disposition: Record<string, unknown> }> {
  return postJson(`/api/merge-review/reviews/${reviewId}/submit`, {
    action,
    replacement_point_id: replacementPointId ?? null,
  })
}
