import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { api } from '../composables/useApi'
import ChargingTab from './ChargingTab.vue'

vi.mock('../composables/useApi', () => ({ api: vi.fn() }))
vi.mock('chart.js', () => ({
  Chart: class {
    static register() {}
    destroy() {}
  },
  registerables: [],
}))

let wrapper

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({})
})

afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

async function openSession(id) {
  let session = {
    id,
    start_ts: new Date().toISOString(),
    end_ts: new Date().toISOString(),
    tier_id: 'public_ac',
    tier_label: 'Public AC',
    energy_kwh: 10,
    cost: id ? 2.5 : null,
  }
  api.mockImplementation(async (method, path, body) => {
    if (method !== 'GET') {
      session = { ...session, ...body, id: id || 42 }
      return session
    }
    if (path === '/api/charging/tiers') {
      return { tiers: [{ id: 'public_ac', label: 'Public AC' }], time_bands: [] }
    }
    if (path === '/api/preferences') return {}
    if (path.startsWith('/api/zones')) return []
    if (path.includes('/charging-sessions?')) return { sessions: [session], timezone: 'UTC' }
    if (path.endsWith('/charging-costs')) return session.id ? [session] : []
    throw new Error(`Unexpected request: ${method} ${path}`)
  })
  wrapper = mount(ChargingTab, { props: { vin: 'V1' }, attachTo: document.body })
  await flushPromises()
  await wrapper.get('button[title="Session details"]').trigger('click')
  await flushPromises()
}

describe.each([
  { label: 'existing session', id: 7, method: 'PUT', path: '/api/vehicles/V1/charging-costs/7' },
  { label: 'detected session', id: null, method: 'POST', path: '/api/vehicles/V1/charging-costs' },
])('$label cost override', ({ id, method, path }) => {
  it.each([
    { input: '12.34', expected: 12.34 },
    { input: '0', expected: 0 },
    { input: '', expected: null },
  ])('sends $expected when Save is clicked', async ({ input, expected }) => {
    await openSession(id)
    const costInput = document.querySelector('input[placeholder="Auto-calculate"]')
    costInput.value = input
    costInput.dispatchEvent(new Event('input', { bubbles: true }))
    document.querySelector('.btn-save').click()
    await flushPromises()

    expect(api).toHaveBeenCalledWith(method, path, expect.objectContaining({ cost: expected }))
    expect(document.querySelector('.session-modal')).toBeNull()
    if (expected != null) {
      expect(wrapper.get('.cell-cost').text()).toBe(`€${expected.toFixed(2)}`)
      // Reopening the saved session keeps the cost in the form.
      await wrapper.get('button[title="Session details"]').trigger('click')
      await flushPromises()
      expect(document.querySelector('input[placeholder="Auto-calculate"]').value).toBe(String(expected))
    }
  })
})
