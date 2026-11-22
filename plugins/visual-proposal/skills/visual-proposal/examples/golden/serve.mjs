import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { watch } from 'node:fs';
import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
let error = null;
let child;
let pending = false;
let stopping = false;
let timer;
function build() {
  if (stopping) return;
  if (child) { pending = true; return; }
  child = spawn(process.execPath, ['build.mjs'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  let message = '';
  child.stdout.on('data', data => process.stdout.write(data));
  child.stderr.on('data', data => { message = (message + data).slice(-16000); process.stderr.write(data); });
  child.on('error', failure => { message = failure.message; });
  child.on('close', code => {
    error = code === 0 ? null : message.trim() || `Build exited ${code}`;
    child = null;
    if (pending) { pending = false; build(); }
  });
}
const watched = new Set(['deck.md', 'theme.css', 'app.css', 'app.js', 'sources.json']);
const watcher = watch(root, (_, file) => {
  if (!watched.has(file)) return;
  clearTimeout(timer);
  timer = setTimeout(build, 250);
});
watcher.on('error', failure => { error = `File watcher: ${failure.message}`; console.error(error); });
const server = createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  if (request.method !== 'GET') { response.writeHead(405, { Allow: 'GET' }).end('Method not allowed'); return; }
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname === '/__status') {
      const [manifest, focus] = await Promise.all([
        readFile(join(root, '.cache/build.json'), 'utf8'), readFile(join(root, 'focus.json'), 'utf8')
      ]);
      const built = JSON.parse(manifest);
      const current = JSON.parse(focus);
      const focusError = !built.ids.includes(current.focus) ? `Unknown focus: ${current.focus}` : null;
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ ...built, ...current, error: error || focusError, building: Boolean(child) }));
    } else if (url.pathname === '/' || url.pathname === '/index.html') {
      response.setHeader('Content-Type', 'text/html; charset=utf-8');
      response.end(await readFile(join(root, 'index.html')));
    } else if (url.pathname === '/favicon.ico') {
      response.writeHead(204).end();
    } else {
      response.writeHead(404).end('Not found');
    }
  } catch (failure) {
    response.writeHead(503, { 'Content-Type': 'text/plain' }).end(`Preview unavailable: ${failure.message}`);
  }
});
try {
  await stat(join(root, 'index.html'));
  const port = Number(process.env.PORT ?? 4178);
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('PORT must be an integer from 0 to 65535');
  server.listen(port, '127.0.0.1', () => console.log(`Preview: http://127.0.0.1:${server.address().port}`));
} catch (failure) {
  watcher.close();
  throw failure;
}
server.on('error', failure => { console.error(failure.message); watcher.close(); process.exitCode = 1; });
function stop() {
  stopping = true;
  clearTimeout(timer);
  watcher.close();
  if (child) child.kill('SIGTERM');
  server.close();
}
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
