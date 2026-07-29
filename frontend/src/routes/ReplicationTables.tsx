import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, Eye, EyeOff, Search } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { BootstrapGate } from '@/components/BootstrapGate'

interface TableInfo {
    schema: string
    table: string
    in_publication: boolean
    pub_via: 'table' | 'schema' | null
    in_subscriber: boolean
    estimated_rows: number
}

interface ConnInfo {
    publisher: { host: string; port: number; db: string; user: string; password: string }
    subscriber: { container: string; host: string; port: number; db: string; user: string; password: string }
    publication_name: string
    subscription_name: string
}

interface FdwState {
    server: { name: string; options: Record<string, string> }
    schemas: { name: string }[]
    tables: { schema: string; name: string }[]
    live_foreign_tables: { schema: string; table: string }[]
    yaml_path: string
    sql_path: string
}

type FilterTab = 'all' | 'replicated' | 'fdw' | 'none'

type TableMode = 'replicated' | 'fdw' | 'none'

// Resolve the table's effective sync mode. Publication + FDW are mutually
// exclusive (enforced server-side), so a single label captures the state.
function tableMode(t: TableInfo, fdwSet: Set<string>): TableMode {
    if (fdwSet.has(`${t.schema}.${t.table}`)) return 'fdw'
    if (t.in_publication) return 'replicated'
    return 'none'
}

const SYNC_KIND_LABEL: Record<string, string> = {
    table_added: 'Table added',
    column_added: 'Column added',
    check_constraint: 'CHECK constraint synced',
    schema_move: 'Schema move',
    fdw_drift: 'FDW re-import',
    trigger_reinstalled: 'Trigger reinstalled',
    loop_error: 'Sync error',
}

function fmtSync(e: any): { label: string; tone: 'ok' | 'warn' | 'err'; lines: string[] } {
    const d = (e && e.detail) || {}
    const label = SYNC_KIND_LABEL[e.kind] || e.kind
    const lines: string[] = []
    let tone: 'ok' | 'warn' | 'err' = 'ok'
    const errCount = Array.isArray(d.errors) ? d.errors.length : 0
    switch (e.kind) {
        case 'table_added': {
            const ss = d.synced || []
            lines.push(`${ss.length} table(s) reflected: ${ss.join(', ')}`)
            if (d.refreshed) lines.push('Subscription refreshed')
            break
        }
        case 'column_added': {
            const cc = d.columns_added || []
            lines.push(`${cc.length} column(s) added`)
            for (const x of cc) lines.push(`· ${x.table}.${x.column} (${x.type})`)
            break
        }
        case 'check_constraint': {
            const cs = d.constraints_synced || []
            lines.push(`${cs.length} constraint(s) synced`)
            for (const x of cs) lines.push(`· ${x.table}.${x.constraint} — ${x.action}`)
            break
        }
        case 'schema_move': {
            const moved = d.moved || []
            const orph = d.orphans || []
            const skip = d.skipped || []
            for (const m of moved) lines.push(`Moved: ${m.table} (${m.from} → ${m.to})`)
            if (orph.length) {
                tone = 'warn'
                for (const o of orph) lines.push(`Orphan (manual cleanup): ${o.table} — ${(o.subscriber_orphan_schemas || []).join(', ')}`)
            }
            if (skip.length) {
                tone = 'warn'
                for (const sk of skip) lines.push(`Skipped: ${sk.table} — ${sk.reason}`)
            }
            if (!lines.length) lines.push('No change')
            break
        }
        case 'fdw_drift': {
            const dr = d.drifted || []
            lines.push(`FDW re-IMPORT: ${dr.join(', ')}`)
            if (d.reapplied) lines.push('Re-applied')
            break
        }
        case 'trigger_reinstalled': {
            lines.push(`Auto-add trigger reinstalled (publication: ${d.publication || '-'})`)
            break
        }
        case 'loop_error': {
            tone = 'err'
            lines.push(String(d.error || 'Unknown error'))
            break
        }
        default:
            lines.push(JSON.stringify(d))
    }
    if (errCount) {
        tone = 'err'
        lines.push(`${errCount} error(s)`)
    }
    return { label, tone, lines }
}

