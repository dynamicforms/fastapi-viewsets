/// <reference types="vitest" />
import vue from '@vitejs/plugin-vue';
import { readdirSync, readFileSync } from 'fs';
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
      rollupTypes: true,
      // fails the build when the generated declarations import a type by a node_modules path,
      // which does not resolve in a consumer's installation
      afterBuild: () => {
        const dist = resolve(import.meta.dirname, 'dist');
        const paths = readdirSync(dist, { recursive: true, encoding: 'utf-8' })
          .filter((file) => file.endsWith('.d.ts'))
          .flatMap((file) => readFileSync(resolve(dist, file), 'utf-8')
            .match(/(?:from |import\()['"][^'"]*node_modules[^'"]*['"]/g) ?? []);
        if (paths.length) throw new Error(`declarations import from node_modules paths: ${paths.join(', ')}`);
      },
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
      formats: ['es'],
      fileName: 'fastapi-viewsets',
    },
    rollupOptions: {
      external: ['axios'],
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
