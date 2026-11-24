import { createRouter, createWebHistory } from 'vue-router'

import MergeReviewView from '@/views/MergeReviewView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/merge-review', name: 'merge-review', component: MergeReviewView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
