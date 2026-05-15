<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../services/api'
import { useAuthStore } from '../stores/authStore'
import type { GmailZaloConfigPayload, GmailZaloEvent, GmailZaloStatus } from '../types'

const router = useRouter()
const auth = useAuthStore()

const loading = ref(true)
const saving = ref(false)
const polling = ref(false)
const testing = ref(false)
const connecting = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const status = ref<GmailZaloStatus | null>(null)

const form = ref({
  enabled: true,
  notify_sale_realtime: true,
  notify_message_realtime: false,
  daily_digest_enabled: true,
  daily_digest_time: '07:00',
  gmail_address: '',
  gmail_app_password: '',
  gmail_oauth_client_id: '',
  gmail_oauth_client_secret: '',
  gmail_oauth_redirect_uri: '',
  gmail_imap_host: 'imap.gmail.com',
  gmail_imap_port: 993,
  gmail_imap_mailbox: 'INBOX',
  gmail_search_since_days: 7,
  gmail_sale_from_addresses: 'transaction@etsy.com',
  gmail_sale_subject: 'You made a sale on Etsy',
  gmail_message_from_addresses: 'no-reply@account.etsy.com,conversations@mail.etsy.com',
  gmail_poll_max_results: 10,
  zalo_worker_url: '',
  zalo_worker_token: '',
  zalo_shared_secret: '',
  zalo_group_id: ''
})

const recentEvents = computed<GmailZaloEvent[]>(() => status.value?.recent_events ?? [])
const config = computed(() => status.value?.config ?? null)
const recommendedRedirectUri = computed(() => {
  if (typeof window === 'undefined') return ''
  return `${window.location.origin}/task-api/admin/integrations/gmail-zalo/oauth/callback`
})

function applyConfig(next: GmailZaloStatus) {
  status.value = next
  const cfg = next.config
  form.value = {
    ...form.value,
    enabled: cfg.enabled,
    notify_sale_realtime: cfg.notify_sale_realtime,
    notify_message_realtime: cfg.notify_message_realtime,
    daily_digest_enabled: cfg.daily_digest_enabled,
    daily_digest_time: cfg.daily_digest_time || '07:00',
    gmail_address: cfg.gmail_address ?? '',
    gmail_app_password: '',
    gmail_oauth_client_id: cfg.gmail_oauth_client_id ?? '',
    gmail_oauth_client_secret: '',
    gmail_oauth_redirect_uri: cfg.gmail_oauth_redirect_uri || recommendedRedirectUri.value,
    gmail_imap_host: cfg.gmail_imap_host || 'imap.gmail.com',
    gmail_imap_port: cfg.gmail_imap_port || 993,
    gmail_imap_mailbox: cfg.gmail_imap_mailbox || 'INBOX',
    gmail_search_since_days: cfg.gmail_search_since_days || 7,
    gmail_sale_from_addresses: cfg.gmail_sale_from_addresses || 'transaction@etsy.com',
    gmail_sale_subject: cfg.gmail_sale_subject || 'You made a sale on Etsy',
    gmail_message_from_addresses:
      cfg.gmail_message_from_addresses || 'no-reply@account.etsy.com,conversations@mail.etsy.com',
    gmail_poll_max_results: cfg.gmail_poll_max_results || 10,
    zalo_worker_url: cfg.zalo_worker_url ?? '',
    zalo_worker_token: '',
    zalo_shared_secret: '',
    zalo_group_id: cfg.zalo_group_id ?? ''
  }
}

