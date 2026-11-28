/**
 * REST proxy for ViewSets: the FE counterpart of the BE `route_viewset` decorator. Only the sending
 * lives here; the rest is in ViewSetProxyBase (proxy-base.ts). Every call carries an `Idempotency-Key`
 * and is repeated with it after a network error, an attempt timeout or a gateway status (call.ts).
 */

import axios, {
  AxiosError,
  type AxiosInstance,
  type AxiosProgressEvent,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';

import {
  type AttemptFailure,
  type CallAttempt,
  issueCall,
  newCallKey,
  runCall,
  type ViewSetCall,
  type ViewSetCalls,
  withKeyHeader,
} from './call';
import { isApiErrorBody } from './errors';
import type { KeyOf, RecordOf, ViewSetWatch } from './events/changes';
import { type FileDownload, fileDownload, flatHeaders, isJsonContentType } from './file-download';
import type { DefaultSortOf, KeyType } from './mixins';
import {
  type BlobRequestOptions,
  FACTORY_BUILT,
  type HttpMethod,
  type ProxyBaseOptions,
  type RequestOptions,
  type ViewSetMixinDeclaration,
  ViewSetProxyBase,
} from './proxy-base';

// ---------------------------------------------------------------------------
// Helper types
// ---------------------------------------------------------------------------

/** ViewSet class constructor (for type-level introspection only). */
type ViewSetClass = abstract new (...args: any[]) => any;

/**
 * A factory-built class's constructor, matched by the `route_rest` overloads that reject it:
 * `route_rest` never instantiates the class, so the factory-built class's own methods would be
 * `undefined` on the proxy it returns. `declares` is required so that a hand-written class falls
 * through to the real overloads.
 */
type FactoryBuiltClass = (abstract new (...args: any[]) => any) & {
  declares: { readonly [FACTORY_BUILT]: any };
};

/** Never assigned; its only use is `typeof FACTORY_BUILT_REJECTION` as a self-describing return type. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- type-only, read via `typeof` below
declare const FACTORY_BUILT_REJECTION: 'route_rest cannot take a factory-built class - extend it directly instead';

/**
 * The mixin interface `M` the caller declares, with every method returning a `ViewSetCall`, plus
 * `watch()`, and `defaultSort()` when `M` has a list action.
 */
export type RestProxy<M> = ViewSetCalls<M> & ViewSetWatch<RecordOf<M>, KeyOf<M>> & DefaultSortOf<keyof M>;

/** Options of a ViewSet proxy over HTTP. */
export interface RestProxyOptions extends ProxyBaseOptions {
  /**
   * Optional: existing axios instance. Defaults to the global axios. Its `defaults.onUploadProgress` and
   * `defaults.onDownloadProgress` are called for every call, next to the proxy's own handlers.
   */
  axiosInstance?: AxiosInstance;
  /**
   * How long one attempt may go without progress, in milliseconds, counted from its start or its last
   * upload (of a file body or with `onUploadProgress`) or download progress event; an attempt that
   * reaches it is repeated at once. `0` means no timeout. Default 30000.
   */
  attemptTimeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Proxy implementation
// ---------------------------------------------------------------------------

/** A ViewSet proxy that sends its calls over HTTP with axios. */
export class RestProxyImpl<K extends KeyType, T, PK extends keyof T> extends ViewSetProxyBase<K, T, PK> {
  protected readonly http: AxiosInstance;

  private readonly attemptTimeoutMs: number;

  constructor(options: RestProxyOptions) {
    super(options);
    this.http = options.axiosInstance ?? axios;
    this.attemptTimeoutMs = options.attemptTimeoutMs ?? DEFAULT_ATTEMPT_TIMEOUT_MS;
  }

  /**
   * Sends the call with its `Idempotency-Key` and repeats it with the same key while an attempt ends
   * without an answer (see `restAttemptFailure`). A non-2xx rejects with axios' own AxiosError.
   */
  protected request(method: HttpMethod, path: string, options: BlobRequestOptions): ViewSetCall<FileDownload>;
  protected request<R>(method: HttpMethod, path: string, options?: RequestOptions): ViewSetCall<R>;
  protected request<R>(method: HttpMethod, path: string, options: RequestOptions = {}): ViewSetCall<R> {
    const key = options.key ?? newCallKey();
    let call: Promise<R>;
    try {
      const url = `${this.basePath}${path}`;
      const headers = withKeyHeader({ ...options.headers }, key);
      // A repeat is sent with the first attempt's headers, interceptor-added ones included: an
      // interceptor reading state bound only for the caller's synchronous part does not see it later.
      let sent: Record<string, string> | undefined;
      call = runCall(
        () =>
          this.attempt<R>(method, url, options, sent ?? headers, (config) => (sent ??= sentHeaders(config, headers))),
        restAttemptFailure,
        options.repeat ?? this.repeatOptions,
        undefined,
        key,
      );
    } catch (error) {
      call = Promise.reject(error);
    }
    return issueCall(call, key, options.background);
  }

  /**
   * One attempt, through axios' per-verb methods: application test doubles stub those, not `request()`.
   * It fails as a timeout (`ECONNABORTED`) after `attemptTimeoutMs` without upload or download progress.
   */
  private attempt<R>(
    method: HttpMethod,
    url: string,
    options: RequestOptions,
    headers: Record<string, string>,
    onFailure: (config: AxiosRequestConfig | undefined) => void,
  ): CallAttempt<R> {
    const controller = new AbortController();
    const idleMs = this.attemptTimeoutMs;
    const blob = options.responseType === 'blob';
    let timer: ReturnType<typeof setTimeout> | undefined;
    let expired = false;
    let settled = false;
    let rearm = (): void => {};
    const progress = (): void => rearm();
    const onUpload = options.onUploadProgress;
    // A request-level handler replaces the instance default in axios; these call it as well.
    const base = this.http.defaults ?? {};

    const config: AxiosRequestConfig = {
      // An array goes out as a repeated key (`ids=1&ids=2`), the form FastAPI reads into a list;
      // axios' default, `ids[]=1&ids[]=2`, reaches FastAPI as an unknown `ids[]` parameter.
      ...(options.query !== undefined && Object.keys(options.query).length > 0
        ? { params: options.query, paramsSerializer: { indexes: null } }
        : {}),
      headers,
      // The attempt timeout is the idle timer below; 0 keeps an instance default from adding a total one.
      timeout: 0,
      signal: controller.signal,
      ...(blob ? { responseType: blobResponseType() } : {}),
      // Only a file body reports upload progress to the idle timer: with the fetch adapter, a progress
      // handler makes axios stream the body, which needs HTTP/2.
      ...(onUpload || (idleMs > 0 && isFileBody(options.body))
        ? {
            onUploadProgress: (event: AxiosProgressEvent) => {
              progress();
              onUpload?.(event.loaded, event.total);
              base.onUploadProgress?.(event);
            },
          }
        : {}),
      ...(idleMs > 0
        ? {
            onDownloadProgress: (event: AxiosProgressEvent) => {
              progress();
              base.onDownloadProgress?.(event);
            },
          }
        : {}),
    };

    let response: Promise<AxiosResponse<R>>;
    switch (method) {
      case 'GET':
        response = this.http.get<R>(url, config);
        break;
      case 'POST':
        response = this.http.post<R>(url, options.body, config);
        break;
      case 'PUT':
        response = this.http.put<R>(url, options.body, config);
        break;
      case 'PATCH':
        response = this.http.patch<R>(url, options.body, config);
        break;
      case 'DELETE':
        // A DELETE body has to go through the config object; axios has no positional slot for it.
        response = this.http.delete<R>(url, options.body === undefined ? config : { ...config, data: options.body });
        break;
    }

    const result = new Promise<R>((resolve, reject) => {
      const fail = (error: unknown): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        onFailure(axios.isAxiosError(error) ? error.config : undefined);
        reject(error);
      };
      const expire = (): void => {
        expired = true;
        controller.abort();
        // axios rejects an aborted request with a CanceledError; a client that ignores the signal fails a tick later.
        timer = setTimeout(() => fail(idleTimeoutError(idleMs, config)), 0);
      };
      rearm = () => {
        if (settled || expired || idleMs <= 0) return;
        clearTimeout(timer);
        timer = setTimeout(expire, idleMs);
      };
      rearm();

      Promise.resolve(response).then(
        (res) => {
          if (settled) return;
          clearTimeout(timer);
          let data: R;
          try {
            data = (blob ? fileDownload(res.data, res.headers) : res.data) as R;
          } catch (error) {
            fail(error);
            return;
          }
          settled = true;
          resolve(data);
        },
        async (error: unknown) => {
          if (settled) return;
          clearTimeout(timer);
          if (expired) {
            fail(idleTimeoutError(idleMs, axios.isAxiosError(error) ? error.config : config, error));
            return;
          }
          if (blob) await decodeJsonErrorBody(error);
          fail(error);
        },
      );
    });
    return {
      result,
      abandon: () => {
        clearTimeout(timer);
        controller.abort();
      },
    };
  }
}

/** Whether `body` is sent as bytes rather than as JSON: a `FormData`, `Blob`, `ArrayBuffer` or typed array. */
function isFileBody(body: unknown): boolean {
  return (
    (typeof FormData !== 'undefined' && body instanceof FormData) ||
    (typeof Blob !== 'undefined' && body instanceof Blob) ||
    isArrayBuffer(body) ||
    ArrayBuffer.isView(body)
  );
}

/** Whether `value` is an `ArrayBuffer`, also one of another realm (a Node buffer's under jsdom). */
function isArrayBuffer(value: unknown): value is ArrayBuffer {
  return Object.prototype.toString.call(value) === '[object ArrayBuffer]';
}

/** The error of an attempt that saw no progress for `idleMs`: an axios timeout, classified `'timeout'`. */
function idleTimeoutError(idleMs: number, config: AxiosRequestConfig | undefined, cause?: unknown): AxiosError {
  const request = (cause as { request?: unknown } | undefined)?.request;
  return new AxiosError(
    `timeout of ${idleMs}ms without progress exceeded`,
    AxiosError.ECONNABORTED,
    config as InternalAxiosRequestConfig | undefined,
    request,
  );
}

/**
 * The axios `responseType` of a blob call: `'blob'` where XHR delivers a Blob, `'arraybuffer'` without
 * XHR. axios' Node http adapter has no `'blob'` and decodes any other body to a UTF-8 string, which
 * corrupts every byte above 0x7F.
 */
function blobResponseType(): 'blob' | 'arraybuffer' {
  return typeof XMLHttpRequest === 'undefined' ? 'arraybuffer' : 'blob';
}

/** A Blob's text as UTF-8; through `FileReader` where `Blob.text()` is missing. */
function blobText(blob: Blob): Promise<string> {
  if (typeof blob.text === 'function') return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

/** The UTF-8 text of a binary error body (Blob, ArrayBuffer, typed array or Buffer); null for another value. */
function bodyText(data: unknown): Promise<string> | null {
  if (typeof Blob !== 'undefined' && data instanceof Blob) return blobText(data);
  if (isArrayBuffer(data) || ArrayBuffer.isView(data)) {
    return Promise.resolve(new TextDecoder().decode(data as ArrayBuffer | ArrayBufferView));
  }
  return null;
}

/**
 * Replaces a binary error body of a JSON type with the decoded object, so that the failure is classified
 * and read (`errorBodyOf`) as for a JSON call. A body that does not parse is left as it is.
 */
async function decodeJsonErrorBody(error: unknown): Promise<void> {
  if (!axios.isAxiosError(error) || !error.response) return;
  const { data } = error.response;
  const contentType =
    flatHeaders(error.response.headers)['content-type'] ??
    (typeof Blob !== 'undefined' && data instanceof Blob ? data.type : undefined);
  if (!isJsonContentType(contentType)) return;
  try {
    const text = bodyText(data);
    if (text === null) return;
    error.response.data = JSON.parse(await text);
  } catch {
    // not JSON after all: the body stays
  }
}

/**
 * Headers axios sets from the request itself, which a repeat leaves to axios to set again: a body
 * type it derives (a `FormData` boundary) must not be carried over.
 */
const TRANSPORT_HEADERS = new Set(['accept', 'accept-encoding', 'content-length', 'content-type', 'user-agent']);

/** The headers an attempt went out with after the request interceptors, less `TRANSPORT_HEADERS`. */
function sentHeaders(config: AxiosRequestConfig | undefined, headers: Record<string, string>): Record<string, string> {
  const raw = config?.headers as { toJSON?: () => Record<string, unknown> } | Record<string, unknown> | undefined;
  if (!raw) return headers;
  const flat = (typeof raw.toJSON === 'function' ? raw.toJSON() : raw) as Record<string, unknown>;
  const own = new Set(Object.keys(headers).map((name) => name.toLowerCase()));
  const out: Record<string, string> = {};
  for (const [name, value] of Object.entries(flat)) {
    const lower = name.toLowerCase();
    if (typeof value === 'string' && !TRANSPORT_HEADERS.has(lower) && !own.has(lower)) out[name] = value;
  }
  return { ...out, ...headers };
}

/** The default of `RestProxyOptions.attemptTimeoutMs`. */
const DEFAULT_ATTEMPT_TIMEOUT_MS = 30_000;

/** The statuses a gateway answers when the application behind it did not. */
const GATEWAY_STATUSES = new Set([502, 503, 504]);

/**
 * An axios error without `response` got no answer: `'timeout'` for the attempt's own timeout, `'lost'`
 * otherwise. A gateway status (502-504) is `'lost'` unless its body is an `ApiErrorBody`: then it is
 * the application's answer, which a repeat would only receive again.
 */
function restAttemptFailure(error: unknown): AttemptFailure {
  if (!axios.isAxiosError(error) || axios.isCancel(error)) return null;
  if (error.response === undefined) {
    return error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT' ? 'timeout' : 'lost';
  }
  if (!GATEWAY_STATUSES.has(error.response.status)) return null;
  return isApiErrorBody(error.response.data) ? null : 'lost';
}

// ---------------------------------------------------------------------------
// Decorator / factory
// ---------------------------------------------------------------------------

/**
 * Builds a REST proxy for a ViewSet; `M` names the mixin interfaces it exposes.
 *
 * @example
 * const items = route_rest<BulkViewSetMixin<number, Item>>(ItemViewSet, '/items', 'id');
 * const item = await items.retrieve(1);
 */
// The first two overloads reject a factory-built class with a message at the call site.
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- M keeps the call syntax identical to the real overload
function route_rest<M = never>(
  viewSetClass: FactoryBuiltClass,
  basePath: string,
  pkFieldName: string,
  axiosInstance?: AxiosInstance,
): typeof FACTORY_BUILT_REJECTION;
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- M keeps the call syntax identical to the real overload
function route_rest<M = never>(
  viewSetClass: FactoryBuiltClass,
  options: RestProxyOptions,
): typeof FACTORY_BUILT_REJECTION;
// The positional form takes no `events`, so its type carries no `watch()`.
function route_rest<M>(
  _viewSetClass: ViewSetClass,
  basePath: string,
  pkFieldName: string,
  axiosInstance?: AxiosInstance,
): ViewSetCalls<M>;
function route_rest<M>(_viewSetClass: ViewSetClass, options: RestProxyOptions): RestProxy<M>;
function route_rest<M>(
  viewSetClass: ViewSetClass,
  basePathOrOptions: string | RestProxyOptions,
  pkFieldName?: string,
  axiosInstance?: AxiosInstance,
): RestProxy<M> | ViewSetCalls<M> | typeof FACTORY_BUILT_REJECTION {
  const options: RestProxyOptions =
    typeof basePathOrOptions === 'string'
      ? {
          basePath: basePathOrOptions,
          pkFieldName: pkFieldName!,
          axiosInstance,
        }
      : basePathOrOptions;
  // The class is used only for its type and its `declares`.
  return new RestProxyImpl({
    declares: (viewSetClass as { declares?: ViewSetMixinDeclaration[] }).declares,
    ...options,
  }) as unknown as RestProxy<M>;
}

export { route_rest };
