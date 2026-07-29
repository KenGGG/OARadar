import type { HTMLAttributes } from "react"
import { cn } from "@/lib/utils"

type Tone = "neutral" | "success" | "warning" | "danger" | "info"

export function Badge({ className, tone = "neutral", ...props }: HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  const tones: Record<Tone, string> = {
    neutral: "border-border bg-muted text-muted-foreground",
    success: "border-emerald-700/20 bg-emerald-700/10 text-emerald-800 dark:text-emerald-300",
    warning: "border-amber-700/20 bg-amber-600/10 text-amber-800 dark:text-amber-300",
    danger: "border-destructive/20 bg-destructive/10 text-destructive",
    info: "border-primary/20 bg-primary/10 text-primary",
  }
  return <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold", tones[tone], className)} {...props} />
}
