---
layout: home

hero:
  name: DynamicForms Viewsets
  text: Full-stack ViewSet library
  tagline: Django REST Framework–style viewsets for FastAPI, Celery and Vue/TypeScript
  actions:
    - theme: brand
      text: Get Started
      link: /guide/getting-started
    - theme: alt
      text: API Reference
      link: /api/python-mixins
    - theme: alt
      text: GitHub
      link: https://github.com/dynamicforms/fastapi-viewsets
    - theme: alt
      text: Changelog
      link: /guide/changelog

features:
  - title: Python mixins for FastAPI
    details: Compose CRUD and bulk endpoints from small, focused mixin classes — just like Django REST Framework, but for FastAPI.
  - title: route_viewset decorator
    details: Register a viewset on a FastAPI router with a single decorator call. Handles type resolution, lifecycle management and OpenAPI schema automatically.
  - title: Backends in the box
    details: CollectionViewSet is a zero-boilerplate viewset backed by any Python list, set or dict. DjangoORMViewSet backs one with a QuerySet, absorbing filtering, ordering and slicing into SQL.
  - title: muxws WebSocket transport
    details: Reach the same viewsets over a single WebSocket instead of one HTTP request per call. Commands dispatch into an app the library builds from the endpoints published on muxws, each carrying the route kwargs REST is given, so validation, dependencies, response models and command middleware behave identically. What you attached anywhere but the endpoint itself sees a command only if you hand your app to process_command.
  - title: Three list shapes
    details: A bare array, offset paging that carries a total wherever the source can be counted — a materialised collection, or a backend that counts in its store — and null where it cannot, or cursor paging whose next and previous links never re-read a row and never drift. A viewset declares which, and may let a client pick per request with an X-List-Shape header.
  - title: Declarative filters
    details: Say which fields accept which operators and get query parameters, an OpenAPI schema and in-memory filtering for free. A backend that can do better takes the whole filter set into its own query, and only when it can translate every filter in it.
  - title: Transparent Celery delegation
    details: Move a viewset's execution to a Celery worker with a single decorator — no code changes to the viewset itself.
  - title: Vue / TypeScript counterpart
    details: Mirror mixin classes and the restViewSet / muxwsViewSet class factory give you a fully typed client that matches your backend viewset exactly — calling an action it did not declare is a compile error.
  - title: Bulk operations
    details: First-class support for bulk create, update, partial update and destroy — on both the backend and the frontend.
---
