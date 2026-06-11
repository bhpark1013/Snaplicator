import { useEffect, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'

interface SnapshotItem {
    name: string
    path: string
    readonly: boolean
    description?: string | null
}

export function Snapshots() {
    const [items, setItems] = useState<SnapshotItem[]>([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [creating, setCreating] = useState(false)
    const [cloning, setCloning] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)
    const [snapshotDesc, setSnapshotDesc] = useState('')

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

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

    useEffect(() => {
        loadSnapshots()
        // eslint-disable-next-line react-hooks-exhaustive-deps
    }, [])

    const onCreateSnapshot = async () => {
        const trimmedDesc = snapshotDesc.trim()
        if (!trimmedDesc) {
            setMessage(null)
            setError('Snapshot description is required.')
            return
        }
        setCreating(true)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: trimmedDesc }),
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
        const trimmedDesc = description.trim()
        if (!trimmedDesc) {
            setMessage(null)
            setError('Clone description is required.')
            return
        }
        setCloning(name)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(name)}/clone`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: trimmedDesc }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Cloned: ${res.clone_subvolume} -> container ${res.container_name} (port ${res.host_port})`)
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setCloning(null)
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
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Snapshots</h1>
                <Button onClick={loadSnapshots} disabled={loading}>
                    {loading ? 'Refreshing...' : 'Refresh'}
                </Button>
            </div>

            <Card className="mt-4">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Input
                        value={snapshotDesc}
                        onChange={(e) => setSnapshotDesc(e.target.value)}
                        placeholder="Description (required)"
                    />
                    <Button onClick={onCreateSnapshot} disabled={creating || !snapshotDesc.trim()}>
                        {creating ? 'Creating...' : 'Create Snapshot'}
                    </Button>
                </div>
                {message && <p className="mb-2 text-[13px] text-success">{message}</p>}
                {error && <p className="mb-2 text-[13px] text-destructive">{error}</p>}
                <ul className="mt-2 grid gap-2">
                    {items.length === 0 && (
                        <li className="rounded-md border border-border bg-secondary px-3.5 py-2.5 text-muted-foreground">
                            No snapshots
                        </li>
                    )}
                    {items.map((it) => (
                        <li
                            key={it.name}
                            className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-secondary px-3.5 py-2.5 transition-colors hover:border-border-strong hover:bg-accent"
                        >
                            <span className="font-medium">{it.name}</span>
                            {it.readonly && <Badge variant="neutral">readonly</Badge>}
                            {it.description && (
                                <span className="text-muted-foreground" title={it.description}>– {it.description}</span>
                            )}
                            <div className="ml-auto flex items-center gap-2">
                                <Input placeholder="Clone description (required)" id={`clone-desc-${it.name}`} className="w-52" />
                                <Button
                                    onClick={() => {
                                        const el = document.getElementById(`clone-desc-${it.name}`) as HTMLInputElement | null
                                        const desc = el?.value || ''
                                        onClone(it.name, desc)
                                    }}
                                    disabled={cloning === it.name}
                                >
                                    {cloning === it.name ? 'Cloning...' : 'Clone'}
                                </Button>
                                <Button variant="destructive" onClick={() => confirmDeleteSnapshot(it.name)}>
                                    Delete
                                </Button>
                            </div>
                        </li>
                    ))}
                </ul>
            </Card>
        </div>
    )
}
