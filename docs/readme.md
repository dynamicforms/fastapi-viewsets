# Documentation

This directory contains the VitePress documentation for `@dynamicforms/fastapi-viewsets`.

## Development

To start the documentation site in development mode:

```bash
# From the root directory
npm run docs:dev

# Or from the docs directory
npm run docs:dev
```

The site will be available at http://localhost:5173/

## Structure

- `.vitepress/`
  - `config.ts` - site title, navigation, sidebars and the Vite/Vuetify plugin setup
  - `theme/index.ts` - extends the default theme and installs Vuetify into the app
- `index.md` - the landing page, whose hero and feature cards are frontmatter
- `guide/` - user guide documentation
- `api/` - API reference documentation

## Building

To build the documentation site for production:

```bash
# From the root directory
npm run docs:build
```

The built site will be in the `docs/.vitepress/dist` directory.

## Adding a Page

1. Create the markdown file under `guide/` (user guide) or `api/` (API reference).
2. Add an entry for it to the matching sidebar section in `.vitepress/config.ts` — `sidebar['/guide/']`
   or `sidebar['/api/']` — as a `{ text, link }` object, where `link` is the path without the `.md`
   extension.

A page reachable from neither sidebar builds and serves, but nothing links to it. Vuetify components
are registered globally by `.vitepress/theme/index.ts`, so a page may use them in its markdown without
importing anything.
