export interface PortConflict {
    port: number
    suggestedPort: number | null
    heldBy: string | null
}

export interface RestartCloneResponse {
    container_name: string
    action: 'start' | 'restart' | 'rebound'
    was_running: boolean
    running: boolean
    ready: boolean
    host_port?: number
}

export type RestartCloneResult =
    | { ok: true; res: RestartCloneResponse }
    | { ok: false; conflict: PortConflict }

// A stopped clone keeps its port only on paper: create another clone while it
// is down and the number can be handed out. Starting it then fails on the
// port alone, which the caller answers by picking a new one — so that case
// comes back as a value, and everything else throws.
export async function restartClone(
    base: string,
    containerName: string,
    port?: number,
): Promise<RestartCloneResult> {
    const r = await fetch(`${base}/clones/${encodeURIComponent(containerName)}/restart`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(port != null ? { port } : {}),
    })
    if (r.status === 409) {
        const body = await r.json().catch(() => null)
        const d = body?.detail
        if (d?.error === 'port_in_use') {
            return {
                ok: false,
                conflict: {
                    port: Number(d.port),
                    suggestedPort: d.suggested_port != null ? Number(d.suggested_port) : null,
                    heldBy: d.held_by ?? null,
                },
            }
        }
    }
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
    return { ok: true, res: (await r.json()) as RestartCloneResponse }
}
