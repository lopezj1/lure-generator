import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  // one copy of three for the viewer and our own code (three-cad-viewer pins it exactly)
  resolve: { dedupe: ["three"] },
});
