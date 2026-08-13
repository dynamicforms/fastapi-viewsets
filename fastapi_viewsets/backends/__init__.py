"""
Concrete data-source backends for viewsets.

`CollectionViewSet` (in the package root) covers anything already in memory. Everything here backs
a viewset with a store that can answer part of a list query itself, and so has to translate the
pipeline's stages into that store's own language rather than filtering a list.
"""
