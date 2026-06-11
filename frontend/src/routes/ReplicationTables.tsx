import { useEffect, useState, useMemo } from 'react'
import { Link } from 'react-router-dom'

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

interface TableInfo {
    schema: string
    table: string
    in_publication: boolean
    pub_via: 'table' | 'schema' | null
    in_subscriber: boolean
    estimated_rows: number
}

interface ConnInfo {
    publisher: {
        host: string
        port: number
        db: string
        user: string
        password: string
    }
    subscriber: {
        container: string
        host: string
        port: number
        db: string
        user: string
        password: string
    }
    publication_name: string
    subscription_name: string
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

export function ReplicationTables() {
    const [tables, setTables] = useState<TableInfo[]>([])
    const [info, setInfo] = useState<ConnInfo | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)

    const [search, setSearch] = useState('')
    const [filter, setFilter] = useState<FilterTab>('all')
    const [selected, setSelected] = useState<Set<string>>(new Set())

    const [actionLoading, setActionLoading] = useState(false)
    const [confirmAction, setConfirmAction] = useState<{ type: 'add' | 'remove' | 'fdw_add' | 'fdw_remove'; tables: string[] } | null>(null)
    const [refreshLoading, setRefreshLoading] = useState(false)

    // Foreign tables managed via configs/fdw.yaml
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
            .then((data) => {
                if (!data) return
                const next = new Set<string>()
                for (const ft of (data.live_foreign_tables || [])) {
                    next.add(`${ft.schema}.${ft.table}`)
                }
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
        // eslint-disable-next-line react-hooks-exhaustive-deps
    }, [])

    const filtered = useMemo(() => {
        let list = tables
        if (filter !== 'all') list = list.filter((t) => tableMode(t, fdwSet) === filter)
        if (search.trim()) {
            const q = search.trim().toLowerCase()
            list = list.filter((t) => t.table.toLowerCase().includes(q) || t.schema.toLowerCase().includes(q))
        }
        return list
    }, [tables, filter, search, fdwSet])

    const stats = useMemo(() => {
        let replicated = 0, fdw = 0, none = 0
        for (const t of tables) {
            const m = tableMode(t, fdwSet)
            if (m === 'replicated') replicated++
            else if (m === 'fdw') fdw++
            else none++
        }
        return { total: tables.length, replicated, fdw, none }
    }, [tables, fdwSet])

    const toggleSelect = (fqn: string) => {
        setSelected((prev) => {
            const next = new Set(prev)
            if (next.has(fqn)) next.delete(fqn)
            else next.add(fqn)
            return next
        })
    }

    const toggleSelectAll = () => {
        const filteredFqns = filtered.map((t) => `${t.schema}.${t.table}`)
        const allSelected = filteredFqns.every((f) => selected.has(f))
        if (allSelected) {
            setSelected((prev) => {
                const next = new Set(prev)
                filteredFqns.forEach((f) => next.delete(f))
                return next
            })
        } else {
            setSelected((prev) => {
                const next = new Set(prev)
                filteredFqns.forEach((f) => next.add(f))
                return next
            })
        }
    }

    const selectedList = Array.from(selected)

    const selectedInPub = selectedList.filter((fqn) => {
        const t = tables.find((t) => `${t.schema}.${t.table}` === fqn)
        return t?.in_publication && t?.pub_via === 'table'
    })
    const selectedSchemaLevel = selectedList.filter((fqn) => {
        const t = tables.find((t) => `${t.schema}.${t.table}` === fqn)
        return t?.in_publication && t?.pub_via === 'schema'
    })
    const selectedNotInPub = selectedList.filter((fqn) => {
        const t = tables.find((t) => `${t.schema}.${t.table}` === fqn)
        return !t?.in_publication
    })

