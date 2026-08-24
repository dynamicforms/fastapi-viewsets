# GAPS

Decisions that could go the other way, and the reasoning behind the way they went. Each is a thing
to argue about, not a thing that is settled. Entries that were argued about and settled are kept
where the reasoning is worth having on record, and marked as such.

## Still open

### The synthetic ASGI request costs a JSON round trip

`process_command` builds an ASGI scope and calls the FastAPI app rather than reaching into
`lifecycle_runner` directly. That buys total parity: parameter binding, validation, `Depends`,
command middleware, context processors, response models and status codes are the same code on both
transports, so they cannot drift.

It costs a decode/re-encode. muxws has already decoded the payload; we re-encode it to bytes for
FastAPI to parse, and decode FastAPI's response bytes again to hand back. Measured on a 50-record
page:

| | per call | share |
|---|---|---|
| whole muxws command | 0.457 ms | |
| the ASGI dispatch itself | 0.252 ms | 55% |
| decoding the response | 0.068 ms | 15% |
| encoding a 50-record request body | 0.076 ms | 17% |

A read pays about 15% for the round trip; a large write about a third. Real, and not where the time
goes: over half the call is FastAPI's own dispatch, which is precisely what the parity is bought
with. The honest fix is not to bypass FastAPI but to let it accept a pre-decoded body, and nothing
here is worth doing before that exists.

### A third transport wants a different parameter, not a third `register_*`

`route_viewset` takes `register_rest` and `register_muxws`. A third transport would make it three,
which is the shape that stops scaling.

A set of flags — `{REGISTER_REST, NO_REGISTER_MUXWS}` — is the worse of the two options: the
setting is tri-state (yes / no / defer), so a set needs a paired positive and negative member per
transport and can express the contradiction of holding both. A mapping cannot:
`transports={"rest": False}`, absent key meaning defer, is tri-state natively and grows by one key
per transport rather than two members.

Not built, because a third transport does not exist and may never — muxws would likely absorb a
Unix socket rather than sit beside one. When it does, the two `register_*` parameters become
shorthand for the mapping rather than being removed.

### `count` is best-effort

A list knows its length; a generator does not, and draining it to find out defeats the purpose, so
`count` is null for lazy sources. A backend that can count cheaply overrides `count_records()` —
the Django one answers with a `SELECT COUNT(*)` — so the null is a statement about the source, not
about the pipeline. A cursor page never counts at all, deliberately.

### A declined push-down reads the whole table, and says nothing

The pipeline lets a backend absorb a stage and report it with `mark_applied`; what it declines
falls to the in-memory default. Declining is all-or-nothing per stage — `DjangoORMViewSet.apply_filter`
marks `filter` applied only when every field in the set translated, because a filter half-done in
SQL and half-forgotten returns too many rows. One operator without a compiler is therefore enough
to send the entire filter set to Python, the client's own filters with it.

What that costs is not a slower query. The in-memory pass goes through `land()`, which materialises
the source and converts every row it materialised into the response model, and it runs before the
page is cut — so both the read and the validation are bounded by the table rather than by `limit`.
Nothing says so. `query.applied` records where each stage ran and dies with the request: no log, no
metric, no field on the response.

Left as it is because the obvious fix is noise. A backend that pushes nothing down — the collection
one — would log on every request, and a viewset filtering ten rows in memory is doing exactly the
right thing. For a message to mean anything the backend has to declare first that it *can* push
down, so the log can say that a stage it was able to absorb was declined, and why. Raising instead
of falling back is a third setting again: it trades a slow correct answer for a 500, which is right
for someone who knows the table is large and wrong as a default.

### OpenAPI cannot say that the response shape depends on a request header

A viewset offering several shapes documents them as an `anyOf`, with one named example per shape so
the docs say `plain` rather than `ListOf_MusicTrack_`. What no part of the schema can state is that
`X-List-Shape` selects between them — OpenAPI has no way to express a response that varies by
request header. That stays prose in the endpoint's description, and a generated client gets a union
it has to narrow itself.

Which is why declaring a single shape stays the recommended default rather than the fallback.

### The demo no longer exercises Celery by default

It used to, unconditionally, which made the transport benchmark meaningless: queueing through Redis
and waiting for a worker dwarfs the difference between HTTP and a WebSocket frame. `demo.py
--celery` turns it back on and an e2e spec covers that path, skipped when Redis is absent. So the
path is covered — just not by simply running the demo.