function buildConfigPayload(): GmailZaloConfigPayload {
  const payload: GmailZaloConfigPayload = {
    enabled: form.value.enabled,
    notify_sale_realtime: form.value.notify_sale_realtime,
    notify_message_realtime: form.value.notify_message_realtime,
    daily_digest_enabled: form.value.daily_digest_enabled,
    daily_digest_time: form.value.daily_digest_time || '07:00',
    gmail_address: form.value.gmail_address,
    gmail_oauth_client_id: form.value.gmail_oauth_client_id,
    gmail_oauth_redirect_uri: form.value.gmail_oauth_redirect_uri || recommendedRedirectUri.value,
    gmail_imap_host: form.value.gmail_imap_host,
    gmail_imap_port: Number(form.value.gmail_imap_port),
    gmail_imap_mailbox: form.value.gmail_imap_mailbox,
    gmail_search_since_days: Number(form.value.gmail_search_since_days),
    gmail_sale_from_addresses: form.value.gmail_sale_from_addresses,
    gmail_sale_subject: form.value.gmail_sale_subject,
    gmail_message_from_addresses: form.value.gmail_message_from_addresses,
    gmail_poll_max_results: Number(form.value.gmail_poll_max_results),
    zalo_worker_url: form.value.zalo_worker_url,
    zalo_group_id: form.value.zalo_group_id
  }
  if (form.value.gmail_oauth_client_secret.trim()) {
    payload.gmail_oauth_client_secret = form.value.gmail_oauth_client_secret.trim()
  }
  if (form.value.gmail_app_password.trim()) payload.gmail_app_password = form.value.gmail_app_password.trim()
  if (form.value.zalo_worker_token.trim()) payload.zalo_worker_token = form.value.zalo_worker_token.trim()
  if (form.value.zalo_shared_secret.trim()) payload.zalo_shared_secret = form.value.zalo_shared_secret.trim()
  return payload
}

async function loadStatus() {
  loading.value = true
  error.value = null
  try {
    applyConfig(await api.getGmailZaloStatus())
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load monitor.'
  } finally {
    loading.value = false
  }
}

async function saveConfig() {
  saving.value = true
  error.value = null
  notice.value = null

  try {
    await api.updateGmailZaloConfig(buildConfigPayload())
    notice.value = 'Config saved.'
    await loadStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to save config.'
  } finally {
    saving.value = false
  }
}

async function connectGmail() {
  connecting.value = true
  error.value = null
  notice.value = null
  try {
    await api.updateGmailZaloConfig(buildConfigPayload())
    const response = await api.startGmailOAuth()
    window.location.href = response.auth_url
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to start Gmail OAuth.'
    connecting.value = false
  }
}

async function disconnectGmail() {
  const accepted = window.confirm('Disconnect Gmail OAuth? Existing received events stay in the dashboard.')
  if (!accepted) return
  connecting.value = true
  error.value = null
  notice.value = null
  try {
    await api.disconnectGmailOAuth()
    notice.value = 'Gmail disconnected.'
    await loadStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to disconnect Gmail.'
  } finally {
    connecting.value = false
  }
}

async function setMonitorEnabled(enabled: boolean) {
  saving.value = true
  error.value = null
  notice.value = null
  try {
    await api.updateGmailZaloConfig({ enabled })
    notice.value = enabled ? 'Monitor enabled.' : 'Monitor paused.'
    await loadStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to update monitor state.'
  } finally {
    saving.value = false
  }
}

async function pollNow() {
  polling.value = true
  error.value = null
  notice.value = null
  try {
    const response = await api.pollGmailZalo()
    status.value = status.value
      ? { ...status.value, recent_events: response.recent_events }
      : await api.getGmailZaloStatus()
    notice.value = response.result.skipped === true
      ? response.result.reason || 'Monitor paused.'
      : `Poll xong: ${response.result.created} event mới, ${response.result.dispatch.sent ?? 0} đã gửi.`
    await loadStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to poll Gmail.'
  } finally {
    polling.value = false
  }
}

async function testZalo() {
  testing.value = true
  error.value = null
  notice.value = null
  try {
    const response = await api.testGmailZalo('Test Gmail/Zalo monitor từ Task Manager.')
    notice.value = `Test sent: ${response.dispatch.sent ?? 0}, pending: ${response.dispatch.pending ?? 0}.`
    await loadStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to test Zalo.'
  } finally {
    testing.value = false
  }
}

function money(event: GmailZaloEvent): string {
  if (event.sale_total_cents === null) return ''
  const prefix = event.sale_currency ? `${event.sale_currency}$` : '$'
  return `${prefix}${(event.sale_total_cents / 100).toFixed(2)}`
}

function messageSender(event: GmailZaloEvent): string {
  const sender = event.payload.message_sender_name
  return typeof sender === 'string' && sender ? sender : event.sender || 'Etsy buyer'
}

function eventShop(event: GmailZaloEvent): string {
  const shop = event.payload.shop
  return typeof shop === 'string' && shop ? shop : ''
}

function eventThumbnail(event: GmailZaloEvent): string {
  const thumbnail = event.payload.thumbnail_url
  return typeof thumbnail === 'string' && thumbnail ? thumbnail : ''
}

