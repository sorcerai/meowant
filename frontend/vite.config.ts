import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  // Flask serves the build under /static, so asset URLs must be /static/-prefixed.
  base: '/static/',
  plugins: [tailwindcss(), svelte()],
  build: { outDir: '../static', emptyOutDir: false, assetsDir: 'assets' },
  server: { proxy: Object.fromEntries(
    ['/state','/cats','/visits','/boxhealth','/bowls','/feeders','/command','/events']
      .map(p => [p, { target: 'http://localhost:8765', changeOrigin: true }])) },
})