## The ViewSet class factory

Fifteen judgement calls made while building `restViewSet` / `muxwsViewSet`, recorded because they
were made without the maintainer in the room. Each states what was decided, why, and what would
change it. None is settled.

### The ViewSet factory is curried, and the first mistake everyone makes has a bad message

`restViewSet<Item>()('id', [ReadOnlyViewSetMixin])` has an empty call in the middle of it because TypeScript has no partial type-argument inference: the model must be stated and the pk field and the mixin list must be inferred, and one call cannot do both (TS2558). Currying is the only shape that delivers the requested syntax.

The cost is the diagnostic when the `()` is dropped, which I measured rather than guessed: `TS2507: Type '<PK extends "name" | "id", D extends …>(pkFieldName: PK, declares: D) => ViewSetClass<…>' is not a constructor function type` followed by `TS2554: Expected 0 arguments, but got 2` — the second says the thing takes no arguments at the exact moment the reader is passing its two real ones. Nothing in the type system improves this; every example in the docs shows the `()` with the reason inline, and that is the whole mitigation.

What would change it: an API that takes the model as a value instead of a type argument (`restViewSet(modelOf<Item>(), 'id', [...])`) would uncurry it at the cost of a fake runtime value, and I judged that a worse trade. If TypeScript ever ships partial inference, the `()` goes away without breaking a single call site.

### The action surface is a conditional intersection, not `Pick<>` of a table

`ActionSurface` is eleven `('list' extends A ? ListMixin<T> : unknown) &` conditionals rather than the obvious `Pick<ActionSignatures<K,T,PK>, A>`. The obvious version does not work: `Pick` is a mapped type, a mapped type re-emits methods as function-valued properties, and a subclass then cannot override an action with a method — `TS2425: … defines instance member property 'list', but extended class defines it as instance member function`. That is not a spelling problem; I rewrote the table in method syntax and re-picked, and the error is identical. Overriding `list()` to add caching is a documented pattern on `RestProxyImpl` today (docs/api/route-rest.md, 'RestProxyImpl') and it survives on the factory path only because of this choice.

The price is that the eleven mixin names are written once more, in one place, and a fourteenth action means editing `ActionSurface` as well as adding a mixin. Forgetting is safe but silent: the action would simply never appear on a factory-built ViewSet.

What would change it: if overriding an action were judged worthless on factory-built classes, `Pick` is shorter and the eleven conditionals collapse to one line. I would want to see that argued explicitly, because the capability is quiet — nobody notices losing it until they need it.

### Mixin members move into a merged interface and become methods

Each mixin is now an interface merged into a same-named class: the interface holds the members in method syntax, the class holds `static actions`. Two reasons. The factory needs method syntax (see the `Pick` entry), and `declare list(): Promise<T[]>` is illegal in a class body (TS1031), so the members had to move somewhere. And once they moved, the six composites could stop restating the thirteen members they inherit by hand — which is where `destroy` had already drifted into two different return types (`DestroyMixin` said `DestroyReturnData`, `ViewSetMixin` said `Record<string, unknown>`) without `implements` or the project typecheck noticing.

The published `.d.ts` changes shape: members appear as methods rather than function-valued properties. Assignability is preserved in every direction that matters — all 24 mixin-as-type sites in the repo compile untouched, including the five base-class usages, the `implements` clause on ViewSetProxyBase and both route_rest overloads — and method parameters are bivariant where property ones were contravariant, so the change only loosens. It is still a visible change to a published type.

What would change it: keeping mixins.ts byte-for-byte was tempting and I nearly took it. It costs a second copy of all thirteen signatures inside the factory, and leaves the drifted `destroy` in the file. If you would rather not touch the most-referenced file in the package right now, the fallback is exactly that: a private `ActionSignatures` table in viewset.ts, mixins.ts untouched, and the duplication accepted.

### Action names come from `keyof InstanceType<typeof Mixin>`, not from the `actions` tuple