const TONE_VARIANT = { ok: 'success', warn: 'warning', err: 'destructive' } as const

function formatRows(n: number) {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
}

const MODE_LABEL: Record<TableMode, string> = {
    replicated: 'Replicate',
    fdw: 'Live',
    none: 'Exclude',
}

function splitFqn(fqn: string) {
    const [schema, ...rest] = fqn.split('.')
    return { schema, name: rest.join('.') }
}

/**
 * The three things a table can be, as one control.
 *
 * Not a checkbox: "replicate or not" was never the question — a table can
 * also be read live, and those two are alternatives rather than a flag with
 * an extra. Showing them side by side is what makes them look like the
 * choice they are, and makes the current one legible at a glance.
 *
 * `value` is null on a schema whose tables disagree; nothing is highlighted
 * then, and pressing one settles the whole schema.
 */
function ModeSwitch({
    value,
    fdwReady,
    onChange,
}: {
    value: TableMode | null
    fdwReady: boolean
    onChange: (mode: TableMode) => void
}) {
    const opts: { mode: TableMode; label: string; on: string }[] = [
        { mode: 'replicated', label: 'Replicate', on: 'bg-success/15 text-success border-success/40' },
        { mode: 'fdw', label: 'Live', on: 'bg-purple/15 text-purple border-purple/40' },
        { mode: 'none', label: 'Exclude', on: 'bg-white/[0.06] text-foreground border-border-strong' },
    ]
    return (
        <div className="flex overflow-hidden rounded-md border border-border" onClick={(e) => e.stopPropagation()}>
            {opts.map((o) => {
                const disabled = o.mode === 'fdw' && !fdwReady
                const active = value === o.mode
                return (
                    <button
                        key={o.mode}
                        disabled={disabled}
                        title={disabled ? 'Reading live needs a login on the primary — see "Live reads (FDW)" above' : undefined}
                        onClick={() => onChange(o.mode)}
                        className={cn(
                            'border-r border-border px-2 py-0.5 text-[11.5px] leading-5 transition-colors last:border-r-0',
                            active ? o.on : 'text-muted-foreground hover:bg-white/[0.04] hover:text-foreground',
                            disabled && 'cursor-not-allowed opacity-40 hover:bg-transparent hover:text-muted-foreground',
                        )}
                    >
                        {o.label}
                    </button>
                )
            })}
        </div>
    )
}

