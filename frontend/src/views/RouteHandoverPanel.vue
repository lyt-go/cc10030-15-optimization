<template>
  <section class="handover" data-testid="handover-panel">
    <header class="handover-head">
      <div>
        <h3>多批次交接巡检</h3>
        <p class="page-desc">
          跨日期多选批次，批量启动或接续；逐条确认已交付的分段，按交付号核对实际里程。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="handover-refresh" type="button" @click="reload">
          刷新交接状态
        </button>
        <button class="btn ghost" data-testid="handover-reset" type="button" @click="reset">
          重置场景
        </button>
      </div>
    </header>

    <p v-if="message" class="handover-message" data-testid="handover-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="handover-error">{{ errorMessage }}</p>

    <div class="handover-toolbar">
      <button class="btn primary" data-testid="handover-prepare" type="button" :disabled="!selected.length || busy" @click="prepare">
        读取分段并登记候选路线
      </button>
      <button class="btn" data-testid="handover-start" type="button" :disabled="!selected.length || busy" @click="startSelected">
        批量启动 / 接续
      </button>
      <button class="btn" data-testid="handover-check" type="button" :disabled="!selected.length || busy" @click="runCheck">
        重跑后核对
      </button>
      <span class="handover-selected">已选 {{ selected.length }} 个批次</span>
      <span v-for="date in selectedDates" :key="date" class="handover-date">{{ date }}</span>
    </div>

    <table class="data-table handover-table">
      <thead>
        <tr>
          <th class="col-check"></th>
          <th>批次</th>
          <th>日期</th>
          <th>状态</th>
          <th>实际采用的图</th>
          <th>已交付段 / 里程</th>
          <th>待交付段 / 里程</th>
          <th>交付号</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="batch in state?.batches ?? []" :key="batch.batch_key">
          <tr :data-testid="`handover-batch-${batch.batch_key}`" :data-status="batch.status">
            <td class="col-check">
              <input
                v-model="selected"
                type="checkbox"
                :value="batch.batch_key"
                :data-testid="`handover-select-${batch.batch_key}`"
              />
            </td>
            <td>{{ batch.batch_key }}</td>
            <td>{{ batch.batch_date }}</td>
            <td>{{ batch.status }}</td>
            <td>
              <span v-for="graph in batch.actual_graph_ids" :key="graph" class="graph-chip">{{ graph }}</span>
            </td>
            <td>{{ batch.delivered_segments.join('、') || '—' }} / {{ batch.delivered_mileage_m }}m</td>
            <td>{{ batch.pending_segments.join('、') || '—' }} / {{ batch.remaining_mileage_m }}m</td>
            <td>
              <button
                v-if="batch.delivery_no"
                class="link"
                type="button"
                :data-testid="`handover-verify-${batch.delivery_no}`"
                @click="verify(batch.delivery_no)"
              >
                {{ batch.delivery_no }}
              </button>
              <span v-else>—</span>
            </td>
            <td class="row-actions">
              <button
                v-if="batch.status !== '已交付'"
                class="link"
                type="button"
                :data-testid="`handover-resume-${batch.batch_key}`"
                @click="resume(batch.batch_key)"
              >
                续做
              </button>
              <button
                v-if="batch.status !== '已交付'"
                class="link"
                type="button"
                :data-testid="`handover-recalc-${batch.batch_key}`"
                @click="recalculate(batch.batch_key)"
              >
                补算
              </button>
              <button
                v-for="closure in closedEdgesFor(batch)"
                :key="closure.edge_id"
                class="link"
                type="button"
                :data-testid="`handover-release-${batch.batch_key}-${closure.edge_id}`"
                @click="releaseAndRecalculate(batch.batch_key, batch.graph_id, closure.edge_id)"
              >
                解除封闭 {{ closure.edge_id }}
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`handover-detail-${batch.batch_key}`"
                @click="openDetail(batch)"
              >
                分段
              </button>
            </td>
          </tr>
          <tr v-if="detailBatch?.batch_key === batch.batch_key" class="handover-detail-row">
            <td :colspan="9">
              <div class="handover-detail">
                <div class="handover-segments">
                  <article
                    v-for="segment in batch.segments"
                    :key="segment.segment_id"
                    class="segment-card"
                    :data-testid="`handover-segment-${batch.batch_key}-${segment.segment_id}`"
                  >
                    <header>
                      <strong>段 {{ segment.segment_id }}：{{ segment.origin_node }} → {{ segment.dest_node }}</strong>
                      <span class="segment-status">{{ segment.status }}</span>
                    </header>
                    <p class="segment-line">
                      图 {{ segment.graph_id }} ·
                      路线 {{ segment.edge_ids.join(' → ') || '无可走路线' }} ·
                      里程 {{ segment.mileage_m ?? '—' }}m
                      （原计划 {{ segment.plan_mileage_m }}m）
                    </p>
                    <p class="segment-line">
                      候选 {{ segment.candidate_routes.length }} 条 ·
                      轨迹点 {{ segment.original_trajectory.length }} 个
                    </p>
                    <button
                      v-if="segment.status !== '已交付'"
                      class="link"
                      type="button"
                      :data-testid="`handover-confirm-${batch.batch_key}-${segment.segment_id}`"
                      @click="confirmSegment(batch.batch_key, segment.segment_id)"
                    >
                      确认该段已交付
                    </button>
                    <span v-else class="segment-line">交付号 {{ segment.delivery_no }}</span>
                  </article>
                </div>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <div v-if="delivery" class="handover-result" data-testid="delivery-record">
      <strong>交付号 {{ delivery.delivery_no }} 对账</strong>
      <span>批次 {{ delivery.batch_key }}（{{ delivery.batch_date }}）</span>
      <span>实际里程 {{ delivery.actual_mileage_m }}m</span>
      <span>采用图 {{ delivery.graph_id }} {{ delivery.graph_version }}</span>
      <span>分段 {{ delivery.segments.map((item) => item.segment_id).join('、') }}</span>
      <span v-if="delivery.mixed_graph_used" class="error-text">检测到跨图拼接</span>
    </div>

    <div v-if="resumeResult" class="handover-result" data-testid="resume-result">
      <strong>续做结果：{{ resumeResult.batch_key }}</strong>
      <span>采用图 {{ resumeResult.adopted_graph?.graph_id }}（{{ resumeResult.adopted_graph?.version }}）</span>
      <span>
        最终交付 {{ resumeResult.final_mileage_m }}m，原计划 {{ resumeResult.original_plan_mileage_m }}m，
        差额 {{ resumeResult.mileage_delta_m }}m
      </span>
      <span>交付号 {{ resumeResult.delivery_no ?? '—' }}</span>
      <span v-if="resumeResult.other_confirmed_batch">
        另一已确认批次 {{ resumeResult.other_confirmed_batch.batch_key }}
        原值 {{ resumeResult.other_confirmed_batch.original_mileage_m }}m
      </span>
      <span v-if="resumeResult.closures?.length">
        临时封闭 {{ resumeResult.closures.map((item) => item.edge_id).join('、') }}
      </span>
    </div>

    <div v-if="recalcResult" class="handover-result" data-testid="recalc-result">
      <strong>补算结果：{{ recalcResult.batch_key }}</strong>
      <span>已交付值 {{ recalcResult.delivered_mileage_m }}m（不变）</span>
      <span>新增可走里程 {{ recalcResult.added_mileage_m }}m</span>
      <span>差额 {{ recalcResult.mileage_delta_m }}m</span>
      <span v-for="item in recalcResult.delta_segments" :key="item.segment_id" class="graph-chip">
        段{{ item.segment_id }} {{ item.previous_mileage_m ?? '—' }}→{{ item.recalculated_mileage_m ?? '—' }}m
      </span>
    </div>

    <div v-if="checkResult" class="handover-result" data-testid="check-result">
      <strong>重跑后核对</strong>
      <span>完成 {{ checkResult.summary.completed }} 个 · 异常 {{ checkResult.summary.abnormal }} 个</span>
      <span>剩余可交接里程 {{ checkResult.summary.remaining_mileage_m }}m</span>
      <span v-for="item in checkResult.batches" :key="item.batch_key" class="graph-chip">
        {{ item.batch_key }}：{{ item.actual_graph_ids.join('+') }}，可继续段
        {{ item.continuable_segments.map((seg) => seg.segment_id).join('、') || '无' }}
      </span>
    </div>

    <div v-if="prepareResult" class="handover-result" data-testid="prepare-result">
      <strong>读取结果</strong>
      <span>日期 {{ prepareResult.dates.join('、') }}</span>
      <span>完成 {{ prepareResult.completed_batches.length }} 个</span>
      <span>异常 {{ prepareResult.abnormal_batches.length }} 个</span>
      <span>待处理 {{ prepareResult.pending_batches.length }} 个</span>
      <span v-if="prepareResult.missing_batches.length" class="error-text">
        不存在 {{ prepareResult.missing_batches.join('、') }}
      </span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  checkHandover,
  confirmSegment as confirmSegmentApi,
  loadHandoverState,
  prepareHandover,
  recalculateBatch,
  releaseClosure,
  resetHandover,
  resumeHandover,
  startHandover,
  verifyDelivery,
} from '@/api/backend-service'
import type {
  DeliveryRecord,
  HandoverBatch,
  HandoverState,
} from '@/api/backend-service'

