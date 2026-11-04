import { copyFileSync, cpSync, createReadStream, existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const here = path.dirname(fileURLToPath(import.meta.url));
// In the repo and in Docker the brand kit sits next to web/: R/brand or /src/brand.
const brandDir = path.resolve(here, '../brand');
const handbookDir = path.resolve(here, '../handbook');
const startHere = path.resolve(here, '../START_HERE.html');
const chatZip = path.resolve(here, '../setup/harborline-chat.zip');

const BRAND_ID = 'virtual:brand-tokens.css';
const RESOLVED_BRAND_ID = '\0' + BRAND_ID;

const MIME: Record<string, string> = {
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.json': 'application/json',
  '.html': 'text/html',
  '.woff2': 'font/woff2',
};

/**
 * Brand tokens are owned by the brand kit (R/brand/tokens.css). This plugin inlines that file when it exists
 * and an empty sheet when it does not, so the app always builds and src/styles/tokens.css supplies fallbacks.
 * In dev it also serves /brand/* and /handbook/* straight from disk (the FastAPI app serves them in production).
 */
function brandKit(): Plugin {
  const tokensPath = path.join(brandDir, 'tokens.css');
  return {
    name: 'harborline-brand-kit',
    resolveId(id) {
      return id === BRAND_ID ? RESOLVED_BRAND_ID : null;
    },
    load(id) {
      if (id !== RESOLVED_BRAND_ID) return null;
      this.addWatchFile(tokensPath);
      if (!existsSync(tokensPath)) return '/* brand/tokens.css not found: using fallbacks */\n';
      // Fonts referenced by the brand sheet are bundled by the app itself (@fontsource), so drop remote imports.
      return readFileSync(tokensPath, 'utf8').replace(/@import\s+url\([^)]*\)\s*;?/g, '');
    },
    configureServer(server) {
      const serveFrom = (prefix: string, root: string) =>
        server.middlewares.use(prefix, (req, res, next) => {
          const rel = decodeURIComponent((req.url ?? '/').split('?')[0]);
          let file = path.join(root, rel);
          if (!file.startsWith(root)) return next();
          if (existsSync(file) && statSync(file).isDirectory()) file = path.join(file, 'index.html');
          if (!existsSync(file)) return next();
          res.setHeader('Content-Type', MIME[path.extname(file)] ?? 'application/octet-stream');
          createReadStream(file).pipe(res);
        });
      serveFrom('/brand', brandDir);
      serveFrom('/handbook', handbookDir);
    },
  };
}

/** Edits a copied file; a string that is gone must stop the build, not ship links that quietly go nowhere. */
function patchCopy(file: string, patches: Array<[from: string | RegExp, to: string]>) {
  let text = readFileSync(file, 'utf8');
  for (const [from, to] of patches) {
    if (typeof from === 'string' ? !text.includes(from) : !from.test(text)) throw new Error(`Demo build: ${String(from)} not found in ${file}. Update the patch in vite.config.ts.`);
    text = text.replace(from, () => to);
  }
  writeFileSync(file, text);
}

/** The brand page links to folders. file:// lists them; a static host needs a page for it. */
function writeFolderPages(dir: string) {
  const entries = readdirSync(dir, { withFileTypes: true }).filter((e) => e.name !== 'index.html');
  const items = entries.map((e) => {
    const name = e.isDirectory() ? `${e.name}/` : e.name;
    return `<li><a href="${encodeURI(name)}">${name}</a></li>`;
  });
  writeFileSync(
    path.join(dir, 'index.html'),
    `<!doctype html>\n<meta charset="utf-8">\n<title>${path.basename(dir)}</title>\n<body style="font-family: system-ui, sans-serif; margin: 2rem">\n<h1>${path.basename(dir)}/</h1>\n<ul>\n<li><a href="../">../</a></li>\n${items.join('\n')}\n</ul>\n`,
  );
  for (const e of entries) if (e.isDirectory()) writeFolderPages(path.join(dir, e.name));
}

/**
 * `vite build --mode demo` for a static host: what the FastAPI app serves next to the SPA has to sit in dist. The handbook
 * links to ../brand and ../START_HERE.html, so all three are copied. The backend redirects /handbook/ to START_HERE.html;
 * a static host cannot, so a small page does. 404.html is the SPA again, which is what makes deep links load on GitHub Pages.
 */
function demoOutput(): Plugin {
  return {
    name: 'harborline-demo-output',
    apply: 'build',
    writeBundle({ dir }) {
      const out = path.resolve(here, dir ?? 'dist');
      const notJunk = (src: string) => path.basename(src) !== '.DS_Store';
      cpSync(handbookDir, path.join(out, 'handbook'), { recursive: true, filter: notJunk });
      cpSync(brandDir, path.join(out, 'brand'), { recursive: true, filter: notJunk });
      writeFolderPages(path.join(out, 'brand'));
      copyFileSync(startHere, path.join(out, 'START_HERE.html'));
      copyFileSync(chatZip, path.join(out, 'harborline-chat.zip')); // the Team page's download; the backend serves it at /api/handbook/chat-zip
      copyFileSync(path.join(out, 'index.html'), path.join(out, '404.html'));
      writeFileSync(
        path.join(out, 'handbook', 'index.html'),
        '<!doctype html>\n<meta charset="utf-8">\n<title>Harborline handbook</title>\n<meta http-equiv="refresh" content="0; url=../START_HERE.html">\n<a href="../START_HERE.html">Open the handbook</a>\n',
      );
      // No backend here: progress stays in the browser, and "the app" is this sub-path. Addresses in the text stay those of an install.
      patchCopy(path.join(out, 'handbook', 'assets', 'handbook.js'), [
        ['var isHttp = /^https?:$/.test(location.protocol);', 'var isHttp = false;'],
        ['var appOrigin = isHttp ? location.origin : APP_DEFAULT;', 'var appOrigin = rootURL.href.replace(/\\/$/, "");'],
        ['el.textContent = appOrigin;', 'el.textContent = APP_DEFAULT;'],
      ]);
      patchCopy(path.join(out, 'START_HERE.html'), [[/<tr><td><a [^>]*data-hb-app="\/docs"[\s\S]*?<\/tr>\n?/, '']]);
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, here, '');
  const mock = env.VITE_MOCK === '1';
  const target = env.HL_API_TARGET || 'http://localhost:8080';
  const proxied = ['/api', '/sfdc', '/carrier', '/docs', '/openapi.json', '/redoc'];

  return {
    plugins: [react(), tailwindcss(), brandKit(), mode === 'demo' ? demoOutput() : null],
    resolve: { alias: { '@': path.resolve(here, 'src'), '@brand': brandDir } },
    server: {
      port: 5173,
      fs: { allow: [path.resolve(here, '..')] },
      proxy: mock ? undefined : Object.fromEntries(proxied.map((p) => [p, { target, changeOrigin: true }])),
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      chunkSizeWarningLimit: 900,
    },
  };
});