The tuple route — `M['actions'][number]` — needs `as const` on every mixin's `static actions`, and `as const` is irreconcilable with the six mixin-to-mixin `extends` links: a two-element tuple is not assignable to a one-element tuple, so TS2417 fires on the static side and no annotation rescues it. Reading the names off the instance type instead means `static readonly actions: readonly string[]` stays exactly as written, the `extends` chain stays, the published `actions` type does not widen, and a shared `declares` array needs no `as const` at the call site — which is what lets the demo's REST and muxws twins pass the same constant.

What this rests on: a mixin's public instance members are its actions, and its `actions` list says the same thing. Nothing enforces the agreement. The merged form keeps it structurally honest — a composite's interface extends precisely the mixins its `actions` spread names — but a hand-written mixin could still declare a member it does not list, or list an action it does not declare. `Extract<…, ActionName>` means the failure direction is a missing action rather than a wrong one, so it fails closed.

What would change it: a mixin that ever needs a non-action public member. Today none has one, and the invariant is now stated in the mixins.ts header where its neighbours already live.

### `ViewSetInternals.request` is a concrete method whose body cannot run

`ViewSetInternals` is a real class — `basePath` plus `request` — that `ViewSetProxyBase` now extends, so `protected` stays nominally correct through the factory's returned type. Its `request` is concrete, with a `throw` that is never reached, and `ViewSetProxyBase` immediately re-declares it `protected abstract`. That looks backwards until you hit the reason: TypeScript propagates an abstract member through a constructor type, so with `request` abstract on the internals every consumer class fails `TS2515: Non-abstract class 'ItemApi' does not implement inherited abstract member request` — for a method the transport underneath it has always implemented. Re-abstracting one level down puts the obligation back where a transport actually is.

The alternatives were worse. A `protected declare request: (…) => …` property collides with the transports' methods (TS2425). An ambient `export declare abstract class` emits no runtime binding and breaks the barrel export at build time with `[MISSING_EXPORT]` — which vue/index.ts:1-2 explicitly warns against solving with `export type`.

What would change it: if TypeScript stopped propagating abstractness through construct signatures, the throw goes away and `request` becomes abstract in one place.

### `this.http` is not on a factory-built ViewSet

The factory's returned type intersects `ViewSetInternals`, which is `basePath` and `request()` and nothing else. A custom endpoint on a factory-built class therefore cannot reach axios directly, and what docs/guide/vue-custom-endpoints.md says about `this.http` now applies only to the `extends RestProxyImpl` form.

Deliberate: `http` is the one member that ties a method body to HTTP, and the whole point of the factory is that one body serves both transports — the demo's `count()` is exactly that. Exposing it would need a second internals class per transport, and the split between `ViewSetInternals` and a REST-flavoured one cannot be expressed in a single inheritance chain without RestProxyImpl inheriting the actions we are trying to hide.

What would change it: a real need that `request()` cannot express — a blob response, a progress callback, an interceptor-specific config. The escape hatch today is to keep extending RestProxyImpl, which is fully supported and unchanged. If that turns out to be common, the answer is to widen `request()`'s options, not to put axios back on the surface.

Confirmed as the standing decision: no such need has surfaced in the demo or anywhere else in the repo, so `request()` stays the only surface and this entry stays open rather than settled — it is reopened the day a concrete case for widening `request()` shows up, not preemptively.

### An empty or annotated `declares` list is an error whose type is the message

`restViewSet<Item>()('id', [])`, and any list annotated `ViewSetMixinClass[]` (which erases which mixins are in it), yield no action names. Left alone that produces a ViewSet with an empty surface and no complaint at the declaration — the failure only shows up later as TS2339 on every call. So `ViewSetClass` returns a string literal type in that case, and TS2507 prints it: `Type '"declares must name at least one action: pass the mixin classes themselves, unannotated"' is not a constructor function type`.

Using a type as an error message is a trick, and tricks age badly. It earns its place here because the alternative failure is silent and the honest ones (a non-empty tuple constraint) would force `as const` on every shared list — the thing the whole name-derivation approach was chosen to avoid.

What would change it: TypeScript gaining real custom diagnostics, or a decision that the silent empty surface is acceptable because nobody writes an empty list twice.

### The pk field is constrained, and K is derived from it

`PkFieldName<T>` restricts the pk argument to the fields of T whose type can be a key, and `K` becomes `NonNullable<T[PK]> & KeyType`. Three statements of the same fact — the `K` type argument, the `PK` type argument and the `pkFieldName` string, which nothing tied together in the demo's old declarations — collapse into one token.

