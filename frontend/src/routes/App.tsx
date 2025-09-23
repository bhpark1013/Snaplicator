import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

interface SnapshotItem {
    name: string
    path: string
    readonly: boolean
}

interface CloneItem {
    id: string
    name: string
    ports: string
    status: string
    labels: string
    is_replica: boolean
    is_clone: boolean
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

    useEffect(() => {
        loadHealth()
        loadClones()
        loadSnapshots()
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    const onCreateSnapshot = async () => {
        setCreating(true)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/snapshots`, { method: 'POST' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const created: SnapshotItem = await r.json()
            setMessage(`Created snapshot: ${created.name}`)
            loadSnapshots()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setCreating(false)
        }
    }

    const onClone = async (name: string) => {
        setCloning(name)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(name)}/clone`, { method: 'POST' })
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

    return (
        <div style={{ padding: 16, fontFamily: 'system-ui, sans-serif' }}>
            <h1>Snaplicator</h1>
            <nav style={{ display: 'flex', gap: 12 }}>
                <Link to="/">Home</Link>
            </nav>
            <p style={{ marginTop: 8 }}>API Base: {api || 'proxy(/api→8000)'}
            </p>

            <section style={{ marginTop: 16 }}>
                <h2>Health</h2>
                <p>Status: <strong style={{ color: health === 'ok' ? 'green' : 'red' }}>{health}</strong></p>
            </section>

            <section style={{ marginTop: 16 }}>
                <h2>Clones</h2>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
                    <button onClick={loadClones} disabled={clonesLoading}>
                        {clonesLoading ? 'Refreshing...' : 'Refresh'}
                    </button>
                </div>
                {clonesError && <p style={{ color: 'red' }}>{clonesError}</p>}
                <ul style={{ marginTop: 8 }}>
                    {clones.map((c) => (
                        <li key={c.id} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
                            <span>{c.name}</span>
                            {c.is_replica && <span style={{ color: 'blue' }}>[replica]</span>}
                            {c.is_clone && <span style={{ color: 'green' }}>[clone]</span>}
                            <span style={{ opacity: 0.7 }}>{c.status}</span>
                            <span style={{ opacity: 0.7 }}>{c.ports}</span>
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
                            <button onClick={() => onClone(it.name)} disabled={cloning === it.name}>
                                {cloning === it.name ? 'Cloning...' : 'Clone'}
                            </button>
                        </li>
                    ))}
                </ul>
            </section>
        </div>
    )
} 