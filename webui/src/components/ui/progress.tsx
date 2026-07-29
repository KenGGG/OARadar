export function Progress({ value, label }: { value: number; label: string }) {
  const safe = Math.min(100, Math.max(0, value))
  return (
    <div className="h-3 w-full overflow-hidden rounded-full border border-border bg-muted" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(safe)}>
      <div className="h-full bg-primary transition-[width] duration-300 motion-reduce:transition-none" style={{ width: `${safe}%` }} />
    </div>
  )
}
