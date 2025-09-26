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
    is_running: boolean
    container_started_at: string | null
    description?: string | null
}

interface ReplicationLag {
    network_lag_seconds: number
    apply_lag_seconds: number
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

    const [lag, setLag] = useState<ReplicationLag | null>(null)
    const [lagLoading, setLagLoading] = useState(false)
    const [lagError, setLagError] = useState<string | null>(null)

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

    const loadLag = () => {
        setLagLoading(true)
        setLagError(null)
        fetch(`${base}/replication/lag`)
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((data: ReplicationLag) => setLag(data))
            .catch(async (e) => {
                const text = e?.status ? `${e.status} ${await e.text()}` : String(e)
                setLagError(text)
            })
            .finally(() => setLagLoading(false))
    }

    useEffect(() => {
        loadHealth()
        loadClones()
        loadSnapshots()
        loadLag()
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // Auto refresh replication lag every 5 seconds
    useEffect(() => {
        const id = setInterval(() => {
            loadLag()
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
        <div style={{ padding: 16, fontFamily: 'system-ui, sans-serif' }}>
            <h1>Snaplicator</h1>

            <section style={{ marginTop: 16 }}>
                <h2>Replication Lag</h2>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
                    <button onClick={loadLag} disabled={lagLoading}>
                        {lagLoading ? 'Refreshing...' : 'Refresh'}
                    </button>
                </div>
                {lagError && <p style={{ color: 'red' }}>{lagError}</p>}
                {lag && (
                    <div style={{ display: 'flex', gap: 16 }}>
                        <div>Network lag: <strong>{lag.network_lag_seconds.toFixed(3)}</strong> s</div>
                        <div>Apply lag: <strong>{lag.apply_lag_seconds.toFixed(3)}</strong> s</div>
                    </div>
                )}
            </section>

            <section style={{ marginTop: 16 }}>
                <h2>Clones</h2>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
                    <button onClick={loadClones} disabled={clonesLoading}>
                        {clonesLoading ? 'Refreshing...' : 'Refresh'}
                    </button>
                    <input
                        value={mainCloneDesc}
                        onChange={(e) => setMainCloneDesc(e.target.value)}
                        placeholder="Clone from main: description (optional)"
                        style={{ padding: 6, border: '1px solid #ccc', borderRadius: 4, minWidth: 260 }}
                    />
                    <button onClick={onCloneFromMain} disabled={mainCloning}>
                        {mainCloning ? 'Cloning...' : 'Clone from Main'}
                    </button>
                </div>
                {clonesError && <p style={{ color: 'red' }}>{clonesError}</p>}
                <ul style={{ marginTop: 8 }}>
                    {clones.length === 0 && <li style={{ opacity: 0.7 }}>No clones</li>}
                    {clones.map((c) => (
                        <li key={c.path} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
                            <span>{c.name}</span>
                            {c.description && (
                                <span style={{ opacity: 0.7 }} title={c.description}>– {c.description}</span>
                            )}
                            {c.has_container ? (
                                <>
                                    <span style={{ color: c.is_running ? 'green' : '#999' }}>[{c.is_running ? 'running' : 'stopped'}]</span>
                                    <span style={{ opacity: 0.7 }}>{c.container_name}</span>
                                    <span style={{ opacity: 0.7 }}>{c.container_status}</span>
                                    <span style={{ opacity: 0.7 }}>{c.container_ports}</span>
                                    {c.container_started_at && (
                                        <span style={{ opacity: 0.7 }}>started: {new Date(c.container_started_at).toLocaleString()}</span>
                                    )}
                                    {c.container_name && (
                                        <button onClick={() => onDelete(c.container_name || '')} disabled={deletingBusy} style={{ marginLeft: 8 }}>
                                            Delete
                                        </button>
                                    )}
                                </>
                            ) : (
                                <span style={{ color: '#999' }}>[stopped]</span>
                            )}
                        </li>
                    ))}
                </ul>
            </section>

            <section style={{ marginTop: 16 }}>
                <h2>Snapshots</h2>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
                    <button onClick={loadSnapshots} disabled={loading}>
                        {loading ? 'Refreshing...' : 'Refresh'}
                    </button>
                    <input
                        value={snapshotDesc}
                        onChange={(e) => setSnapshotDesc(e.target.value)}
                        placeholder="Description (optional)"
                        style={{ padding: 6, border: '1px solid #ccc', borderRadius: 4, minWidth: 240 }}
                    />
                    <button onClick={onCreateSnapshot} disabled={creating}>
                        {creating ? 'Creating...' : 'Create Snapshot'}
                    </button>
                </div>
                {message && <p style={{ color: 'green' }}>{message}</p>}
                {error && <p style={{ color: 'red' }}>{error}</p>}
                <ul style={{ marginTop: 8 }}>
                    {items.map((it) => (
                        <li key={it.name} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
                            <span>{it.name} {it.readonly ? '(ro)' : ''}</span>
                            {it.description && (
                                <span style={{ opacity: 0.7 }} title={it.description}>– {it.description}</span>
                            )}
                            <input placeholder="Clone description (optional)" style={{ padding: 4, border: '1px solid #ccc', borderRadius: 4 }} id={`clone-desc-${it.name}`} />
                            <button onClick={() => {
                                const el = document.getElementById(`clone-desc-${it.name}`) as HTMLInputElement | null
                                const desc = el?.value || ''
                                onClone(it.name, desc)
                            }} disabled={cloning === it.name}>
                                {cloning === it.name ? 'Cloning...' : 'Clone'}
                            </button>
                            <button onClick={() => confirmDeleteSnapshot(it.name)} style={{ marginLeft: 8 }}>
                                Delete
                            </button>
                        </li>
                    ))}
                </ul>
            </section>

            {deleting && (
                <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', display: 'grid', placeItems: 'center', zIndex: 50 }}>
                    <div style={{ background: 'white', color: '#111', padding: 16, borderRadius: 8, minWidth: 320 }}>
                        <h3 style={{ marginTop: 0 }}>Delete clone</h3>
                        <p style={{ margin: '8px 0' }}>컨테이너와 연결된 btrfs 서브볼륨이 함께 삭제됩니다.</p>
                        <p style={{ margin: '8px 0' }}>
                            삭제 대상: <strong>{deleting}</strong>
                        </p>
                        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 12 }}>
                            <button onClick={() => setDeleting(null)} disabled={deletingBusy}>Cancel</button>
                            <button onClick={confirmDelete} disabled={deletingBusy} style={{ background: '#dc2626', color: 'white', padding: '6px 10px', borderRadius: 4 }}>
                                {deletingBusy ? 'Deleting...' : 'Delete'}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    )
} 