<template>
  <section class="page" data-module="ods_layer">
    <header class="page-head">
      <div>
        <h2>贴源层管理管理</h2>
        <p class="page-desc">维护贴源层ODS表的输入、执行约束与结果记录，为贴源层管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记贴源表</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出贴源层管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click="showDetail(row)">
              详情
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无贴源层管理数据，可先登记贴源表</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记贴源表</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="selectedRow" class="detail-panel" data-testid="detail-panel">
      <h3>关联信息：{{ selectedRow['表名称'] }}
        <button class="btn" type="button" data-testid="detail-close" @click="selectedRow = null">关闭</button>
      </h3>
      <div class="detail-section">
        <h4>来源入库批次</h4>
        <table class="data-table small">
          <thead><tr><th>批次编号</th><th>来源系统</th><th>文件格式</th><th>原始行数</th><th>入库时间</th></tr></thead>
          <tbody>
            <tr v-for="b in relatedBatches" :key="String(b['批次编号'])"><td>{{ b['批次编号'] }}</td><td>{{ b['来源系统'] }}</td><td>{{ b['文件格式'] }}</td><td>{{ b['原始行数'] }}</td><td>{{ b['入库时间'] }}</td></tr>
            <tr v-if="!relatedBatches.length"><td colspan="5" class="empty-state">无关联批次</td></tr>
          </tbody>
        </table>
      </div>
      <div class="detail-section">
        <h4>模拟表数据预览（前5行）</h4>
        <table class="data-table small">
          <thead><tr><th v-for="col in tableDataColumns" :key="col">{{ col }}</th></tr></thead>
          <tbody>
            <tr v-for="(r, idx) in tableDataPreview" :key="idx"><td v-for="col in tableDataColumns" :key="col">{{ r[col] ?? '—' }}</td></tr>
            <tr v-if="!tableDataPreview.length"><td :colspan="tableDataColumns.length || 1" class="empty-state">暂无数据</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/ods_layer'
const columns = ["表编号", "来源系统", "表名称", "分区字段", "存储格式", "保留天数", "压缩算法", "创建批次"]
const actions = ["提交校验", "确认上线", "标记下线"]
const statuses = ["草稿", "待校验", "已上线", "已下线"]
const metricNames = ["贴源表总数", "待校验表", "已上线表"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const selectedRow = ref<Row | null>(null)
const relatedBatches = ref<Row[]>([])
const tableDataPreview = ref<Row[]>([])
const tableDataColumns = ref<string[]>([])
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'ods_layer.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('贴源层管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '贴源层管理操作失败'
  }
}

async function showDetail(row: Row) {
  errorMessage.value = ''
  selectedRow.value = row
  try {
    const [batchesRes, dataRes] = await Promise.all([
      request(`${ENDPOINT}/${row.id}/batches`),
      request(`${ENDPOINT}/${row.id}/data`),
    ])
    relatedBatches.value = (await batchesRes.json()).items ?? []
    tableDataPreview.value = ((await dataRes.json()).items ?? []).slice(0, 5)
    tableDataColumns.value = tableDataPreview.value.length
      ? Object.keys(tableDataPreview.value[0])
      : []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关联信息读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('贴源表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '贴源层管理列表读取失败'
  }
}

onMounted(reload)
</script>