const state = ref<HandoverState | null>(null)
const selected = ref<string[]>([])
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')
const detailBatch = ref<HandoverBatch | null>(null)
const delivery = ref<DeliveryRecord | null>(null)
const prepareResult = ref<Awaited<ReturnType<typeof prepareHandover>> | null>(null)
const resumeResult = ref<Awaited<ReturnType<typeof resumeHandover>> | null>(null)
const recalcResult = ref<Awaited<ReturnType<typeof recalculateBatch>> | null>(null)
const checkResult = ref<Awaited<ReturnType<typeof checkHandover>> | null>(null)

const selectedDates = computed(() => {
  const batches = state.value?.batches ?? []
  const dates = new Set(
    batches.filter((item) => selected.value.includes(item.batch_key)).map((item) => item.batch_date),
  )
  return [...dates].sort()
})

function resetResults() {
  message.value = ''
  errorMessage.value = ''
  delivery.value = null
}

async function run<T>(task: () => Promise<T>, onDone: (payload: T) => void) {
  busy.value = true
  resetResults()
  try {
    const payload = await task()
    onDone(payload)
    await reloadState()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接操作失败'
  } finally {
    busy.value = false
  }
}

async function reloadState() {
  state.value = await loadHandoverState()
  if (detailBatch.value) {
    detailBatch.value =
      state.value.batches.find((item) => item.batch_key === detailBatch.value?.batch_key) ?? null
  }
}

