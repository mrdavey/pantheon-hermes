// Native unified package: plain ESM, SDK + React only, no Electron or raw HTTP.
import { useQuery, host, useValue, gatewayActivationEpoch, useGatewayActivationEpoch, Button, Popover, PopoverContent, PopoverTrigger } from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useId, useRef, useState } from 'react'

const HANDLE = /^[A-Za-z0-9_-]{16,80}$/
const LABELS = Object.freeze({ connected: 'Connected', waiting: 'Waiting', disconnected: 'Disconnected',
  expired: 'Expired', closed: 'Chat ended', revoked: 'Access revoked' })
export function chatURL(value) {
  try {
    const url = new URL(value)
    if (url.origin !== 'https://pantheon.talk' || url.username || url.password || url.hash || url.pathname !== '/app' ||
      [...url.searchParams.keys()].length !== 1 || !/^[A-Za-z0-9_-]{1,128}$/.test(url.searchParams.get('chat') || '')) return null
    return url.href
  } catch { return null }
}
export function snapshot(value) {
  if (!value || !HANDLE.test(value.grant || '') || !Array.isArray(value.connections) || value.connections.length > 1000) return null
  const seen = new Set()
  return { grant: value.grant, connections: value.connections.flatMap(row => {
    if (!row || !HANDLE.test(row.id || '') || seen.has(row.id) || !Object.hasOwn(LABELS, row.state)) return []
    seen.add(row.id)
    return [{ id: row.id, state: row.state, url: chatURL(row.url), session: typeof row.session === 'string' ? row.session : '',
      deadline: typeof row.deadline === 'string' && Number.isFinite(Date.parse(row.deadline)) ? row.deadline : null }]
  }) }
}