Two deliberate details. The constraint is on the parameter, so a bad pk errors at the factory call (`Argument of type '"id"' is not assignable to parameter of type '"name"'`) rather than at every later call site as 'not assignable to never' — which is what the conditional-type version of this does, and it was the single worst diagnostic in any of the three proposals. And `NonNullable` is what makes an optional pk (`id?: number`, an extremely ordinary model) work at all; without it the same model collapses K to never.

What would change it: a backend whose pk field name is not a property of the FE model at all. Today `pkFieldName` is typed `string` on ProxyBaseOptions and that is expressible; on the factory it is not. Nothing in the repo does it, and the factory is not the only way in — but it is a real narrowing of what can be said.

### Two factories, not one

`restViewSet` and `muxwsViewSet` are separate because their options genuinely differ: muxws requires `peer`, REST accepts an optional `axiosInstance`. A single factory parameterised by transport would have to make `peer` optional and fail at runtime instead of at the call, which is the wrong trade for a library whose selling point is catching this class of thing at compile time.

The duplication is six lines. What would change it: a third transport, at which point the shape to reach for is probably a transport descriptor passed to one factory rather than a third sibling — the same argument the BE `route_viewset` entry in this file makes about `register_rest`/`register_muxws`.

### The cross-transport custom endpoint needs a class-expression mixin, and the library does not ship one

Sharing one method body across the REST and muxws twins requires a mixin function over the factory's returned class — `WithCount(restViewSet<T>()(...))`. The demo's previous trick, a free function with a structural `this: { request(...) }` assigned as a field, could not have worked: `request` is protected, so calling it is TS2684. It survived only because nothing called `count()`. The demo now uses the class-expression mixin.

Two things a reader must be told, both measured. The helper's return type must be annotated `TBase & (abstract new (...args: any[]) => Counts)` — an inferred return fails declaration emit with `TS4094: Property 'basePath' of exported anonymous class type may not be private or protected`, on the helper and on every class built from it. And the helper's body reaches only `ViewSetInternals`, so it can call `request()` but not `list()`; sharing a method that composes a declared action needs a wider constraint that restates the model and the action subset.

The library does not ship the helper. It is four lines, it is standard TypeScript, and a wrapper would have to guess at the constraint. What would change it: if every consumer ends up writing the same wrapper, exporting a typed one is a small addition — but I would rather see the copies first.

### A ViewSet cannot ask for a list shape, so declaring more than one is a claim it cannot honour

`GET {basePath}` answers in the shape the BE viewset declared as its default, and a client selects a
different one with an `X-List-Shape` header. This client cannot choose one per call: `RequestOptions` is
`{ query?, body? }` and neither transport takes a per-call header. It can send the header on *every*
call — muxws through its constructor `headers`, REST through the axios instance's defaults — which
pins one shape for the whole proxy rather than selecting per method. So `list()`, `listPage()` and
`listCursor()` issue the identical request, and
calling the one that does not match the BE's default decodes the wrong envelope silently:
`listPage()` against a cursor-default viewset yields `offset`/`limit`/`count` undefined.

The documentation now says this plainly rather than teaching the header. What it leaves standing is
that the demo declares `[BulkViewSetMixin, CursorListMixin, PaginatedListMixin, LookupMixin]` for
`/music`, whose default is `cursor` - so `listPage` is declared and unusable, and `list`, arriving
through `BulkViewSetMixin`, is in exactly the same position. It calls only `listCursor`, so nothing
is broken; the declaration is simply wider than the truth.

Two ways out, and the choice is not obvious. Give `RequestOptions` a `headers` field and have each
list method send the shape it wants, which makes the declaration honest and costs a per-call header
on both transports. Or accept that the shape is a property of the endpoint, and have the demo
enumerate leaves - `[CursorListMixin, CreateMixin, RetrieveMixin, …]` - which is truthful and
verbose, and makes the composites useless for any viewset with `list_shapes`.

## Settled, kept for the reasoning

### An unhandled exception answers 500 rather than resetting the stream

muxws' own idiom is that a handler which raises produces `reset(APPLICATION_ERROR)`. This does not.
By the time the app re-raises it has already produced a 500, and an HTTP client would simply have
received it: a status is an answer, not a transport failure. Resetting would tell the caller the
connection misbehaved when the server in fact answered, and would make the two transports report
the same server bug in two different shapes. The traceback goes to the log instead.

