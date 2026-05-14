import { useEffect, useState, useMemo } from 'react'
import { Link } from 'react-router-dom'

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

    useEffect(() => {
        loadTables()
        loadInfo()
        loadFdw()
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

    return (
        <div className="container">
            <div className="header">
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <Link to="/" className="btn" style={{ padding: '6px 10px' }}>&larr; Back</Link>
                    <div className="title">Replication Tables</div>
                </div>
                <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                    <span className="subtle">
                        {stats.replicated} replicated · {stats.fdw} FDW · {stats.none} none · {stats.total} total
                    </span>
                    <button className="btn" onClick={loadTables} disabled={loading}>
                        {loading ? 'Loading...' : 'Reload'}
                    </button>
                </div>
            </div>

            {info && (
                <div style={{ display: 'flex', gap: 12, marginTop: 16, flexWrap: 'wrap' }}>
                    <div className="card" style={{ flex: 1, minWidth: 280, padding: '12px 16px' }}>
                        <div style={{ fontWeight: 600, marginBottom: 8, color: '#60a5fa' }}>Publisher</div>
                        <div style={{ fontSize: 13, lineHeight: 1.8, fontFamily: 'monospace' }}>
                            <div>Host: {info.publisher.host}</div>
                            <div>Port: {info.publisher.port}</div>
                            <div>DB: {info.publisher.db}</div>
                            <div>User: {info.publisher.user}</div>
                            <div>Password: {info.publisher.password}</div>
                            <div>Publication: {info.publication_name}</div>
                        </div>
                    </div>
                    <div className="card" style={{ flex: 1, minWidth: 280, padding: '12px 16px' }}>
                        <div style={{ fontWeight: 600, marginBottom: 8, color: '#4ade80' }}>Subscriber</div>
                        <div style={{ fontSize: 13, lineHeight: 1.8, fontFamily: 'monospace' }}>
                            <div>Container: {info.subscriber.container}</div>
                            <div>Host: {info.subscriber.host}</div>
                            <div>Port: {info.subscriber.port}</div>
                            <div>DB: {info.subscriber.db}</div>
                            <div>User: {info.subscriber.user}</div>
                            <div>Password: {info.subscriber.password}</div>
                            <div>Subscription: {info.subscription_name}</div>
                        </div>
                    </div>
                </div>
            )}

            {message && <p style={{ color: '#4ade80', marginTop: 12 }}>{message}</p>}
            {error && <p style={{ color: '#f87171', marginTop: 12 }}>{error}</p>}

            <div style={{ display: 'flex', gap: 8, marginTop: 16, flexWrap: 'wrap', alignItems: 'center' }}>
                <input
                    className="input"
                    placeholder="Search tables..."
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    style={{ minWidth: 200, flex: 1, maxWidth: 400 }}
                />
                <div style={{ display: 'flex', gap: 4 }}>
                    {([
                        ['all', `All (${stats.total})`],
                        ['replicated', `Replicated (${stats.replicated})`],
                        ['fdw', `FDW (${stats.fdw})`],
                        ['none', `None (${stats.none})`],
                    ] as [FilterTab, string][]).map(([key, label]) => (
                        <button
                            key={key}
                            className="btn"
                            style={{
                                background: filter === key ? 'var(--primary)' : 'transparent',
                                color: filter === key ? '#111' : 'var(--text)',
                                border: `1px solid ${filter === key ? 'transparent' : 'var(--border)'}`,
                                padding: '6px 12px',
                                fontSize: 13,
                            }}
                            onClick={() => setFilter(key)}
                        >
                            {label}
                        </button>
                    ))}
                </div>
            </div>

            <div className="card" style={{ marginTop: 12, padding: 0, overflow: 'hidden' }}>
                <div style={{
                    display: 'grid',
                    gridTemplateColumns: '40px 1fr 100px 100px 100px 100px',
                    padding: '10px 12px',
                    borderBottom: '1px solid var(--border)',
                    fontWeight: 600,
                    fontSize: 13,
                    alignItems: 'center',
                }}>
                    <div>
                        <input
                            type="checkbox"
                            checked={allFilteredSelected}
                            onChange={toggleSelectAll}
                            style={{ cursor: 'pointer' }}
                        />
                    </div>
                    <div>Table</div>
                    <div style={{ textAlign: 'center' }}>Publication</div>
                    <div style={{ textAlign: 'center' }}>Subscriber</div>
                    <div style={{ textAlign: 'center' }} title="Foreign Data Wrapper (live remote read)">FDW</div>
                    <div style={{ textAlign: 'right' }}>Est. Rows</div>
                </div>

                {loading && filtered.length === 0 && (
                    <div style={{ padding: 24, textAlign: 'center', opacity: 0.7 }}>Loading...</div>
                )}
                {!loading && filtered.length === 0 && (
                    <div style={{ padding: 24, textAlign: 'center', opacity: 0.7 }}>No tables found</div>
                )}

                <div style={{ maxHeight: 'calc(100vh - 360px)', overflowY: 'auto' }}>
                    {filtered.map((t) => {
                        const fqn = `${t.schema}.${t.table}`
                        const isSelected = selected.has(fqn)
                        return (
                            <div
                                key={fqn}
                                onClick={() => toggleSelect(fqn)}
                                style={{
                                    display: 'grid',
                                    gridTemplateColumns: '40px 1fr 100px 100px 100px 100px',
                                    padding: '8px 12px',
                                    borderBottom: '1px solid var(--border)',
                                    cursor: 'pointer',
                                    alignItems: 'center',
                                    fontSize: 13,
                                    background: isSelected ? 'rgba(255,255,255,0.04)' : 'transparent',
                                    transition: 'background .1s',
                                }}
                            >
                                <div>
                                    <input
                                        type="checkbox"
                                        checked={isSelected}
                                        onChange={() => toggleSelect(fqn)}
                                        onClick={(e) => e.stopPropagation()}
                                        style={{ cursor: 'pointer' }}
                                    />
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'monospace' }}>
                                    {(() => {
                                        const mode = tableMode(t, fdwSet)
                                        const label = mode === 'replicated' ? 'Replicated' : mode === 'fdw' ? 'FDW' : 'None'
                                        const color = mode === 'replicated' ? '#4ade80' : mode === 'fdw' ? '#c084fc' : 'var(--muted)'
                                        const borderColor = mode === 'replicated' ? '#22c55e33' : mode === 'fdw' ? '#a855f733' : 'var(--border)'
                                        return (
                                            <span className="badge" style={{ color, borderColor, minWidth: 76, textAlign: 'center' }}>
                                                {label}
                                            </span>
                                        )
                                    })()}
                                    <span>{fqn}</span>
                                </div>
                                <div style={{ textAlign: 'center' }}>
                                    <span className="badge" style={{
                                        color: t.in_publication
                                            ? (t.pub_via === 'schema' ? '#fbbf24' : '#4ade80')
                                            : 'var(--muted)',
                                        borderColor: t.in_publication
                                            ? (t.pub_via === 'schema' ? '#f59e0b33' : '#22c55e33')
                                            : 'var(--border)',
                                    }}>
                                        {t.in_publication ? (t.pub_via === 'schema' ? 'Schema' : 'Yes') : 'No'}
                                    </span>
                                </div>
                                <div style={{ textAlign: 'center' }}>
                                    <span className="badge" style={{
                                        color: t.in_subscriber ? '#60a5fa' : 'var(--muted)',
                                        borderColor: t.in_subscriber ? '#3b82f633' : 'var(--border)',
                                    }}>
                                        {t.in_subscriber ? 'Yes' : 'No'}
                                    </span>
                                </div>
                                <div style={{ textAlign: 'center' }}>
                                    {fdwSet.has(fqn) ? (
                                        <span className="badge" style={{ color: '#c084fc', borderColor: '#a855f733' }}>FDW</span>
                                    ) : (
                                        <span className="badge" style={{ color: 'var(--muted)', borderColor: 'var(--border)' }}>No</span>
                                    )}
                                </div>
                                <div style={{ textAlign: 'right', fontFamily: 'monospace', opacity: 0.8 }}>
                                    {formatRows(t.estimated_rows)}
                                </div>
                            </div>
                        )
                    })}
                </div>
            </div>

            {/* Action bar */}
            <div style={{
                display: 'flex',
                gap: 8,
                marginTop: 12,
                alignItems: 'center',
                flexWrap: 'wrap',
            }}>
                <span className="subtle">
                    {selected.size} selected
                </span>
                <button
                    className="btn"
                    disabled={selectedNotInPub.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'add', tables: selectedNotInPub })}
                >
                    Add to Publication ({selectedNotInPub.length})
                </button>
                <button
                    className="btn btn-danger"
                    disabled={selectedInPub.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'remove', tables: selectedInPub })}
                    title={selectedSchemaLevel.length > 0 ? `${selectedSchemaLevel.length} schema-level table(s) cannot be removed individually` : undefined}
                >
                    Remove from Publication ({selectedInPub.length})
                    {selectedSchemaLevel.length > 0 && (
                        <span style={{ color: '#fbbf24', marginLeft: 4, fontSize: 12 }}>
                            ({selectedSchemaLevel.length} schema-level excluded)
                        </span>
                    )}
                </button>
                <button
                    className="btn"
                    disabled={refreshLoading}
                    onClick={onRefresh}
                >
                    {refreshLoading ? 'Refreshing...' : 'Refresh Subscription'}
                </button>
                <span style={{ width: 1, height: 24, background: 'var(--border)', margin: '0 4px' }} />
                <button
                    className="btn"
                    disabled={selectedFdwAddable.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'fdw_add', tables: selectedFdwAddable })}
                    title="Map selected tables as postgres_fdw foreign tables (live remote read). Cannot coexist with publication for the same table."
                >
                    Add to FDW ({selectedFdwAddable.length})
                </button>
                <button
                    className="btn btn-danger"
                    disabled={selectedFdwRemovable.length === 0 || actionLoading}
                    onClick={() => setConfirmAction({ type: 'fdw_remove', tables: selectedFdwRemovable })}
                >
                    Remove from FDW ({selectedFdwRemovable.length})
                </button>
            </div>

            {/* Confirm dialog */}
            {confirmAction && (
                <div style={{
                    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
                    display: 'grid', placeItems: 'center', zIndex: 50,
                }}>
                    <div className="card" style={{ minWidth: 400, maxWidth: 560 }}>
                        <h3 style={{ marginTop: 0 }}>
                            {confirmAction.type === 'add' && 'Add Tables to Publication'}
                            {confirmAction.type === 'remove' && 'Remove Tables from Publication'}
                            {confirmAction.type === 'fdw_add' && 'Add Tables to FDW'}
                            {confirmAction.type === 'fdw_remove' && 'Remove Tables from FDW'}
                        </h3>
                        <p className="subtle" style={{ margin: '8px 0' }}>
                            {confirmAction.type === 'add' &&
                                'The following tables will be added to the publication and the subscription will be refreshed.'}
                            {confirmAction.type === 'remove' &&
                                'The following tables will be removed from the publication and the subscription will be refreshed.'}
                            {confirmAction.type === 'fdw_add' &&
                                'The following tables will be mapped as live foreign tables. Existing local tables with the same names will be dropped (their row data is presumed empty). configs/fdw.yaml will be updated.'}
                            {confirmAction.type === 'fdw_remove' &&
                                'The following foreign-table mappings will be removed. configs/fdw.yaml will be updated.'}
                        </p>
                        <div style={{
                            maxHeight: 200, overflowY: 'auto',
                            background: '#101010', borderRadius: 8, padding: 8, margin: '8px 0',
                            fontFamily: 'monospace', fontSize: 13,
                        }}>
                            {confirmAction.tables.map((t) => (
                                <div key={t}>{t}</div>
                            ))}
                        </div>
                        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 12 }}>
                            <button className="btn" onClick={() => setConfirmAction(null)} disabled={actionLoading}>
                                Cancel
                            </button>
                            <button
                                className={confirmAction.type === 'remove' || confirmAction.type === 'fdw_remove' ? 'btn btn-danger' : 'btn'}
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
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    )
}
