<template>
  <section class="page" data-module="column_mapping">
    <header class="page-head">
      <div>
        <h2>字段映射管理</h2>
        <p class="page-desc">维护字段映射规则的输入、执行约束与结果记录，为字段映射提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记映射规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出字段映射清单</button>
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
            <button
              class="link"
              type="button"
              :data-testid="`type-check-${row.id}`"
              @click="runTypeCheck(row)"
            >
              类型核对
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无字段映射数据，可先登记映射规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记映射规则</h3>
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

    <div v-if="typeCheckResult" class="modal-backdrop" data-testid="type-check-modal">
      <div class="modal">
        <h3>字段类型核对：{{ typeCheckResult.规则编号 }}</h3>
        <p :class="typeCheckResult.全部一致 ? '' : 'error-text'">
          {{ typeCheckResult.全部一致 ? '映射类型与结构定义归一化后一致（旧简写与新类型兼容）' : '存在不一致或结构定义缺失，请核对结构定义版本表' }}
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>字段名称</th>
              <th>映射类型写法</th>
              <th>结构定义写法</th>
              <th>映射规范类型</th>
              <th>结构定义规范类型</th>
              <th>类型一致</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in typeCheckResult.字段核对" :key="item.字段名称">
              <td>{{ item.字段名称 }}</td>
              <td>{{ item.映射类型写法 || '—' }}</td>
              <td>{{ item.结构定义写法 || '—' }}</td>
              <td>{{ item.映射规范类型 }}</td>
              <td>{{ item.结构定义规范类型 }}</td>
              <td>{{ item.类型一致 ? '是' : '否' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <RouterLink class="btn" to="/schema_version">前往结构定义版本表</RouterLink>
          <button class="btn primary" type="button" data-testid="type-check-close" @click="typeCheckResult = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条字段映射记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface TypeCheckItem {
  字段名称: string
  映射类型写法: string
  结构定义写法: string
  映射规范类型: string
  结构定义规范类型: string
  类型一致: boolean
}
interface TypeCheckResult {
  规则编号: string
  表名称: string
  全部一致: boolean
  字段核对: TypeCheckItem[]
}

const ENDPOINT = '/api/column_mapping'
const columns = ["规则编号", "源字段名", "目标字段名", "数据类型", "默认值", "转换函数", "规则版本", "生效日期"]
const actions = ["提交校验", "标记生效", "标记废弃"]
const statuses = ["草稿", "待校验", "已生效", "已废弃"]
const metricNames = ["映射规则总数", "待校验规则", "已生效规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const typeCheckResult = ref<TypeCheckResult | null>(null)
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
    link.download = 'column_mapping.csv'
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
      throw new Error('字段映射动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段映射操作失败'
  }
}

async function runTypeCheck(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/type_check`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '类型核对失败')
    typeCheckResult.value = payload.entry as TypeCheckResult
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '类型核对失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('映射规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段映射列表读取失败'
  }
}

onMounted(reload)
</script>
