import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api, getStoredToken, setStoredToken } from '../services/api'
import type { User } from '../types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getStoredToken())
  const user = ref<User | null>(null)
  const initialized = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const isAuthenticated = computed(() => Boolean(token.value && user.value))

  async function bootstrap() {
    if (initialized.value) return
    if (!token.value) {
      initialized.value = true
      return
    }

    loading.value = true
    error.value = null
    try {
      user.value = await api.getMe()
    } catch {
      token.value = null
      user.value = null
      setStoredToken(null)
    } finally {
      initialized.value = true
      loading.value = false
    }
  }

  async function login(username: string, password: string) {
    loading.value = true
    error.value = null
    try {
      const response = await api.login(username, password)
      token.value = response.access_token
      setStoredToken(response.access_token)
      user.value = response.user
      initialized.value = true
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Login failed.'
      throw err
    } finally {
      loading.value = false
    }
  }

  function logout() {
    token.value = null
    user.value = null
    setStoredToken(null)
  }

  return {
    token,
    user,
    initialized,
    loading,
    error,
    isAuthenticated,
    bootstrap,
    login,
    logout,
  }
})
