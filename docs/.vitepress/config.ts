import vuetify from 'vite-plugin-vuetify';
import { defineConfig } from 'vitepress';

export default defineConfig({
  title: 'DynamicForms Viewsets',
  description: 'Full-stack ViewSet library for FastAPI (Python) and Vue/TypeScript',
  themeConfig: {
    logo: '/logo.png',
    nav: [
      { text: 'Home', link: '/' },
      { text: 'Guide', link: '/guide/getting-started' },
      { text: 'API Reference', link: '/api/python-mixins' },
    ],
    sidebar: {
      '/guide/': [
        {
          text: 'Getting Started',
          items: [
            { text: 'Rationale', link: '/guide/rationale' },
            { text: 'Installation', link: '/guide/getting-started#installation' },
            { text: 'Quick Start', link: '/guide/getting-started#quick-start' },
          ],
        },
        {
          text: 'Python (FastAPI)',
          items: [
            { text: 'Mixins', link: '/guide/python-mixins' },
            { text: 'Routers & Decorators', link: '/guide/routers' },
            { text: 'CollectionViewSet', link: '/guide/collection-viewset' },
            { text: 'CeleryViewSet', link: '/guide/celery-viewset' },
            { text: 'Custom Endpoints', link: '/guide/custom-endpoints' },
          ],
        },
        {
          text: 'Vue / TypeScript',
          items: [
            { text: 'Mixins', link: '/guide/vue-mixins' },
            { text: 'route_rest factory', link: '/guide/route-rest' },
          ],
        },
      ],
      '/api/': [
        {
          text: 'API Reference',
          items: [
            { text: 'Python mixins', link: '/api/python-mixins' },
            { text: 'route_viewset', link: '/api/route-viewset' },
            { text: 'CollectionViewSet', link: '/api/collection-viewset' },
            { text: 'CeleryViewSet', link: '/api/celery-viewset' },
            { text: 'Vue mixins', link: '/api/vue-mixins' },
            { text: 'route_rest', link: '/api/route-rest' },
          ],
        },
      ],
    },
    socialLinks: [
      { icon: 'github', link: 'https://github.com/dynamicforms/viewsets' },
    ],
    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Copyright © 2025 Jure Erznožnik',
    },
  },
  ignoreDeadLinks: [/^http:\/\/localhost/],
  vite: {
    plugins: [vuetify()],
    optimizeDeps: {
      include: ['vuetify'],
    },
    ssr: {
      noExternal: [
        /vuetify/,
      ],
    },
  },
});

