import velis from 'eslint-config-velis';

export default [
  ...velis,
  {
    files: ['demo/frontend/src/**/*.{ts,vue}'],
    languageOptions: {
      parserOptions: { project: './demo/frontend/tsconfig.json' },
      // velis's globals list omits these browser APIs, which the demo reads
      globals: { location: 'readonly', performance: 'readonly' },
    },
  },
  {
    files: ['demo/e2e/**/*.ts'],
    languageOptions: {
      parserOptions: { project: './demo/e2e/tsconfig.json' },
      // Node's global fetch, which velis's globals list omits
      globals: { fetch: 'readonly' },
    },
  },
  {
    rules: {
      'no-redeclare': 'off',
      '@typescript-eslint/no-redeclare': 'error',
    },
    ignores: [
      'dist/*',
      'coverage/*',
      'node_modules/*',
      'docs/*',
      'vite.config.ts'
    ]
  }
];
