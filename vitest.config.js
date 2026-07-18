import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['myapp/static/assets/js/**/*.test.js'],
  },
});
