/// <reference types="vitest" />
import vue from '@vitejs/plugin-vue';
import { resolve } from 'path';
import { defineConfig } from 'vite';
import dts from 'vite-plugin-dts';
import { visualizer } from 'rollup-plugin-visualizer';

/** @type {import('vite').UserConfig} */
export default defineConfig({
  plugins: [
    vue(),
    dts({
      tsconfigPath: './tsconfig.build.json',
      rollupTypes: true
    }),
    visualizer({
      open: false,
      filename: 'coverage/stats.html',
      gzipSize: true,
      brotliSize: true,
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(import.meta.dirname, './vue'),
      // '~': resolve(import.meta.dirname, '../../node_modules'),
    },
    extensions: [
      '.js',
      '.mjs',
      '.ts',
    ],
  },
  build: {
    target: 'es2015',
    sourcemap: true,
    lib: {
      entry: resolve(import.meta.dirname, 'vue/index.ts'),
      formats: ['umd', 'es'],
      fileName: 'fastapi-viewsets',
      name: 'fastapi-viewsets.[name]',
    },
    rollupOptions: {
      external: [
        '@dynamicforms/vue-forms',
        'axios',
        'lodash-es',
        'vue',
      ],
      output: {
        globals: (id: string) => id, // all external modules are currently not aliased to anything but their own names
      }
    }
  },
  test: {
    // The library's own specs only. demo/e2e is Playwright, which vitest would otherwise collect
    // and fail to run - two runners fighting over the same *.spec.ts glob.
    include: ['vue/**/*.spec.ts'],
    coverage: {
      provider: 'v8',
      include: [
        'vue/**/*'
      ],
      exclude: [
        '**/index.ts',
      ],
    },
    globals: true,
    environment: 'jsdom',
  },
});
