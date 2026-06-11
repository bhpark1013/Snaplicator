import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'

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
    const [message, setMessage] = useState<string | null>(null)
    const [snapshotDesc, setSnapshotDesc] = useState('')

    const [cloneFor, setCloneFor] = useState<SnapshotItem | null>(null)
    const [cloneDesc, setCloneDesc] = useState('')
    const [cloneBusy, setCloneBusy] = useState(false)
    const [cloneError, setCloneError] = useState<string | null>(null)

    const [deleting, setDeleting] = useState<SnapshotItem | null>(null)
    const [deletingBusy, setDeletingBusy] = useState(false)

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

    const openClone = (it: SnapshotItem) => {
        setCloneFor(it)
        setCloneDesc(it.description ?? '')
        setCloneError(null)
    }

    const confirmClone = async () => {
        if (!cloneFor) return
        const desc = cloneDesc.trim()
        if (!desc) {
            setCloneError('Description is required.')
            return
        }
        setCloneBusy(true)
        setCloneError(null)
        setMessage(null)
        setError(null)
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(cloneFor.name)}/clone`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: desc }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Cloned: ${res.clone_subvolume} → container ${res.container_name} (port ${res.host_port})`)
            setCloneFor(null)
        } catch (e: any) {
            setCloneError(String(e?.message || e))
        } finally {
            setCloneBusy(false)
        }
    }

    const confirmDelete = async () => {
        if (!deleting) return
        setDeletingBusy(true)
        setError(null)
        setMessage(null)
        try {
            const r = await fetch(`${base}/snapshots/${encodeURIComponent(deleting.name)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Deleted snapshot subvolume: ${res.subvolume_deleted}`)
            setDeleting(null)
            loadSnapshots()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setDeletingBusy(false)
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
                <div className="mb-3 flex flex-wrap items-center gap-2">
                    <Input
                        value={snapshotDesc}
                        onChange={(e) => setSnapshotDesc(e.target.value)}
                        placeholder="Snapshot description (required)"
                        className="min-w-0 flex-1"
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
                            role="button"
                            tabIndex={0}
                            onClick={() => openClone(it)}
                            onKeyDown={(e) => {
                                if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
                                    e.preventDefault()
                                    openClone(it)
                                }
                            }}
                            className="flex cursor-pointer items-center gap-3 rounded-md border border-border bg-secondary px-3.5 py-2.5 transition-colors hover:border-border-strong hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                            <div className="grid min-w-0 flex-1 gap-1">
                                <span className="min-w-0 truncate text-[13px] font-medium text-zinc-100">
                                    {it.description?.trim() ? it.description : <span className="text-muted-foreground">(no description)</span>}
                                </span>
                                <span className="min-w-0 truncate font-mono text-[12px] text-muted-foreground">{it.name}</span>
                            </div>
                            <div className="ml-auto flex flex-none items-center gap-2">
                                <Button onClick={(e) => { e.stopPropagation(); openClone(it) }}>Clone</Button>
                                <Button variant="destructive" onClick={(e) => { e.stopPropagation(); setDeleting(it) }}>
                                    Delete
                                </Button>
                            </div>
                        </li>
                    ))}
                </ul>
            </Card>

            <Dialog open={!!cloneFor} onOpenChange={(open) => { if (!open && !cloneBusy) setCloneFor(null) }}>
                <DialogContent>
                    <DialogTitle>Clone from snapshot</DialogTitle>
                    <DialogDescription>
                        A new writable clone with its own Postgres container is created from this snapshot.
                    </DialogDescription>
                    <div className="mt-2 break-all rounded-md border border-border bg-secondary px-3 py-2 font-mono text-[12px] text-muted-foreground">
                        {cloneFor?.name}
                    </div>
                    <label className="mt-3 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">New clone description (required)</span>
                        <Input
                            autoFocus
                            value={cloneDesc}
                            onChange={(e) => setCloneDesc(e.target.value)}
                            placeholder="e.g. bugfix-1234 testing"
                            className="w-full"
                        />
                    </label>
                    {cloneError && <p className="mt-2 whitespace-pre-wrap text-[13px] text-destructive">{cloneError}</p>}
                    <DialogFooter>
                        <Button onClick={() => setCloneFor(null)} disabled={cloneBusy}>Cancel</Button>
                        <Button variant="primary" onClick={confirmClone} disabled={cloneBusy || !cloneDesc.trim()}>
                            {cloneBusy ? 'Cloning...' : 'Create Clone'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={!!deleting} onOpenChange={(open) => { if (!open && !deletingBusy) setDeleting(null) }}>
                <DialogContent>
                    <DialogTitle>Delete snapshot</DialogTitle>
                    <DialogDescription>
                        This removes the snapshot subvolume. Clones already created from it are unaffected.
                    </DialogDescription>
                    <div className="mt-2 break-all rounded-md border border-border bg-secondary px-3 py-2 font-mono text-[12px] text-muted-foreground">
                        {deleting?.name}
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setDeleting(null)} disabled={deletingBusy}>Cancel</Button>
                        <Button variant="destructive" onClick={confirmDelete} disabled={deletingBusy}>
                            {deletingBusy ? 'Deleting...' : 'Delete'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