async function reload() {
  try {
    await reloadState()
    message.value = '交接状态已刷新'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接状态读取失败'
  }
}

function openDetail(batch: HandoverBatch) {
  detailBatch.value = detailBatch.value?.batch_key === batch.batch_key ? null : batch
}

function prepare() {
  void run(() => prepareHandover(selected.value), (payload) => {
    prepareResult.value = payload
    message.value = payload.message
  })
}

function startSelected() {
  void run(() => startHandover(selected.value), (payload) => {
    message.value = payload.message
    const resumed = payload.results.find((item) => 'final_mileage_m' in item)
    if (resumed) resumeResult.value = resumed as Awaited<ReturnType<typeof resumeHandover>>
  })
}

function resume(batchKey: string) {
  void run(() => resumeHandover(batchKey), (payload) => {
    resumeResult.value = payload
    message.value = payload.message
  })
}

function confirmSegment(batchKey: string, segmentId: number) {
  void run(() => confirmSegmentApi(batchKey, segmentId), (payload) => {
    message.value = payload.message
  })
}

function recalculate(batchKey: string) {
  void run(() => recalculateBatch(batchKey), (payload) => {
    recalcResult.value = payload
    message.value = payload.message
  })
}

function closedEdgesFor(batch: HandoverBatch) {
  const graph = state.value?.graphs.find((item) => item.graph_id === batch.graph_id)
  // 只提供临时封闭记录的解封入口；图内长期封闭的路段不在此列。
  return (graph?.closures ?? []).filter((item) => item.active)
}

function releaseAndRecalculate(batchKey: string, graphId: string, edgeId: string) {
  void run(async () => {
    await releaseClosure(graphId, edgeId)
    return recalculateBatch(batchKey)
  }, (payload) => {
    recalcResult.value = payload
    message.value = payload.message
  })
}

function verify(deliveryNo: string) {
  void run(() => verifyDelivery(deliveryNo), (payload) => {
    delivery.value = payload
    message.value = payload.message
  })
}

function runCheck() {
  void run(() => checkHandover(selected.value), (payload) => {
    checkResult.value = payload
    message.value = `核对完成：完成 ${payload.summary.completed} 个，异常 ${payload.summary.abnormal} 个`
  })
}

function reset() {
  void run(() => resetHandover(), async (payload) => {
    selected.value = []
    detailBatch.value = null
    prepareResult.value = null
    resumeResult.value = null
    recalcResult.value = null
    checkResult.value = null
    message.value = payload.message
  })
}

onMounted(reload)
</script>
