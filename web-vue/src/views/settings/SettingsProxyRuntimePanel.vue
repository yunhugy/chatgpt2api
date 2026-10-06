<template>
  <div class="space-y-4">
    <FormSection title="代理运行时">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox
              v-model="runtime.enabled"
              :disabled="fieldReadOnly('proxy_runtime.enabled')"
            >启用运行时代理</Checkbox>
            <HelpTip text="开启后，图片与账号等上游请求按代理配置出网；关闭时全部直连。" />
          </div>
        </div>
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox
              v-model="runtime.skip_ssl_verify"
              :disabled="fieldReadOnly('proxy_runtime.skip_ssl_verify')"
            >跳过 SSL 证书校验</Checkbox>
            <HelpTip text="仅在上游证书异常时临时开启，默认关闭。" />
          </div>
        </div>
      </div>

      <FormField label="资源代理地址">
        <template #label-extra>
          <HelpTip text="用于下载图片、字体等静态资源的代理；留空表示直连。" />
        </template>
        <Input
          v-model.trim="runtime.resource_proxy_url"
          block
          root-class="font-mono"
          placeholder="http://127.0.0.1:7890"
          :disabled="fieldReadOnly('proxy_runtime.resource_proxy_url')"
        />
      </FormField>
    </FormSection>

    <FormSection title="Cloudflare 清障">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox
              v-model="clearance.enabled"
              :disabled="fieldReadOnly('proxy_runtime.clearance.enabled')"
            >启用 CF 清障</Checkbox>
            <HelpTip text="遇到 Cloudflare 挑战时自动获取 cf_clearance 并在后续请求中复用。" />
          </div>
        </div>
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox
              v-model="clearance.warm_up_on_start"
              :disabled="!clearance.enabled || fieldReadOnly('proxy_runtime.clearance.warm_up_on_start')"
            >启动时预热</Checkbox>
            <HelpTip text="服务启动后立即刷新一次清障结果，减少首个请求的等待。" />
          </div>
        </div>
      </div>

      <FormField label="清障模式">
        <template #label-extra>
          <HelpTip text="manual：手工填写 cf_clearance；flaresolverr：由 FlareSolverr 自动获取。" />
        </template>
        <GroupedSelectMenu
          v-model="clearance.mode"
          :options="clearanceModeOptions"
          selected-indicator="none"
          :disabled="!clearance.enabled || fieldReadOnly('proxy_runtime.clearance.mode')"
          aria-label="清障模式"
          block
        />
      </FormField>

      <FormField v-if="clearance.mode === 'flaresolverr'" label="FlareSolverr 地址">
        <template #label-extra>
          <HelpTip text="例如 http://flaresolverr:8191；留空视为未配置，清障会直接失败。" />
        </template>
        <Input
          v-model.trim="clearance.flaresolverr_url"
          block
          root-class="font-mono"
          placeholder="http://flaresolverr:8191"
          :disabled="!clearance.enabled || fieldReadOnly('proxy_runtime.clearance.flaresolverr_url')"
        />
      </FormField>

      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <FormField label="清障超时（秒）">
          <template #label-extra>
            <HelpTip text="单次刷新 cf_clearance 的最长等待时间，默认 60，最小 1。" />
          </template>
          <Input
            :model-value="String(clearance.timeout_sec ?? '')"
            type="number"
            min="1"
            block
            :disabled="!clearance.enabled"
            @update:model-value="setClearanceNumber('timeout_sec', $event, 1)"
          />
        </FormField>

        <FormField label="刷新间隔（秒）">
          <template #label-extra>
            <HelpTip text="清障结果的后台刷新周期，默认 3600，最小 60。" />
          </template>
          <Input
            :model-value="String(clearance.refresh_interval ?? '')"
            type="number"
            min="60"
            block
            :disabled="!clearance.enabled"
            @update:model-value="setClearanceNumber('refresh_interval', $event, 60)"
          />
        </FormField>
      </div>

      <FormField label="清障 User-Agent">
        <template #label-extra>
          <HelpTip text="清障时使用的浏览器 UA，留空使用默认值。" />
        </template>
        <Input
          v-model.trim="clearance.user_agent"
          block
          root-class="font-mono"
          :disabled="!clearance.enabled || fieldReadOnly('proxy_runtime.clearance.user_agent')"
        />
      </FormField>

      <template v-if="clearance.mode === 'manual'">
        <FormField label="cf_clearance">
          <template #label-extra>
            <HelpTip :text="clearance.has_cf_clearance ? '已保存，留空表示不修改。' : '尚未保存。'" />
          </template>
          <Input
            v-model.trim="clearance.cf_clearance"
            block
            root-class="font-mono"
            :placeholder="clearance.has_cf_clearance ? '已保存，留空不修改' : '粘贴 cf_clearance'"
            :disabled="!clearance.enabled"
          />
        </FormField>

        <FormField label="cf_cookies">
          <template #label-extra>
            <HelpTip :text="clearance.has_cf_cookies ? '已保存，留空表示不修改。' : '尚未保存。'" />
          </template>
          <Input
            v-model.trim="clearance.cf_cookies"
            block
            root-class="font-mono"
            :placeholder="clearance.has_cf_cookies ? '已保存，留空不修改' : '粘贴 Cookie 串'"
            :disabled="!clearance.enabled"
          />
        </FormField>
      </template>

      <div class="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          :disabled="clearanceTesting"
          @click="runClearanceTest"
        >
          {{ clearanceTesting ? '测试中...' : '测试清障' }}
        </Button>
        <Button
          size="sm"
          variant="primary"
          :disabled="runtimeLoading"
          @click="loadRuntime"
        >
          {{ runtimeLoading ? '读取中...' : '刷新运行状态' }}
        </Button>
      </div>

      <p v-if="runtimeStatus" class="text-xs leading-5 text-muted-foreground">
        运行时：{{ runtimeStatus.enabled ? '已启用' : '未启用' }} ·
        代理来源 {{ runtimeStatus.proxy_source || 'direct' }} ·
        清障 {{ runtimeStatus.clearance_enabled ? runtimeStatus.clearance_mode : '未启用' }} ·
        已缓存主机 {{ runtimeStatus.cached_clearance_hosts?.length || 0 }} 个
      </p>

      <p
        v-if="clearanceResult"
        class="text-xs leading-5"
        :class="clearanceResult.ok ? 'text-emerald-600' : 'text-red-600'"
      >
        清障{{ clearanceResult.ok ? '成功' : '失败' }}：{{ clearanceResult.status }} ·
        {{ clearanceResult.latency_ms }}ms
        <span v-if="clearanceResult.error"> · {{ clearanceResult.error }}</span>
      </p>
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button, Checkbox, FormField, FormSection, GroupedSelectMenu, HelpTip, Input } from 'nanocat-ui'

