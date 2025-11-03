import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

interface SnapshotItem {
    name: string
    path: string
    readonly: boolean
    description?: string | null
}

interface CloneItem {
    name: string
    path: string
    is_btrfs: boolean
    has_container: boolean
    container_name: string | null
    container_status: string | null
    container_ports: string | null
    host_port?: number | null
    is_running: boolean
    container_started_at: string | null
    description?: string | null
}


interface CopyProgress {
    status: 'idle' | 'copying' | 'complete'
    total_tables: number
    finished_tables: number
    percent: number
    active?: { schema: string; table: string; bytes_processed?: number; bytes_total?: number; percent?: number | null }[] | null
    details?: { state: string; schema: string; table: string }[] | null
}

interface ReplicationCheckSide {
    ok: boolean
    output?: string | null
    error?: string | null
}

interface ReplicationCheckResult {
    sql?: string | null
    publisher: ReplicationCheckSide
    subscriber: ReplicationCheckSide
}

export function App() {
    const [health, setHealth] = useState<string>('unknown')
    const [items, setItems] = useState<SnapshotItem[]>([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [creating, setCreating] = useState(false)
    const [cloning, setCloning] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)

    const [clones, setClones] = useState<CloneItem[]>([])
    const [clonesLoading, setClonesLoading] = useState(false)
    const [clonesError, setClonesError] = useState<string | null>(null)


    const [copy, setCopy] = useState<CopyProgress | null>(null)
    const [copyError, setCopyError] = useState<string | null>(null)

    const [check, setCheck] = useState<ReplicationCheckResult | null>(null)
    const [checkLoading, setCheckLoading] = useState(false)
    const [checkError, setCheckError] = useState<string | null>(null)

    const [deleting, setDeleting] = useState<string | null>(null)
    const [deletingBusy, setDeletingBusy] = useState(false)

    const [snapshotDesc, setSnapshotDesc] = useState('')
    const [mainCloneDesc, setMainCloneDesc] = useState('')
    const [mainCloning, setMainCloning] = useState(false)

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const loadHealth = async () => {
        try {
            const r = await fetch(`${base}/health`)
            if (!r.ok) throw new Error(`${r.status}`)
            const data = await r.json()
            setHealth(data?.status || 'ok')
        } catch {
            setHealth('down')
        }
    }

    const loadSnapshots = () => {
        setLoading(true)
        setError(null)
        fetch(`${base}/snapshots`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data) => setItems(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setError(text)
            })
            .finally(() => setLoading(false))
    }

    const loadClones = () => {
        setClonesLoading(true)
        setClonesError(null)
        fetch(`${base}/clones`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data) => setClones(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setClonesError(text)
            })
            .finally(() => setClonesLoading(false))
    }


    const loadCopy = () => {
        fetch(`${base}/replication/copy-progress`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: CopyProgress) => setCopy(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setCopyError(text)
            })
    }

    const runCheck = () => {
        setCheckLoading(true)
        setCheckError(null)
        fetch(`${base}/replication/check`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: ReplicationCheckResult) => setCheck(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setCheckError(text)
            })
            .finally(() => setCheckLoading(false))
    }

    useEffect(() => {
        loadHealth()
        loadClones()
        loadSnapshots()
        loadCopy()
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // Auto refresh copy progress every 5 seconds
    useEffect(() => {
        const id = setInterval(() => {
            loadCopy()
        }, 5000)
        return () => clearInterval(id)
    }, [])

    const onCreateSnapshot = async () => {
        setCreating(true)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: snapshotDesc || null }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const created: SnapshotItem = await r.json()
            setMessage(`Created snapshot: ${created.name}`)
            setSnapshotDesc('')
            loadSnapshots()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setCreating(false)
        }
    }

    const onClone = async (name: string, description: string) => {
        setCloning(name)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(name)}/clone`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: description || null }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Cloned: ${res.clone_subvolume} -> container ${res.container_name} (port ${res.host_port})`)
            loadClones()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setCloning(null)
        }
    }

    const onCloneFromMain = async () => {
        setMainCloning(true)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/clones`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: mainCloneDesc || null }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Cloned from main: ${res.clone_subvolume} -> container ${res.container_name} (port ${res.host_port})`)
            setMainCloneDesc('')
            loadClones()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setMainCloning(false)
        }
    }

    const onDelete = (containerName: string) => {
        setDeleting(containerName)
        setMessage(null)
        setError(null)
    }

    const confirmDelete = async () => {
        if (!deleting) return
        setDeletingBusy(true)
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(deleting)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Deleted ${res.containers_removed?.join(', ') || deleting} and subvolume ${res.subvolume_deleted}`)
            loadClones()
            setDeleting(null)
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setDeletingBusy(false)
        }
    }

    const confirmDeleteSnapshot = async (name: string) => {
        if (!window.confirm(`Delete snapshot ${name}?`)) return
        setError(null)
        setMessage(null)
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(name)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Deleted snapshot subvolume: ${res.subvolume_deleted}`)
            loadSnapshots()
        } catch (e: any) {
            setError(String(e?.message || e))
        }
    }

    return (
        <div className="container">
            <div className="header">
                <div className="title">Snaplicator</div>
                <div className="row">
                    <button className="btn" onClick={loadHealth}>Health</button>
                    <button className="btn" onClick={loadClones} disabled={clonesLoading}>{clonesLoading ? 'Refreshing...' : 'Refresh Clones'}</button>
                    <button className="btn" onClick={loadSnapshots} disabled={loading}>{loading ? 'Refreshing...' : 'Refresh Snapshots'}</button>
                </div>
            </div>

            <section className="card" style={{ marginTop: 16 }}>
                <h2>Initial Copy</h2>
                {copy && (
                    <div style={{ marginTop: 8 }}>
                        <div>Initial copy status: <strong>{copy.status}</strong></div>
                        {copy.total_tables > 0 && (
                            <div style={{ marginTop: 4 }}>
                                <div>{copy.finished_tables} / {copy.total_tables} tables ({copy.percent.toFixed(1)}%)</div>
                                {copy.active && copy.active.length > 0 && (
                                    <ul style={{ marginTop: 4 }}>
                                        {copy.active.slice(0, 3).map((a, i) => (
                                            <li key={i} style={{ opacity: 0.8 }}>
                                                {a.schema}.{a.table}
                                                {typeof a.percent === 'number' ? ` – ${a.percent.toFixed(1)}%` : ''}
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </div>
                        )}
                        {copyError && <p style={{ color: 'red' }}>{copyError}</p>}
                    </div>
                )}
            </section>

            <section className="card" style={{ marginTop: 16 }}>
                <h2>Replication Check</h2>
                <div className="row" style={{ margin: '8px 0' }}>
                    <button className="btn" onClick={runCheck} disabled={checkLoading}>
                        {checkLoading ? 'Running...' : 'Run Check SQL'}
                    </button>
                </div>
                {checkError && <p style={{ color: 'red' }}>{checkError}</p>}
                {check && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 8 }}>
                        {typeof check.sql === 'string' && (
                            <div>
                                <div style={{ fontWeight: 600, marginBottom: 4 }}>SQL</div>
                                <pre>
                                    {check.sql.trim()}
                                </pre>
                            </div>
                        )}
                        <div style={{ display: 'flex', gap: 24 }}>
                            <div>
                                <div style={{ fontWeight: 600 }}>Publisher</div>
                                {check.publisher.ok ? (
                                    <pre>
                                        {String(check.publisher.output || '').trim()}
                                    </pre>
                                ) : (
                                    <pre style={{ color: '#fca5a5' }}>
                                        {String(check.publisher.error || 'Error')}
                                    </pre>
                                )}
                            </div>
                            <div>
                                <div style={{ fontWeight: 600 }}>Subscriber</div>
                                {check.subscriber.ok ? (
                                    <pre>
                                        {String(check.subscriber.output || '').trim()}
                                    </pre>
                                ) : (
                                    <pre style={{ color: '#fca5a5' }}>
                                        {String(check.subscriber.error || 'Error')}
                                    </pre>
                                )}
                            </div>
                        </div>
                    </div>
                )}
            </section>

            <section className="card" style={{ marginTop: 16 }}>
                <h2>Clones</h2>
                <div className="row" style={{ margin: '8px 0' }}>
                    <button className="btn" onClick={loadClones} disabled={clonesLoading}>
                        {clonesLoading ? 'Refreshing...' : 'Refresh'}
                    </button>
                    <input className="input"
                        value={mainCloneDesc}
                        onChange={(e) => setMainCloneDesc(e.target.value)}
                        placeholder="Clone from main: description (optional)"
                    />
                    <button className="btn" onClick={onCloneFromMain} disabled={mainCloning}>
                        {mainCloning ? 'Cloning...' : 'Clone from Main'}
                    </button>
                </div>
                {clonesError && <p style={{ color: 'red' }}>{clonesError}</p>}
                <ul className="list" style={{ marginTop: 8 }}>
                    {clones.length === 0 && <li style={{ opacity: 0.7 }}>No clones</li>}
                    {clones.map((c) => {
                        const targetName = c.container_name || c.name
                        return (
                            <li key={c.path} style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 8, alignItems: 'center' }}>
                                <div style={{ display: 'grid', gap: 4 }}>
                                    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                                        <span style={{ fontWeight: 700 }}>{c.name}</span>
                                        {c.has_container ? (
                                            <span className="badge" style={{ color: c.is_running ? '#22c55e' : '#9ca3af' }}>
                                                {c.is_running ? 'running' : 'stopped'}
                                            </span>
                                        ) : (
                                            <span className="badge">no-container</span>
                                        )}
                                        {typeof c.host_port === 'number' && (
                                            <span className="badge">port {c.host_port}</span>
                                        )}
                                        {c.container_started_at && (
                                            <span className="badge">started {new Date(c.container_started_at).toLocaleString()}</span>
                                        )}
                                    </div>
                                    <div className="subtle" style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                                        {c.description && <span title={c.description}>{c.description}</span>}
                                        {c.container_status && <span>status: {c.container_status}</span>}
                                    </div>
                                </div>
                                <div style={{ display: 'flex', gap: 8 }}>
                                    <button className="btn btn-danger" onClick={() => onDelete(targetName)} disabled={deletingBusy}>Delete</button>
                                </div>
                            </li>
                        )
                    })}
                </ul>
            </section>

            <section className="card" style={{ marginTop: 16 }}>
                <h2>Snapshots</h2>
                <div className="row" style={{ margin: '8px 0' }}>
                    <button className="btn" onClick={loadSnapshots} disabled={loading}>
                        {loading ? 'Refreshing...' : 'Refresh'}
                    </button>
                    <input className="input"
                        value={snapshotDesc}
                        onChange={(e) => setSnapshotDesc(e.target.value)}
                        placeholder="Description (optional)"
                    />
                    <button className="btn" onClick={onCreateSnapshot} disabled={creating}>
                        {creating ? 'Creating...' : 'Create Snapshot'}
                    </button>
                </div>
                {message && <p style={{ color: 'green' }}>{message}</p>}
                {error && <p style={{ color: 'red' }}>{error}</p>}
                <ul className="list" style={{ marginTop: 8 }}>
                    {items.map((it) => (
                        <li key={it.name} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
                            <span>{it.name} {it.readonly ? '(ro)' : ''}</span>
                            {it.description && (
                                <span style={{ opacity: 0.7 }} title={it.description}>– {it.description}</span>
                            )}
                            <input className="input" placeholder="Clone description (optional)" id={`clone-desc-${it.name}`} />
                            <button className="btn" onClick={() => {
                                const el = document.getElementById(`clone-desc-${it.name}`) as HTMLInputElement | null
                                const desc = el?.value || ''
                                onClone(it.name, desc)
                            }} disabled={cloning === it.name}>
                                {cloning === it.name ? 'Cloning...' : 'Clone'}
                            </button>
                            <button className="btn btn-danger" onClick={() => confirmDeleteSnapshot(it.name)}>
                                Delete
                            </button>
                        </li>
                    ))}
                </ul>
            </section>

            {deleting && (
                <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', display: 'grid', placeItems: 'center', zIndex: 50 }}>
                    <div className="card" style={{ minWidth: 320 }}>
                        <h3 style={{ marginTop: 0 }}>Delete clone</h3>
                        <p className="subtle" style={{ margin: '8px 0' }}>컨테이너와 연결된 btrfs 서브볼륨이 함께 삭제됩니다.</p>
                        <p style={{ margin: '8px 0' }}>
                            삭제 대상: <strong>{deleting}</strong>
                        </p>
                        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 12 }}>
                            <button className="btn" onClick={() => setDeleting(null)} disabled={deletingBusy}>Cancel</button>
                            <button className="btn btn-danger" onClick={confirmDelete} disabled={deletingBusy}>
                                {deletingBusy ? 'Deleting...' : 'Delete'}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    )
} 