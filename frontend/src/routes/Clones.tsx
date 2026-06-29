import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, Copy, Star } from 'lucide-react'

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
import { useToast } from '@/components/ui/toast'
import { cn, copyText } from '@/lib/utils'
import { RetentionSelect } from '@/components/RetentionSelect'
import { LineageGraph, computeInsertParams, type Slot, type SnapshotItem } from '@/components/LineageGraph'
import { WhatsNew } from '@/components/WhatsNew'

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
    display_name?: string | null
    description?: string | null
    db_user?: string | null
    db_password?: string | null
    db_name?: string | null
}

interface CloneSnapshotOption {
    name: string
    description?: string | null
}

export function Clones() {
    const navigate = useNavigate()
    const toast = useToast()
    const [clones, setClones] = useState<CloneItem[]>([])
    const [clonesLoading, setClonesLoading] = useState(false)
    const [clonesError, setClonesError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)
    const [error, setError] = useState<string | null>(null)

    const [deleting, setDeleting] = useState<string | null>(null)
    const [deletingBusy, setDeletingBusy] = useState(false)

    const [createOpen, setCreateOpen] = useState(false)
    const [createName, setCreateName] = useState('')
    const [createDesc, setCreateDesc] = useState('')
    const [createPort, setCreatePort] = useState('')
    const [createUser, setCreateUser] = useState('')
    const [createPw, setCreatePw] = useState('')
    const [createError, setCreateError] = useState<string | null>(null)
    const [mainCloning, setMainCloning] = useState(false)
    const defaultUser = 'snaplicator'
    const [refreshingClone, setRefreshingClone] = useState<string | null>(null)
    const [refreshFor, setRefreshFor] = useState<CloneItem | null>(null)
    const [copiedClone, setCopiedClone] = useState<string | null>(null)

    const [snapshotFor, setSnapshotFor] = useState<CloneItem | null>(null)
    const [snapshotDesc, setSnapshotDesc] = useState('')
    const [snapshotSlot, setSnapshotSlot] = useState<Slot | null>(null)
    const [snapshotRetention, setSnapshotRetention] = useState(14)
    const [allSnapshots, setAllSnapshots] = useState<SnapshotItem[]>([])
    const [snapshotBusy, setSnapshotBusy] = useState(false)
    const [snapshotError, setSnapshotError] = useState<string | null>(null)

    const connHost = (typeof window !== 'undefined' && window.location.hostname) || 'localhost'
    const buildConnUrl = (c: CloneItem, masked: boolean) =>
        `postgresql://${c.db_user ?? ''}:${masked ? '••••••••' : (c.db_password ?? '')}@${connHost}:${c.host_port ?? ''}/${c.db_name ?? ''}`

    const onCopyUrl = async (c: CloneItem) => {
        const ok = await copyText(buildConnUrl(c, false))
        if (!ok) {
            setError('Copy failed. Select the connection string and copy manually.')
            return
        }
        setCopiedClone(c.name)
        setTimeout(() => setCopiedClone((v) => (v === c.name ? null : v)), 1500)
    }

    const openSnapshot = async (c: CloneItem) => {
        setSnapshotFor(c)
        setSnapshotDesc('')
        setSnapshotError(null)
        setSnapshotRetention(14)
        setSnapshotSlot(null)
        setAllSnapshots([])
        try {
            const [allR, cloneR] = await Promise.all([
                fetch(`${base}/snapshots`),
                fetch(`${base}/clones/${encodeURIComponent(c.name)}/snapshots`),
            ])
            if (allR.ok) setAllSnapshots(await allR.json())
            if (cloneR.ok) {
                const snaps: CloneSnapshotOption[] = await cloneR.json()
                // default insertion = right after this clone's most recent snapshot
                if (snaps.length) setSnapshotSlot({ kind: 'after', parent: snaps[snaps.length - 1].name })
            }
        } catch {
            /* graph just stays empty */
        }
    }

    const confirmSnapshot = async () => {
        if (!snapshotFor) return
        const desc = snapshotDesc.trim()
        if (!desc) {
            setSnapshotError('Description is required.')
            return
        }
        setSnapshotBusy(true)
        setSnapshotError(null)
        setMessage(null)
        setError(null)
        const tid = toast.loading('Creating snapshot…')
        const { previous_snapshot, insert_before } = snapshotSlot
            ? computeInsertParams(snapshotSlot)
            : { previous_snapshot: null, insert_before: null }
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(snapshotFor.name)}/snapshots`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: desc, previous_snapshot, insert_before, retention_days: snapshotRetention }),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Snapshot created: ${res.name}`)
            setSnapshotFor(null)
        } catch (e: any) {
            toast.update(tid, 'error', `Snapshot failed: ${String(e?.message || e)}`)
            setSnapshotError(String(e?.message || e))
        } finally {
            setSnapshotBusy(false)
        }
    }

    const FAV_KEY = 'snaplicator.favoriteClones'
    const [favorites, setFavorites] = useState<Set<string>>(() => {
        try {
            return new Set<string>(JSON.parse(localStorage.getItem(FAV_KEY) || '[]'))
        } catch {
            return new Set<string>()
        }
    })
    const toggleFavorite = (name: string) => {
        setFavorites((prev) => {
            const next = new Set(prev)
            if (next.has(name)) next.delete(name)
            else next.add(name)
            localStorage.setItem(FAV_KEY, JSON.stringify(Array.from(next)))
            return next
        })
    }

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

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
        loadClones()
        // eslint-disable-next-line react-hooks-exhaustive-deps
    }, [])

    const onCreateClone = async () => {
        const trimmedName = createName.trim()
        const trimmedDesc = createDesc.trim()
        const user = createUser.trim()
        const pw = createPw
        if (!trimmedName) {
            setCreateError('Name is required.')
            return
        }
        if (!!user !== !!pw) {
            setCreateError('Username and password must be provided together.')
            return
        }
        setMainCloning(true)
        setCreateError(null)
        setMessage(null)
        setError(null)
        const tid = toast.loading(`Creating clone “${trimmedName}”…`)
        try {
            const portNum = createPort.trim() ? parseInt(createPort.trim(), 10) : undefined
            const bodyData: Record<string, unknown> = { name: trimmedName, description: trimmedDesc, port: portNum }
            if (user) {
                bodyData.username = user
                bodyData.password = pw
            }
            const r = await fetch(`${base}/clones`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(bodyData),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Clone “${trimmedName}” created on port ${res.host_port}`)
            setCreateOpen(false)
            setCreateName('')
            setCreateDesc('')
            setCreatePort('')
            setCreateUser('')
            setCreatePw('')
            loadClones()
        } catch (e: any) {
            toast.update(tid, 'error', `Clone failed: ${String(e?.message || e)}`)
            setCreateError(String(e?.message || e))
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
        const tid = toast.loading('Deleting clone…')
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(deleting)}`, { method: 'DELETE' })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Deleted clone (${res.subvolume_deleted ? 'subvolume removed' : 'done'})`)
            loadClones()
            setDeleting(null)
        } catch (e: any) {
            toast.update(tid, 'error', `Delete failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setDeletingBusy(false)
        }
    }

    const onRefreshClone = (clone: CloneItem) => {
        const targetName = clone.container_name || clone.name
        if (!targetName || !clone.has_container) {
            setClonesError('Cannot refresh a clone without a running container.')
            return
        }
        setRefreshFor(clone)
    }

    const confirmRefreshClone = async () => {
        if (!refreshFor) return
        const targetName = refreshFor.container_name || refreshFor.name
        setRefreshingClone(targetName)
        setMessage(null)
        setError(null)
        setClonesError(null)
        const tid = toast.loading('Refreshing clone from main…')
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(targetName)}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            toast.update(tid, 'success', `Refreshed ${res.refreshed_container}`)
            setRefreshFor(null)
            loadClones()
        } catch (e: any) {
            toast.update(tid, 'error', `Refresh failed: ${String(e?.message || e)}`)
            setError(String(e?.message || e))
        } finally {
            setRefreshingClone(null)
        }
    }

    const snapshotSlotSummary = () => {
        const label = (name: string) => allSnapshots.find((s) => s.name === name)?.description?.trim() || name
        const s = snapshotSlot
        if (!s) return 'Start a new chain (no previous snapshot)'
        if (s.kind === 'after') return `After “${label(s.parent)}”`
        if (s.kind === 'edge') return `Between “${label(s.parent)}” and “${label(s.child)}”`
        return `New root before “${label(s.child)}”`
    }

    const renderClone = (c: CloneItem) => {
        const targetName = c.container_name || c.name
        const isFav = favorites.has(c.name)
        const running = c.has_container && c.is_running
        const goDetail = () => navigate(`/clones/${encodeURIComponent(targetName)}`)
        return (
            <li
                key={c.path}
                role="button"
                tabIndex={0}
                onClick={goDetail}
                onKeyDown={(e) => {
                    if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
                        e.preventDefault()
                        goDetail()
                    }
                }}
                className={cn(
                    'flex cursor-pointer items-center gap-3 rounded-md border bg-secondary px-3.5 py-2.5 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    isFav ? 'border-warning/35' : 'border-border hover:border-border-strong',
                )}
            >
                <button
                    className={cn(
                        'flex-none rounded p-1 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                        isFav ? 'text-warning' : 'text-muted-foreground hover:text-warning',
                    )}
                    onClick={(e) => { e.stopPropagation(); toggleFavorite(c.name) }}
                    title={isFav ? 'Remove from favorites' : 'Add to favorites'}
                    aria-label="toggle favorite"
                >
                    <Star className="size-3.5" fill={isFav ? 'currentColor' : 'none'} />
                </button>
                <div className="grid min-w-0 flex-1 gap-1">
                    <div className="flex min-w-0 items-center gap-2">
                        <span
                            className={cn('size-1.5 flex-none rounded-full', running ? 'bg-success' : 'bg-zinc-600')}
                            title={running ? 'running' : 'stopped'}
                        />
                        <span className="min-w-0 truncate text-[13px] font-medium text-zinc-100">
                            {(c.display_name ?? c.description)?.trim() ? (c.display_name ?? c.description) : <span className="text-muted-foreground">(unnamed clone)</span>}
                        </span>
                    </div>
                    <div className="flex min-w-0 items-center gap-1.5">
                        <code className="min-w-0 truncate font-mono text-[12px] text-muted-foreground">{buildConnUrl(c, true)}</code>
                        <button
                            onClick={(e) => { e.stopPropagation(); onCopyUrl(c) }}
                            title="Copy connection string"
                            aria-label="Copy connection string"
                            className="flex-none rounded p-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                            {copiedClone === c.name ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
                        </button>
                    </div>
                </div>
                <div className="ml-auto flex flex-none gap-2">
                    <Button
                        onClick={(e) => { e.stopPropagation(); openSnapshot(c) }}
                        disabled={!c.has_container}
                        title={c.has_container ? 'Create a snapshot from this clone' : 'No container to snapshot.'}
                    >
                        Snapshot
                    </Button>
                    <Button
                        onClick={(e) => { e.stopPropagation(); onRefreshClone(c) }}
                        disabled={refreshingClone === targetName || !c.has_container}
                        title={c.has_container ? 'Replace the container data with the latest from main' : 'No container to refresh.'}
                    >
                        {refreshingClone === targetName ? 'Refreshing...' : 'Refresh'}
                    </Button>
                    <Button variant="destructive" onClick={(e) => { e.stopPropagation(); onDelete(targetName) }} disabled={deletingBusy}>
                        Delete
                    </Button>
                </div>
            </li>
        )
    }

    return (
        <div className="mx-auto max-w-5xl animate-page-in px-6 pb-20 pt-6">
            <div className="mb-2 flex items-center justify-between gap-4 border-b border-border pb-4">
                <h1 className="text-base font-semibold tracking-tight">Clones</h1>
                <div className="flex items-center gap-2">
                    <Button onClick={() => { setCreateError(null); setCreateOpen(true) }}>
                        New Clone
                    </Button>
                    <Button onClick={loadClones} disabled={clonesLoading}>
                        {clonesLoading ? 'Refreshing...' : 'Refresh'}
                    </Button>
                </div>
            </div>

            <WhatsNew />

            <Card className="mt-4">
                {clonesError && <p className="mb-2 text-[13px] text-destructive">{clonesError}</p>}
                {message && <p className="mb-2 text-[13px] text-success">{message}</p>}
                {error && <p className="mb-2 text-[13px] text-destructive">{error}</p>}

                {clones.some((c) => favorites.has(c.name)) && (
                    <>
                        <div className="mb-1.5 mt-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                            Favorites
                        </div>
                        <ul className="grid gap-2">
                            {clones.filter((c) => favorites.has(c.name)).map(renderClone)}
                        </ul>
                        <div className="mb-1.5 mt-4 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                            All clones
                        </div>
                    </>
                )}
                <ul className="mt-2 grid gap-2">
                    {clones.length === 0 && (
                        <li className="rounded-md border border-border bg-secondary px-3.5 py-2.5 text-muted-foreground">
                            No clones
                        </li>
                    )}
                    {clones.filter((c) => !favorites.has(c.name)).map(renderClone)}
                </ul>
            </Card>

            <Dialog open={createOpen} onOpenChange={(open) => { if (!mainCloning) setCreateOpen(open) }}>
                <DialogContent>
                    <DialogTitle>New clone from main</DialogTitle>
                    <div className="grid gap-3">
                        <label className="grid gap-1.5">
                            <span className="text-[13px] text-muted-foreground">Name (required)</span>
                            <Input
                                autoFocus
                                value={createName}
                                onChange={(e) => setCreateName(e.target.value)}
                                placeholder="e.g. feature-xyz"
                                className="w-full"
                            />
                        </label>
                        <label className="grid gap-1.5">
                            <span className="text-[13px] text-muted-foreground">Description (optional)</span>
                            <Input
                                value={createDesc}
                                onChange={(e) => setCreateDesc(e.target.value)}
                                placeholder="e.g. testing the new checkout flow"
                                className="w-full"
                            />
                        </label>
                        <label className="grid gap-1.5">
                            <span className="text-[13px] text-muted-foreground">Port (auto-assigned if empty)</span>
                            <Input
                                value={createPort}
                                onChange={(e) => setCreatePort(e.target.value)}
                                placeholder="e.g. 5440"
                                className="w-full"
                            />
                        </label>
                        <div className="grid grid-cols-2 gap-3">
                            <label className="grid gap-1.5">
                                <span className="text-[13px] text-muted-foreground">Username (default: {defaultUser})</span>
                                <Input
                                    value={createUser}
                                    onChange={(e) => setCreateUser(e.target.value)}
                                    placeholder={defaultUser}
                                    className="w-full"
                                />
                            </label>
                            <label className="grid gap-1.5">
                                <span className="text-[13px] text-muted-foreground">Password (default: {defaultUser})</span>
                                <Input
                                    type="password"
                                    value={createPw}
                                    onChange={(e) => setCreatePw(e.target.value)}
                                    placeholder="••••••••"
                                    className="w-full"
                                />
                            </label>
                        </div>
                        <DialogDescription className="text-xs leading-relaxed">
                            Leave Username/Password empty to connect with the default account{' '}
                            <code className="rounded bg-secondary px-1 py-0.5 font-mono text-[11px]">{defaultUser}</code>{' '}
                            and its default password. If provided, the account is created in this clone.
                        </DialogDescription>
                        {createError && <p className="whitespace-pre-wrap text-[13px] text-destructive">{createError}</p>}
                    </div>
                    <DialogFooter>
                        <Button onClick={() => setCreateOpen(false)} disabled={mainCloning}>Cancel</Button>
                        <Button variant="primary" onClick={onCreateClone} disabled={mainCloning || !createName.trim()}>
                            {mainCloning ? 'Cloning...' : 'Create Clone'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={!!snapshotFor} onOpenChange={(open) => { if (!open && !snapshotBusy) setSnapshotFor(null) }}>
                <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
                    <DialogTitle>Create snapshot</DialogTitle>
                    <DialogDescription>
                        A read-only btrfs snapshot is captured from this clone's current state.
                    </DialogDescription>
                    <label className="mt-1 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Description (required)</span>
                        <Input
                            autoFocus
                            value={snapshotDesc}
                            onChange={(e) => setSnapshotDesc(e.target.value)}
                            placeholder="e.g. before-migration"
                            className="w-full"
                        />
                    </label>
                    <div className="mt-3">
                        <RetentionSelect value={snapshotRetention} onChange={setSnapshotRetention} />
                    </div>
                    <div className="mt-4 grid gap-1.5">
                        <span className="text-[13px] text-muted-foreground">Insertion point — click a <span className="text-primary">+</span> in the graph</span>
                        <LineageGraph
                            items={allSnapshots}
                            mode="insert"
                            selectedSlot={snapshotSlot}
                            onSelectSlot={setSnapshotSlot}
                            maxHeight={300}
                        />
                        <span className="text-xs text-muted-foreground">{snapshotSlotSummary()}</span>
                    </div>
                    {snapshotError && <p className="whitespace-pre-wrap text-[13px] text-destructive">{snapshotError}</p>}
                    <DialogFooter>
                        <Button onClick={() => setSnapshotFor(null)} disabled={snapshotBusy}>Cancel</Button>
                        <Button variant="primary" onClick={confirmSnapshot} disabled={snapshotBusy || !snapshotDesc.trim()}>
                            {snapshotBusy ? 'Creating...' : 'Create Snapshot'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={!!deleting} onOpenChange={(open) => { if (!open && !deletingBusy) setDeleting(null) }}>
                <DialogContent>
                    <DialogTitle>Delete clone</DialogTitle>
                    <DialogDescription>
                        The container and its btrfs subvolume will be deleted together.
                    </DialogDescription>
                    <p className="mt-2 text-[13px]">
                        Target: <strong className="font-semibold">{deleting}</strong>
                    </p>
                    <DialogFooter>
                        <Button onClick={() => setDeleting(null)} disabled={deletingBusy}>Cancel</Button>
                        <Button variant="destructive" onClick={confirmDelete} disabled={deletingBusy}>
                            {deletingBusy ? 'Deleting...' : 'Delete'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={!!refreshFor} onOpenChange={(open) => { if (!open && refreshingClone === null) setRefreshFor(null) }}>
                <DialogContent>
                    <DialogTitle>Refresh clone</DialogTitle>
                    <DialogDescription>
                        This re-syncs the clone with the latest data from main and recreates its container.
                        The name and description are kept, and any changes made inside this clone are discarded.
                    </DialogDescription>
                    <p className="mt-2 text-[13px]">
                        Target: <strong className="font-semibold">{refreshFor?.display_name?.trim() || refreshFor?.container_name || refreshFor?.name}</strong>
                    </p>
                    <DialogFooter>
                        <Button onClick={() => setRefreshFor(null)} disabled={refreshingClone !== null}>Cancel</Button>
                        <Button variant="primary" onClick={confirmRefreshClone} disabled={refreshingClone !== null}>
                            {refreshingClone !== null ? 'Refreshing...' : 'Refresh'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}
