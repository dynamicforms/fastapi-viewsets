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

### `fastapi` is capped below 0.137, and needs unpinning ASAP

`fastapi` 0.137.0 replaced `include_router`'s flattened route list with a lazy view
(`_IncludedRouter` / `_EffectiveRouteContext` in `fastapi.routing`): a router that has been included
into another no longer contributes plain `APIRoute` objects to `app.routes`, so any code walking
that list and reading `.path` off what it finds breaks with `AttributeError: '_IncludedRouter'
object has no attribute 'path'`. Bisected precisely: `fastapi==0.136.3` is clean, `0.137.0` is the
first broken release; the current PyPI latest (`0.141.1`) is on the new engine throughout.

The library's own runtime is not on that path: `route_viewset.py`, `build_schema.py` and
`celery_viewset/{server,client}.py` only ever walk `cls.__router.routes` — the per-viewset router
this library builds directly and never passes through `include_router` itself — so schema building,
dispatch and Celery task registration are unaffected, and every test that exercises them through a
real request (`TestClient(...).get(...)`) passes unmodified against `0.141.1`. The five tests that
broke (`custom_endpoints_test.py` ×4, `route_viewset_depends_test.py` ×1) all read
`{route.path for route in app.routes}` directly against the *app's* post-include route list to
assert a route got registered — an assumption about `app.routes`'s internal shape, not about this
library's dispatch. Confirmed independent of Starlette: the full suite is green against
`fastapi==0.135.1` paired with `starlette==1.6.0`, so only the `fastapi` version is load-bearing
here — nothing needs pinning on `starlette`.

Pinned rather than fixed immediately because the fix needs to land as its own change: rewrite those
five assertions off the internal route list (recurse through `_IncludedRouter.original_router`, or
switch to asserting against `app.openapi()["paths"]` / an actual request instead, which is the more
future-proof form and stops relying on `include_router`'s internal shape at all), then re-run the
full matrix (3.10-3.13) and sweep the rest of the codebase for the same `app.routes`/`.path`
assumption (`mux_ws/registry.py`, `endpoint_docs.py`, the docs guide examples) in case something
there hits the same wall under real usage rather than just in a test. Estimated at half a day for
the five known assertions plus verification, and up to a full day with the sweep and matrix re-run
included, since `fastapi` has moved four more feature releases past 0.137.0 by the time this is
picked up and the sweep may turn up more than the five known sites.

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

### `static declares` on a factory-built class is a compile error

The returned class type carries `readonly declares: D`, so a subclass restating it — `class ItemDbApi extends ItemApi { static declares = [CursorListMixin] }` — fails with TS2417. On a factory-built class that is right: the type and the declaration come from one call, and a subclass that changes one without the other is the exact divergence this library exists to catch. To narrow, call the factory again.

Worth knowing before this is filed as a regression: it is the same error a hand-written `extends RestProxyImpl` subclass already gets today for any list that is not a subset of its parent's — I reproduced it against the unmodified repo, identical message. The demo escapes it today only because CursorListMixin happens to be in the parent's list. So the rule is not new; the factory just makes it apply to the narrowing case too.

The message is not great — it bottoms out in 'Property 'lookup' is missing in type CursorListMixin' — but the head line names `declares`, and the alternative I tested (a branded array type with a self-describing property name) buys a better sentence at the cost of an invented concept in the public types. What would change it: if the error turns out to confuse people in practice, the brand is a two-line change.

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

### route_rest and route_muxws stay, and remain a way to lose your own methods

They are not deprecated and not changed. Removing them is a semver-major and you are away; the docs present the factory as the way to declare a ViewSet and leave the old pages standing as the older form.

One sharp edge is worth naming rather than fixing: `route_rest<InstanceType<typeof ItemApi>>(ItemApi, '/items', 'id')` type-checks and hands back an object on which `ItemApi`'s own methods do not exist, because route_rest builds a bare RestProxyImpl and throws the class away (rest-proxy.ts:161-164). That hole is not new — it is the same one that exists today for any hand-written subclass with custom methods — but the factory makes classes-with-methods the normal thing to write, so it becomes easier to hit. docs/api/route-rest.md says plainly not to pass a factory-built class to route_rest.

What would change it: making route_rest reject a class that carries the factory's brand would close it at compile time, at the cost of another invented type. If the two forms are going to coexist for a long time, that may be worth doing.

### The cross-transport custom endpoint needs a class-expression mixin, and the library does not ship one

Sharing one method body across the REST and muxws twins requires a mixin function over the factory's returned class — `WithCount(restViewSet<T>()(...))`. The demo's previous trick, a free function with a structural `this: { request(...) }` assigned as a field, could not have worked: `request` is protected, so calling it is TS2684. It survived only because nothing called `count()`. The demo now uses the class-expression mixin.

Two things a reader must be told, both measured. The helper's return type must be annotated `TBase & (abstract new (...args: any[]) => Counts)` — an inferred return fails declaration emit with `TS4094: Property 'basePath' of exported anonymous class type may not be private or protected`, on the helper and on every class built from it. And the helper's body reaches only `ViewSetInternals`, so it can call `request()` but not `list()`; sharing a method that composes a declared action needs a wider constraint that restates the model and the action subset.

The library does not ship the helper. It is four lines, it is standard TypeScript, and a wrapper would have to guess at the constraint. What would change it: if every consumer ends up writing the same wrapper, exporting a typed one is a small addition — but I would rather see the copies first.

### The SQLite demo backend gains a retrieve endpoint instead of the benchmark losing one

demo/frontend/src/benchmark.ts calls `retrieve()` on whichever backend the UI has selected, and the Django-backed viewset serves no such endpoint — a live 404 that the current FE types cannot see, because `declares` narrows nothing. Under the factory it becomes a compile error, so one side has to give. I added `RetrieveMixin` to MusicTrackDbViewSet: one line, verified to register `GET /{pk}`, and it keeps the benchmark comparing the two backends on the same operation.

The alternative — benchmarking only the in-memory backend — would leave App.vue's backend toggle feeding a number that does not depend on it, which is a demo that lies.

What would change it: if the Django viewset is meant to demonstrate a deliberately minimal surface, then the benchmark is the thing to change, and it should say in the UI that it always measures the in-memory backend.

### `STANDARD_FE_METHODS` is typed but not proved exhaustive

Both constant tables in proxy-base.ts are now typed `readonly ActionName[]`, so a typo or a renamed action fails to compile. Neither is checked for coverage: a list naming twelve of the thirteen actions still compiles, and the missing one would simply never be reported by the schema check.

A coverage check is possible — one `Record<ActionName, true>` assertion — and I left it out because it is a second concept in a file that reads well without it, and because the realistic failure is a typo rather than an omission.

What would change it: an action being added and quietly not reported. That is a cheap five-line insurance policy if it ever happens once.

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

### `pkFieldName` is still stored and still read by nobody

`ViewSetProxyBase.pkFieldName` is assigned at construction and never read anywhere in the library, the demo or the tests. The factory binds it at factory time and removes it from the constructor's options, so on the new form it is one fewer thing to state — but the field, the `ProxyBaseOptions` member and the `route_rest` positional argument all remain.

I left it alone on purpose: removing a public option is a separate decision from adding a factory, and it would touch route_rest's signature, both its overloads and three doc pages.

What would change it: deciding it is genuinely dead. Then it goes in one commit that also simplifies route_rest's positional form, and `ProxyBaseOptions` loses a required member — which is a breaking change for anyone constructing a proxy directly.

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
