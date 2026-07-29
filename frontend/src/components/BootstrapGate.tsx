import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Database, Loader2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'

type BootstrapState = 'not_started' | 'running' | 'done' | 'failed'

interface BootstrapStatus {
    state: BootstrapState
    pid: number | null
    exit_code: number | null
    started_at: number | null
    log_tail: string
}

interface CopyProgress {
    status: 'idle' | 'copying' | 'complete'
    total_tables: number
    finished_tables: number
    percent: number
    active?: { schema: string; table: string; bytes_processed: number; bytes_total: number; percent: number | null }[] | null
    details?: { state: string; schema: string; table: string }[] | null
}

// pg_subscription_rel.srsubstate, in words. The letters are the only place
// PostgreSQL says where a table is, and they are not words anyone should have
// to look up while watching a copy run.
const SUB_STATE: Record<string, { label: string; tone: string }> = {
    i: { label: 'waiting', tone: 'text-muted-foreground' },
    d: { label: 'copying', tone: 'text-info' },
    f: { label: 'finishing', tone: 'text-info' },
    s: { label: 'synchronised', tone: 'text-success' },
    r: { label: 'ready', tone: 'text-success' },
}

function fmtBytes(n: number) {
    if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GiB`
    if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MiB`
    if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(0)} KiB`
    return `${n} B`
}

/**
 * Shown until the replica has actually been filled.
 *
 * The install stops before the initial copy, because the copy is what makes
 * the choice of what to replicate permanent — reversing it means tearing the
 * replica down. So the last step is taken here, deliberately, rather than by
 * a script that was never in a position to ask.
 *
 * "Filled", not "subscribed": CREATE SUBSCRIPTION returns in a moment and the
 * copy that follows can run for hours, so treating the subscription's
 * existence as the finish line would hide exactly the part worth watching.
 * The copy is followed to its end, table by table.
 */
export function BootstrapGate({ onDone, hint }: { onDone?: () => void; hint?: React.ReactNode }) {
    const [status, setStatus] = useState<BootstrapStatus | null>(null)
    const [copy, setCopy] = useState<CopyProgress | null>(null)
    const [starting, setStarting] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [now, setNow] = useState(() => Date.now())
    const [showAll, setShowAll] = useState(false)
    const settled = useRef(false)

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const load = async () => {
        try {
            const r = await fetch(`${base}/replication/bootstrap?tail=12`)
            if (r.ok) setStatus(await r.json())
        } catch {
            /* the manager restarting is not news worth showing */
        }
        try {
            const c = await fetch(`${base}/replication/copy-progress`)
            setCopy(c.ok ? await c.json() : null)
        } catch {
            setCopy(null)
        }
    }

    useEffect(() => {
        load()
        const id = setInterval(() => {
            setNow(Date.now())
            load()
        }, 3000)
        return () => clearInterval(id)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // Tell the page once, when there is finally something to show on it.
    const complete = status?.state === 'done' && (!copy || copy.status !== 'copying')
    useEffect(() => {
        if (complete && !settled.current) {
            settled.current = true
            onDone?.()
        }
        if (!complete) settled.current = false
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [complete])

    const start = async () => {
        setStarting(true)
        setError(null)
        try {
            const r = await fetch(`${base}/replication/bootstrap`, { method: 'POST' })
            if (!r.ok) setError(`${r.status} ${await r.text()}`)
            await load()
        } catch (e) {
            setError(String(e))
        } finally {
            setStarting(false)
        }
    }

    if (!status || complete) return null

    const copying = copy && copy.status === 'copying'
    const elapsed = status.started_at ? Math.max(0, Math.floor(now / 1000 - status.started_at)) : 0
    const mm = String(Math.floor(elapsed / 60)).padStart(2, '0')
    const ss = String(elapsed % 60).padStart(2, '0')

    // In flight first, then queued: what is happening beats what is about to.
    const pending = (copy?.details || []).slice().sort((a, b) => {
        const rank = (s: string) => (s === 'd' ? 0 : s === 'f' ? 1 : s === 'i' ? 2 : 3)
        return rank(a.state) - rank(b.state) || `${a.schema}.${a.table}`.localeCompare(`${b.schema}.${b.table}`)
    })
    const shown = showAll ? pending : pending.slice(0, 8)
    const bytesFor = (schema: string, table: string) =>
        (copy?.active || []).find((a) => a.schema === schema && a.table === table)

    const busy = status.state === 'running' || copying

    return (
        <Card className="mb-4 p-4">
            {busy ? (
                <>
                    <div className="flex items-center gap-2 text-[13px] font-medium">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        {copying
                            ? `Copying from the primary — ${copy!.finished_tables} of ${copy!.total_tables} tables`
                            : 'Setting up the replica'}
                        <span className="ml-auto font-mono text-xs text-muted-foreground">{mm}:{ss}</span>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                        {copying
                            ? 'Each table is copied in full before it starts following changes. Closing this page does not stop it.'
                            : 'The schema is cloned first, then the subscription is created and the copying begins.'}
                    </p>

                    {copying && (
                        <>
                            <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-secondary">
                                <div
                                    className="h-full rounded-full bg-info transition-[width] duration-500"
                                    style={{ width: `${Math.min(100, Math.max(2, copy!.percent))}%` }}
                                />
                            </div>

                            {shown.length > 0 && (
                                <div className="mt-3 rounded-md border border-border">
                                    {shown.map((d) => {
                                        const st = SUB_STATE[d.state] || { label: d.state, tone: 'text-muted-foreground' }
                                        const b = bytesFor(d.schema, d.table)
                                        return (
                                            <div
                                                key={`${d.schema}.${d.table}`}
                                                className="flex items-center gap-2 border-b border-border/60 px-3 py-1.5 text-[13px] last:border-b-0"
                                            >
                                                <span className="font-mono">{d.schema}.{d.table}</span>
                                                {b && (
                                                    <span className="text-xs text-muted-foreground">
                                                        {fmtBytes(b.bytes_processed)}
                                                        {b.bytes_total > 0 ? ` of ${fmtBytes(b.bytes_total)}` : ''}
                                                    </span>
                                                )}
                                                <span className={cn('ml-auto text-xs', st.tone)}>{st.label}</span>
                                            </div>
                                        )
                                    })}
                                </div>
                            )}
                            {pending.length > 8 && (
                                <Button size="sm" variant="ghost" className="mt-2" onClick={() => setShowAll((s) => !s)}>
                                    {showAll ? 'Show fewer' : `Show all ${pending.length}`}
                                </Button>
                            )}
                        </>
                    )}
                </>
            ) : status.state === 'failed' ? (
                <>
                    <div className="flex items-center gap-2 text-[13px] font-medium text-destructive">
                        <AlertTriangle className="h-4 w-4" />
                        Replication did not start
                        {status.exit_code != null ? ` (exit ${status.exit_code})` : ''}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                        Nothing has been copied. Fix what the log points at and start it again.
                    </p>
                </>
            ) : (
                <>
                    <div className="flex items-center gap-2 text-[13px] font-medium">
                        <Database className="h-4 w-4" />
                        Replication has not started yet
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                        {hint ?? (
                            <>
                                Nothing has been copied from the primary. The first copy is what fixes
                                what gets replicated, so it waits for you to say go.
                            </>
                        )}
                    </p>
                </>
            )}

            {status.log_tail && !copying ? (
                <pre className="mt-3 max-h-40 overflow-auto rounded-md bg-secondary/60 p-2 text-[11px] leading-relaxed text-muted-foreground">
                    {status.log_tail}
                </pre>
            ) : null}

            {error ? <p className="mt-2 text-xs text-destructive">{error}</p> : null}

            {!busy ? (
                <div className="mt-3">
                    <Button variant="primary" onClick={start} disabled={starting}>
                        {starting ? 'Starting…' : status.state === 'failed' ? 'Try again' : 'Start replication'}
                    </Button>
                </div>
            ) : null}
        </Card>
    )
}
