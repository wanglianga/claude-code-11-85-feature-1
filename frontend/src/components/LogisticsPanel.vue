<template>
  <div>
    <div v-if="loading" class="muted small">加载物流守恒分析…</div>
    <template v-else-if="data">
      <el-alert v-if="data.overall_status === 'no_logistics_record'" type="info" :closable="false"
                :title="data.detail?.scope_note || '窗口内无进销存/供药/签收材料，无法核对实物流。'" />
      <template v-else>
        <!-- 结论与关键指标 -->
        <div class="card">
          <div class="flex-between">
            <h3 style="margin:0">
              🚚 配送解释核对结论
              <el-tag :type="STATUS_TYPE[data.overall_status] || 'info'" size="small" class="ml8">
                {{ STATUS_LABEL[data.overall_status] || data.overall_status }}
              </el-tag>
              <el-tag size="small" effect="plain" class="ml8">
                {{ data.method_profile === 'batch' ? '逐批守恒匹配' : '批次+按日流量混合（含区间）' }}
                · v{{ data.version }}
              </el-tag>
            </h3>
            <el-button size="small" :loading="rerunning" @click="rerun">重算（保留历史版本）</el-button>
          </div>
          <el-alert class="mt12" :closable="false" :type="data.explanation_capable ? 'success' : 'error'"
                    :title="headline" :description="data.detail?.rules?.[3]" />
          <div class="grid stat-grid mt12">
            <div class="stat"><div class="n">{{ data.demand_qty }}</div><div class="l">被解释交易需求量</div></div>
            <div class="stat"><div class="n" style="color:#16a34a">{{ data.covered_qty }}</div><div class="l">已证实可覆盖量</div></div>
            <div class="stat"><div class="n" style="color:#dc2626">{{ data.uncovered_qty }}</div><div class="l">未覆盖/超库存量</div></div>
            <div class="stat"><div class="n" style="color:#b45309">{{ data.shortage_qty }}</div><div class="l">缺货反证累计量</div></div>
            <div class="stat"><div class="n">{{ (data.detail?.conservation?.coverage_interval || []).join(' ~ ') }}</div><div class="l">覆盖区间（保守~乐观）</div></div>
            <div class="stat"><div class="n">{{ data.ledger_gap_count }}</div><div class="l">台账缺口</div></div>
            <div class="stat"><div class="n">{{ data.timeline_violations }}</div><div class="l">时序倒挂</div></div>
            <div class="stat"><div class="n">{{ data.duplicate_photo_groups }}</div><div class="l">重复照片组</div></div>
          </div>
          <el-alert v-if="data.detail?.conservation?.interval_only" class="mt12" type="warning" :closable="false"
                    :title="`台账存在缺口（${(data.detail.conservation.ledger_gap_supplies || []).join('、')}），按日流量平衡只能给区间，不能做单点结论。`" />
        </div>

        <!-- 缺货反证案件包 -->
        <div class="card">
          <h3>📁 缺货反证案件包（仅汇集证据，不自动定性）</h3>
          <el-table :data="findings" size="small" border>
            <el-table-column prop="code" label="编号" width="120" />
            <el-table-column label="级别" width="84" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="row.severity === 'critical' ? 'danger' : 'warning'">
                  {{ row.severity === 'critical' ? '严重' : '提示' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="title" label="反证事项" min-width="220" />
            <el-table-column prop="shortage_qty" label="缺口量" width="84" align="center" />
            <el-table-column label="状态" width="96" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="FINDING_TYPE[row.status]">{{ FINDING_LABEL[row.status] }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="人工裁定" width="200" align="center">
              <template #default="{ row }">
                <template v-if="row.status === 'open'">
                  <el-button link type="danger" size="small" @click="adjudicate(row, 'accepted')">纳入疑点</el-button>
                  <el-button link type="success" size="small" @click="adjudicate(row, 'dismissed')">材料补正排除</el-button>
                </template>
                <span v-else class="small muted">{{ row.decided_by }} · {{ row.decision_note || '—' }}</span>
              </template>
            </el-table-column>
          </el-table>
          <p class="small muted mt8">
            裁定只作用于案件包条目，案件定性仍须在“案件投影”页走人工核查状态；重算时已裁定条目保留，开放条目编号稳定可追溯。
          </p>
        </div>

        <el-tabs class="card">
          <!-- 库存批次账 -->
          <el-tab-pane label="库存批次账" name="batch">
            <el-collapse v-for="b in data.detail?.batch_ledger || []" :key="`${b.pharmacy}-${b.batch}`">
              <el-collapse-item :name="`${b.pharmacy}|${b.batch}`">
                <template #title>
                  <b>{{ b.pharmacy }} · {{ b.ingredient }} · 批次 {{ b.batch }}</b>
                  <el-tag size="small" class="ml8" :type="b.negative_count ? 'danger' : 'info'">
                    期末余额 {{ b.final_balance }}
                  </el-tag>
                  <el-tag v-if="b.negative_count" size="small" type="danger" class="ml8">
                    {{ b.negative_count }} 次负库存
                  </el-tag>
                </template>
                <el-table :data="b.rows" size="small" border :row-class-name="rowClass">
                  <el-table-column prop="date" label="日期" width="110" />
                  <el-table-column prop="change_type" label="变动类型" width="120" />
                  <el-table-column prop="change_qty" label="变动量" width="90" align="center" />
                  <el-table-column prop="ref_no" label="单据号" width="140" />
                  <el-table-column prop="counterpart" label="对手机构" width="100" />
                  <el-table-column prop="running_balance" label="流水余额" width="100" align="center" />
                </el-table>
              </el-collapse-item>
            </el-collapse>
          </el-tab-pane>

          <!-- 守恒匹配 -->
          <el-tab-pane label="守恒匹配" name="conservation">
            <p class="small muted">{{ data.detail?.conservation?.method_note }}</p>
            <el-table :data="data.detail?.conservation?.matches || []" size="small" border>
              <el-table-column prop="supply" label="供药单" width="100" />
              <el-table-column prop="pharmacy" label="药店" width="80" />
              <el-table-column prop="batch" label="批次" width="130" />
              <el-table-column prop="date" label="出库日" width="100" />
              <el-table-column prop="demand_qty" label="需求量" width="80" align="center" />
              <el-table-column prop="matched_qty" label="已匹配" width="80" align="center" />
              <el-table-column prop="unmatched_qty" label="未匹配" width="80" align="center" />
              <el-table-column label="结论" width="110" align="center">
                <template #default="{ row }">
                  <el-tag size="small" :type="MATCH_TYPE[row.status]">{{ MATCH_LABEL[row.status] }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="入库来源（先入先出）" min-width="220">
                <template #default="{ row }">
                  <span v-if="!row.matched_from?.length" class="muted small">—</span>
                  <el-tag v-for="m in row.matched_from" :key="m.ref" size="small" class="ml8"
                          :type="m.inflow_type === 'purchase_in' ? 'success' : 'warning'">
                    {{ m.inflow_date }} {{ m.ref }} ×{{ m.qty }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
            <h4 class="mt16">调拨双边守恒</h4>
            <el-table :data="data.detail?.conservation?.transfer_checks || []" size="small" border>
              <el-table-column prop="transfer" label="调拨单" width="90" />
              <el-table-column prop="from" label="调出" width="80" />
              <el-table-column prop="to" label="调入" width="80" />
              <el-table-column prop="qty" label="数量" width="80" align="center" />
              <el-table-column label="数量守恒" width="90" align="center">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.qty_conserved ? 'success' : 'danger'">
                    {{ row.qty_conserved ? '守恒' : '不一致' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="时序" width="90" align="center">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.timeline_ok ? 'success' : 'danger'">
                    {{ row.timeline_ok ? '正常' : '倒挂' }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
            <h4 class="mt16">处方—供药数量守恒</h4>
            <el-table :data="data.detail?.conservation?.rx_checks || []" size="small" border>
              <el-table-column prop="supply" label="供药单" width="100" />
              <el-table-column prop="rx_qty" label="处方量" width="100" align="center" />
              <el-table-column prop="supply_qty" label="供药量" width="100" align="center" />
              <el-table-column label="守恒" width="90" align="center">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.conserved ? 'success' : 'danger'">
                    {{ row.conserved ? '一致' : '不符' }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <!-- 按日流量 -->
          <el-tab-pane label="按日库存流量" name="daily">
            <el-table :data="data.detail?.conservation?.daily_balance || []" size="small" border
                      :row-class-name="rowClass">
              <el-table-column prop="pharmacy" label="药店" width="80" />
              <el-table-column prop="ingredient" label="成分" width="110" />
              <el-table-column prop="batch" label="批次" width="130" />
              <el-table-column prop="date" label="日期" width="110" />
              <el-table-column prop="inflow" label="入库" width="90" align="center" />
              <el-table-column prop="outflow" label="出库" width="90" align="center" />
              <el-table-column prop="demand" label="案件需求量" width="100" align="center" />
              <el-table-column prop="running_balance" label="批次累计余额" width="120" align="center" />
            </el-table>
            <p class="small muted mt8">负余额日即“无足够实物仍出库”的缺货反证；按批次独立累计，避免其他批次库存掩盖缺口。</p>
          </el-tab-pane>

          <!-- 配送时序 -->
          <el-tab-pane label="配送时序对齐" name="timeline">
            <el-alert v-for="v in data.detail?.timeline?.violations || []" :key="v.type + (v.delivery || '')"
                      class="mb8" type="error" show-icon :closable="false"
                      :title="`时序倒挂：${TIMELINE_LABEL[v.type] || v.type}（${v.delivery || v.detail.transfer}）`"
                      :description="JSON.stringify(v.detail)" />
            <el-table :data="data.detail?.timeline?.events || []" size="small" border>
              <el-table-column prop="delivery" label="签收单" width="100" />
              <el-table-column prop="pharmacy" label="药店" width="80" />
              <el-table-column label="到货→出库→签收" min-width="300">
                <template #default="{ row }">
                  <el-tag v-for="(s, i) in row.chain" :key="i" size="small" class="ml8"
                          :type="row.violations?.length ? 'danger' : 'success'" effect="plain">
                    {{ s.stage }}：{{ s.at || '无记录' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="违规" width="120" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.violations?.length" size="small" type="danger">
                    {{ row.violations.map((x) => TIMELINE_LABEL[x] || x).join('、') }}
                  </el-tag>
                  <el-tag v-else size="small" type="success">时序正常</el-tag>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <!-- 签收证据质量 -->
          <el-tab-pane label="签收证据质量" name="signature">
            <el-alert class="mb8" type="warning" :closable="false"
                      :title="data.detail?.signature_quality?.rule_note" />
            <div class="flex gap8 mb8">
              <el-tag type="success">独立证据 {{ data.detail?.signature_quality?.independent_evidence }}
                / {{ data.detail?.signature_quality?.total_deliveries }}</el-tag>
              <el-tag type="warning">重复照片 {{ data.detail?.signature_quality?.duplicate_count }}</el-tag>
              <el-tag type="danger">签名不匹配 {{ data.detail?.signature_quality?.mismatch_count }}</el-tag>
            </div>
            <el-table :data="data.detail?.signature_quality?.items || []" size="small" border>
              <el-table-column prop="delivery" label="签收单" width="100" />
              <el-table-column prop="receiver" label="签收人" width="90" />
              <el-table-column prop="signed_at" label="签收时间" width="170" />
              <el-table-column prop="photo_hash" label="照片指纹" width="150" />
              <el-table-column label="签名核验" width="90" align="center">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.signature_match ? 'success' : 'danger'">
                    {{ row.signature_match ? '匹配' : '不匹配' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="证据权重" width="90" align="center">
                <template #default="{ row }">
                  <b :style="{ color: row.evidence_weight ? '#16a34a' : '#dc2626' }">
                    {{ row.evidence_weight }}
                  </b>
                </template>
              </el-table-column>
              <el-table-column prop="quality_note" label="质量说明" min-width="200" />
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </template>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import api from '../api.js'
import { ElMessage, ElMessageBox } from 'element-plus'

const props = defineProps({ caseCode: { type: String, required: true } })
const data = ref(null)
const loading = ref(true)
const rerunning = ref(false)

const STATUS_LABEL = {
  explains: '可由配送正常解释',
  partial: '材料不足·仅区间解释',
  cannot_explain: '配送量超库存·不能作正常解释',
  no_logistics_record: '无物流材料'
}
const STATUS_TYPE = { explains: 'success', partial: 'warning', cannot_explain: 'danger' }
const FINDING_LABEL = { open: '待核', accepted: '已纳入疑点', dismissed: '已排除' }
const FINDING_TYPE = { open: 'warning', accepted: 'danger', dismissed: 'success' }
const MATCH_LABEL = { covered: '已守恒覆盖', ledger_gap: '台账缺口·区间', over_stock: '超库存' }
const MATCH_TYPE = { covered: 'success', ledger_gap: 'warning', over_stock: 'danger' }
const TIMELINE_LABEL = {
  sign_before_supply: '签收早于出库',
  outflow_before_inflow: '出库早于到货',
  transfer_order_inverted: '调拨时序倒挂'
}

const findings = computed(() => data.value?.detail?.shortage_findings || [])
const headline = computed(() => {
  if (data.value.overall_status === 'explains') {
    return '进货、库存、出库、处方、配送与签收链守恒一致，集中购药可由正常配送/调拨解释。'
  }
  if (data.value.overall_status === 'partial') {
    return '部分材料缺失或签收证据有缺陷，只能给出覆盖区间，配送解释尚未被充分证实。'
  }
  return '存在配送/供药量超过当时可用库存与负库存日，配送不能作为正常解释；缺货反证已进入案件包，待人工裁定。'
})

function rowClass({ row }) {
  if (row.negative || (row.running_balance !== undefined && row.running_balance < 0)) return 'neg-row'
  return ''
}

async function load() {
  loading.value = true
  try {
    data.value = await api.get(`/api/logistics/cases/${props.caseCode}`)
  } finally {
    loading.value = false
  }
}
async function rerun() {
  rerunning.value = true
  try {
    data.value = await api.post(`/api/logistics/cases/${props.caseCode}/rerun`)
    ElMessage.success(`已生成 v${data.value.version}，历史版本保留`)
  } catch (e) {
    // 拦截器已提示
  } finally {
    rerunning.value = false
  }
}
async function adjudicate(row, decision) {
  const action = decision === 'accepted' ? '纳入疑点' : '材料补正后排除'
  let note = ''
  try {
    const r = await ElMessageBox.prompt(`请填写 ${row.code}「${action}」的核查意见`, '缺货反证人工裁定', {
      confirmButtonText: '提交裁定', cancelButtonText: '取消',
      inputPlaceholder: '将写入审计记录；不改变案件定性'
    })
    note = r.value || ''
  } catch {
    return
  }
  await api.post(`/api/logistics/findings/${row.code}/adjudicate`, { decision, note })
  ElMessage.success('裁定已保存（案件定性不变）')
  load()
}
watch(() => props.caseCode, load)
onMounted(load)
</script>

<style scoped>
.ml8 { margin-left: 8px; }
.mb8 { margin-bottom: 8px; }
:deep(.neg-row) { background: #fef2f2 !important; }
:deep(.el-tabs__content) { padding-top: 8px; }
</style>