### Two viewsets on one base path warn rather than raise

Routing picks the first match, so the second viewset's endpoints are unreachable, and neither
FastAPI nor this library would otherwise say so. A warning rather than an error, because that is
proportionate to how easily it is fixed and because refusing would be a new way for an application
that starts today to stop starting.

It was briefly removed for firing 75 times in this project's own suite — test scaffolding is
indistinguishable from the real mistake. That was letting test convenience decide a production
question; the suite filters it in `pyproject.toml` instead.

### Response status travels in leading headers

It rode in `trailers` until muxws 0.3.1, which carries `headers` on the first `data` frame a peer
sends as well as on `open`. Trailers only attach to a frame with `end: true`, so the status arrived
*after* the body — free for a unary reply, wrong for a streaming one, where a caller would have had
to read a whole response to discover it was an error.

### The cursor is not signed

Base64 over JSON is transport encoding, not protection: a client can read the ordering values and
forge them. Dismissed on argument — the cursor only ever contains values from rows that client was
already served, and the queryset it indexes into still comes from `get_queryset(context)`, so
tampering moves you around inside data you could already reach, which a filter would do more
easily. Sign it only if the ordering fields are themselves sensitive.

### Filters are in the cursor's fingerprint

A cursor issued under one filter is refused under another, so changing a filter restarts paging.
Stricter than strictly necessary — the position is still well defined in the ordering — but it
matches what a filter change already did, and the page you would otherwise get is one nobody asked
for.

### One list endpoint can serve several shapes

Paging was first built as an opt-in per request, then abandoned, then rebuilt. The lesson is in the
middle step.

The soft reason to abandon it was that clients must branch on the shape they got. The hard one was
that `list[T] | PaginatedList[T]` broke TypeVar resolution outright: `types.UnionType` is not
subscriptable, so the resolver handed back the annotation unresolved, and pydantic collapsed
`PaginatedList[T]` to the bare class inside a union's `get_args`. The schema silently described
results as a list of anything.

That blocker was removed as a side effect of later work — `PaginatedList` moved to its own TypeVar
and `resolve_typevars` learned about pydantic generics — and nobody went back to re-examine the
decision it had forced, until it came up again in conversation. `typing.Union` (not `X | Y`) now
resolves correctly, so a viewset can declare `list_shapes` and offer all three.

Worth keeping: a decision forced by a technical limit needs revisiting when the limit goes, and
nothing prompts that on its own.

### Celery already carried the whole list pipeline

A design was proposed where the list parameters are shipped to the worker, the worker reports back
which it implemented, and FastAPI applies the rest. Half was already true and the other half should
not be built.

`celery_viewset_client` patches the route endpoints — `list_items`, not `perform_list` — so the
entire pipeline already runs in the worker, and filter, sort and pagination parameters already
cross. Which means there is nothing to report back: `query.mark_applied()` is an intra-process
contract and the worker *is* that process. Reporting back would mean shipping unreduced records
across the queue to be reduced somewhere else, which is the opposite of what push-down is for.

### `STANDARD_FE_METHODS` is now the exhaustiveness proof, not a list beside one

`ENDPOINT_TO_FE_METHOD` and `STANDARD_FE_METHODS` in proxy-base.ts are two independently hand-written
tables sharing the same thirteen action names, each typed `readonly ActionName[]`; a typo or a
renamed action failed to compile, but nothing checked that either table named all thirteen, so an
action added to `ActionName` without a matching entry in both would have compiled clean and simply
never been reported by the schema check.

`STANDARD_FE_METHODS` is now `Object.keys()` of `ACTION_NAME_COVERAGE`, a `Record<ActionName, true>`
object literal: TypeScript checks an object literal against a `Record` for both a missing key and an
excess one, so dropping an entry here - or `ActionName` gaining one with no matching entry - fails to
compile instead of silently narrowing what the mismatch check can report. `ENDPOINT_TO_FE_METHOD`
stays a separate table, since it maps each BE endpoint/verb pair to the action names *that* endpoint
answers for rather than merely naming actions, and the two cannot collapse into one without losing
that mapping.

### `static declares` on a factory-built class names itself in the whole error, not just the head line

