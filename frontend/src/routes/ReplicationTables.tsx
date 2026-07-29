import { useEffect, useMemo, useRef, useState } from 'react'
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

/** A checkbox that can say "some of these", which a schema row usually has to. */
function TriCheckbox({
    checked,
    indeterminate,
    onChange,
    className,
}: {
    checked: boolean
    indeterminate?: boolean
    onChange: () => void
    className?: string
}) {
    const ref = useRef<HTMLInputElement>(null)
    useEffect(() => {
        if (ref.current) ref.current.indeterminate = !!indeterminate && !checked
    }, [indeterminate, checked])
    return (
        <input
            ref={ref}
            type="checkbox"
            checked={checked}
            onChange={onChange}
            onClick={(e) => e.stopPropagation()}
            className={cn('cursor-pointer accent-primary', className)}
        />
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
    const [selected, setSelected] = useState<Set<string>>(new Set())
    const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

    const [actionLoading, setActionLoading] = useState(false)
    const [confirmAction, setConfirmAction] = useState<{ type: 'add' | 'remove' | 'fdw_add' | 'fdw_remove'; tables: string[] } | null>(null)
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
                setSelected(new Set())
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

    const filtered = useMemo(() => {
        let list = tables
        if (filter !== 'all') list = list.filter((t) => tableMode(t, fdwSet) === filter)
        if (search.trim()) {
            const q = search.trim().toLowerCase()
            list = list.filter((t) => `${t.schema}.${t.table}`.toLowerCase().includes(q))
        }
        return list
    }, [tables, filter, search, fdwSet])

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
                let replicated = 0, fdwCount = 0, rows = 0, viaSchema = 0, missingOnSub = 0
                for (const t of items) {
                    const m = tableMode(t, fdwSet)
                    if (m === 'replicated') replicated++
                    else if (m === 'fdw') fdwCount++
                    rows += t.estimated_rows
                    if (t.pub_via === 'schema') viaSchema++
                    if (t.in_publication && !t.in_subscriber) missingOnSub++
                }
                return { schema, items: items.sort((a, b) => a.table.localeCompare(b.table)), replicated, fdwCount, rows, viaSchema, missingOnSub }
            })
            .sort((a, b) => a.schema.localeCompare(b.schema))
    }, [filtered, fdwSet])

    const stats = useMemo(() => {
        let replicated = 0, fdwCount = 0, none = 0
        for (const t of tables) {
            const m = tableMode(t, fdwSet)
            if (m === 'replicated') replicated++
            else if (m === 'fdw') fdwCount++
            else none++
        }
        return { total: tables.length, replicated, fdw: fdwCount, none, schemas: new Set(tables.map((t) => t.schema)).size }
    }, [tables, fdwSet])

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

    const toggleSelect = (fqn: string) =>
        setSelected((prev) => {
            const next = new Set(prev)
            if (next.has(fqn)) next.delete(fqn)
            else next.add(fqn)
            return next
        })

    const setMany = (fqns: string[], on: boolean) =>
        setSelected((prev) => {
            const next = new Set(prev)
            for (const f of fqns) {
                if (on) next.add(f)
                else next.delete(f)
            }
            return next
        })

    const selectedList = Array.from(selected)
    const byFqn = useMemo(() => {
        const m = new Map<string, TableInfo>()
        for (const t of tables) m.set(`${t.schema}.${t.table}`, t)
        return m
    }, [tables])

    const selectedInPub = selectedList.filter((f) => byFqn.get(f)?.in_publication && byFqn.get(f)?.pub_via === 'table')
    const selectedSchemaLevel = selectedList.filter((f) => byFqn.get(f)?.in_publication && byFqn.get(f)?.pub_via === 'schema')
    const selectedNotInPub = selectedList.filter((f) => byFqn.get(f) && !byFqn.get(f)!.in_publication)
    // FDW is only addable to a table that is not published: the same name
    // cannot be both a local replicated table and a foreign one.
    const selectedFdwAddable = selectedList.filter((f) => byFqn.get(f) && !byFqn.get(f)!.in_publication && !fdwSet.has(f))
    const selectedFdwRemovable = selectedList.filter((f) => fdwSet.has(f))

    const executeFdwAction = async (type: 'fdw_add' | 'fdw_remove', tableList: string[]) => {
        setActionLoading(true)
        setError(null)
        setMessage(null)
        try {
            const method = type === 'fdw_add' ? 'POST' : 'DELETE'
            const payload = {
                tables: tableList.map((fqn) => {
                    const [schema, ...rest] = fqn.split('.')
                    return { schema, name: rest.join('.') }
                }),
            }
            const r = await fetch(`${base}/replication/fdw/tables`, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            const actionWord = type === 'fdw_add' ? 'Mapped as foreign' : 'Unmapped'
            const affected = type === 'fdw_add' ? res.added : res.removed
            const skipped = res.skipped || res.not_found || []
            let msg = `${actionWord}: ${affected?.length || 0} table(s)`
            if (skipped.length > 0) msg += ` (${skipped.length} skipped)`
            setMessage(msg)
            setConfirmAction(null)
            loadFdw()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionLoading(false)
        }
    }

    const executeAction = async (type: 'add' | 'remove', tableList: string[]) => {
        setActionLoading(true)
        setError(null)
        setMessage(null)
        try {
            const method = type === 'add' ? 'POST' : 'DELETE'
            const r = await fetch(`${base}/replication/tables`, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tables: tableList, refresh: true }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            const actionWord = type === 'add' ? 'Now replicating' : 'Stopped replicating'
            const affected = type === 'add' ? res.added : res.removed
            const skipped = res.skipped || []
            let msg = `${actionWord}: ${affected?.length || 0} table(s)`
            if (skipped.length > 0) msg += ` (${skipped.length} skipped)`
            if (res.refresh?.refreshed) msg += ' + subscription refreshed'
            setMessage(msg)
            setConfirmAction(null)
            loadTables()
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
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-32 pt-6">
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
                hint="Nothing has been copied from the primary yet. Pick the schemas and tables below — whatever is in the publication when the copy starts is what gets replicated, and changing it afterwards means copying again."
            />

            {/* What is happening to this database, in one line. */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
                <span><span className="font-semibold text-success">{stats.replicated}</span> <span className="text-muted-foreground">replicated</span></span>
                <span><span className="font-semibold text-purple">{stats.fdw}</span> <span className="text-muted-foreground">live (FDW)</span></span>
                <span><span className="font-semibold">{stats.none}</span> <span className="text-muted-foreground">not replicated</span></span>
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
                title="Foreign tables (FDW)"
                summary={fdwTarget ? `${fdw?.live_foreign_tables?.length || 0} live · ${fdwTarget}` : 'not configured'}
            >
                <p className="mb-3 text-[13px] leading-relaxed text-muted-foreground">
                    A foreign table is read straight from the primary at query time — always current, never
                    copied, and only as fast as the link. Replication is the opposite trade. A table can be
                    one or the other, never both.
                </p>
                {fdw ? (
                    <div className="grid gap-6 sm:grid-cols-2">
                        <dl className="grid grid-cols-[92px_1fr] gap-y-1 text-[13px]">
                            <dt className="text-muted-foreground">Server</dt><dd className="font-mono">{fdw.server?.name || '—'}</dd>
                            <dt className="text-muted-foreground">Target</dt><dd className="font-mono">{fdwTarget || '—'}</dd>
                            <dt className="text-muted-foreground">Live tables</dt><dd className="font-mono">{fdw.live_foreign_tables?.length || 0}</dd>
                            <dt className="text-muted-foreground">Config</dt><dd className="truncate font-mono text-xs" title={fdw.yaml_path}>{fdw.yaml_path}</dd>
                        </dl>
                        <div>
                            <div className="mb-1 text-[13px] font-semibold">Whole schemas imported</div>
                            {fdw.schemas?.length ? (
                                <div className="flex flex-wrap gap-1">
                                    {fdw.schemas.map((s) => <Badge key={s.name} variant="purple">{s.name}</Badge>)}
                                </div>
                            ) : (
                                <div className="text-[13px] text-muted-foreground">None — foreign tables are listed one by one.</div>
                            )}
                        </div>
                    </div>
                ) : (
                    <div className="text-[13px] text-muted-foreground">No FDW configuration found.</div>
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
                        ['fdw', `FDW (${stats.fdw})`],
                        ['none', `Not replicated (${stats.none})`],
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
                    const selectedHere = fqns.filter((f) => selected.has(f)).length
                    const open = isOpen(g.schema)
                    return (
                        <div key={g.schema} className="border-b border-border last:border-b-0">
                            <div
                                onClick={() => toggleSchema(g.schema)}
                                className="flex cursor-pointer items-center gap-2 bg-white/[0.015] px-3 py-2 transition-colors hover:bg-white/[0.035]"
                            >
                                <TriCheckbox
                                    checked={selectedHere === fqns.length && fqns.length > 0}
                                    indeterminate={selectedHere > 0}
                                    onChange={() => setMany(fqns, selectedHere !== fqns.length)}
                                />
                                {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
                                <span className="font-mono text-[13px] font-semibold">{g.schema}</span>
                                <span className="text-xs text-muted-foreground">{g.items.length} tables</span>

                                <div className="ml-auto flex items-center gap-1.5">
                                    {g.viaSchema > 0 && (
                                        <Badge variant="warning" title="Published as a whole schema — individual tables inside cannot be removed">
                                            schema-level
                                        </Badge>
                                    )}
                                    {g.missingOnSub > 0 && (
                                        <Badge variant="warning" title="Published but not yet present on the subscriber">
                                            {g.missingOnSub} not copied
                                        </Badge>
                                    )}
                                    {g.replicated > 0 && <Badge variant="success">{g.replicated} replicated</Badge>}
                                    {g.fdwCount > 0 && <Badge variant="purple">{g.fdwCount} FDW</Badge>}
                                    <span className="w-16 text-right font-mono text-xs text-muted-foreground">{formatRows(g.rows)}</span>
                                </div>
                            </div>

                            {open && g.items.map((t) => {
                                const fqn = `${t.schema}.${t.table}`
                                const isSelected = selected.has(fqn)
                                const mode = tableMode(t, fdwSet)
                                return (
                                    <div
                                        key={fqn}
                                        onClick={() => toggleSelect(fqn)}
                                        className={cn(
                                            'flex cursor-pointer items-center gap-2 border-t border-border/60 py-1.5 pl-9 pr-3 text-[13px] transition-colors',
                                            isSelected ? 'bg-white/[0.05]' : 'hover:bg-white/[0.02]',
                                        )}
                                    >
                                        <TriCheckbox checked={isSelected} onChange={() => toggleSelect(fqn)} />
                                        <span className="font-mono">{t.table}</span>

                                        {/* Only what disagrees with the mode is worth a second badge. */}
                                        {t.in_publication && !t.in_subscriber && (
                                            <Badge variant="warning" title="In the publication, but not on the subscriber yet">not copied</Badge>
                                        )}
                                        {t.pub_via === 'schema' && (
                                            <Badge variant="neutral" title="Published because its whole schema is — cannot be removed on its own">via schema</Badge>
                                        )}

                                        <div className="ml-auto flex items-center gap-3">
                                            <Badge variant={mode === 'replicated' ? 'success' : mode === 'fdw' ? 'purple' : 'neutral'} className="min-w-[92px] justify-center">
                                                {mode === 'replicated' ? 'Replicated' : mode === 'fdw' ? 'Live (FDW)' : 'Not replicated'}
                                            </Badge>
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

            {/* Actions follow the selection instead of sitting there greyed out:
                a button that cannot apply to what is selected is not shown. */}
            {selected.size > 0 && (
                <div className="fixed inset-x-0 bottom-0 z-20 border-t border-border-strong bg-background/95 backdrop-blur">
                    <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 px-6 py-3">
                        <span className="text-[13px]">
                            <span className="font-semibold">{selected.size}</span> <span className="text-muted-foreground">selected</span>
                        </span>
                        <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
                        <span className="mx-1 h-6 w-px bg-border-strong" />

                        {selectedNotInPub.length > 0 && (
                            <Button size="sm" variant="primary" disabled={actionLoading}
                                onClick={() => setConfirmAction({ type: 'add', tables: selectedNotInPub })}>
                                Replicate ({selectedNotInPub.length})
                            </Button>
                        )}
                        {selectedInPub.length > 0 && (
                            <Button size="sm" variant="destructive" disabled={actionLoading}
                                onClick={() => setConfirmAction({ type: 'remove', tables: selectedInPub })}>
                                Stop replicating ({selectedInPub.length})
                            </Button>
                        )}
                        {selectedFdwAddable.length > 0 && (
                            <Button size="sm" disabled={actionLoading}
                                onClick={() => setConfirmAction({ type: 'fdw_add', tables: selectedFdwAddable })}>
                                Read live via FDW ({selectedFdwAddable.length})
                            </Button>
                        )}
                        {selectedFdwRemovable.length > 0 && (
                            <Button size="sm" variant="destructive" disabled={actionLoading}
                                onClick={() => setConfirmAction({ type: 'fdw_remove', tables: selectedFdwRemovable })}>
                                Unmap FDW ({selectedFdwRemovable.length})
                            </Button>
                        )}

                        {selectedSchemaLevel.length > 0 && (
                            <span className="text-xs text-warning">
                                {selectedSchemaLevel.length} published via their schema — PostgreSQL cannot drop those individually
                            </span>
                        )}
                    </div>
                </div>
            )}

            <Dialog open={!!confirmAction} onOpenChange={(open) => { if (!open && !actionLoading) setConfirmAction(null) }}>
                <DialogContent className="max-w-lg">
                    {confirmAction && (
                        <>
                            <DialogTitle>
                                {confirmAction.type === 'add' && 'Start replicating these tables'}
                                {confirmAction.type === 'remove' && 'Stop replicating these tables'}
                                {confirmAction.type === 'fdw_add' && 'Read these tables live from the primary'}
                                {confirmAction.type === 'fdw_remove' && 'Remove these foreign tables'}
                            </DialogTitle>
                            <DialogDescription>
                                {confirmAction.type === 'add' &&
                                    'They join the publication and the subscription is refreshed, which starts an initial copy of each.'}
                                {confirmAction.type === 'remove' &&
                                    'They leave the publication and the subscription is refreshed. Rows already copied stay on the subscriber, frozen where they are.'}
                                {confirmAction.type === 'fdw_add' &&
                                    'They become foreign tables read from the primary at query time. Local tables of the same name are dropped (their rows are presumed empty), and configs/fdw.yaml is updated.'}
                                {confirmAction.type === 'fdw_remove' &&
                                    'The foreign-table mappings are dropped and configs/fdw.yaml is updated.'}
                            </DialogDescription>
                            <div className="my-2 max-h-52 overflow-y-auto rounded-md border border-border bg-secondary p-2 font-mono text-[13px]">
                                {confirmAction.tables.map((t) => (<div key={t}>{t}</div>))}
                            </div>
                            <DialogFooter>
                                <Button onClick={() => setConfirmAction(null)} disabled={actionLoading}>Cancel</Button>
                                <Button
                                    variant={confirmAction.type === 'remove' || confirmAction.type === 'fdw_remove' ? 'destructive' : 'primary'}
                                    onClick={() => {
                                        if (confirmAction.type === 'fdw_add' || confirmAction.type === 'fdw_remove') {
                                            executeFdwAction(confirmAction.type, confirmAction.tables)
                                        } else {
                                            executeAction(confirmAction.type, confirmAction.tables)
                                        }
                                    }}
                                    disabled={actionLoading}
                                >
                                    {actionLoading ? 'Working…' : 'Confirm'}
                                </Button>
                            </DialogFooter>
                        </>
                    )}
                </DialogContent>
            </Dialog>
        </div>
    )
}
