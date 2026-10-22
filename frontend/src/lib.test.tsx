import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, qs, setTokens } from './api'
import { Markdown, healthTone, pct } from './lib'

afterEach(() => { vi.restoreAllMocks(); setTokens(null) })

describe('Markdown renderer', () => {
  it('renders bold, code and list items without interpreting HTML', () => {
    render(<Markdown text={'**Hello** `1FT` <img src=x onerror=alert(1)>\n- one\n2. two'} />)
    expect(screen.getByText('Hello').tagName).toBe('STRONG')
    expect(screen.getByText('1FT').tagName).toBe('CODE')
    expect(document.querySelector('img')).toBeNull() // raw HTML is shown as text, never injected
    expect(screen.getByText('one')).toBeInTheDocument()
  })
})

describe('helpers', () => {
  it('formats risk and picks tones by health', () => {
    expect(pct(0.874)).toBe('87%')
    expect(pct(null)).toBe('—')
    expect(healthTone(30)).toBe('text-crit')
    expect(healthTone(70)).toBe('text-warn')
    expect(healthTone(95)).toBe('text-ok')
  })
  it('builds query strings skipping empty values', () => {
    expect(qs({ a: 1, b: '', c: undefined, d: 'x y' })).toBe('?a=1&d=x%20y')
  })
})

describe('api client', () => {
  it('sends the bearer token and surfaces server errors', async () => {
    setTokens({ access_token: 'AT', refresh_token: 'RT' })
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'missing permission alerts:write' }), { status: 403 }))
    vi.stubGlobal('fetch', f)
    await expect(api('/api/v1/alerts/1', { method: 'PATCH', json: { status: 'resolved' } })).rejects.toMatchObject({ status: 403, message: 'missing permission alerts:write' })
    const [, init] = f.mock.calls[0]
    expect(init.headers.Authorization).toBe('Bearer AT')
    expect(init.headers['Content-Type']).toBe('application/json')
    expect(new ApiError(1, 'x')).toBeInstanceOf(Error)
  })

  it('refreshes once on 401 and retries the request', async () => {
    setTokens({ access_token: 'old', refresh_token: 'RT' })
    const f = vi.fn()
      .mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: 'new', refresh_token: 'RT2' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: 1 }), { status: 200 }))
    vi.stubGlobal('fetch', f)
    expect(await api('/api/v1/vehicles')).toEqual({ ok: 1 })
    expect(f.mock.calls[1][0]).toBe('/auth/refresh')
    expect(f.mock.calls[2][1].headers.Authorization).toBe('Bearer new')
  })
})
