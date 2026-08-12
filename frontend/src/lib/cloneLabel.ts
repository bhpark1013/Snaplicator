/** What a clone is called, for a person.
 *
 * A clone has three names and only one of them is anyone's: the display name
 * they typed. The other two — the btrfs subvolume and the docker container —
 * exist so the machine can find it, and putting them on screen asks the
 * reader to learn a naming scheme in order to recognise their own clone.
 *
 * So the fallback is "(unnamed clone)", never `replica-clone-20260727-042854`
 * and never `snaplicator_replica-20260727-042854`. An unnamed clone reads as
 * unnamed, which is true and is also the nudge to name it.
 */
export interface CloneNamed {
    display_name?: string | null
    description?: string | null
}

export const UNNAMED_CLONE = '(unnamed clone)'

export function cloneLabel(c: CloneNamed | null | undefined): string {
    return c?.display_name?.trim() || c?.description?.trim() || UNNAMED_CLONE
}