    // FDW selectors: addable requires the row to be (a) not already FDW-mapped
    // and (b) not currently in publication (same name would collide).
    const selectedFdwAddable = selectedList.filter((fqn) => {
        const t = tables.find((t) => `${t.schema}.${t.table}` === fqn)
        return t && !t.in_publication && !fdwSet.has(fqn)
    })
    const selectedFdwRemovable = selectedList.filter((fqn) => fdwSet.has(fqn))

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
            const actionWord = type === 'fdw_add' ? 'Added to FDW' : 'Removed from FDW'
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
            const actionWord = type === 'add' ? 'Added' : 'Removed'
            const affected = type === 'add' ? res.added : res.removed
            const skipped = res.skipped || []
            let msg = `${actionWord} ${affected?.length || 0} table(s)`
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
            setMessage('Subscription refreshed successfully')
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setRefreshLoading(false)
        }
    }

    const formatRows = (n: number) => {
        if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
        if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
        return String(n)
    }

    const allFilteredSelected = filtered.length > 0 && filtered.every((t) => selected.has(`${t.schema}.${t.table}`))

    const gridCols = 'grid grid-cols-[40px_1fr_100px_100px_100px_100px] items-center'

    return (
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <div className="flex items-center gap-3">
                    <Button asChild size="sm">
                        <Link to="/config">&larr; Back</Link>
                    </Button>
                    <h1 className="text-base font-semibold tracking-tight">Replication Tables</h1>
                </div>
                <div className="flex items-center gap-3">
                    <span className="text-[13px] text-muted-foreground">
                        {stats.replicated} replicated · {stats.fdw} FDW · {stats.none} none · {stats.total} total
                    </span>
                    <Button onClick={loadTables} disabled={loading}>
                        {loading ? 'Loading...' : 'Reload'}
                    </Button>
                </div>
            </div>

            {info && (
                <div className="mt-4 flex flex-wrap gap-3">
                    <Card className="min-w-72 flex-1 px-4 py-3">
                        <div className="mb-2 text-[13px] font-semibold text-info">Publisher</div>
                        <div className="font-mono text-[13px] leading-7">
                            <div>Host: {info.publisher.host}</div>
                            <div>Port: {info.publisher.port}</div>
                            <div>DB: {info.publisher.db}</div>
                            <div>User: {info.publisher.user}</div>
                            <div>Password: {info.publisher.password}</div>
                            <div>Publication: {info.publication_name}</div>
                        </div>
                    </Card>
                    <Card className="min-w-72 flex-1 px-4 py-3">
                        <div className="mb-2 text-[13px] font-semibold text-success">Subscriber</div>
                        <div className="font-mono text-[13px] leading-7">
                            <div>Container: {info.subscriber.container}</div>
                            <div>Host: {info.subscriber.host}</div>
                            <div>Port: {info.subscriber.port}</div>
                            <div>DB: {info.subscriber.db}</div>
                            <div>User: {info.subscriber.user}</div>
                            <div>Password: {info.subscriber.password}</div>
                            <div>Subscription: {info.subscription_name}</div>
                        </div>
                    </Card>
                </div>
            )}

            {message && <p className="mt-3 text-[13px] text-success">{message}</p>}
            {error && <p className="mt-3 text-[13px] text-destructive">{error}</p>}

            <div className="mt-4 flex flex-wrap items-center gap-2">
                <Input
                    placeholder="Search tables..."
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    className="max-w-96 flex-1"
                />
                <div className="flex gap-1">
                    {([
                        ['all', `All (${stats.total})`],
                        ['replicated', `Replicated (${stats.replicated})`],
                        ['fdw', `FDW (${stats.fdw})`],
                        ['none', `None (${stats.none})`],
                    ] as [FilterTab, string][]).map(([key, label]) => (
                        <Button
                            key={key}
                            variant={filter === key ? 'primary' : 'ghost'}
                            onClick={() => setFilter(key)}
                        >
                            {label}
                        </Button>
                    ))}
                </div>
            </div>

            <Card className="mt-3 px-4 py-3">
                <div className="mb-2 flex items-center justify-between">
                    <div className="text-[13px] font-semibold">Auto-Sync Activity</div>
                    <span className="text-xs text-muted-foreground">{syncEvents.length} recent · auto-refresh 15s</span>
                </div>
                {syncEvents.length === 0 && (
                    <div className="text-[13px] text-muted-foreground">No auto-sync events recorded yet.</div>
                )}
                {syncEvents.length > 0 && (
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
            </Card>

            <Card className="mt-3 overflow-hidden p-0">
                <div className={cn(gridCols, 'border-b border-border-strong px-3 py-2.5 text-[13px] font-semibold')}>
                    <div>
                        <input
                            type="checkbox"
                            checked={allFilteredSelected}
                            onChange={toggleSelectAll}
                            className="cursor-pointer accent-primary"
                        />
                    </div>
                    <div>Table</div>
                    <div className="text-center">Publication</div>
                    <div className="text-center">Subscriber</div>
                    <div className="text-center" title="Foreign Data Wrapper (live remote read)">FDW</div>
                    <div className="text-right">Est. Rows</div>
                </div>

                {loading && filtered.length === 0 && (
                    <div className="p-6 text-center text-muted-foreground">Loading...</div>
                )}
                {!loading && filtered.length === 0 && (
                    <div className="p-6 text-center text-muted-foreground">No tables found</div>
                )}

                <div className="max-h-[calc(100vh-360px)] overflow-y-auto">
                    {filtered.map((t) => {
                        const fqn = `${t.schema}.${t.table}`
                        const isSelected = selected.has(fqn)
                        const mode = tableMode(t, fdwSet)
                        return (
                            <div
                                key={fqn}
                                onClick={() => toggleSelect(fqn)}
                                className={cn(
                                    gridCols,
                                    'cursor-pointer border-b border-border px-3 py-2 text-[13px] transition-colors last:border-b-0',
                                    isSelected ? 'bg-white/[0.04]' : 'hover:bg-white/[0.02]',
                                )}
                            >
                                <div>
                                    <input
                                        type="checkbox"
                                        checked={isSelected}
                                        onChange={() => toggleSelect(fqn)}
                                        onClick={(e) => e.stopPropagation()}
                                        className="cursor-pointer accent-primary"
                                    />
                                </div>
                                <div className="flex items-center gap-2">
                                    <Badge
                                        variant={mode === 'replicated' ? 'success' : mode === 'fdw' ? 'purple' : 'neutral'}
                                        className="min-w-[84px] justify-center"
                                    >
                                        {mode === 'replicated' ? 'Replicated' : mode === 'fdw' ? 'FDW' : 'None'}
                                    </Badge>
                                    <span className="font-mono">{fqn}</span>
                                </div>
                                <div className="text-center">
                                    <Badge variant={t.in_publication ? (t.pub_via === 'schema' ? 'warning' : 'success') : 'neutral'}>
                                        {t.in_publication ? (t.pub_via === 'schema' ? 'Schema' : 'Yes') : 'No'}
                                    </Badge>
                                </div>
                                <div className="text-center">
                                    <Badge variant={t.in_subscriber ? 'info' : 'neutral'}>
                                        {t.in_subscriber ? 'Yes' : 'No'}
                                    </Badge>
                                </div>
                                <div className="text-center">
                                    <Badge variant={fdwSet.has(fqn) ? 'purple' : 'neutral'}>
                                        {fdwSet.has(fqn) ? 'FDW' : 'No'}
                                    </Badge>
                                </div>
                                <div className="text-right font-mono opacity-80">
                                    {formatRows(t.estimated_rows)}
                                </div>
                            </div>
                        )
                    })}
                </div>
            </Card>

            {/* Action bar */}
            <div className="mt-3 flex flex-wrap items-center gap-2">
                <span className="text-[13px] text-muted-foreground">
                    {selected.size} selected
                </span>
                <Button
                    disabled={selectedNotInPub.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'add', tables: selectedNotInPub })}
                >
                    Add to Publication ({selectedNotInPub.length})
                </Button>
                <Button
                    variant="destructive"
                    disabled={selectedInPub.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'remove', tables: selectedInPub })}
                    title={selectedSchemaLevel.length > 0 ? `${selectedSchemaLevel.length} schema-level table(s) cannot be removed individually` : undefined}
                >
                    Remove from Publication ({selectedInPub.length})
                    {selectedSchemaLevel.length > 0 && (
                        <span className="ml-1 text-xs text-warning">
                            ({selectedSchemaLevel.length} schema-level excluded)
                        </span>
                    )}
                </Button>
                <Button disabled={refreshLoading} onClick={onRefresh}>
                    {refreshLoading ? 'Refreshing...' : 'Refresh Subscription'}
                </Button>
                <span className="mx-1 h-6 w-px bg-border-strong" />
                <Button
                    disabled={selectedFdwAddable.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'fdw_add', tables: selectedFdwAddable })}
                    title="Map selected tables as postgres_fdw foreign tables (live remote read). Cannot coexist with publication for the same table."
                >
                    Add to FDW ({selectedFdwAddable.length})
                </Button>
                <Button
                    variant="destructive"
                    disabled={selectedFdwRemovable.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'fdw_remove', tables: selectedFdwRemovable })}
                >
                    Remove from FDW ({selectedFdwRemovable.length})
                </Button>
            </div>

            {/* Confirm dialog */}
            <Dialog open={!!confirmAction} onOpenChange={(open) => { if (!open && !actionLoading) setConfirmAction(null) }}>
                <DialogContent className="max-w-lg">
                    {confirmAction && (
                        <>
                            <DialogTitle>
                                {confirmAction.type === 'add' && 'Add Tables to Publication'}
                                {confirmAction.type === 'remove' && 'Remove Tables from Publication'}
                                {confirmAction.type === 'fdw_add' && 'Add Tables to FDW'}
                                {confirmAction.type === 'fdw_remove' && 'Remove Tables from FDW'}
                            </DialogTitle>
                            <DialogDescription>
                                {confirmAction.type === 'add' &&
                                    'The following tables will be added to the publication and the subscription will be refreshed.'}
                                {confirmAction.type === 'remove' &&
                                    'The following tables will be removed from the publication and the subscription will be refreshed.'}
                                {confirmAction.type === 'fdw_add' &&
                                    'The following tables will be mapped as live foreign tables. Existing local tables with the same names will be dropped (their row data is presumed empty). configs/fdw.yaml will be updated.'}
                                {confirmAction.type === 'fdw_remove' &&
                                    'The following foreign-table mappings will be removed. configs/fdw.yaml will be updated.'}
                            </DialogDescription>
                            <div className="my-2 max-h-52 overflow-y-auto rounded-md border border-border bg-secondary p-2 font-mono text-[13px]">
                                {confirmAction.tables.map((t) => (
                                    <div key={t}>{t}</div>
                                ))}
                            </div>
                            <DialogFooter>
                                <Button onClick={() => setConfirmAction(null)} disabled={actionLoading}>
                                    Cancel
                                </Button>
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
                                    {actionLoading ? 'Processing...' : 'Confirm'}
                                </Button>
                            </DialogFooter>
                        </>
                    )}
                </DialogContent>
            </Dialog>
        </div>
    )
}
