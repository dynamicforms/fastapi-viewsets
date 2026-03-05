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
      link: https://github.com/dynamicforms/viewsets

features:
  - title: Python mixins for FastAPI
    details: Compose CRUD and bulk endpoints from small, focused mixin classes — just like Django REST Framework, but for FastAPI.
  - title: route_viewset decorator
    details: Register a viewset on a FastAPI router with a single decorator call. Handles type resolution, lifecycle management and OpenAPI schema automatically.
  - title: CollectionViewSet
    details: Zero-boilerplate in-memory viewset backed by any Python list, set or dict. Great for prototyping and testing.
  - title: CeleryViewSet
    details: Delegate all CRUD operations to Celery tasks. Ideal for long-running or background processing scenarios.
  - title: Vue / TypeScript counterpart
    details: Mirror mixin classes and a route_rest factory give you a fully typed HTTP client that matches your backend viewset exactly.
  - title: Bulk operations
    details: First-class support for bulk create, update, partial update and destroy — on both the backend and the frontend.
---
