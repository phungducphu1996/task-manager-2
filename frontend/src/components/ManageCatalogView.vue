<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/authStore'
import { useTaskStore } from '../stores/taskStore'

const router = useRouter()
const auth = useAuthStore()
const store = useTaskStore()

const bootstrapping = ref(true)
const newTypeName = ref('')
const newShopName = ref('')
const typeDrafts = ref<Record<number, string>>({})
const shopDrafts = ref<Record<number, string>>({})

watch(
  () => store.taskTypes,
  (next) => {
    const draftMap: Record<number, string> = {}
    for (const item of next) {
      draftMap[item.id] = typeDrafts.value[item.id] ?? item.name
    }
    typeDrafts.value = draftMap
  },
  { immediate: true, deep: true }
)

watch(
  () => store.shops,
  (next) => {
    const draftMap: Record<number, string> = {}
    for (const item of next) {
      draftMap[item.id] = shopDrafts.value[item.id] ?? item.name
    }
    shopDrafts.value = draftMap
  },
  { immediate: true, deep: true }
)

onMounted(async () => {
  await auth.bootstrap()
  if (!auth.user) {
    await router.replace('/login')
    return
  }

  await store.bootstrap(auth.user.id, auth.user.role)
  if (!store.isAdmin) {
    await router.replace('/today')
    return
  }
  bootstrapping.value = false
})

async function goBack() {
  await router.push('/today')
}

async function createType() {
  const name = newTypeName.value.trim()
  if (!name) return
  try {
    await store.createTaskType(name)
    newTypeName.value = ''
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function saveType(typeId: number) {
  const name = (typeDrafts.value[typeId] ?? '').trim()
  if (!name) return
  try {
    await store.updateTaskType(typeId, name)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function removeType(typeId: number) {
  const target = store.taskTypes.find((item) => item.id === typeId)
  const accepted = window.confirm(`Delete task type "${target?.name ?? typeId}"?`)
  if (!accepted) return
  try {
    await store.deleteTaskType(typeId)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function createShop() {
  const name = newShopName.value.trim()
  if (!name) return
  try {
    await store.createShop(name)
    newShopName.value = ''
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function saveShop(shopId: number) {
  const name = (shopDrafts.value[shopId] ?? '').trim()
  if (!name) return
  try {
    await store.updateShop(shopId, name)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}

async function removeShop(shopId: number) {
  const target = store.shops.find((item) => item.id === shopId)
  const accepted = window.confirm(`Delete shop "${target?.name ?? shopId}"?`)
  if (!accepted) return
  try {
    await store.deleteShop(shopId)
  } catch {
    // Error message is shown by store.typeManageError.
  }
}
</script>

<template>
  <main class="manage-page">
    <header class="manage-header">
      <div>
        <h1>Manage Catalog</h1>
        <p>Edit names or add/remove shops and task types.</p>
      </div>
      <button class="ghost-btn" type="button" @click="goBack">Back to Tasks</button>
    </header>

    <section v-if="bootstrapping" class="loading-state">Loading management data...</section>

    <template v-else>
      <p v-if="store.typeManageError" class="error-message">{{ store.typeManageError }}</p>

      <div class="manage-grid">
        <section class="manage-card">
          <h2>Task Types</h2>

          <div class="manage-create">
            <input
              v-model="newTypeName"
              type="text"
              placeholder="Add task type"
              :disabled="store.typeManageLoading"
              @keydown.enter.prevent="createType"
            />
            <button class="primary-btn" type="button" :disabled="store.typeManageLoading" @click="createType">
              Add
            </button>
          </div>

          <ul class="manage-list">
            <li v-for="item in store.taskTypes" :key="item.id">
              <input v-model="typeDrafts[item.id]" type="text" :disabled="store.typeManageLoading" />
              <button class="ghost-btn" type="button" :disabled="store.typeManageLoading" @click="saveType(item.id)">
                Save
              </button>
              <button class="ghost-btn danger" type="button" :disabled="store.typeManageLoading" @click="removeType(item.id)">
                Remove
              </button>
            </li>
          </ul>
        </section>

        <section class="manage-card">
          <h2>Shops</h2>

          <div class="manage-create">
            <input
              v-model="newShopName"
              type="text"
              placeholder="Add shop"
              :disabled="store.typeManageLoading"
              @keydown.enter.prevent="createShop"
            />
            <button class="primary-btn" type="button" :disabled="store.typeManageLoading" @click="createShop">
              Add
            </button>
          </div>

          <ul class="manage-list">
            <li v-for="item in store.shops" :key="item.id">
              <input v-model="shopDrafts[item.id]" type="text" :disabled="store.typeManageLoading" />
              <button class="ghost-btn" type="button" :disabled="store.typeManageLoading" @click="saveShop(item.id)">
                Save
              </button>
              <button class="ghost-btn danger" type="button" :disabled="store.typeManageLoading" @click="removeShop(item.id)">
                Remove
              </button>
            </li>
          </ul>
        </section>
      </div>
    </template>
  </main>
</template>
