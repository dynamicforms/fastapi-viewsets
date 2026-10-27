/**
 * Status and headers of a muxws answer: `:status` in its leading headers, the other keys the HTTP headers.
 */

/** The status in `replyHeaders[':status']`, a number or a string of one; 200 without a usable `:status`. */
export function readStatus(replyHeaders: Record<string, unknown> | null | undefined): number {
  const raw = replyHeaders?.[':status'];
  if (typeof raw === 'number') return raw;
  if (typeof raw === 'string') {
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return 200;
}

/** The reply headers without the `:`-prefixed pseudo-headers, every value as a string. */
export function readHeaders(replyHeaders: Record<string, unknown> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(replyHeaders ?? {})) {
    if (!key.startsWith(':')) out[key] = String(value);
  }
  return out;
}
