import * as React from 'react'

import { cn } from '@/lib/utils'

const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
    ({ className, ...props }, ref) => {
        return (
            <textarea
                className={cn(
                    'w-full rounded-md border border-border-strong bg-secondary px-2.5 py-2 font-mono text-xs leading-relaxed text-foreground transition-colors placeholder:text-zinc-600 focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/25 read-only:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50',
                    className,
                )}
                ref={ref}
                {...props}
            />
        )
    },
)
Textarea.displayName = 'Textarea'

export { Textarea }
