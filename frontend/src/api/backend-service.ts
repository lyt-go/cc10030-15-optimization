import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

// -- 路径还原交接作业 --

export type HandoverSegment = {
  segment_id: number
  origin_node: string
  dest_node: string
  graph_id: string
  status: string
  edge_ids: string[]
  road_names: string[]
  mileage_m: number | null
  plan_mileage_m: number
  delivered_mileage_m: number | null
  delivery_no: string | null
  mileage_delta_m: number
  candidate_routes: {
    rank: number
    graph_id: string
    edge_ids: string[]
    road_names: string[]
    mileage_m: number
    usable: boolean
    closed_edges: string[]
  }[]
  original_trajectory: { point_id: number; lat: number; lon: number; timestamp: string }[]
}

export type HandoverUsage = {
  seq: number
  event: string
  graph_id: string
  graph_version: string
  segment_ids: number[]
  mileage_m: number
  mileage_delta_m: number | null
  closed_edges: string[]
  at: number
}

export type HandoverBatch = {
  batch_key: string
  batch_date: string
  status: string
  graph_id: string
  actual_graph_ids: string[]
  delivery_no: string | null
  segment_count: number
  delivered_segments: number[]
  pending_segments: number[]
  failed_segments: number[]
  delivered_mileage_m: number
  remaining_mileage_m: number
  original_plan: { graph_id: string; mileage_m: number; description: string }
  retry_requests: { request_id: string; requested_at: number; status: string }[]
  graph_usages: HandoverUsage[]
  segments: HandoverSegment[]
}

export type HandoverState = {
  graphs: {
    graph_id: string
    name: string
    version: string
    effective_from: string
    effective_to: string
    retroactive_dates: string[]
    closed_edges: string[]
    closures: { closure_id: string; edge_id: string; reason: string; temporary: boolean; active: boolean }[]
    edge_count: number
  }[]
  batches: HandoverBatch[]
  deliveries: {
    delivery_no: string
    batch_key: string
    batch_date: string
    graph_id: string
    mileage_m: number
    segments: number[]
  }[]
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? '交接操作失败')
  }
  return payload as T
}

export function loadHandoverState(): Promise<HandoverState> {
  return request('/api/route-handover/state').then(async (response) => {
    if (!response.ok) throw new Error('交接状态读取失败')
    return (await response.json()) as HandoverState
  })
}

export function prepareHandover(batchKeys: string[]) {
  return postJson<{
    ok: boolean
    dates: string[]
    completed_batches: string[]
    abnormal_batches: string[]
    pending_batches: string[]
    missing_batches: string[]
    batches: HandoverBatch[]
    message: string
  }>('/api/route-handover/prepare', { batch_keys: batchKeys })
}

export function startHandover(batchKeys: string[]) {
  return postJson<{ ok: boolean; results: Record<string, unknown>[]; message: string }>(
    '/api/route-handover/start',
    { batch_keys: batchKeys },
  )
}

export function resumeHandover(batchKey: string) {
  return postJson<{
    ok: boolean
    batch_key: string
    delivery_no: string | null
    already_delivered?: boolean
    adopted_graph?: { graph_id: string; version: string; registered_at: number }
    discovered_graphs?: { graph_id: string; version: string }[]
    original_plan_mileage_m: number
    final_mileage_m: number
    mileage_delta_m: number
    final_route: {
      graph_id: string
      graph_version: string
      segments: {
        segment_id: number
        edge_ids: string[]
        road_names: string[]
        mileage_m: number | null
        delivered_mileage_m: number | null
        status: string
      }[]
    }
    other_confirmed_batch: {
      batch_key: string
      delivery_no: string
      original_mileage_m: number
      graph_id: string
    } | null
    closures: { edge_id: string; reason: string; active: boolean }[]
    message: string
  }>('/api/route-handover/resume', { batch_key: batchKey })
}

export function confirmSegment(batchKey: string, segmentId: number) {
  return postJson<{
    ok: boolean
    batch_key: string
    delivery_no: string
    already_confirmed?: boolean
    batch_status?: string
    segment: HandoverSegment
    message: string
  }>('/api/route-handover/confirm', { batch_key: batchKey, segment_id: segmentId })
}

export type DeliveryRecord = {
  ok: boolean
  delivery_no: string
  batch_key: string
  batch_date: string
  graph_id: string
  graph_version: string
  actual_mileage_m: number
  mixed_graph_used: boolean
  segments: { segment_id: number; edge_ids: string[]; mileage_m: number; graph_id: string }[]
  message: string
}

export async function verifyDelivery(deliveryNo: string): Promise<DeliveryRecord> {
  const response = await request(`/api/route-handover/deliveries/${encodeURIComponent(deliveryNo)}`)
  if (!response.ok) throw new Error('交付号不存在或无法对账')
  return (await response.json()) as DeliveryRecord
}

export function recalculateBatch(batchKey: string, releaseEdges: string[] = []) {  return postJson<{
    ok: boolean
    batch_key: string
    delivered_mileage_m: number
    added_mileage_m: number
    mileage_delta_m: number
    total_mileage_m: number
    delivered_segments: { segment_id: number; delivery_no: string; mileage_m: number; frozen: boolean }[]
    delta_segments: {
      segment_id: number
      previous_mileage_m: number | null
      recalculated_mileage_m: number | null
      mileage_delta_m: number | null
      new_route: boolean
      route?: { edge_ids: string[]; road_names: string[]; mileage_m: number }
      message?: string
    }[]
    message: string
  }>('/api/route-handover/recalculate', { batch_key: batchKey, release_edges: releaseEdges })
}

export function checkHandover(batchKeys: string[]) {
  return postJson<{
    ok: boolean
    batches: {
      batch_key: string
      status: string
      delivery_no: string | null
      actual_graph_ids: string[]
      continuable_segments: { segment_id: number; graph_id: string; mileage_m: number }[]
      delivered_mileage_m: number
      remaining_mileage_m: number
      mixed_graph_used: boolean
    }[]
    completed_batches: string[]
    abnormal_batches: string[]
    summary: { batch_count: number; completed: number; abnormal: number; remaining_mileage_m: number }
  }>('/api/route-handover/check', { batch_keys: batchKeys })
}

export function resetHandover() {
  return postJson<{ ok: boolean; message: string }>('/api/route-handover/reset', {})
}

export function releaseClosure(graphId: string, edgeId: string) {
  return postJson<{ ok: boolean; graph_id: string; edge_id: string; status: string; message: string }>(
    '/api/route-handover/closures/release',
    { graph_id: graphId, edge_id: edgeId },
  )
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
