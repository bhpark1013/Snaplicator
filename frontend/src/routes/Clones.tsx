import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Star } from 'lucide-react'

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

export function Clones() {
    const [clones, setClones] = useState<CloneItem[]>([])
    const [clonesLoading, setClonesLoading] = useState(false)
    const [clonesError, setClonesError] = useState<string | null>(null)
    const [message, setMessage] = useState<string | null>(null)
    const [error, setError] = useState<string | null>(null)

    const [deleting, setDeleting] = useState<string | null>(null)
    const [deletingBusy, setDeletingBusy] = useState(false)

    const [createOpen, setCreateOpen] = useState(false)
    const [createDesc, setCreateDesc] = useState('')
    const [createPort, setCreatePort] = useState('')
    const [createUser, setCreateUser] = useState('')
    const [createPw, setCreatePw] = useState('')
    const [createError, setCreateError] = useState<string | null>(null)
    const [mainCloning, setMainCloning] = useState(false)
    const defaultUser = 'snaplicator'
    const [refreshingClone, setRefreshingClone] = useState<string | null>(null)

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
        const trimmedDesc = createDesc.trim()
        const user = createUser.trim()
        const pw = createPw
        if (!trimmedDesc) {
            setCreateError('Description is required.')
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
        try {
            const portNum = createPort.trim() ? parseInt(createPort.trim(), 10) : undefined
            const bodyData: Record<string, unknown> = { description: trimmedDesc, port: portNum }
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
            setMessage(`Cloned from main: ${res.clone_subvolume} -> container ${res.container_name} (port ${res.host_port}, user ${res.db_user || defaultUser})`)
            setCreateOpen(false)
            setCreateDesc('')
            setCreatePort('')
            setCreateUser('')
            setCreatePw('')
            loadClones()
        } catch (e: any) {
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

    const onRefreshClone = async (clone: CloneItem) => {
        const targetName = clone.container_name || clone.name
        if (!targetName || !clone.has_container) {
            setClonesError('Cannot refresh a clone without a running container.')
            return
        }

        const ok = window.confirm(`Refresh clone ${targetName} with the latest data from main?`)
        if (!ok) return

        setRefreshingClone(targetName)
        setMessage(null)
        setError(null)
        setClonesError(null)
        try {
            const r = await fetch(`${base}/clones/${encodeURIComponent(targetName)}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            })
            if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
            const res = await r.json()
            setMessage(`Refreshed ${res.refreshed_container} using ${res.clone_subvolume}`)
            loadClones()
        } catch (e: any) {
            setError(String(e?.message || e))
        } finally {
            setRefreshingClone(null)
        }
    }

    const renderClone = (c: CloneItem) => {
        const targetName = c.container_name || c.name
        const statusLabel = c.has_container
            ? (c.container_status || (c.is_running ? 'running' : 'stopped'))
            : 'no-container'
        const statusVariant = c.has_container && c.is_running ? 'success' : 'neutral'
        const isFav = favorites.has(c.name)
        return (
            <li
                key={c.path}
                className={cn(
                    'flex items-center justify-between gap-3 rounded-md border bg-secondary px-3.5 py-2.5 transition-colors hover:bg-accent',
                    isFav ? 'border-warning/35' : 'border-border hover:border-border-strong',
                )}
            >
                <button
                    className={cn(
                        'flex-none rounded p-1 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                        isFav ? 'text-warning' : 'text-muted-foreground hover:text-warning',
                    )}
                    onClick={() => toggleFavorite(c.name)}
                    title={isFav ? 'Remove from favorites' : 'Add to favorites'}
                    aria-label="toggle favorite"
                >
                    <Star className="size-3.5" fill={isFav ? 'currentColor' : 'none'} />
                </button>
                <div className="grid min-w-0 gap-1">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold">{c.name}</span>
                        <Badge variant={statusVariant}>{statusLabel}</Badge>
                    </div>
                    <div className="flex flex-wrap gap-3 text-[13px] text-muted-foreground">
                        {c.description && <span title={c.description}>{c.description}</span>}
                        {typeof c.host_port === 'number' && <span>port: {c.host_port}</span>}
                        {c.container_started_at && <span>started at: {new Date(c.container_started_at).toLocaleString()}</span>}
                    </div>
                </div>
                <div className="ml-auto flex flex-shrink-0 gap-2">
                    <Button asChild>
                        <Link to={`/clones/${encodeURIComponent(targetName)}`}>View</Link>
                    </Button>
                    <Button
                        onClick={() => onRefreshClone(c)}
                        disabled={refreshingClone === targetName || !c.has_container}
                        title={c.has_container ? 'Replace the container data with the latest from main' : 'No container to refresh.'}
                    >
                        {refreshingClone === targetName ? 'Refreshing...' : 'Refresh'}
                    </Button>
                    <Button variant="destructive" onClick={() => onDelete(targetName)} disabled={deletingBusy}>
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
                            <span className="text-[13px] text-muted-foreground">Description (required)</span>
                            <Input
                                autoFocus
                                value={createDesc}
                                onChange={(e) => setCreateDesc(e.target.value)}
                                placeholder="e.g. feature-xyz testing"
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
                        <Button variant="primary" onClick={onCreateClone} disabled={mainCloning || !createDesc.trim()}>
                            {mainCloning ? 'Cloning...' : 'Create Clone'}
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
        </div>
    )
}
