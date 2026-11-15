import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Overridable so the e2e run can start its own backend on a port of its own rather than colliding
// with whatever the developer already has running.
const api = `http://127.0.0.1:${process.env.DEMO_API_PORT ?? 8000}`;

// GitHub Codespaces serves the dev server at https://<codespace name>-<port>.<forwarding domain> on port 443.
// There it listens on all addresses: the default binds only the first address `localhost` resolves to.
const forwardingDomain = process.env.CODESPACES ? process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN : undefined;

export default defineConfig({
  plugins: [vue()],
  server: {
    port: Number(process.env.DEMO_FE_PORT ?? 5173),
    ...(forwardingDomain ? { host: true, allowedHosts: [`.${forwardingDomain}`], hmr: { clientPort: 443 } } : {}),
    proxy: {
      '/music': api,
      '/music-db': api,
      '/backends': api,
      '/events': api,
      '/redoc': api,
      '/docs': api,
      '/openapi.json': api,
      '/ws': { target: api.replace('http', 'ws'), ws: true },
    },
  },
});