async function goBack() {
  await router.push('/manage')
}

onMounted(async () => {
  await auth.bootstrap()
  if (!auth.user) {
    await router.replace('/login')
    return
  }
  if ((auth.user.role ?? '').toLowerCase() !== 'admin') {
    await router.replace('/today')
    return
  }
  await loadStatus()
})
</script>

<template>
  <main class="manage-page integration-page">
    <header class="manage-header">
      <div>
        <p>Gmail / Zalo Monitor</p>
        <h1>Sales & messages</h1>
      </div>
      <button class="ghost-btn" type="button" @click="goBack">Back</button>
    </header>

    <section v-if="loading" class="loading-state">Loading monitor...</section>

    <template v-else>
      <div class="integration-toolbar">
        <div class="integration-stat">
          <span>Total</span>
          <strong>{{ status?.counts.total ?? 0 }}</strong>
        </div>
        <div class="integration-stat">
          <span>Sales</span>
          <strong>{{ status?.counts.sales ?? 0 }}</strong>
        </div>
        <div class="integration-stat">
          <span>Messages</span>
          <strong>{{ status?.counts.messages ?? 0 }}</strong>
        </div>
        <div class="integration-stat">
          <span>Status</span>
          <strong>{{ config?.enabled ? 'On' : 'Paused' }}</strong>
        </div>
        <div class="integration-stat">
          <span>Gmail</span>
          <strong>{{ config?.gmail_oauth_connected ? 'Connected' : 'Not connected' }}</strong>
        </div>
        <button
          class="ghost-btn"
          type="button"
          :disabled="saving"
          @click="setMonitorEnabled(!config?.enabled)"
        >
          {{ config?.enabled ? 'Pause' : 'Enable' }}
        </button>
        <button class="primary-btn" type="button" :disabled="polling" @click="pollNow">
          {{ polling ? 'Polling...' : 'Poll Gmail' }}
        </button>
        <button class="ghost-btn" type="button" :disabled="testing" @click="testZalo">
          {{ testing ? 'Testing...' : 'Test Zalo' }}
        </button>
      </div>

      <p v-if="error" class="error-message">{{ error }}</p>
      <p v-if="notice" class="success-message">{{ notice }}</p>

      <div class="manage-grid integration-grid">
        <section class="manage-card integration-config-card">
          <div class="manage-card-head">
            <div>
              <h2>Connection</h2>
              <p>Use Google OAuth for Gmail. Secrets are write-only; leave a secret blank to keep it.</p>
            </div>
          </div>

          <label class="integration-toggle">
            <input v-model="form.enabled" type="checkbox" />
            <span>
              <strong>{{ form.enabled ? 'Monitor enabled' : 'Monitor paused' }}</strong>
              <small>Systemd timers keep running, but backend skips Gmail work while paused.</small>
            </span>
          </label>

          <div class="integration-switch-grid">
            <label class="integration-toggle">
              <input v-model="form.notify_sale_realtime" type="checkbox" />
              <span>
                <strong>Báo sale mới ngay</strong>
                <small>Sale Etsy mới sẽ gửi thẳng về Zalo group.</small>
              </span>
            </label>
            <label class="integration-toggle">
              <input v-model="form.notify_message_realtime" type="checkbox" />
              <span>
                <strong>Báo tin nhắn ngay</strong>
                <small>Tạm thời nên tắt để tin nhắn chỉ nằm trong tổng hợp ngày.</small>
              </span>
            </label>
            <label class="integration-toggle">
              <input v-model="form.daily_digest_enabled" type="checkbox" />
              <span>
                <strong>Tổng hợp hằng ngày</strong>
                <small>Gửi sale và tin nhắn trong ngày theo giờ bên dưới.</small>
              </span>
            </label>
            <label>
              Giờ tổng hợp
              <input v-model="form.daily_digest_time" type="time" />
            </label>
          </div>

          <div class="integration-form">
            <label>
              Gmail address / connected email
              <input v-model="form.gmail_address" type="email" autocomplete="off" />
            </label>
            <label>
              OAuth client ID
              <input v-model="form.gmail_oauth_client_id" type="text" autocomplete="off" />
            </label>
            <label>
              OAuth client secret
              <input
                v-model="form.gmail_oauth_client_secret"
                type="password"
                autocomplete="new-password"
                :placeholder="config?.gmail_oauth_client_secret_configured ? 'Configured' : 'Required'"
              />
            </label>
            <label>
              Redirect URI
              <input v-model="form.gmail_oauth_redirect_uri" type="url" />
            </label>
            <label>
              Lookback days
              <input v-model.number="form.gmail_search_since_days" type="number" min="1" max="90" />
            </label>
            <label>
              Sale senders
              <input v-model="form.gmail_sale_from_addresses" type="text" />
            </label>
            <label>
              Sale subject
              <input v-model="form.gmail_sale_subject" type="text" />
            </label>
            <label>
              Message senders
              <input v-model="form.gmail_message_from_addresses" type="text" />
            </label>
            <label>
              Poll limit
              <input v-model.number="form.gmail_poll_max_results" type="number" min="1" max="100" />
            </label>
            <label>
              Zalo worker URL
              <input v-model="form.zalo_worker_url" type="url" />
            </label>
            <label>
              Zalo group ID
              <input v-model="form.zalo_group_id" type="text" />
            </label>
            <label>
              Worker token
              <input
                v-model="form.zalo_worker_token"
                type="password"
                autocomplete="new-password"
                :placeholder="config?.zalo_worker_token_configured ? 'Configured' : 'Optional'"
              />
            </label>
            <label>
              Shared secret
              <input
                v-model="form.zalo_shared_secret"
                type="password"
                autocomplete="new-password"
                :placeholder="config?.zalo_shared_secret_configured ? 'Configured' : 'Optional'"
              />
            </label>
          </div>

          <div class="integration-actions">
            <button class="primary-btn" type="button" :disabled="saving" @click="saveConfig">
              {{ saving ? 'Saving...' : 'Save config' }}
            </button>
            <button
              class="ghost-btn"
              type="button"
              :disabled="connecting || saving"
              @click="connectGmail"
            >
              {{ connecting ? 'Connecting...' : config?.gmail_oauth_connected ? 'Reconnect Gmail' : 'Connect Gmail' }}
            </button>
            <button
              v-if="config?.gmail_oauth_connected"
              class="ghost-btn danger"
              type="button"
              :disabled="connecting"
              @click="disconnectGmail"
            >
              Disconnect
            </button>
            <small v-if="config?.updated_at_label">Updated {{ config.updated_at_label }}</small>
            <small v-if="config?.gmail_oauth_email">Connected as {{ config.gmail_oauth_email }}</small>
          </div>
        </section>

        <section class="manage-card integration-events-card">
          <div class="manage-card-head">
            <div>
              <h2>Received mail</h2>
              <p>Latest detected Etsy sales and buyer messages.</p>
            </div>
          </div>

          <div class="integration-events">
            <article v-for="event in recentEvents" :key="event.id" class="integration-event">
              <div class="event-main" :class="{ 'has-thumb': eventThumbnail(event) }">
                <img
                  v-if="eventThumbnail(event)"
                  class="event-thumb"
                  :src="eventThumbnail(event)"
                  alt=""
                  loading="lazy"
                />
                <span class="event-type" :class="`event-${event.event_type}`">{{ event.event_type }}</span>
                <strong>{{ event.sale_order_id ? `Order #${event.sale_order_id}` : event.subject }}</strong>
                <small>{{ event.received_at_label || 'No received time' }}</small>
              </div>
              <p>
                <template v-if="event.event_type === 'sale'">
                  {{ money(event) }} · {{ eventShop(event) || 'Unknown shop' }} · {{ event.buyer_username || event.buyer_name || 'Unknown buyer' }}
                </template>
                <template v-else>
                  {{ messageSender(event) }}
                </template>
              </p>
              <p v-if="event.snippet" class="event-snippet">{{ event.snippet }}</p>
              <div class="event-delivery">
                <span
                  class="delivery-pill"
                  :class="`delivery-${event.notification?.status ?? 'missing'}`"
                >
                  {{ event.notification?.status ?? 'missing' }}
                </span>
                <span v-if="event.notification?.delivered_at_label">Sent {{ event.notification.delivered_at_label }}</span>
                <span v-else-if="event.notification?.last_error">{{ event.notification.last_error }}</span>
              </div>
            </article>

            <p v-if="recentEvents.length === 0" class="empty-hint">No Gmail events yet. Save config, then poll Gmail.</p>
          </div>
        </section>
      </div>
    </template>
  </main>
</template>