The returned class type carries `readonly declares: D`, so a subclass restating it — `class
ItemDbApi extends ItemApi { static declares = [CursorListMixin] }` — fails with TS2417. On a
factory-built class that is right: the type and the declaration come from one call, and a subclass
that changes one without the other is the exact divergence this library exists to catch. To narrow,
call the factory again.

The head line named `declares`, but the message used to bottom out several levels down in which
specific method one mixin was missing relative to another - noise once the head line had already
said enough. `declares` is now typed `FactoryDeclares<D>`, a record type keying `D` behind the
`FACTORY_BUILT` `unique symbol` (declared in proxy-base.ts, not viewset.ts - see the next entry for
why) rather than `D & {brand}`: an intersection would still expose `D`'s own array shape to the
comparison and recurse into it exactly as before, but a plain array literal (which is all a restated
`static declares` ever is) has no property at all under that key, so the mismatch now stops at one
flat "Property '[FACTORY_BUILT]' is missing... required in type 'FactoryDeclares<...>'" line. Erased
at runtime: `bindViewSet` casts through `unknown` to produce the base class in the first place, and
every internal reader of `declares` (rest-proxy.ts, muxws-proxy.ts, proxy-base.ts's
`declaredActions`) already read it through its own cast rather than through this type, so none of
them needed to change.

### route_rest and route_muxws now refuse a factory-built class at compile time

`route_rest<InstanceType<typeof ItemApi>>(ItemApi, '/items', 'id')` used to type-check and hand back
an object on which `ItemApi`'s own methods did not exist, because route_rest builds a bare
RestProxyImpl and throws the class away rather than using it - the same hole any hand-written
subclass with custom methods always had, just easier to hit now that the factory makes
classes-with-methods the normal thing to write. docs/api/route-rest.md said plainly not to do it;
nothing enforced that.

Both `route_rest` and `route_muxws` gain a pair of overloads, tried first, whose parameter type is
`FactoryBuiltClass` - the same `{ declares: { [FACTORY_BUILT]: any } }` shape `viewset.ts`'s
`FactoryDeclares<D>` produces - and whose return type is a self-describing string literal rather
than a usable proxy. `FACTORY_BUILT` moved to proxy-base.ts, the one module both `viewset.ts` and
the two proxy modules already import from, so all three sides check the same `unique symbol` without
a new import cycle. A `C extends ViewSetClass` type parameter conditioned on the argument
(`C extends FactoryBuiltClass ? Message : C`) was tried first and does not work: TypeScript cannot
infer a type parameter that appears only inside a conditional's checked position, so `C` silently
fell back to its default and the check never saw the real argument - a plain overload parameter is
the only form that does. One more consequence of the return type being a string rather than `never`
or throwing: a call whose result is never used produces no diagnostic regardless of which overload
matched (an unused expression is not itself an error), so the visible `TS2339` lands on the first
property or method the caller accesses on the result, not on the `route_rest`/`route_muxws` call -
both test files' `@ts-expect-error` sit there for exactly that reason, and so does the sentence
saying so in the two API doc pages.

### `pkFieldName` is public now, not dead

`ViewSetProxyBase.pkFieldName` was assigned at construction and never read anywhere in the library,
the demo or the tests - but it was also `protected`, so nothing outside a subclass could have read
it even if it wanted to. A generic caller holding a ViewSet instance without knowing its concrete
class - a grid or table component asking "which field identifies a row" - had no way to ask, which
is a real, near-term use case (`demo/frontend/src/App.vue`'s grid column and filter-param setup, and
its `key-field` binding, all hardcoded `'id'` for exactly this reason) rather than a hypothetical
one. Made `readonly` and public instead of removed.

A factory-built class needed its own fix on top: the constructor `ViewSetClass<D>` returns is
`ViewSetInternals & ActionSurface<...>`, and `ViewSetInternals` is deliberately narrow (protected
`basePath`/`request` only - see its own doc comment), so it did not expose `pkFieldName` even though
the real runtime object (a `RestProxyImpl`/`MuxwsProxyImpl` under the hood) always carried it. The
factory's return type now intersects one more member, `{ readonly pkFieldName: PK }` - typed as the
literal `PK`, e.g. `'id'`, not widened to `string`, since the factory already knows exactly which
field it is.
