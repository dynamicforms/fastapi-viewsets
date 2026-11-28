/** The answer of a REST call made with `RequestOptions.responseType: 'blob'`. */

/** A downloaded file: the body, its name from `Content-Disposition`, and the response headers. */
export interface FileDownload {
  /** The response body; its `type` is `contentType`. */
  blob: Blob;
  /** The file name `Content-Disposition` gives (see `contentDispositionFilename`); null without one. */
  filename: string | null;
  /** The `Content-Type` header; `''` without one. */
  contentType: string;
  /** The response headers, by lower-case name. */
  headers: Record<string, string>;
}

/** One `name=value` parameter of a header; the value is a token or a quoted string. */
const PARAMETER = /;\s*([!#$%&'*+.^_`|~0-9A-Za-z-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)/g;

/** An RFC 8187 / RFC 5987 extended value: `charset'language'percent-encoded`. */
const EXTENDED_VALUE = /^([!#$%&+^_`{}~0-9A-Za-z-]*)'[^']*'(.*)$/;

function unquote(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return trimmed.slice(1, -1).replace(/\\(.)/g, '$1');
  }
  return trimmed;
}

/** Decodes an extended value; null for an unknown charset or a malformed encoding. */
function decodeExtended(value: string): string | null {
  const match = EXTENDED_VALUE.exec(unquote(value));
  if (!match) return null;
  const [, charset, encoded] = match;
  try {
    switch (charset.toLowerCase()) {
      case 'utf-8':
        return decodeURIComponent(encoded);
      case 'iso-8859-1':
        return encoded.replace(/%([0-9A-Fa-f]{2})/g, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
      default:
        return null;
    }
  } catch {
    return null;
  }
}

/**
 * The file name in a `Content-Disposition` header: `filename*` (RFC 6266, e.g. `UTF-8''track-1-%C3%A9.pdf`)
 * when it decodes, else `filename` (quoted or a token), else null.
 */
export function contentDispositionFilename(header: string | null | undefined): string | null {
  if (!header) return null;
  let extended: string | null = null;
  let plain: string | null = null;
  for (const [, name, value] of `;${header}`.matchAll(PARAMETER)) {
    const lower = name.toLowerCase();
    if (lower === 'filename*' && extended === null) extended = decodeExtended(value);
    else if (lower === 'filename' && plain === null) plain = unquote(value);
  }
  return extended || plain || null;
}

/** Response headers, from axios' `AxiosHeaders` or a plain object, by lower-case name; list values joined. */
export function flatHeaders(raw: unknown): Record<string, string> {
  if (typeof raw !== 'object' || raw === null) return {};
  const source = raw as { toJSON?: () => Record<string, unknown> } & Record<string, unknown>;
  const flat = typeof source.toJSON === 'function' ? source.toJSON() : source;
  const out: Record<string, string> = {};
  for (const [name, value] of Object.entries(flat)) {
    if (typeof value === 'string') out[name.toLowerCase()] = value;
    else if (typeof value === 'number' || typeof value === 'boolean') out[name.toLowerCase()] = String(value);
    else if (Array.isArray(value)) out[name.toLowerCase()] = value.map(String).join(', ');
  }
  return out;
}

/** A `FileDownload` of a response body and its headers; a body that is not a Blob of that type is wrapped in one. */
export function fileDownload(data: unknown, rawHeaders: unknown): FileDownload {
  const headers = flatHeaders(rawHeaders);
  const contentType = headers['content-type'] ?? '';
  const blob =
    data instanceof Blob && data.type === contentType
      ? data
      : new Blob(data === undefined || data === null ? [] : [data as BlobPart], { type: contentType });
  return { blob, filename: contentDispositionFilename(headers['content-disposition']), contentType, headers };
}

/** Whether a `Content-Type` names JSON: `application/json` or a `+json` suffix. */
export function isJsonContentType(contentType: string | undefined): boolean {
  const essence = (contentType ?? '').split(';')[0].trim().toLowerCase();
  return essence === 'application/json' || (essence.startsWith('application/') && essence.endsWith('+json'));
}
