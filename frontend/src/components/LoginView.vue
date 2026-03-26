<script setup lang="ts">
import { reactive } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '../stores/authStore'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({
  username: '',
  password: '',
})

async function submit() {
  if (!form.username.trim() || !form.password) return
  await auth.login(form.username.trim(), form.password)
  await router.replace('/today')
}
</script>

<template>
  <main class="login-screen">
    <section class="login-card">
      <h1>Team Task Manager</h1>
      <p>Login to continue</p>

      <form class="login-form" @submit.prevent="submit">
        <label>
          Username
          <input v-model="form.username" type="text" autocomplete="username" />
        </label>

        <label>
          Password
          <input v-model="form.password" type="password" autocomplete="current-password" />
        </label>

        <button class="primary-btn login-btn" type="submit" :disabled="auth.loading">
          {{ auth.loading ? 'Signing in…' : 'Sign in' }}
        </button>
      </form>

      <p v-if="auth.error" class="login-error">{{ auth.error }}</p>
    </section>
  </main>
</template>
