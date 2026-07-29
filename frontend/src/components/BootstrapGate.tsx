import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Database, Loader2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'

type BootstrapState = 'not_started' | 'running' | 'done' | 'failed'

interface BootstrapStatus {
    state: BootstrapState
    pid: number | null
    exit_code: number | null
    started_at: number | null
    log_tail: string
}

/**
 * Shown until the replica exists.
 *
 * The install now stops before the initial copy, because the copy is what
 * makes the choice of what to replicate permanent — reversing it means
 * tearing the replica down. So the last step is taken here, deliberately,
 * rather than by a script that was never in a position to ask.
 *
 * Renders nothing once the replica is subscribed, which is the normal state
 * of a running installation: this is a first-run screen, not a dashboard.
 */
export function BootstrapGate({ onDone }: { onDone?: () => void }) {
    const [status, setStatus] = useState<BootstrapStatus | null>(null)
    const [starting, setStarting] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [now, setNow] = useState(() => Date.now())
    const wasRunning = useRef(false)

    const api = import.meta.env.VITE_API_BASE_URL || ''
    const base = api ? api : '/api'

    const load = () =>
        fetch(`${base}/replication/bootstrap?tail=12`)
            .then((r) => (r.ok ? r.json() : null))
            .then((d: BootstrapStatus | null) => {
                if (!d) return
                setStatus(d)
                // Tell the page the moment it becomes worth reloading: a
                // finished copy means clones can be made, which is the thing
                // the rest of the screen is about.
                if (wasRunning.current && d.state === 'done') onDone?.()
                wasRunning.current = d.state === 'running'
            })
            .catch(() => {})

    useEffect(() => {
        load()
        // Poll only while there is something to watch. A settled installation
        // asks once on mount and then leaves the backend alone.
        const id = setInterval(() => {
            setNow(Date.now())
            load()
        }, 3000)
        return () => clearInterval(id)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    const start = async () => {
        setStarting(true)
        setError(null)
        try {
            const r = await fetch(`${base}/replication/bootstrap`, { method: 'POST' })
            if (!r.ok) setError(`${r.status} ${await r.text()}`)
            await load()
        } catch (e) {
            setError(String(e))
        } finally {
            setStarting(false)
        }
    }

    if (!status || status.state === 'done') return null

    const elapsed = status.started_at ? Math.max(0, Math.floor(now / 1000 - status.started_at)) : 0
    const mm = String(Math.floor(elapsed / 60)).padStart(2, '0')
    const ss = String(elapsed % 60).padStart(2, '0')

    return (
        <Card className="mb-4 p-4">
            {status.state === 'running' ? (
                <>
                    <div className="flex items-center gap-2 text-[13px] font-medium">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        Copying from the primary — {mm}:{ss}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                        The schema is cloned first, then every published table is copied. Large
                        databases take a while; this page keeps up on its own, and closing it
                        does not stop the copy.
                    </p>
                </>
            ) : status.state === 'failed' ? (
                <>
                    <div className="flex items-center gap-2 text-[13px] font-medium text-destructive">
                        <AlertTriangle className="h-4 w-4" />
                        Replication did not start
                        {status.exit_code != null ? ` (exit ${status.exit_code})` : ''}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                        Nothing has been copied. Fix what the log points at and start it again.
                    </p>
                </>
            ) : (
                <>
                    <div className="flex items-center gap-2 text-[13px] font-medium">
                        <Database className="h-4 w-4" />
                        Replication has not started yet
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                        Nothing has been copied from the primary. The first copy is what fixes
                        what gets replicated, so it waits for you to say go.
                    </p>
                </>
            )}

            {status.log_tail ? (
                <pre className="mt-3 max-h-40 overflow-auto rounded-md bg-secondary/60 p-2 text-[11px] leading-relaxed text-muted-foreground">
                    {status.log_tail}
                </pre>
            ) : null}

            {error ? <p className="mt-2 text-xs text-destructive">{error}</p> : null}

            {status.state !== 'running' ? (
                <div className="mt-3">
                    <Button variant="primary" onClick={start} disabled={starting}>
                        {starting ? 'Starting…' : status.state === 'failed' ? 'Try again' : 'Start replication'}
                    </Button>
                </div>
            ) : null}
        </Card>
    )
}
