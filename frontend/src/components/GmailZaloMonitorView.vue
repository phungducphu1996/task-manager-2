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
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const status = ref<GmailZaloStatus | null>(null)

const form = ref({
  gmail_address: '',
  gmail_app_password: '',
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

function applyConfig(next: GmailZaloStatus) {
  status.value = next
  const cfg = next.config
  form.value = {
    ...form.value,
    gmail_address: cfg.gmail_address ?? '',
    gmail_app_password: '',
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
  const payload: GmailZaloConfigPayload = {
    gmail_address: form.value.gmail_address,
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
  if (form.value.gmail_app_password.trim()) payload.gmail_app_password = form.value.gmail_app_password.trim()
  if (form.value.zalo_worker_token.trim()) payload.zalo_worker_token = form.value.zalo_worker_token.trim()
  if (form.value.zalo_shared_secret.trim()) payload.zalo_shared_secret = form.value.zalo_shared_secret.trim()

  try {
    await api.updateGmailZaloConfig(payload)
    notice.value = 'Config saved.'
    await loadStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to save config.'
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
    notice.value = `Poll xong: ${response.result.created} event mới, ${response.result.dispatch.sent ?? 0} đã gửi.`
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
          <span>Sent</span>
          <strong>{{ status?.notification_counts.sent ?? 0 }}</strong>
        </div>
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
              <p>Secrets are write-only. Leave a secret blank to keep the current value.</p>
            </div>
          </div>

          <div class="integration-form">
            <label>
              Gmail address
              <input v-model="form.gmail_address" type="email" autocomplete="off" />
            </label>
            <label>
              App password
              <input
                v-model="form.gmail_app_password"
                type="password"
                autocomplete="new-password"
                :placeholder="config?.gmail_app_password_configured ? 'Configured' : 'Required'"
              />
            </label>
            <label>
              IMAP host
              <input v-model="form.gmail_imap_host" type="text" />
            </label>
            <label>
              IMAP port
              <input v-model.number="form.gmail_imap_port" type="number" min="1" max="65535" />
            </label>
            <label>
              Mailbox
              <input v-model="form.gmail_imap_mailbox" type="text" />
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
            <small v-if="config?.updated_at_label">Updated {{ config.updated_at_label }}</small>
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
              <div class="event-main">
                <span class="event-type" :class="`event-${event.event_type}`">{{ event.event_type }}</span>
                <strong>{{ event.sale_order_id ? `Order #${event.sale_order_id}` : event.subject }}</strong>
                <small>{{ event.received_at_label || 'No received time' }}</small>
              </div>
              <p>
                <template v-if="event.event_type === 'sale'">
                  {{ money(event) }} · {{ event.buyer_username || event.buyer_name || 'Unknown buyer' }}
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
