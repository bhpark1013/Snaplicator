import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import type { PortConflict } from '@/lib/restartClone'

interface Props {
    conflict: PortConflict | null
    cloneName: string
    busy: boolean
    onCancel: () => void
    onStart: (port: number) => void
}

export function PortConflictDialog({ conflict, cloneName, busy, onCancel, onStart }: Props) {
    const [port, setPort] = useState('')

    useEffect(() => {
        if (conflict) setPort(String(conflict.suggestedPort ?? ''))
    }, [conflict])

    const parsed = Number(port)
    const valid = Number.isInteger(parsed) && parsed > 1024 && parsed < 65536 && parsed !== conflict?.port

    return (
        <Dialog open={!!conflict} onOpenChange={(open) => { if (!open && !busy) onCancel() }}>
            <DialogContent>
                <DialogTitle>Port {conflict?.port} is taken</DialogTitle>
                <DialogDescription>
                    {conflict?.heldBy
                        ? `${conflict.heldBy} is published on that port, so this clone cannot take it back.`
                        : 'Another listener holds that port, so this clone cannot take it back.'}
                    {' '}Start it on a different port — the container is recreated over the same data.
                </DialogDescription>
                <p className="mt-2 text-[13px]">
                    Clone: <strong className="font-semibold">{cloneName}</strong>
                </p>
                <label className="mt-3 block text-[12px] text-muted-foreground" htmlFor="new-clone-port">
                    New host port
                </label>
                <Input
                    id="new-clone-port"
                    value={port}
                    inputMode="numeric"
                    autoFocus
                    onChange={(e) => setPort(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter' && valid && !busy) onStart(parsed) }}
                    placeholder="5450"
                />
                {!valid && port.trim() !== '' && (
                    <p className="mt-1 text-[12px] text-destructive">
                        Pick a free port between 1025 and 65535, other than {conflict?.port}.
                    </p>
                )}
                <DialogFooter>
                    <Button onClick={onCancel} disabled={busy}>Cancel</Button>
                    <Button onClick={() => onStart(parsed)} disabled={busy || !valid}>
                        {busy ? 'Starting…' : 'Start on this port'}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    )
}
