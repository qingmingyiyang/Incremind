import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import postcss from 'postcss';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';

const frontend = fileURLToPath(new URL('../', import.meta.url));
const aliases = { react: path.join(frontend, 'node_modules/react'), 'react-dom': path.join(frontend, 'node_modules/react-dom') };
if (process.env.CHRIPTMAS_MCP_APPS_MODULE) aliases['@modelcontextprotocol/ext-apps'] = process.env.CHRIPTMAS_MCP_APPS_MODULE;

export default defineConfig({
  plugins: [react(), {
    name: 'existing-theme-tokens',
    resolveId(id) { if (id === 'virtual:incremind-theme.css') return '\0incremind-theme.css'; },
    load(id) {
      if (id !== '\0incremind-theme.css') return undefined;
      const source = postcss.parse(fs.readFileSync(path.join(frontend, 'src/styles.css'), 'utf8'));
      const tokens = postcss.root();
      source.walkRules(rule => {
        if (rule.selector.includes(':root')) {
          const selected = rule.clone();
          selected.nodes = selected.nodes.filter(node => node.type === 'decl' && (node.prop.startsWith('--') || node.prop === 'color-scheme'));
          if (selected.nodes.length) tokens.append(selected);
        }
      });
      return tokens.toString();
    },
  }],
  resolve: { alias: aliases },
  build: { outDir: path.join(frontend, 'dist/mcp-app'), emptyOutDir: true,
    lib: { entry: path.join(frontend, 'chatgpt-app/index.jsx'), name: 'IncremindMemoryApp', formats: ['iife'], fileName: () => 'widget.js', cssFileName: 'widget' },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
