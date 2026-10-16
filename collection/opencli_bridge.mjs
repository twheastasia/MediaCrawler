// Use reviewed OpenCLI adapters with our own CDP IPage, never its target picker,
// Browser Bridge daemon, stealth injection, auth cache or command retry runner.
import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const root = path.resolve(process.argv[2]);
const site = process.argv[3], command = process.argv[4];
const allowed = {maimai: ['search-talents'], linkedin: ['search', 'people-search', 'posts', 'search-posts']};
const write = value => process.stdout.write(JSON.stringify(value) + '\n');
console.log = console.warn = console.error = () => {}; // No adapter diagnostics/credentials.
const lines = createInterface({input: process.stdin});
let next = 0;
const pending = new Map();
let started = false;
function rpc(method, args) {
  const id = ++next;
  write({rpc: method, id, args});
  return new Promise((resolve, reject) => pending.set(id, {resolve, reject}));
}
const page = new Proxy({}, {get: (_, method) => (...args) => rpc(method, args)});

async function run(args) {
  if (!allowed[site]?.includes(command)) throw Object.assign(new Error(), {code: 'UNSUPPORTED_COMMAND'});
  const registry = await import(pathToFileURL(path.join(root, 'dist/src/registry-api.js')));
  if (command === 'search-posts') {
    // Project extension: OpenCLI's `linkedin search` searches jobs, not posts.
    const core = await import(pathToFileURL(path.join(root, 'clis/linkedin/posts-core.js')));
    const unsafe = 'for (const expander of expanders.slice(0, 20)) {\n      try { expander.click(); } catch {}\n    }';
    const original = core.buildPostsScript();
    if (!original.includes(unsafe)) throw Object.assign(new Error(), {code: 'ADAPTER_CHANGED'});
    const script = original.replace(unsafe, '/* Expansion is performed one element at a time by the host. */');
    await page.goto(`https://www.linkedin.com/search/results/content/?keywords=${encodeURIComponent(args.query)}`);
    await page.wait(2);
    const rows = new Map();
    let stalls = 0;
    for (let step = 0; step < 40 && rows.size < (args.limit || 100) && stalls < 2; step++) {
      const result = await page.evaluate(`(() => {
        const payload = ${script};
        const cards = [...document.querySelectorAll('article,[role="article"],.feed-shared-update-v2,.occludable-update')];
        return {...payload, rows: payload.rows.map(row => {
          const card = cards.find(c => [...c.querySelectorAll('a[href]')].some(a => a.href === row.url));
          const author = card?.querySelector('a[href*="/in/"],a[href*="/company/"]');
          return {...row, author_url:author?.href || '', raw_rendered_text:card?.innerText || ''};
        })};
      })()`);
      if (!Array.isArray(result?.rows)) throw Object.assign(new Error(), {code: 'ADAPTER_CHANGED'});
      const before = rows.size;
      for (const row of result.rows) if (row.url) rows.set(row.url, row);
      stalls = rows.size === before ? stalls + 1 : 0;
      if (rows.size >= (args.limit || 100)) break;
      await page.autoScroll({times: 1});
    }
    return [...rows.values()].slice(0, args.limit || 100);
  }
  await import(pathToFileURL(path.join(root, `clis/${site}/${command}.js`)));
  const spec = registry.getRegistry().get(`${site}/${command}`);
  if (!spec || spec.access !== 'read' || !spec.func) throw Object.assign(new Error(), {code: 'UNSUPPORTED_COMMAND'});
  const defaults = Object.fromEntries(spec.args.filter(a => a.default !== undefined).map(a => [a.name, a.default]));
  return spec.func(page, {...defaults, ...args});
}
lines.on('line', line => {
  let value;
  try { value = JSON.parse(line); } catch { process.exitCode = 2; lines.close(); return; }
  if (value.reply) {
    const item = pending.get(value.reply);
    if (!item) return;
    pending.delete(value.reply);
    if (value.error) item.reject(Object.assign(new Error(), {code: 'HOST_BLOCKED'}));
    else item.resolve(value.result);
    return;
  }
  if (started) return;
  started = true;
  run(value.args || {}).then(result => write({done: true, result})).catch(error => {
    write({done: true, error: {code: error.code || 'ADAPTER_ERROR', type: error.name || 'Error'}});
  }).finally(() => lines.close());
});
