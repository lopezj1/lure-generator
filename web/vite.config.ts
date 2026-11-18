import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [svelte()],
  // three-cad-viewer and our own renderers must share one three.js instance.
  resolve: { dedupe: ['three'] },
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
});
