import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

interface CloneDetailResponse {
    name: string
    path: string
    is_btrfs: boolean
    has_container: boolean
    container_name?: string | null
    container_status?: string | null
    container_ports?: string | null
    host_port?: number | null
    is_running: boolean
    container_started_at?: string | null
    description?: string | null
    metadata?: Record<string, unknown> | null
    readonly?: boolean
    created_at?: string | null
    refreshed_at?: string | null
    reset_at?: string | null
    reset_from_snapshot?: string | null
}

interface CloneSnapshotItem {
    name: string
    path: string
    readonly: boolean
    description?: string | null
    metadata?: Record<string, unknown> | null
}

interface SnapshotListItem {
    name: string
    path: string
    readonly: boolean
    description?: string | null
}

interface CloneUsageSummary {
    usage_bytes?: number | null
    fs_size_bytes?: number | null
    fs_used_bytes?: number | null
    calculated_at?: string | null
}

export function CloneDetail() {
    const { cloneId = '' } = useParams<{ cloneId: string }>()
    const navigate = useNavigate()

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const [detail, setDetail] = useState<CloneDetailResponse | null>(null)
    const [cloneSnapshots, setCloneSnapshots] = useState<CloneSnapshotItem[]>([])
    const [allSnapshots, setAllSnapshots] = useState<SnapshotListItem[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)
    const [actionBusy, setActionBusy] = useState(false)
    const [usage, setUsage] = useState<CloneUsageSummary | null>(null)
    const [overviewExpanded, setOverviewExpanded] = useState(false)

    const toggleOverview = useCallback(() => {
        setOverviewExpanded((prev) => !prev)
    }, [])

    const handleOverviewKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggleOverview()
        }
    }, [toggleOverview])

    const formatBytes = useCallback((n?: number | null) => {
        if (n == null || isNaN(n)) return '-'
        const units = ['B', 'KB', 'MB', 'GB', 'TB']
        let v = n
        let i = 0
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024
            i++
        }
        return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
    }, [])

    const fetchDetail = useCallback(async () => {
        if (!cloneId) return
        setLoading(true)
        setError(null)
        try {
            const encoded = encodeURIComponent(cloneId)
            const r = await fetch(`${base}/clones/${encoded}`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: CloneDetailResponse = await r.json()
            setDetail(data)
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setLoading(false)
        }
    }, [base, cloneId])

    const fetchCloneSnapshots = useCallback(async () => {
        if (!cloneId) return
        try {
            const encoded = encodeURIComponent(cloneId)
            const r = await fetch(`${base}/clones/${encoded}/snapshots`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: CloneSnapshotItem[] = await r.json()
            setCloneSnapshots(data)
        } catch (e: any) {
            setCloneSnapshots([])
            setError((prev) => prev ?? String(e?.message || e))
        }
    }, [base, cloneId])

    const fetchAllSnapshots = useCallback(async () => {
        try {
            const r = await fetch(`${base}/snapshots`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: SnapshotListItem[] = await r.json()
            setAllSnapshots(data)
        } catch (e: any) {
            setAllSnapshots([])
            setError((prev) => prev ?? String(e?.message || e))
        }
    }, [base])

    const fetchUsage = useCallback(async () => {
        if (!cloneId) return
        try {
            const encoded = encodeURIComponent(cloneId)
            const r = await fetch(`${base}/clones/${encoded}/usage`)
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const data: CloneUsageSummary = await r.json()
            setUsage(data)
        } catch (e: any) {
            setUsage(null)
            setError((prev) => prev ?? String(e?.message || e))
        }
    }, [base, cloneId])

    useEffect(() => {
        fetchDetail()
        fetchCloneSnapshots()
        fetchAllSnapshots()
        fetchUsage()
    }, [fetchDetail, fetchCloneSnapshots, fetchAllSnapshots, fetchUsage])

    useEffect(() => {
        setOverviewExpanded(false)
    }, [cloneId])

    const onRefresh = useCallback(async () => {
        if (!detail) return
        if (!detail.container_name) {
            setError('컨테이너가 존재하지 않아 최신화할 수 없습니다.')
            return
        }
        const input = window.prompt('새 description을 입력하세요. 변경하지 않으려면 그대로 두고 OK를 누르세요.', detail.description || '')
        if (input === null) return
        const trimmed = input.trim()

        setActionBusy(true)
        setMessage(null)
        setError(null)
        try {
            const encoded = encodeURIComponent(detail.container_name)
            const r = await fetch(`${base}/clones/${encoded}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(trimmed ? { description: trimmed } : {}),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Refreshed ${res.refreshed_container}`)
            await Promise.all([fetchDetail(), fetchCloneSnapshots(), fetchUsage()])
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, detail, fetchCloneSnapshots, fetchDetail, fetchUsage])

    const onDelete = useCallback(async () => {
        if (!detail) return
        if (!window.confirm('정말 이 클론을 삭제할까요? 컨테이너와 서브볼륨이 모두 삭제됩니다.')) return
        setActionBusy(true)
        setError(null)
        setMessage(null)
        try {
            const target = detail.container_name || detail.name
            const r = await fetch(`${base}/clones/${encodeURIComponent(target)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            navigate('/')
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, detail, navigate])

    const onCreateSnapshot = useCallback(async () => {
        if (!cloneId) return
        let input = window.prompt('스냅샷 description을 입력하세요 (필수).', detail?.description || '')
        if (input === null) return
        input = input.trim()
        if (!input) {
            alert('설명을 입력해 주세요.')
            return
        }

        setActionBusy(true)
        setError(null)
        setMessage(null)
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: input }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const created = await r.json()
            setMessage(`Snapshot created: ${created.name}`)
            await fetchCloneSnapshots()
            await fetchAllSnapshots()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, fetchCloneSnapshots, fetchAllSnapshots])

    const onReset = useCallback(async (snapshotName: string) => {
        const ok = window.confirm(`스냅샷 ${snapshotName} 으로 클론을 되돌릴까요? 컨테이너가 재기동됩니다.`)
        if (!ok) return

        setActionBusy(true)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(cloneId)}/reset`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ snapshot_name: snapshotName }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Reset to snapshot ${res.snapshot_used}`)
            await Promise.all([fetchDetail(), fetchCloneSnapshots(), fetchUsage()])
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setActionBusy(false)
        }
    }, [base, cloneId, fetchCloneSnapshots, fetchDetail, fetchUsage])

    const metadataEntries = useMemo(() => {
        const meta = detail?.metadata
        if (!meta) return [] as Array<[string, unknown]>
        return Object.entries(meta)
    }, [detail])

    return (
        <div className="container">
            <div className="header">
                <div className="title">Clone Detail</div>
                <div className="row">
                    <Link className="btn" to="/">← Back</Link>
                    <button className="btn" onClick={() => { fetchDetail(); fetchCloneSnapshots(); fetchAllSnapshots(); }}>
                        Refresh Data
                    </button>
                </div>
            </div>

            {loading && <p>Loading...</p>}
            {error && <p style={{ color: 'red' }}>{error}</p>}
            {message && <p style={{ color: 'green' }}>{message}</p>}

            {detail && (
                <section className="card" style={{ marginTop: 16 }}>
                    <h2>Overview</h2>
                    <div
                        role="button"
                        tabIndex={0}
                        onClick={toggleOverview}
                        onKeyDown={handleOverviewKeyDown}
                        style={{
                            marginTop: 8,
                            padding: 12,
                            borderRadius: 8,
                            border: '1px solid #e5e7eb',
                            background: '#ffffff',
                            display: 'grid',
                            gap: 6,
                            cursor: 'pointer',
                            transition: 'box-shadow 0.15s ease, border-color 0.15s ease',
                            boxShadow: overviewExpanded ? '0 4px 12px rgba(15, 23, 42, 0.08)' : 'none',
                        }}
                    >
                        <div style={{ fontSize: 16, fontWeight: 700, color: '#0f172a' }}>{detail.name}</div>
                        <div style={{ color: '#475569' }}>{detail.description?.trim() ? detail.description : '(no description)'}</div>
                        <div style={{ color: '#475569' }}>Port: {detail.host_port ?? 'N/A'}</div>
                        <div style={{ fontSize: 12, color: '#94a3b8' }}>
                            {overviewExpanded ? 'Click to collapse details' : 'Click to expand details'}
                        </div>
                    </div>

                    {overviewExpanded && (
                        <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
                            <div><strong>Container:</strong> {detail.container_name || '없음'}</div>
                            <div><strong>Subvolume:</strong> {detail.name}</div>
                            <div><strong>Path:</strong> {detail.path}</div>
                            <div><strong>Status:</strong> {detail.container_status || 'unknown'}</div>
                            <div><strong>Created:</strong> {detail.created_at ? new Date(detail.created_at).toLocaleString() : 'N/A'}</div>
                            <div><strong>Usage:</strong> {formatBytes(usage?.usage_bytes)}</div>
                            {usage?.calculated_at && (
                                <div><strong>Measured:</strong> {new Date(usage.calculated_at).toLocaleString()}</div>
                            )}
                            {metadataEntries.length > 0 && (
                                <div>
                                    <h3 style={{ marginTop: 8, marginBottom: 4 }}>Metadata</h3>
                                    <pre style={{ maxHeight: 200, overflow: 'auto' }}>{JSON.stringify(detail.metadata, null, 2)}</pre>
                                </div>
                            )}
                        </div>
                    )}

                    <div className="row" style={{ marginTop: 16, gap: 8, flexWrap: 'wrap' }}>
                        <button className="btn" onClick={onRefresh} disabled={actionBusy || !detail.has_container}>Refresh</button>
                        <button className="btn" onClick={onCreateSnapshot} disabled={actionBusy}>Create Snapshot</button>
                        <button className="btn btn-danger" onClick={onDelete} disabled={actionBusy}>Delete</button>
                    </div>
                </section>
            )}

            <section className="card" style={{ marginTop: 16 }}>
                <h2>Snapshots from this Clone</h2>
                {cloneSnapshots.length === 0 ? (
                    <p style={{ opacity: 0.7 }}>No snapshots derived from this clone.</p>
                ) : (
                    <ul className="list" style={{ marginTop: 8 }}>
                        {cloneSnapshots.map((snap) => (
                            <li key={snap.name} style={{ display: 'grid', gridTemplateColumns: '1fr auto', alignItems: 'center', gap: 8 }}>
                                <div style={{ display: 'grid', gap: 4 }}>
                                    <div style={{ fontWeight: 600 }}>{snap.name}</div>
                                    <div className="subtle">{snap.description || '(no description)'}</div>
                                </div>
                                <div style={{ display: 'flex', gap: 8 }}>
                                    <button className="btn" onClick={() => onReset(snap.name)} disabled={actionBusy}>Reset to this snapshot</button>
                                </div>
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            <section className="card" style={{ marginTop: 16 }}>
                <h2>All Snapshots</h2>
                {allSnapshots.length === 0 ? (
                    <p style={{ opacity: 0.7 }}>No snapshots found.</p>
                ) : (
                    <ul className="list" style={{ marginTop: 8 }}>
                        {allSnapshots.map((snap) => (
                            <li key={snap.name} style={{ display: 'grid', gridTemplateColumns: '1fr auto', alignItems: 'center', gap: 8 }}>
                                <div style={{ display: 'grid', gap: 4 }}>
                                    <div style={{ fontWeight: 600 }}>{snap.name}</div>
                                    <div className="subtle">{snap.description || '(no description)'}</div>
                                </div>
                                <span className="badge">{snap.readonly ? 'readonly' : 'writable'}</span>
                            </li>
                        ))}
                    </ul>
                )}
            </section>
        </div>
    )
}

