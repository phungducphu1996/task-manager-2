import { createRouter, createWebHistory } from 'vue-router'
import LoginView from '../components/LoginView.vue'
import TaskBoard from '../components/TaskBoard.vue'
import ManageCatalogView from '../components/ManageCatalogView.vue'
import { useAuthStore } from '../stores/authStore'

const routes = [
  { path: '/', redirect: '/today' },
  {
    path: '/login',
    name: 'login',
    component: LoginView
  },
  {
    path: '/:view(today|upcoming|inbox|anytime|review|logbook)',
    name: 'board',
    component: TaskBoard
  },
  {
    path: '/someday',
    redirect: '/today'
  },
  {
    path: '/manage',
    name: 'manage',
    component: ManageCatalogView
  }
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.bootstrap()

  if (to.name === 'login' && auth.isAuthenticated) {
    return { path: '/today' }
  }

  if (to.name !== 'login' && !auth.isAuthenticated) {
    return { path: '/login' }
  }

  if (to.name === 'manage' && (auth.user?.role ?? '').toLowerCase() !== 'admin') {
    return { path: '/today' }
  }

  return true
})
