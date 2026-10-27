"""
Concrete data-source backends for viewsets.

Each backs a viewset with a store that answers part of a list query itself, translating the pipeline's
stages into the store's own query language. In-memory data uses `CollectionViewSet` instead.
"""
