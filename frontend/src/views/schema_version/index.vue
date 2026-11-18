<template>
  <section class="page" data-module="schema_version">
    <header class="page-head">
      <div>
        <h2>结构定义版本表</h2>
        <p class="page-desc">
          旧类型映射与新类型版本同批迁移，兼容 STRING 等旧类型别名；先在
          <RouterLink class="link" to="/schema_registry">结构注册入口</RouterLink>
          核对结构定义，再回到本页核对版本结果。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="run-migrate" type="button" :disabled="busy" @click="runMigrate">
          {{ busy ? '迁移中…' : '发起迁移（超时自动重试）' }}
        </button>
        <button class="btn" data-testid="run-switch" type="button" :disabled="busy" @click="runSwitch">统一切换批次</button>
        <button class="btn ghost" data-testid="run-rollback" type="button" :disabled="busy || !activeBatch" @click="rollbackAll">
          整批回滚
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出版本表</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">版本片段总数</span>
        <strong class="stat-value">{{ total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已迁移片段</span>
        <strong class="stat-value">{{ migratedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">冲突片段</span>
        <strong class="stat-value">{{ conflictCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">迁移失败片段</span>
        <strong class="stat-value">{{ failedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前切换批次</span>
        <strong class="stat-value" style="font-size: 14px">{{ activeBatch ?? '未切换' }}</strong>
      </article>
    </div>

    <h3>逐表迁移进度</h3>
    <table class="data-table" data-testid="progress-table">
      <thead>
        <tr>
          <th v-for="col in progressColumns" :key="col">{{ col }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in progress" :key="item.表名称" :data-testid="`progress-${item.表名称}`">
          <td v-for="col in progressColumns" :key="col">{{ item[col] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="filterTable(item.表名称)">只看该表</button>
            <button
              v-if="activeBatch"
              class="link"
              type="button"
              :data-testid="`rollback-${item.表名称}`"
              @click="rollbackTable(item.表名称)"
            >
              单表回滚
            </button>
          </td>
        </tr>
        <tr v-if="!progress.length">
          <td :colspan="progressColumns.length + 1" class="empty-state">尚未迁移，点击「发起迁移」生成结构定义版本表</td>
        </tr>
      </tbody>
    </table>

    <h3>批次台账（迁移批次 / 切换批次 / 回滚点）</h3>
    <table class="data-table" data-testid="batch-table">
      <thead>
        <tr>
          <th v-for="col in batchColumns" :key="col">{{ col }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="String(batch.批次编号)" :data-testid="`batch-${batch.批次编号}`">
          <td v-for="col in batchColumns" :key="col">{{ formatBatch(col, batch[col]) }}</td>
        </tr>
        <tr v-if="!batches.length">
          <td :colspan="batchColumns.length" class="empty-state">暂无批次记录</td>
        </tr>
      </tbody>
    </table>

    <div class="filter-bar">
      <div class="tabs" role="tablist">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="btn"
          :class="{ primary: activeTab === tab.key }"
          type="button"
          :data-testid="`tab-${tab.key}`"
          @click="switchTab(tab.key)"
        >
          {{ tab.label }}
        </button>
      </div>
      <label class="filter-item" v-if="activeTab === 'versions'">
        <span>表名称</span>
        <input v-model="filters.table_name" data-testid="filter-table" placeholder="按表名称检索" />
      </label>
      <label class="filter-item" v-if="activeTab === 'versions'">
        <span>冲突</span>
        <select v-model="filters.conflict" data-testid="filter-conflict">
          <option value="">全部</option>
          <option value="有冲突">有冲突</option>
          <option value="别名兼容">别名兼容</option>
          <option value="待裁决">待裁决</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="button" @click="reload">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </div>

    <!-- 片段明细：冲突片段全部保留 -->
    <table v-if="activeTab === 'versions'" class="data-table" data-testid="version-table">
      <thead>
        <tr>
          <th v-for="col in versionColumns" :key="col">{{ col }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-conflict="String(row.冲突标识)">
          <td v-for="col in versionColumns" :key="col">{{ row[col] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-if="row.冲突标识 !== '无'"
              class="link"
              type="button"
              :data-testid="`resolve-${row.id}`"
              @click="openResolve(row)"
            >
              裁决
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="versionColumns.length + 1" class="empty-state">暂无版本记录</td>
        </tr>
      </tbody>
    </table>

    <!-- 合并视图：一份完整结构定义版本表 -->
    <table v-else-if="activeTab === 'consolidated'" class="data-table" data-testid="consolidated-table">
      <thead>
        <tr>
          <th v-for="col in consolidatedColumns" :key="col">{{ col }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in consolidated" :key="`${row.表名称}-${row.字段名称}`">
          <td v-for="col in consolidatedColumns" :key="col">{{ row[col] ?? '—' }}</td>
        </tr>
        <tr v-if="!consolidated.length">
          <td :colspan="consolidatedColumns.length" class="empty-state">暂无合并视图，请先迁移</td>
        </tr>
      </tbody>
    </table>

    <!-- 冲突裁决清单 -->
    <table v-else class="data-table" data-testid="conflict-table">
      <thead>
        <tr>
          <th v-for="col in conflictColumns" :key="col">{{ col }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in conflicts" :key="String(row.id)">
          <td v-for="col in conflictColumns" :key="col">{{ row[col] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`resolve-c-${row.id}`" @click="openResolve(row)">裁决</button>
          </td>
        </tr>
        <tr v-if="!conflicts.length">
          <td :colspan="conflictColumns.length + 1" class="empty-state">当前没有类型冲突</td>
        </tr>
      </tbody>
    </table>

    <div v-if="resolveTarget" class="modal-backdrop" data-testid="resolve-modal">
      <form class="modal" @submit.prevent="submitResolve">
        <h3>冲突裁决：{{ resolveTarget.表名称 }} / {{ resolveTarget.字段名称 }}</h3>
        <p class="page-desc">{{ resolveTarget.冲突说明 }}</p>
        <label class="create-field">
          <span>最终规范类型</span>
          <input v-model="resolveCanonical" data-testid="resolve-canonical" required />
        </label>
        <label class="create-field">
          <span>裁决说明</span>
          <input v-model="resolveNote" data-testid="resolve-note" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="resolveTarget = null">取消</button>
          <button class="btn primary" data-testid="resolve-submit" type="submit">确认裁决</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条版本片段，合并视图 {{ consolidated.length }} 个表字段</span>
      <span v-if="message" :class="messageOk ? '' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface ProgressItem extends Row {
  表名称: string
  总片段: number
  已迁移: number
  迁移失败: number
  待迁移: number
  已回滚: number
  冲突片段: number
}

const ENDPOINT = '/api/schema_version'
const tabs = [
  { key: 'versions', label: '版本表明细（含冲突片段）' },
  { key: 'consolidated', label: '合并视图（一份完整表）' },
  { key: 'conflicts', label: '冲突裁决' },
] as const

const versionColumns = [
  '版本记录编号', '表名称', '字段名称', '旧类型写法', '规范类型', '类型版本', '旧类型别名',
  '来源模块', '来源编号', '定义版本', '迁移批次', '迁移状态', '冲突标识', '裁决结果',
  '切换批次', '切换状态',
]
const consolidatedColumns = [
  '表名称', '字段名称', '旧类型写法集合', '规范类型集合', '来源片段数', '冲突标识',
  '裁决结果', '迁移状态', '切换状态', '切换批次', '类型版本',
]
const conflictColumns = ['版本记录编号', '表名称', '字段名称', '旧类型写法', '规范类型', '冲突标识', '冲突说明', '裁决优先来源']
const progressColumns = ['表名称', '总片段', '已迁移', '待迁移', '迁移失败', '已回滚', '冲突片段']
const batchColumns = ['批次编号', '批次类型', '状态', '覆盖表', '回滚点', '创建时间']

const rows = ref<Row[]>([])
const consolidated = ref<Row[]>([])
const conflicts = ref<Row[]>([])
const progress = ref<ProgressItem[]>([])
const batches = ref<Row[]>([])
const total = ref(0)
const activeTab = ref<(typeof tabs)[number]['key']>('versions')
const filters = ref<Record<string, string>>({ table_name: '', conflict: '' })
const busy = ref(false)
const message = ref('')
const messageOk = ref(true)
const resolveTarget = ref<Row | null>(null)
const resolveCanonical = ref('')
const resolveNote = ref('')

const conflictCount = computed(() => rows.value.filter((r) => r.冲突标识 !== '无').length)
const migratedCount = computed(() => rows.value.filter((r) => r.迁移状态 === '已迁移').length)
const failedCount = computed(() => rows.value.filter((r) => r.迁移状态 === '迁移失败').length)
const activeBatch = computed(() => {
  const batch = [...batches.value].reverse().find((b) => b.批次类型 === '切换批次' && b.状态 === '已生效')
  return batch ? String(batch.批次编号) : ''
})

function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function formatBatch(col: string, value: unknown) {
  if (col === '覆盖表' && typeof value === 'string') return value
  return (value as string | number | null) ?? '—'
}

async function reload() {
  const params = new URLSearchParams()
  if (filters.value.table_name) params.set('table_name', filters.value.table_name)
  if (filters.value.conflict) params.set('conflict', filters.value.conflict)
  const [listRes, consolidatedRes, conflictRes, progressRes, batchRes] = await Promise.all([
    request(`${ENDPOINT}?${params.toString()}`),
    request(`${ENDPOINT}/consolidated`),
    request(`${ENDPOINT}/conflicts`),
    request(`${ENDPOINT}/progress`),
    request(`${ENDPOINT}/batches`),
  ])
  const listPayload = await listRes.json()
  rows.value = listPayload.items ?? []
  total.value = listPayload.total ?? 0
  consolidated.value = (await consolidatedRes.json()).items ?? []
  conflicts.value = (await conflictRes.json()).items ?? []
  progress.value = (await progressRes.json()).items ?? []
  batches.value = (await batchRes.json()).items ?? []
}

function resetFilters() {
  filters.value = { table_name: '', conflict: '' }
  void reload()
}

function switchTab(key: (typeof tabs)[number]['key']) {
  activeTab.value = key
}

function filterTable(tableName: string) {
  activeTab.value = 'versions'
  filters.value.table_name = tableName
  void reload()
}

async function postAction(path: string, okText: string) {
  busy.value = true
  try {
    const response = await request(path, { method: 'POST' })
    const payload = await response.json()
    notify(payload.message || okText, Boolean(payload.ok))
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '操作失败', false)
  } finally {
    busy.value = false
  }
}

function runMigrate() {
  return postAction(`${ENDPOINT}/migrate`, '迁移完成')
}

function runSwitch() {
  return postAction(`${ENDPOINT}/switch`, '切换完成')
}

function rollbackAll() {
  if (!activeBatch.value) return
  return postAction(`${ENDPOINT}/rollback?switch_batch=${encodeURIComponent(activeBatch.value)}`, '整批已回滚')
}

function rollbackTable(tableName: string) {
  if (!activeBatch.value) return
  return postAction(
    `${ENDPOINT}/rollback?switch_batch=${encodeURIComponent(activeBatch.value)}&table_name=${encodeURIComponent(tableName)}`,
    '单表已回滚',
  )
}

function openResolve(row: Row) {
  resolveTarget.value = row
  resolveCanonical.value = String(row.裁决结果 || row.规范类型 || '')
  resolveNote.value = ''
}

async function submitResolve() {
  if (!resolveTarget.value) return
  try {
    const response = await request(`${ENDPOINT}/conflicts/${resolveTarget.value.id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ canonical: resolveCanonical.value, note: resolveNote.value }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message || '裁决失败')
    resolveTarget.value = null
    notify('冲突已裁决')
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '裁决失败', false)
  }
}

async function exportRows() {
  const params = new URLSearchParams()
  if (filters.value.table_name) params.set('table_name', filters.value.table_name)
  if (filters.value.conflict) params.set('conflict', filters.value.conflict)
  const response = await request(`${ENDPOINT}/export?${params.toString()}`)
  if (!response.ok) {
    notify('导出失败，请稍后重试', false)
    return
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = 'schema_version.csv'
  link.click()
  URL.revokeObjectURL(url)
}

onMounted(reload)
</script>

<style scoped>
.tabs {
  display: flex;
  gap: 8px;
}
h3 {
  margin: 16px 0 8px;
  font-size: 14px;
}
</style>
