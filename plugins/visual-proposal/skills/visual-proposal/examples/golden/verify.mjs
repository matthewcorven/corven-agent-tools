import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { readFile, writeFile, mkdir, mkdtemp, copyFile, cp, symlink, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { plantumlPath, verifyJar } from './plantuml.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const read = name => readFile(join(root, name), 'utf8');
const [html, markdown, manifestText, sourcesText, focusText] =
  await Promise.all(['index.html', 'deck.md', '.cache/build.json', 'sources.json', 'focus.json'].map(read));
const manifest = JSON.parse(manifestText);
const focus = JSON.parse(focusText);
const evidence = join(root, 'evidence');
await mkdir(evidence, { recursive: true });
const report = { revision: manifest.revision, htmlSha256: createHash('sha256').update(html).digest('hex'),
  testedAt: new Date().toISOString(), slides: manifest.slides, diagrams: manifest.diagrams.length,
  viewports: [], checks: [], layoutIssues: [] };
assert.equal(report.htmlSha256, manifest.htmlSha256, 'HTML must match the built revision');
assert.equal([...markdown.matchAll(/```(?:mermaid|plantuml)\n/g)].length, manifest.diagrams.length);
assert.ok(manifest.diagrams.some(diagram => diagram.language === 'plantuml'));
assert.ok(manifest.diagrams.some(diagram => diagram.language === 'mermaid'));
assert.throws(() => verifyJar(Buffer.from('not the pinned jar')), /SHA-256 mismatch/);
const browser = await chromium.launch({ headless: true });
const pageErrors = [];
const network = [];
let server;
let fixture;
let serverLog = '';
const inventory = async page => {
  assert.equal(await page.locator('#deck section').count(), manifest.slides, 'Slide inventory mismatch');
  assert.equal(await page.locator('img[data-diagram]').count(), manifest.diagrams.length, 'Diagram inventory mismatch');
};
const diagrams = async locator => locator.evaluateAll(async images => {
  await Promise.all(images.map(async image => {
    await image.decode();
    const xml = new DOMParser().parseFromString(atob(image.src.split(',')[1]), 'image/svg+xml');
    if (xml.querySelector('parsererror') || ![...xml.querySelectorAll('text')].some(text => text.textContent.trim())) {
      throw new Error(`Invalid or empty SVG: ${image.alt}`);
    }
    if (!image.naturalWidth || !image.naturalHeight) throw new Error(`Missing SVG dimensions: ${image.alt}`);
  }));
});
const observe = page => page.on('pageerror', error => pageErrors.push(error.message));
async function waitForStatus(url, predicate, label) {
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const response = await fetch(`${url}/__status`);
    const status = await response.json();
    if (predicate(status)) return status;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out: ${label}\n${serverLog}`);
}
try {
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 216, height: 713 }]) {
    const context = await browser.newContext({ viewport, acceptDownloads: true });
    const page = await context.newPage();
    observe(page);
    page.on('request', request => { if (/^https?:/.test(request.url())) network.push(request.url()); });
    await context.setOffline(true);
    await page.goto(pathToFileURL(join(root, 'index.html')).href);
    await inventory(page);
    assert.equal(await page.locator('#live-status').textContent(), 'Standalone edition - everything is embedded');
    const embedded = await page.locator('#deck-data').textContent();
    assert.deepEqual(JSON.parse(embedded).sources, JSON.parse(sourcesText), 'Source provenance must be embedded unchanged');
    for (let index = 0; index < manifest.slides; index++) {
      await page.selectOption('#slide-picker', String(index));
      const slide = page.locator('#deck section').nth(index);
      await diagrams(slide.locator('img[data-diagram]'));
      assert.equal(await page.locator('#deck section:visible').count(), 1);
      assert.equal(await slide.isVisible(), true);
      const overflow = await slide.evaluate(section => {
        const bounds = section.getBoundingClientRect();
        const offenders = [...section.querySelectorAll('h1,h2,h3,p,.columns,table,.diagram')].filter(element => {
          const box = element.getBoundingClientRect();
          return box.bottom > bounds.bottom + 2 || box.top < bounds.top - 2 || box.right > bounds.right + 2 || box.left < bounds.left - 2;
        }).map(element => `${element.tagName}: ${element.textContent.slice(0, 60)}`);
        if (document.documentElement.scrollWidth > innerWidth + 1) offenders.push('page width');
        return offenders;
      });
      if (overflow.length) report.layoutIssues.push({ id: manifest.ids[index], width: viewport.width, overflow });
      if (viewport.width !== 216) await slide.screenshot({ path: join(evidence, `${viewport.width}-${manifest.ids[index]}.png`) });
    }
    await page.selectOption('#slide-picker', '0');
    assert.equal(await page.locator('#previous').isDisabled(), true);
    await page.click('#next');
    assert.equal(await page.locator('#slide-picker').inputValue(), '1');
    await page.locator('h2:visible').click();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#slide-picker').inputValue(), '2');
    const downloadEvent = page.waitForEvent('download');
    await page.click('#source');
    const download = await downloadEvent;
    assert.equal(download.suggestedFilename(), focus.downloadName);
    assert.equal(await readFile(await download.path(), 'utf8'), markdown, 'Embedded Markdown must match authoring source');
    await page.click('#reading');
    assert.equal(await page.locator('#deck section:visible').count(), manifest.slides);
    await page.click('#reading');
    assert.equal(await page.locator('#deck section:visible').count(), 1);
    const before = await page.locator('#deck').evaluate(deck => deck.getBoundingClientRect().width);
    await page.click('#zoom');
    assert.ok(await page.locator('#deck').evaluate(deck => deck.getBoundingClientRect().width) > before);
    const leftVisible = await page.locator('#deck').evaluate(deck => deck.getBoundingClientRect().left >= 0);
    assert.equal(leftVisible, true, 'Zoom must not strand the left edge outside the scrollable stage');
    await page.click('#zoom');
    report.viewports.push({ ...viewport, allSlidesInspected: true, controls: true, sourceDownload: true });
    await context.close();
  }
  assert.deepEqual(network, [], 'Standalone rendering must make zero HTTP requests');
  report.checks.push('Every slide and diagram inspected at three widths', 'Exact Markdown download and source inventory',
    'Offline with zero HTTP requests', 'Navigation, keyboard, read-all and actual zoom dimensions');

  fixture = await mkdtemp(join(tmpdir(), 'visual-proposal-test-'));
  for (const name of ['build.mjs', 'serve.mjs', 'plantuml.mjs', 'deck.md', 'theme.css', 'app.css', 'app.js',
    'sources.json', 'focus.json', 'package.json', 'package-lock.json', 'index.html']) {
    await copyFile(join(root, name), join(fixture, name));
  }
  await cp(join(root, '.cache'), join(fixture, '.cache'), { recursive: true });
  await symlink(join(root, 'node_modules'), join(fixture, 'node_modules'), 'junction');
  server = spawn(process.execPath, ['serve.mjs'], {
    cwd: fixture, env: { ...process.env, PORT: '0', PLANTUML_JAR: plantumlPath }, stdio: ['ignore', 'pipe', 'pipe']
  });
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Preview startup timeout\n${serverLog}`)), 15000);
    server.on('error', error => { clearTimeout(timer); reject(error); });
    server.on('exit', code => { clearTimeout(timer); reject(new Error(`Preview exited ${code}\n${serverLog}`)); });
    server.stderr.on('data', data => { serverLog += data; });
    server.stdout.on('data', data => {
      serverLog += data;
      const match = serverLog.match(/Preview: (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
  });
  assert.equal((await fetch(`${url}/deck.md`)).status, 404, 'Server must not expose source files');
  assert.equal((await fetch(`${url}/__status`, { method: 'POST' })).status, 405, 'Preview cannot accept decisions');
  await waitForStatus(url, status => !status.building && !status.error, 'initial readiness');
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const live = await context.newPage();
  observe(live);
  await live.goto(url);
  const writeFocus = value => writeFile(join(fixture, 'focus.json'), JSON.stringify({ ...focus, focus: value }));
  await writeFocus(manifest.ids[2]);
  await live.waitForFunction(id => location.hash === `#${id}`, manifest.ids[2]);
  await live.click('#previous');
  const manual = await live.evaluate(() => location.hash);
  await writeFocus(manifest.ids[3]);
  await live.waitForTimeout(1600);
  assert.equal(await live.evaluate(() => location.hash), manual, 'Manual navigation pauses following');
  await live.click('#follow');
  await live.waitForFunction(id => location.hash === `#${id}`, manifest.ids[3]);
  await live.click('#previous');
  const preserved = await live.evaluate(() => location.hash);
  await writeFile(join(fixture, 'deck.md'), `${markdown}\n<!-- live-rebuild-fixture -->\n`);
  const updated = await waitForStatus(url, status => !status.building && !status.error && status.revision !== manifest.revision, 'watcher rebuild');
  await live.waitForFunction(revision => JSON.parse(document.getElementById('deck-data').textContent).revision === revision, updated.revision);
  assert.equal(await live.evaluate(() => location.hash), preserved, 'Manual position survives real rebuild');
  assert.equal(await live.locator('#follow').getAttribute('aria-pressed'), 'false');
  const lastGood = await readFile(join(fixture, 'index.html'), 'utf8');
  await writeFile(join(fixture, 'deck.md'), `${markdown}\n<!-- slide: opening -->\n`);
  await waitForStatus(url, status => !status.building && status.error?.includes('Slide metadata mismatch'), 'invalid source rejection');
  await live.waitForFunction(() => document.getElementById('live-status').textContent.includes('Build failed:'));
  assert.equal(await readFile(join(fixture, 'index.html'), 'utf8'), lastGood, 'Failed build preserves exact last-good HTML');
  await writeFile(join(fixture, 'deck.md'), markdown);
  await waitForStatus(url, status => !status.building && !status.error, 'recovery after corrected source');
  await writeFocus('does-not-exist');
  await live.waitForFunction(() => document.getElementById('live-status').textContent.includes('Unknown focus'));
  await writeFocus(manifest.ids[0]);
  await waitForStatus(url, status => !status.error, 'focus correction');
  const closed = once(server, 'exit');
  server.kill('SIGTERM');
  await closed;
  await live.waitForFunction(() => document.getElementById('live-status').textContent.includes('Preview disconnected'));
  await inventory(live);
  await diagrams(live.locator('img[data-diagram]'));
  await live.locator('img[data-diagram]').first().evaluate(image => image.remove());
  await assert.rejects(() => inventory(live), /Diagram inventory mismatch/);
  await live.locator('img[data-diagram]').first().evaluate(image => { image.src = 'data:image/svg+xml;base64,YnJva2Vu'; });
  await assert.rejects(() => diagrams(live.locator('img[data-diagram]')));
  report.checks.push('Real loopback server routes, watch/rebuild, focus and manual follow persistence',
    'Invalid source and focus rejected; last-good HTML retained; correction recovers',
    'Disconnected server displays explicit retained-state error',
    'Same inventory/decode assertions reject removed and corrupt emitted diagrams',
    'Incorrect PlantUML jar rejected by SHA-256');
  await context.close();
  assert.deepEqual(pageErrors, [], 'No browser JavaScript exceptions');
  assert.deepEqual(report.layoutIssues, [], 'Slide content must not overflow; see evidence/report.json');
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.failure = error.stack;
  throw error;
} finally {
  if (server && server.exitCode === null && server.signalCode === null) {
    const closed = once(server, 'exit');
    server.kill('SIGTERM');
    await closed;
  }
  await browser.close();
  if (fixture) await rm(fixture, { recursive: true, force: true });
  await writeFile(join(evidence, 'report.json'), JSON.stringify(report, null, 2));
}
console.log(`PASS: ${report.slides} slides, ${report.diagrams} diagrams, three viewports; offline/live/mutation checks.`);