import { proxyApi, type ClearanceTestResult, type ProxyRuntimeStatus } from '@/api/proxy'
import { useToast } from '@/composables/useToast'
import type { Settings } from '@/types/api'
import {
  settingsFieldOptions,
  settingsFieldReadOnly,
  type SettingsFields,
} from '@/views/settings/settingsView'

const props = defineProps<{
  settings: Settings
  fields: SettingsFields
}>()

const toast = useToast()

const clearanceTesting = ref(false)
const runtimeLoading = ref(false)
const clearanceResult = ref<ClearanceTestResult | null>(null)
const runtimeStatus = ref<ProxyRuntimeStatus | null>(null)

const runtime = computed(() => props.settings.proxy_runtime)
const clearance = computed(() => props.settings.proxy_runtime.clearance)

const fieldReadOnly = (path: string) => settingsFieldReadOnly(props.fields, path)

const clearanceModeOptions = computed(() => (
  settingsFieldOptions(props.fields, 'proxy_runtime.clearance.mode', clearance.value.mode)
))

function setClearanceNumber(
  key: 'timeout_sec' | 'refresh_interval',
  value: string | number,
  minimum: number,
) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return
  clearance.value[key] = Math.max(minimum, Math.trunc(parsed))
}

async function loadRuntime() {
  runtimeLoading.value = true
  try {
    const response = await proxyApi.getRuntime()
    runtimeStatus.value = response.status
  } catch (error: any) {
    toast.error(error?.message || '读取代理运行状态失败')
  } finally {
    runtimeLoading.value = false
  }
}

async function runClearanceTest() {
  clearanceTesting.value = true
  try {
    const response = await proxyApi.testClearance()
    clearanceResult.value = response.result
    if (response.result.ok) toast.success('清障测试通过')
    else toast.warning(`清障测试未通过：${response.result.status}`)
  } catch (error: any) {
    toast.error(error?.message || '清障测试失败')
  } finally {
    clearanceTesting.value = false
  }
}

onMounted(loadRuntime)
</script>

<style scoped>
.settings-check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
  gap: 8px;
}

.settings-check-grid--single {
  grid-template-columns: minmax(0, 1fr);
}

.settings-check-item {
  min-height: 38px;
  border: 1px solid hsl(var(--border));
  border-radius: 14px;
  background: hsl(var(--background) / 0.72);
  transition:
    border-color 0.16s ease,
    background-color 0.16s ease;
}

.settings-check-item:hover {
  border-color: hsl(var(--foreground) / 0.18);
  background: hsl(var(--muted) / 0.24);
}

.settings-check-control {
  display: flex;
  min-height: 38px;
  align-items: center;
  gap: 8px;
  padding-right: 10px;
}

.settings-check-item :deep(label) {
  display: flex;
  width: 100%;
  flex: 1;
  min-height: 38px;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
}

.settings-check-item :deep(label > span:last-child) {
  color: hsl(var(--foreground) / 0.78);
  line-height: 1.35;
}
</style>