/** A titled section that stays out of the way until asked for. */
function Disclosure({
    title,
    summary,
    defaultOpen = false,
    children,
}: {
    title: string
    summary?: React.ReactNode
    defaultOpen?: boolean
    children: React.ReactNode
}) {
    const [open, setOpen] = useState(defaultOpen)
    return (
        <Card className="mt-3 overflow-hidden p-0">
            <button
                onClick={() => setOpen((o) => !o)}
                className="flex w-full items-center gap-2 px-4 py-2.5 text-left transition-colors hover:bg-white/[0.02]"
            >
                {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
                <span className="text-[13px] font-semibold">{title}</span>
                <span className="ml-auto truncate pl-3 text-xs text-muted-foreground">{summary}</span>
            </button>
            {open && <div className="border-t border-border px-4 py-3">{children}</div>}
        </Card>
    )
}

function Secret({ value }: { value: string }) {
    const [shown, setShown] = useState(false)
    if (!value) return <span className="text-muted-foreground">—</span>
    return (
        <span className="inline-flex items-center gap-1.5">
            <span className="font-mono">{shown ? value : '•'.repeat(Math.min(12, value.length))}</span>
            <button
                onClick={() => setShown((s) => !s)}
                className="text-muted-foreground transition-colors hover:text-foreground"
                title={shown ? 'Hide' : 'Show'}
            >
                {shown ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
            </button>
        </span>
    )
}

export function ReplicationTables() {
    const [tables, setTables] = useState<TableInfo[]>([])
    const [info, setInfo] = useState<ConnInfo | null>(null)
    const [fdw, setFdw] = useState<FdwState | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)

    const [search, setSearch] = useState('')
    const [filter, setFilter] = useState<FilterTab>('all')
    const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

    // Only the changes are held, never the whole answer: the answer is what
    // the publisher already says, and everything starts included because
    // that is what a fresh publication does. This page is where things are
    // taken out, so an empty map means "leave it exactly as it is".
    const [overrides, setOverrides] = useState<Map<string, TableMode>>(new Map())

    const [actionLoading, setActionLoading] = useState(false)
    const [confirmOpen, setConfirmOpen] = useState(false)
    const [refreshLoading, setRefreshLoading] = useState(false)

    const [fdwSet, setFdwSet] = useState<Set<string>>(new Set())
    const [syncEvents, setSyncEvents] = useState<any[]>([])

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const loadTables = () => {
        setLoading(true)
        setError(null)
        fetch(`${base}/replication/tables`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: TableInfo[]) => {
                setTables(data)
                setOverrides(new Map())
                // Everything open is unreadable past a handful of schemas, and
                // everything shut hides the only thing the page is for. Open
                // what fits on a screen; shut the rest.
                const bySchema = new Set(data.map((t) => t.schema))
                setCollapsed(bySchema.size > 4 || data.length > 60 ? bySchema : new Set())
            })
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setError(text)
            })
            .finally(() => setLoading(false))
    }

    const loadInfo = () => {
        fetch(`${base}/replication/info`)
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => { if (data) setInfo(data) })
            .catch(() => {})
    }

    const loadFdw = () => {
        fetch(`${base}/replication/fdw`)
            .then((r) => (r.ok ? r.json() : null))
            .then((data: FdwState | null) => {
                if (!data) return
                setFdw(data)
                const next = new Set<string>()
                for (const ft of (data.live_foreign_tables || [])) next.add(`${ft.schema}.${ft.table}`)
                setFdwSet(next)
            })
            .catch(() => {})
    }

    const loadSyncLog = () => {
        fetch(`${base}/replication/sync-log?limit=50`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d && d.events) setSyncEvents(d.events) })
            .catch(() => {})
    }

    useEffect(() => {
        loadTables()
        loadInfo()
        loadFdw()
        loadSyncLog()
        const id = setInterval(loadSyncLog, 15000)
        return () => clearInterval(id)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // FDW needs a role on the primary to read as. Without one the option is
    // not a choice the user has, so it is shown as unavailable with the
    // reason rather than offered and then refused by the server.
    const fdwReady = !!(fdw?.server?.options?.host)

    const currentMode = (t: TableInfo): TableMode => tableMode(t, fdwSet)
    const modeOf = (t: TableInfo): TableMode => overrides.get(`${t.schema}.${t.table}`) ?? currentMode(t)

    const setMode = (fqns: string[], mode: TableMode) =>
        setOverrides((prev) => {
            const next = new Map(prev)
            for (const fqn of fqns) {
                const t = tables.find((x) => `${x.schema}.${x.table}` === fqn)
                if (!t) continue
                if (currentMode(t) === mode) next.delete(fqn)
                else next.set(fqn, mode)
            }
            return next
        })

    const filtered = useMemo(() => {
        let list = tables
        if (filter !== 'all') list = list.filter((t) => modeOf(t) === filter)
        if (search.trim()) {
            const q = search.trim().toLowerCase()
            list = list.filter((t) => `${t.schema}.${t.table}`.toLowerCase().includes(q))
        }
        return list
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tables, filter, search, fdwSet, overrides])

    // Grouped by schema, which is the shape the database has and the shape the
    // decision has: whole schemas are what people actually keep or drop, and
    // individual tables are the exception inside one.
    const groups = useMemo(() => {
        const map = new Map<string, TableInfo[]>()
        for (const t of filtered) {
            const arr = map.get(t.schema)
            if (arr) arr.push(t)
            else map.set(t.schema, [t])
        }
        return Array.from(map.entries())
            .map(([schema, items]) => {
                let replicated = 0, fdwCount = 0, excluded = 0, rows = 0, changed = 0, missingOnSub = 0
                for (const t of items) {
                    const m = modeOf(t)
                    if (m === 'replicated') replicated++
                    else if (m === 'fdw') fdwCount++
                    else excluded++
                    rows += t.estimated_rows
                    if (m !== currentMode(t)) changed++
                    if (t.in_publication && !t.in_subscriber) missingOnSub++
                }
                return {
                    schema,
                    items: items.slice().sort((a, b) => a.table.localeCompare(b.table)),
                    replicated, fdwCount, excluded, rows, changed, missingOnSub,
                }
            })
            .sort((a, b) => a.schema.localeCompare(b.schema))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filtered, fdwSet, overrides])

    const stats = useMemo(() => {
        let replicated = 0, fdwCount = 0, none = 0
        for (const t of tables) {
            const m = modeOf(t)
            if (m === 'replicated') replicated++
            else if (m === 'fdw') fdwCount++
            else none++
        }
        return { total: tables.length, replicated, fdw: fdwCount, none, schemas: new Set(tables.map((t) => t.schema)).size }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tables, fdwSet, overrides])

    // A search is a request to see what matched, so matching schemas open
    // themselves and the collapse state is left alone underneath.
    const searching = search.trim().length > 0
    const isOpen = (schema: string) => searching || !collapsed.has(schema)

    const toggleSchema = (schema: string) =>
        setCollapsed((prev) => {
            const next = new Set(prev)
            if (next.has(schema)) next.delete(schema)
            else next.add(schema)
            return next
        })

    const pending = useMemo(() => {
        const out: { fqn: string; from: TableMode; to: TableMode }[] = []
        for (const [fqn, to] of overrides) {
            const t = tables.find((x) => `${x.schema}.${x.table}` === fqn)
            if (!t) continue
            const from = currentMode(t)
            if (from !== to) out.push({ fqn, from, to })
        }
        return out.sort((a, b) => a.fqn.localeCompare(b.fqn))
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [overrides, tables, fdwSet])

    const apply = async () => {
        setActionLoading(true)
        setError(null)
        setMessage(null)
        try {
            const wantFdw = new Set<string>()
            const wantReplicate: string[] = []
            for (const t of tables) {
                const fqn = `${t.schema}.${t.table}`
                const m = modeOf(t)
                if (m === 'fdw') wantFdw.add(fqn)
                else if (m === 'replicated') wantReplicate.push(fqn)
            }
            // A schema follows its future tables only when nothing in it is
            // being left out — that is the server's rule, mirrored here so the
            // request says what it means.
            const autoSchemas = Array.from(new Set(tables.map((t) => t.schema))).filter((s) =>
                tables.filter((t) => t.schema === s).every((t) => modeOf(t) === 'replicated'),
            )

            // Unmap first, publish second, map last: a table cannot be both a
            // foreign table and a published one, so each change passes through
            // the state where it is neither.
            const leavingFdw = Array.from(fdwSet).filter((f) => !wantFdw.has(f))
            if (leavingFdw.length) {
                const r = await fetch(`${base}/replication/fdw/tables`, {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tables: leavingFdw.map(splitFqn) }),
                })
                if (!r.ok) throw new Error(`unmapping FDW: ${r.status} ${await r.text()}`)
            }

            const sel = await fetch(`${base}/replication/selection`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tables: wantReplicate, auto_schemas: autoSchemas }),
            })
            if (!sel.ok) throw new Error(`publication: ${sel.status} ${await sel.text()}`)
            const selRes = await sel.json()

            const joiningFdw = Array.from(wantFdw).filter((f) => !fdwSet.has(f))
            if (joiningFdw.length) {
                const r = await fetch(`${base}/replication/fdw/tables`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tables: joiningFdw.map(splitFqn) }),
                })
                if (!r.ok) throw new Error(`mapping FDW: ${r.status} ${await r.text()}`)
            }

            setMessage(
                `Publication now ${selRes.form} — ${selRes.count} table(s)` +
                (selRes.subscription_refreshed ? ', subscription refreshed' : ''),
            )
            setConfirmOpen(false)
            loadTables()
            loadFdw()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionLoading(false)
        }
    }

    const onRefresh = async () => {
        setRefreshLoading(true)
        setError(null)
        setMessage(null)
        try {
            const r = await fetch(`${base}/replication/refresh`, { method: 'POST' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            setMessage('Subscription refreshed')
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setRefreshLoading(false)
        }
    }

    const fdwOpts = fdw?.server?.options || {}
    const fdwTarget = fdwOpts.host ? `${fdwOpts.host}:${fdwOpts.port || 5432}/${fdwOpts.dbname || ''}` : null

    return (
        <div className={cn('mx-auto max-w-5xl animate-page-in px-6 pt-6', pending.length ? 'pb-32' : 'pb-20')}>
            <div className="mb-4 flex items-center justify-between gap-4 border-b border-border pb-4">
                <div className="flex items-center gap-3">
                    <Button asChild size="sm">
                        <Link to="/config">&larr; Back</Link>
                    </Button>
                    <h1 className="text-base font-semibold tracking-tight">Replication</h1>
                </div>
                <div className="flex items-center gap-2">
                    <Button onClick={onRefresh} disabled={refreshLoading} size="sm">
                        {refreshLoading ? 'Refreshing…' : 'Refresh subscription'}
                    </Button>
                    <Button onClick={loadTables} disabled={loading} size="sm">
                        {loading ? 'Loading…' : 'Reload'}
                    </Button>
                </div>
            </div>

            {/* Before the first copy this page is the whole install: the
                choice is made here, and the button that acts on it belongs
                next to the choice rather than a page away. */}
            <BootstrapGate
                onDone={loadTables}
                hint="Nothing has been copied from the primary yet. Everything below is included to begin with — take out what you do not want, then start. Whatever is included when the copy starts is what gets replicated."
            />

            {/* What is happening to this database, in one line. */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
                <span><span className="font-semibold text-success">{stats.replicated}</span> <span className="text-muted-foreground">replicated</span></span>
                <span><span className="font-semibold text-purple">{stats.fdw}</span> <span className="text-muted-foreground">live (FDW)</span></span>
                <span><span className="font-semibold">{stats.none}</span> <span className="text-muted-foreground">excluded</span></span>
                <span className="text-muted-foreground">·</span>
                <span className="text-muted-foreground">{stats.total} tables in {stats.schemas} schemas</span>
            </div>

            <Disclosure
                title="Connection"
                summary={info ? `${info.publisher.host}:${info.publisher.port}/${info.publisher.db} → ${info.subscriber.container}` : undefined}
            >
                {info && (
                    <div className="grid gap-6 sm:grid-cols-2">
                        <div>
                            <div className="mb-2 text-[13px] font-semibold text-info">Publisher</div>
                            <dl className="grid grid-cols-[92px_1fr] gap-y-1 text-[13px]">
                                <dt className="text-muted-foreground">Host</dt><dd className="font-mono">{info.publisher.host}:{info.publisher.port}</dd>
                                <dt className="text-muted-foreground">Database</dt><dd className="font-mono">{info.publisher.db}</dd>
                                <dt className="text-muted-foreground">User</dt><dd className="font-mono">{info.publisher.user}</dd>
                                <dt className="text-muted-foreground">Password</dt><dd><Secret value={info.publisher.password} /></dd>
                                <dt className="text-muted-foreground">Publication</dt><dd className="font-mono">{info.publication_name}</dd>
                            </dl>
                        </div>
                        <div>
                            <div className="mb-2 text-[13px] font-semibold text-success">Subscriber</div>
                            <dl className="grid grid-cols-[92px_1fr] gap-y-1 text-[13px]">
                                <dt className="text-muted-foreground">Container</dt><dd className="font-mono">{info.subscriber.container}</dd>
                                <dt className="text-muted-foreground">Host</dt><dd className="font-mono">{info.subscriber.host}:{info.subscriber.port}</dd>
                                <dt className="text-muted-foreground">Database</dt><dd className="font-mono">{info.subscriber.db}</dd>
                                <dt className="text-muted-foreground">User</dt><dd className="font-mono">{info.subscriber.user}</dd>
                                <dt className="text-muted-foreground">Password</dt><dd><Secret value={info.subscriber.password} /></dd>
                                <dt className="text-muted-foreground">Subscription</dt><dd className="font-mono">{info.subscription_name}</dd>
                            </dl>
                        </div>
                    </div>
                )}
            </Disclosure>

            <Disclosure
                title="Live reads (FDW)"
                summary={fdwReady ? `${fdw?.live_foreign_tables?.length || 0} live · ${fdwTarget}` : 'not set up — no reader role'}
                defaultOpen={!fdwReady && pending.some((p) => p.to === 'fdw')}
            >
                <p className="mb-3 text-[13px] leading-relaxed text-muted-foreground">
                    A live table is read straight from the primary when queried — always current, never
                    copied, and only as fast as the link. Replication is the opposite trade. A table is
                    one or the other, never both.
                </p>
                {fdwReady ? (
                    <div className="grid gap-6 sm:grid-cols-2">
                        <dl className="grid grid-cols-[92px_1fr] gap-y-1 text-[13px]">
                            <dt className="text-muted-foreground">Server</dt><dd className="font-mono">{fdw?.server?.name || '—'}</dd>
                            <dt className="text-muted-foreground">Reads from</dt><dd className="font-mono">{fdwTarget}</dd>
                            <dt className="text-muted-foreground">Live tables</dt><dd className="font-mono">{fdw?.live_foreign_tables?.length || 0}</dd>
                            <dt className="text-muted-foreground">Config</dt><dd className="truncate font-mono text-xs" title={fdw?.yaml_path}>{fdw?.yaml_path}</dd>
                        </dl>
                        <div>
                            <div className="mb-1 text-[13px] font-semibold">Whole schemas imported</div>
                            {fdw?.schemas?.length ? (
                                <div className="flex flex-wrap gap-1">
                                    {fdw.schemas.map((s) => <Badge key={s.name} variant="purple">{s.name}</Badge>)}
                                </div>
                            ) : (
                                <div className="text-[13px] text-muted-foreground">None — live tables are listed one by one.</div>
                            )}
                        </div>
                    </div>
                ) : (
                    <div className="text-[13px] leading-relaxed">
                        <p className="mb-2">
                            Reading live needs its own login on the primary — replication's connection cannot
                            be reused, because a foreign table is queried as whoever asks, whenever they ask.
                            Until one is set, <span className="font-medium">Live</span> is not offered.
                        </p>
                        <p className="mb-1 text-muted-foreground">Give it a read-only role and restart the stack:</p>
                        <pre className="overflow-x-auto rounded-md bg-secondary p-2 font-mono text-[11.5px] leading-relaxed">{`# on the machine running Snaplicator
printf 'FDW_USER=readonly_user\\nFDW_PASSWORD=…\\n' >> /opt/snaplicator/deploy/.env
cd /opt/snaplicator/deploy && docker compose -p snaplicator up -d`}</pre>
                    </div>
                )}
            </Disclosure>

            {message && <p className="mt-3 text-[13px] text-success">{message}</p>}
            {error && <p className="mt-3 text-[13px] text-destructive">{error}</p>}

            <div className="mt-4 flex flex-wrap items-center gap-2">
                <div className="relative max-w-96 flex-1">
                    <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                        placeholder="Search schema.table…"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        className="pl-8"
                    />
                </div>
                <div className="flex gap-1">
                    {([
                        ['all', `All (${stats.total})`],
                        ['replicated', `Replicated (${stats.replicated})`],
                        ['fdw', `Live (${stats.fdw})`],
                        ['none', `Excluded (${stats.none})`],
                    ] as [FilterTab, string][]).map(([key, label]) => (
                        <Button key={key} size="sm" variant={filter === key ? 'primary' : 'ghost'} onClick={() => setFilter(key)}>
                            {label}
                        </Button>
                    ))}
                </div>
                {groups.length > 0 && (
                    <Button
                        size="sm"
                        variant="ghost"
                        className="ml-auto"
                        onClick={() => setCollapsed(collapsed.size ? new Set() : new Set(groups.map((g) => g.schema)))}
                    >
                        {collapsed.size ? 'Expand all' : 'Collapse all'}
                    </Button>
                )}
            </div>

            <Card className="mt-3 overflow-hidden p-0">
                {loading && groups.length === 0 && <div className="p-8 text-center text-[13px] text-muted-foreground">Loading…</div>}
                {!loading && groups.length === 0 && (
                    <div className="p-8 text-center text-[13px] text-muted-foreground">
                        {tables.length === 0 ? 'No tables on the publisher.' : 'Nothing matches this filter.'}
                    </div>
                )}

                {groups.map((g) => {
                    const fqns = g.items.map((t) => `${t.schema}.${t.table}`)
                    const open = isOpen(g.schema)
                    const schemaMode: TableMode | null =
                        g.replicated === g.items.length ? 'replicated'
                            : g.fdwCount === g.items.length ? 'fdw'
                                : g.excluded === g.items.length ? 'none'
                                    : null
                    return (
                        <div key={g.schema} className="border-b border-border last:border-b-0">
                            <div
                                onClick={() => toggleSchema(g.schema)}
                                className="flex cursor-pointer items-center gap-2 bg-white/[0.015] px-3 py-2 transition-colors hover:bg-white/[0.035]"
                            >
                                {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
                                <span className="font-mono text-[13px] font-semibold">{g.schema}</span>
                                <span className="text-xs text-muted-foreground">{g.items.length} tables</span>
                                {g.changed > 0 && <Badge variant="info">{g.changed} changed</Badge>}
                                {g.missingOnSub > 0 && (
                                    <Badge variant="warning" title="Published but not yet present on the subscriber">
                                        {g.missingOnSub} not copied
                                    </Badge>
                                )}

                                <div className="ml-auto flex items-center gap-3">
                                    {schemaMode === null && (
                                        <span className="text-xs text-muted-foreground">
                                            {g.replicated} replicated · {g.fdwCount} live · {g.excluded} excluded
                                        </span>
                                    )}
                                    <ModeSwitch
                                        value={schemaMode}
                                        fdwReady={fdwReady}
                                        onChange={(m) => setMode(fqns, m)}
                                    />
                                    <span className="w-16 text-right font-mono text-xs text-muted-foreground">{formatRows(g.rows)}</span>
                                </div>
                            </div>

                            {open && g.items.map((t) => {
                                const fqn = `${t.schema}.${t.table}`
                                const m = modeOf(t)
                                const changed = m !== currentMode(t)
                                return (
                                    <div
                                        key={fqn}
                                        className={cn(
                                            'flex items-center gap-2 border-t border-border/60 py-1.5 pl-9 pr-3 text-[13px]',
                                            changed && 'bg-info/[0.06]',
                                        )}
                                    >
                                        <span className="font-mono">{t.table}</span>
                                        {t.in_publication && !t.in_subscriber && (
                                            <Badge variant="warning" title="In the publication, but not on the subscriber yet">not copied</Badge>
                                        )}
                                        <div className="ml-auto flex items-center gap-3">
                                            <ModeSwitch value={m} fdwReady={fdwReady} onChange={(next) => setMode([fqn], next)} />
                                            <span className="w-16 text-right font-mono text-xs text-muted-foreground">{formatRows(t.estimated_rows)}</span>
                                        </div>
                                    </div>
                                )
                            })}
                        </div>
                    )
                })}
            </Card>

            <Disclosure title="Auto-sync activity" summary={`${syncEvents.length} recent · refreshes every 15s`}>
                {syncEvents.length === 0 ? (
                    <div className="text-[13px] text-muted-foreground">Nothing recorded yet.</div>
                ) : (
                    <div className="max-h-72 overflow-auto">
                        {syncEvents.map((e, i) => {
                            const f = fmtSync(e)
                            return (
                                <div key={i} className="border-b border-border py-2 last:border-b-0">
                                    <div className="mb-1 flex items-center gap-2">
                                        <Badge variant={TONE_VARIANT[f.tone]}>{f.label}</Badge>
                                        <span className="ml-auto text-xs text-muted-foreground" title={e.ts}>{new Date(e.ts).toLocaleString()}</span>
                                    </div>
                                    <div className="text-[13px] leading-relaxed">
                                        {f.lines.map((ln, j) => (
                                            <div key={j} className={cn(j !== 0 && 'text-muted-foreground', ln.startsWith('·') && 'pl-2.5')}>{ln}</div>
                                        ))}
                                    </div>
                                </div>
                            )
                        })}
                    </div>
                )}
            </Disclosure>

            {/* Nothing is sent until it is asked for: the switches are a draft
                of the publication, and this is where the draft becomes it. */}
            {pending.length > 0 && (
                <div className="fixed inset-x-0 bottom-0 z-20 border-t border-border-strong bg-background/95 backdrop-blur">
                    <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 px-6 py-3">
                        <span className="text-[13px]">
                            <span className="font-semibold">{pending.length}</span>{' '}
                            <span className="text-muted-foreground">change{pending.length === 1 ? '' : 's'} not applied</span>
                        </span>
                        <Button size="sm" variant="ghost" onClick={() => setOverrides(new Map())} disabled={actionLoading}>
                            Discard
                        </Button>
                        <Button size="sm" variant="primary" onClick={() => setConfirmOpen(true)} disabled={actionLoading}>
                            Apply
                        </Button>
                        <span className="text-xs text-muted-foreground">
                            {pending.filter((p) => p.to === 'none').length} excluded ·{' '}
                            {pending.filter((p) => p.to === 'fdw').length} live ·{' '}
                            {pending.filter((p) => p.to === 'replicated').length} replicated
                        </span>
                    </div>
                </div>
            )}

            <Dialog open={confirmOpen} onOpenChange={(open) => { if (!open && !actionLoading) setConfirmOpen(false) }}>
                <DialogContent className="max-w-lg">
                    <DialogTitle>Apply {pending.length} change{pending.length === 1 ? '' : 's'}</DialogTitle>
                    <DialogDescription>
                        The publication is rewritten to match — PostgreSQL cannot take a table out of one
                        that covers everything, so the first exclusion replaces it. Schemas with nothing
                        excluded keep picking up new tables on their own; the others stop doing that.
                        {tables.some((t) => t.in_subscriber)
                            ? ' The subscription is refreshed afterwards: tables added start copying, tables removed keep the rows they already have.'
                            : ' Nothing has been copied yet, so this only decides what the first copy will include.'}
                    </DialogDescription>
                    <div className="my-2 max-h-52 overflow-y-auto rounded-md border border-border bg-secondary p-2 font-mono text-[13px]">
                        {pending.map((p) => (
                            <div key={p.fqn} className="flex items-center gap-2">
                                <span className="truncate">{p.fqn}</span>
                                <span className="ml-auto shrink-0 text-muted-foreground">
                                    {MODE_LABEL[p.from]} → <span className="text-foreground">{MODE_LABEL[p.to]}</span>
                                </span>
                            </div>
                        ))}
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setConfirmOpen(false)} disabled={actionLoading}>Cancel</Button>
                        <Button variant="primary" onClick={apply} disabled={actionLoading}>
                            {actionLoading ? 'Working…' : 'Apply'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