export function PantheonControl({ ctx }) {
  const profile = useValue(host.state.profile)
  const gateway = useValue(host.state.gateway)
  const epoch = useGatewayActivationEpoch()
  const scope = profile + ':' + epoch
  const current = useRef(scope); current.current = scope
  const [loaded, setLoaded] = useState(null)
  const [phase, setPhase] = useState('loading')
  const [open, setOpen] = useState(false)
  const [revision, redraw] = useState(0)
  const attempts = useRef(new Map())
  const refresh = useRef(() => {})
  const authGeneration = useRef(0)
  const authBusy = useRef(false)
  const revokedScope = useRef(null)
  const heading = useId()
  const view = loaded?.scope === scope ? loaded.value : null
  const alive = () => current.current === scope && host.state.profile.get() === profile && gatewayActivationEpoch() === epoch
  void revision

  const [foreground, setForeground] = useState(() => !document.hidden && navigator.onLine)
  useEffect(() => {
    const visibility = () => {
      const active = !document.hidden && navigator.onLine
      setForeground(active)
      if (active && !authBusy.current) void refresh.current()
    }
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('online', visibility); window.addEventListener('offline', visibility)
    return () => { document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('online', visibility); window.removeEventListener('offline', visibility) }
  }, [])
  useEffect(() => {
    authBusy.current = false; setLoaded(null); setPhase('loading')
  }, [scope])
  const query = useQuery({
    queryKey: ['pantheon', 'connections', profile, epoch],
    enabled: foreground && gateway === 'open' && phase !== 'revoking',
    retry: false, gcTime: 0, staleTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: q => Math.min(30000, 5000 * 2 ** q.state.fetchFailureCount),
    refetchIntervalInBackground: false,
    queryFn: async ({ signal }) => {
      if (!alive() || authBusy.current) return null
      const authScope = authGeneration.current
      try {
        const next = snapshot(await ctx.rest('/connections', { timeoutMs: 30000 }))
        if (!next) throw new Error('invalid snapshot')
        if ((!signal.aborted && alive()) && authGeneration.current === authScope) {
          setLoaded({ scope, value: next }); setPhase('ready')
          revokedScope.current = null
          // Reconcile only original-scope attempts and original operation IDs.
          for (const [key, attempt] of attempts.current) {
            if ((signal.aborted || !alive()) || authGeneration.current !== authScope) break
            if (attempt.scope !== scope || attempt.grant !== next.grant || attempt.state !== 'uncertain') continue
            try {
              const receipt = await ctx.rest('/operations/' + attempt.operation + '?expected_grant=' + attempt.grant, { timeoutMs: 30000 })
              if (receipt?.grant === attempt.grant && receipt.connection?.id === attempt.id && receipt.connection?.state === 'disconnected') {
                attempts.current.set(key, { ...attempt, state: 'disconnected' })
                if (!signal.aborted && alive()) redraw(n => n + 1)
              }
            } catch { /* Unknown stays unknown; no automatic mutation replay. */ }
          }
          return next
        }
      } catch (error) {
        if ((!signal.aborted && alive()) && authGeneration.current === authScope) {
          setLoaded(null)
          setPhase(revokedScope.current === scope ? 'revoked' : 'unavailable')
        }
        throw error
      }
      return null
    },
  })
  refresh.current = () => query.refetch({ cancelRefetch: false })

  async function disconnect(row) {
    if (!view || !alive()) return
    const key = scope + ':' + view.grant + ':' + row.id
    if (attempts.current.has(key)) return
    const attempt = { scope, grant: view.grant, id: row.id, operation: crypto.randomUUID().replaceAll('-', ''), state: 'pending' }
    attempts.current.set(key, attempt); redraw(n => n + 1)
    try {
      const result = await ctx.rest('/connections/' + row.id + '/disconnect', { method: 'POST', timeoutMs: 30000,
        body: { operation: attempt.operation, grant: attempt.grant } })
      attempt.state = result?.grant === attempt.grant && result.connection?.id === row.id && result.connection?.state === 'disconnected' ? 'disconnected' : 'uncertain'
    } catch { attempt.state = 'uncertain' }
    // Retain the original outcome in its own scope; never publish into a new one.
    if (alive()) { redraw(n => n + 1); void refresh.current() }
  }
  async function revoke() {
    if (!view || !alive() || phase === 'revoking') return
    ++authGeneration.current; authBusy.current = true
    setPhase('revoking')
    try {
      await ctx.rest('/authorization/revoke', { method: 'POST', timeoutMs: 30000,
        body: { grant: view.grant, operation: crypto.randomUUID().replaceAll('-', '') } })
      if (alive()) { revokedScope.current = scope; setLoaded(null); setPhase('revoked') }
    } catch { if (alive()) { setLoaded(null); setPhase('unavailable') } }
    finally { if (alive()) { ++authGeneration.current; authBusy.current = false } }
  }
  const label = phase === 'revoked' ? 'Authorization revoked' : phase === 'loading' ? 'Loading…' :
    phase === 'unavailable' ? 'Backend or sign-in unavailable' : 'No connections'
  return h(Popover, { modal: true, open, onOpenChange: setOpen },
    h(PopoverTrigger, { asChild: true }, h(Button, { type: 'button', variant: 'ghost', 'aria-label': 'Pantheon connections' }, 'Pantheon')),
    h(PopoverContent, { align: 'end', 'aria-labelledby': heading, style: { width: 340, maxWidth: 'calc(100vw - 24px)',
      maxHeight: '75vh', overflowY: 'auto', boxSizing: 'border-box', padding: 16 } },
      h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
        h('h2', { id: heading, style: { margin: 0, fontSize: 16 } }, 'Pantheon'),
        h(Button, { type: 'button', variant: 'ghost', 'aria-label': 'Close Pantheon', style: { minWidth: 44, minHeight: 44 }, onClick: () => setOpen(false) }, '×')),
      view?.connections.length ? view.connections.map((row, index) => {
        const attempt = attempts.current.get(scope + ':' + view.grant + ':' + row.id)
        const status = attempt?.state === 'disconnected' ? 'Disconnected' : attempt?.state === 'pending' ? 'Disconnecting…' : LABELS[row.state]
        return h('article', { key: row.id, style: { padding: '12px 0', borderBottom: '1px solid var(--ui-stroke-secondary)' } },
          h('h3', { style: { margin: '0 0 6px', fontSize: 14 } }, 'Chat ', index + 1),
          h('p', { role: 'status', style: { margin: '4px 0' } }, status),
          row.deadline && h('time', { dateTime: row.deadline, style: { display: 'block', fontSize: 12, marginBottom: 8 } }, new Date(row.deadline).toLocaleString()),
          attempt?.state === 'uncertain' && h('p', { role: 'alert' }, 'Checking disconnect outcome…'),
          h('div', { style: { display: 'flex', gap: 8 } },
            h(Button, { type: 'button', variant: 'outline', style: { minHeight: 44 }, disabled: !row.url,
              onClick: () => { if (alive() && row.url) void ctx.os.openExternal(row.url) } }, 'Open chat'),
            h(Button, { type: 'button', variant: 'outline', style: { minHeight: 44 }, disabled: !!attempt || ['disconnected', 'revoked'].includes(row.state),
              'aria-busy': attempt?.state === 'pending', onClick: () => void disconnect(row) }, 'Disconnect')))
      }) : h('p', { role: 'status' }, label),
      phase === 'unavailable' && h('p', null, 'Sign in in this profile: ', h('code', null, 'hermes auth add pantheon:pantheon')),
      h('details', { style: { marginTop: 16 } }, h('summary', { style: { minHeight: 32 } }, 'Diagnostics'),
        h('p', null, 'Pantheon 1.0.5 · ', phase),
        h(Button, { type: 'button', variant: 'outline', style: { minHeight: 44 }, onClick: () => void ctx.os.writeClipboard('Pantheon 1.0.5 · ' + phase) }, 'Copy diagnostics'),
        view && h(Button, { type: 'button', variant: 'outline', disabled: phase === 'revoking', style: { minHeight: 44 }, onClick: () => void revoke() }, 'Revoke authorization')),
      h(Button, { type: 'button', variant: 'ghost', style: { minHeight: 44, marginTop: 8 }, onClick: () => host.navigate('/capabilities?tab=plugins') }, 'Plugin settings')))
}
export default { id: 'pantheon', name: 'Pantheon', defaultEnabled: false,
  register(ctx) { ctx.register({ id: 'connections', area: 'statusBar.right', order: 130, render: () => h(PantheonControl, { ctx }) }) } }
