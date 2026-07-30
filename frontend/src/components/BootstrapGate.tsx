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
    details?: { state: string; schema: string; table: string; size_bytes?: number }[] | null
}

type Phase = 'done' | 'copying' | 'waiting'

// The five states PostgreSQL tracks collapse into the three anyone watching a
// copy actually asks about: what is through, what is moving, what is left.
const PHASE_OF: Record<string, Phase> = { r: 'done', s: 'done', d: 'copying', f: 'copying', i: 'waiting' }

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

/** A short, foldable list of table names — enough to check, not to read. */
function NameList({
    title,
    tone,
    items,
    expanded,
}: {
    title: string
    tone: string
    items: { schema: string; table: string }[]
    expanded: boolean
}) {
    if (!items.length) return null
    const shown = expanded ? items : items.slice(0, 3)
    return (
        <div className="min-w-48 text-xs">
            <div className={cn('mb-0.5 font-medium', tone)}>{title} ({items.length})</div>
            <div className="font-mono leading-relaxed text-muted-foreground">
                {shown.map((d) => (
                    <div key={`${d.schema}.${d.table}`} className="truncate">{d.schema}.{d.table}</div>
                ))}
                {!expanded && items.length > shown.length && <div>+{items.length - shown.length} more</div>}
            </div>
        </div>
    )
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
export function BootstrapGate({
    onDone,
    hint,
    title,
    startLabel,
}: {
    onDone?: () => void
    hint?: React.ReactNode
    // Overridden when the situation is not the one the default describes —
    // arriving at an existing publication is a confirmation, not a choice.
    title?: string
    startLabel?: string
}) {
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
    const details = copy?.details || []
    const groups: Record<Phase, typeof details> = { copying: [], waiting: [], done: [] }
    for (const d of details) groups[PHASE_OF[d.state] || 'waiting'].push(d)
    for (const k of Object.keys(groups) as Phase[]) {
        groups[k].sort((a, b) => `${a.schema}.${a.table}`.localeCompare(`${b.schema}.${b.table}`))
    }
    const bytesFor = (schema: string, table: string) =>
        (copy?.active || []).find((a) => a.schema === schema && a.table === table)

    // Bytes landed against bytes expected, so a run made of one large table
    // and twenty small ones does not sit at "1 of 21" for most of its life.
    const bytesDone = details.reduce((n, d) => n + (PHASE_OF[d.state] === 'done' ? (d.size_bytes || 0) : 0), 0)
    const bytesMoving = (copy?.active || []).reduce((n, a) => n + a.bytes_processed, 0)
    const pctOf = (n: number) => (details.length ? (n / details.length) * 100 : 0)

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
                            {/* One bar, three parts: through, moving, left.
                                A single filled fraction answers "how far" and
                                nothing else — the interesting question during
                                an hour-long copy is what is still to come. */}
                            <div className="mt-3 flex h-2 overflow-hidden rounded-full bg-secondary">
                                <div
                                    className="bg-success transition-[width] duration-500"
                                    style={{ width: `${pctOf(groups.done.length)}%` }}
                                />
                                <div
                                    className="animate-pulse bg-info transition-[width] duration-500"
                                    style={{ width: `${pctOf(groups.copying.length)}%` }}
                                />
                            </div>

                            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                                <span className="text-success">✓ {groups.done.length} done</span>
                                <span className="text-info">⟳ {groups.copying.length} copying</span>
                                <span className="text-muted-foreground">· {groups.waiting.length} to go</span>
                                {bytesDone + bytesMoving > 0 && (
                                    <span className="ml-auto font-mono text-muted-foreground">
                                        {fmtBytes(bytesDone + bytesMoving)} copied
                                    </span>
                                )}
                            </div>

                            {/* In flight, always and in full: it is the part
                                that is changing. */}
                            {groups.copying.length > 0 && (
                                <div className="mt-3 rounded-md border border-border">
                                    {groups.copying.map((d) => {
                                        const b = bytesFor(d.schema, d.table)
                                        const pct = b && b.bytes_total > 0 ? (b.bytes_processed / b.bytes_total) * 100 : null
                                        return (
                                            <div
                                                key={`${d.schema}.${d.table}`}
                                                className="border-b border-border/60 px-3 py-2 last:border-b-0"
                                            >
                                                <div className="flex items-center gap-2 text-[13px]">
                                                    <Loader2 className="h-3 w-3 shrink-0 animate-spin text-info" />
                                                    <span className="font-mono">{d.schema}.{d.table}</span>
                                                    <span className="ml-auto font-mono text-xs text-muted-foreground">
                                                        {b
                                                            ? `${fmtBytes(b.bytes_processed)}${b.bytes_total > 0 ? ` / ${fmtBytes(b.bytes_total)}` : ''}`
                                                            : SUB_STATE[d.state]?.label}
                                                    </span>
                                                </div>
                                                {pct != null && (
                                                    <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-secondary">
                                                        <div className="h-full bg-info" style={{ width: `${Math.min(100, pct)}%` }} />
                                                    </div>
                                                )}
                                            </div>
                                        )
                                    })}
                                </div>
                            )}

                            {/* Done and queued as names, folded away: worth
                                being able to check, not worth the room. */}
                            <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1">
                                <NameList title="Copied" tone="text-success" items={groups.done} expanded={showAll} />
                                <NameList title="Waiting" tone="text-muted-foreground" items={groups.waiting} expanded={showAll} />
                            </div>
                            {groups.done.length + groups.waiting.length > 6 && (
                                <Button size="sm" variant="ghost" className="mt-1" onClick={() => setShowAll((v) => !v)}>
                                    {showAll ? 'Show fewer' : 'Show every table'}
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
                        {title ?? 'Replication has not started yet'}
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
                        {starting ? 'Starting…' : status.state === 'failed' ? 'Try again' : startLabel ?? 'Start replication'}
                    </Button>
                </div>
            ) : null}
        </Card>
    )
}
