# DynamicForms Vue Forms Documentation

This directory contains the VitePress documentation for `@dynamicforms/vuetify-forms`.

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

- `.vitepress/` - VitePress configuration
  - `theme/` - Custom theme configuration
  - `config.ts` - VitePress configuration
- `guide/` - User guide documentation
- `api/` - API reference documentation
- `examples/` - Interactive examples
- `components/` - Vue components used in the documentation

## Building

To build the documentation site for production:

```bash
# From the root directory
npm run docs:build
```

The built site will be in the `docs/.vitepress/dist` directory.

## Adding New Examples

1. Create a new Vue component in `.vitepress/components/`
2. Create a new markdown page in `examples/`
3. Import and use the component in your markdown page
