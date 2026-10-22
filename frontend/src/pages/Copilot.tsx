import { useEffect, useRef, useState } from 'react'
import { useMe } from '../App'
import { api } from '../api'
import { Badge, Markdown } from '../lib'

const SUGGESTIONS = [
  'Which vehicles are at highest maintenance risk?',
  'Why is fleet utilization down today?',
  'Show vehicles with abnormal battery degradation.',
  'Which vehicles have repeated engine faults?',
  'What actions should the fleet manager take?',
  'How much money could we save by reducing idling?',
  'Which drivers are dangerous?',
  'Schedule maintenance with 24 workshop hours',
]

interface Turn { role: 'user' | 'agent'; text: string; data?: any }

export default function CopilotPage() {
  const me = useMe()
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [session, setSession] = useState<string | undefined>()
  const [audit, setAudit] = useState<any[] | null>(null)
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns])

  const ask = async (q: string) => {
    if (!q.trim() || busy) return
    setTurns((t) => [...t, { role: 'user', text: q }]); setInput(''); setBusy(true)
    try {
      const r = await api('/api/v1/agent/chat', { method: 'POST', json: { message: q, session_id: session } })
      setSession(r.session_id); setTurns((t) => [...t, { role: 'agent', text: r.answer, data: r }])
    } catch (e) { setTurns((t) => [...t, { role: 'agent', text: `Error: ${(e as Error).message}` }]) } finally { setBusy(false) }
  }
  const confirm = async (token: string, approve: boolean, idx: number) => {
    try {
      const r = await api('/api/v1/agent/confirm', { method: 'POST', json: { token, approve } })
      setTurns((t) => t.map((x, i) => i === idx ? { ...x, data: { ...x.data, pending_confirmation: null } } : x).concat({ role: 'agent', text: r.executed ? `Done — ${JSON.stringify(r.result)}` : r.message }))
    } catch (e) { setTurns((t) => [...t, { role: 'agent', text: `Could not confirm: ${(e as Error).message}` }]) }
  }
  const showAudit = async () => { setAudit((await api('/api/v1/agent/audit' + (session ? `?session_id=${session}` : ''))).items) }

  return (
    <div className="mx-auto flex h-full max-w-4xl flex-col gap-3">
      <div className="flex items-center justify-between"><div><h1 className="text-xl font-semibold">AI Fleet Copilot</h1>
        <p className="text-xs text-muted">A tool-using agent: every answer is built from live fleet data (no free-form guessing). Write actions need your confirmation. Tools allowed for {me.roles.join(', ')}.</p></div>
        <button className="btn-ghost" onClick={showAudit}>Audit trail</button></div>
      <div className="card flex-1 space-y-4 overflow-y-auto">
        {!turns.length && <div className="space-y-2"><div className="text-sm text-muted">Try one of these:</div><div className="flex flex-wrap gap-2">{SUGGESTIONS.map((s) => <button key={s} onClick={() => ask(s)} className="rounded-sm border border-line px-3 py-1 text-xs hover:bg-panel2">{s}</button>)}</div></div>}
        {turns.map((t, i) => t.role === 'user' ? <div key={i} className="ml-auto max-w-[80%] rounded bg-blue-50 border border-blue-200 px-3 py-2 text-sm">{t.text}</div> : (
          <div key={i} className="max-w-[92%] space-y-2 rounded border border-line bg-panel2 px-3 py-2">
            <Markdown text={t.text} />
            {t.data?.recommended_actions?.length > 0 && <div className="rounded border border-green-200 bg-green-50 p-2"><div className="mb-1 text-xs font-semibold text-ok">Recommended actions</div>
              <ul className="space-y-1 text-xs">{t.data.recommended_actions.map((a: string, k: number) => <li key={k}>→ {a}</li>)}</ul></div>}
            {t.data?.pending_confirmation && <div className="rounded border border-amber-200 bg-amber-50 p-2 text-xs"><div className="mb-2">Confirmation required: {t.data.pending_confirmation.description}</div>
              <button className="btn mr-2" onClick={() => confirm(t.data.pending_confirmation.token, true, i)}>Confirm</button><button className="btn-ghost" onClick={() => confirm(t.data.pending_confirmation.token, false, i)}>Cancel</button></div>}
            {t.data && <div className="flex flex-wrap items-center gap-1 border-t border-line pt-2 text-[11px] text-muted">intent <Badge tone="border-line text-gray-700">{t.data.intent}</Badge>
              {t.data.tool_calls?.map((c: any, k: number) => <Badge key={k} tone="border-blue-200 text-brand">{c.tool} · {c.ms}ms</Badge>)}
              {t.data.llm_used && <Badge tone="border-green-200 text-ok">LLM-narrated (grounding-checked)</Badge>}{t.data.guardrail && <Badge tone="border-red-200 text-crit">guardrail: {t.data.guardrail}</Badge>}</div>}
          </div>))}
        {busy && <div className="text-sm text-muted">Querying fleet data…</div>}<div ref={end} />
      </div>
      <form onSubmit={(e) => { e.preventDefault(); ask(input) }} className="flex gap-2"><input className="input" value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask about maintenance risk, utilisation, batteries, faults, idling cost, or a VIN…" maxLength={1000} /><button className="btn" disabled={busy}>Ask</button></form>
      {audit && <div className="fixed inset-y-0 right-0 z-10 w-[420px] overflow-y-auto border-l border-line bg-panel p-4 text-xs shadow-lg"><div className="mb-2 flex justify-between"><b>Agent audit trail (hash-chained)</b><button onClick={() => setAudit(null)}>✕</button></div>
        {audit.map((a) => <div key={a.id} className="mb-2 rounded border border-line p-2"><div className="flex justify-between"><b>{a.step}{a.tool ? ` · ${a.tool}` : ''}</b><span className={a.allowed ? 'text-ok' : 'text-crit'}>{a.allowed ? 'allowed' : 'blocked'}</span></div>
          <pre className="mt-1 whitespace-pre-wrap break-all text-muted">{JSON.stringify(a.payload).slice(0, 300)}</pre><div className="font-mono text-[10px] text-muted">{a.hash.slice(0, 16)}…</div></div>)}</div>}
    </div>
  )
}
