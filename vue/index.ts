// Values, not types: a ViewSet names them in a value position — `static declares =
// [ReadOnlyViewSetMixin]` — which is TS1362 for a name exported as a type, so the list cannot be
// written at all. The schema check reads each mixin's `actions` off that list at runtime.
export {
  BulkCreateMixin,
  BulkDestroyMixin,
  BulkOnlyCreateMixin,
  BulkOnlyDestroyMixin,
  BulkOnlyUpdateMixin,
  BulkUpdateMixin,
  BulkViewSetMixin,
  CreateMixin,
  CursorListMixin,
  DestroyMixin,
  ListMixin,
  LookupMixin,
  PaginatedListMixin,
  ReadOnlyViewSetMixin,
  RetrieveMixin,
  UpdateMixin,
  ViewSetMixin,
} from './mixins';

export type {
  ActionName,
  ActionSurface,
  CursorPage,
  DestroyReturnData,
  CursorParams,
  KeyType,
  ListParams,
  LookupItem,
  PageParams,
  PaginatedList,
} from './mixins';

export type { HttpMethod, ProxyBaseOptions, QueryParams, RequestOptions, ViewSetMixinDeclaration } from './proxy-base';
export { ViewSetInternals, ViewSetProxyBase, ViewSetRequestError } from './proxy-base';

export type { PkFieldName, ViewSetClass, ViewSetMixinClass } from './viewset';
export { muxwsViewSet, restViewSet } from './viewset';

export type { RestProxy, RestProxyOptions } from './rest-proxy';
export { route_rest, RestProxyImpl } from './rest-proxy';

export type { MuxwsPeerLike, MuxwsPeerSource, MuxwsProxy, MuxwsProxyOptions, MuxwsStreamLike } from './muxws-proxy';
export { route_muxws, MuxwsProxyImpl } from './muxws-proxy';

export type { ApiErrorBody } from './errors';
export { translatableStrings, translateApiError, translateStrings } from './errors';
