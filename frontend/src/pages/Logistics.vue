<template>
  <div class="page">
    <h2 class="page-title">配送解释 · 库存守恒核对</h2>
    <p class="page-sub">
      核对药品<b>实物流、库存变动与签收链</b>，而非重复关系社区裁定：库存批次账、调拨处方守恒器、
      配送路线时序对齐器、签收证据质量器与缺货反证器联动，比较进货、库存、出库、处方、配送单与签收时间，
      计算同批药品能否覆盖被解释交易。逐批守恒证据强但依赖台账质量；按日流量平衡覆盖广却只能给区间。
    </p>
    <div class="disclaimer-banner">
      ⚠️ 配送量超库存时<b>不能作正常解释</b>；重复签收照片<b>不得独立加分</b>；
      缺货反证只进入案件包并保持待核，<b>不自动改变案件定性</b>。
    </div>

    <el-row :gutter="14">
      <el-col :span="6">
        <div class="card">
          <h3>核对结论概览</h3>
          <div v-if="ov" class="small" style="line-height:2">
            <div>已分析案件：<b>{{ ov.analyzed_cases }} / {{ ov.total_cases }}</b></div>
            <el-tag type="success" size="small">可解释 {{ ov.by_status.explains || 0 }}</el-tag>
            <el-tag type="warning" size="small" class="ml8">区间 {{ ov.by_status.partial || 0 }}</el-tag>
            <el-tag type="danger" size="small" class="ml8">不能解释 {{ ov.by_status.cannot_explain || 0 }}</el-tag>
            <div class="mt8">案件包：待核 <b style="color:#b45309">{{ ov.open_findings }}</b> ·
              纳入疑点 <b style="color:#dc2626">{{ ov.accepted_findings }}</b> ·
              已排除 <b style="color:#16a34a">{{ ov.dismissed_findings }}</b></div>
            <div>缺货累计量：<b>{{ ov.total_shortage_qty }}</b> 最小单位</div>
          </div>
          <el-divider style="margin:10px 0" />
          <div v-for="c in ov?.cases || []" :key="c.case_code"
               class="case-line" :class="{ active: code === c.case_code }" @click="code = c.case_code">
            <div class="flex-between">
              <b class="small">{{ c.case_code }}</b>
              <el-tag v-if="c.logistics" size="small" :type="TYPE[c.logistics.overall_status] || 'info'">
                {{ LABEL[c.logistics.overall_status] || c.logistics.overall_status }}
              </el-tag>
              <el-tag v-else size="small" type="info">无材料</el-tag>
            </div>
            <div class="small muted" style="line-height:1.5;margin:3px 0">{{ c.title }}</div>
            <div v-if="c.logistics" class="small">
              覆盖 <b style="color:#16a34a">{{ c.logistics.covered_qty }}</b> /
              需求 {{ c.logistics.demand_qty }}
              <span v-if="c.logistics.shortage_qty"> · 缺货 <b style="color:#dc2626">{{ c.logistics.shortage_qty }}</b></span>
            </div>
          </div>
        </div>
      </el-col>
      <el-col :span="18">
        <LogisticsPanel :key="code" :case-code="code" />
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api.js'
import LogisticsPanel from '../components/LogisticsPanel.vue'

const ov = ref(null)
const code = ref('C-009')
const LABEL = {
  explains: '可解释', partial: '区间', cannot_explain: '不能解释', no_logistics_record: '无材料'
}
const TYPE = { explains: 'success', partial: 'warning', cannot_explain: 'danger' }

onMounted(async () => {
  ov.value = await api.get('/api/logistics/overview')
  // 默认选中当前用户有权限的第一个案件（片区稽核员看不到 HD 区 C-009）
  const first = ov.value.cases?.[0]
  if (first && !ov.value.cases.some((c) => c.case_code === code.value)) {
    code.value = first.case_code
  }
})
</script>

<style scoped>
.case-line { padding: 9px 10px; border: 1px solid var(--rg-line); border-radius: 9px; margin-bottom: 8px; cursor: pointer; }
.case-line:hover { border-color: #93c5fd; }
.case-line.active { border-color: var(--rg-blue); background: #eff6ff; }
.ml8 { margin-left: 8px; }
</style>
