import { Marp } from '@marp-team/marp-core';
import { chromium } from 'playwright';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { readFile, writeFile, mkdir, rename } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { plantumlPath, readVerifiedJar } from './plantuml.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const read = name => readFile(join(root, name), 'utf8');
const hash = text => createHash('sha256').update(text).digest('hex');
const escape = text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const [markdown, theme, appStyle, appScript, focusText, sourcesText, builder, lockfile, jarModule, marpLicense, mermaidLicense] =
  await Promise.all(['deck.md', 'theme.css', 'app.css', 'app.js', 'focus.json', 'sources.json', 'build.mjs',
    'package-lock.json', 'plantuml.mjs', 'node_modules/@marp-team/marp-core/LICENSE', 'node_modules/mermaid/LICENSE'].map(read));
const focus = JSON.parse(focusText);
if (!focus.title || !focus.phase || !/^[a-z0-9-]+\.md$/.test(focus.downloadName)) throw new Error('Invalid presentation metadata in focus.json');
const sources = JSON.parse(sourcesText);
if (!sources.documents?.length) throw new Error('Source inventory cannot be empty');
const ids = [...markdown.matchAll(/<!-- slide: ([a-z0-9-]+) -->/g)].map(match => match[1]);
if (!ids.includes(focus.focus)) throw new Error(`Unknown focused slide: ${focus.focus}`);
const rendererKey = hash(builder + lockfile + jarModule);
const revision = hash([markdown, theme, appStyle, appScript, sourcesText, focusText, rendererKey].join('\0')).slice(0, 16);
const blocks = [...markdown.matchAll(/```(mermaid|plantuml)\n([\s\S]*?)```/g)];
if (!blocks.length) throw new Error('The deck must contain rendered diagrams');
if (blocks.some(block => block[1] === 'plantuml')) await readVerifiedJar();
await mkdir(join(root, '.cache'), { recursive: true });
let browser;
let page;
let processed = markdown;
const diagrams = [];
try {
  for (const [index, block] of blocks.entries()) {
    const [, language, source] = block;
    if (language === 'plantuml' && source.split('\n').some(line => /^\s*!/.test(line) && line.trim() !== '!pragma layout smetana')) {
      throw new Error('PlantUML preprocessing is disabled except !pragma layout smetana; no includes or external reads.');
    }
    const key = hash(`${rendererKey}:${language}:${source}`);
    const cached = join(root, '.cache', `${key}.svg`);
    let svg;
    if (existsSync(cached)) {
      svg = await readFile(cached, 'utf8');
    } else if (language === 'mermaid') {
      if (!browser) {
        browser = await chromium.launch({ headless: true });
        page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
        await page.route('**/*', route => route.abort());
        await page.setContent('<!doctype html><html><body></body></html>');
        await page.addScriptTag({ path: join(root, 'node_modules/mermaid/dist/mermaid.min.js') });
        await page.evaluate(() => mermaid.initialize({
          startOnLoad: false, securityLevel: 'strict', theme: 'base', htmlLabels: false,
          layout: 'dagre', look: 'classic', markdownAutoWrap: false,
          themeVariables: {
            fontFamily: 'Arial', fontSize: '22px', primaryColor: '#deedf1', primaryTextColor: '#172f39',
            primaryBorderColor: '#387789', lineColor: '#3e6372', secondaryColor: '#f3e7d8',
            tertiaryColor: '#eef3f5', background: '#f5f7f8', noteBkgColor: '#f3e7d8', noteTextColor: '#172f39'
          },
          flowchart: { theme: 'base', look: 'classic', curve: 'linear', nodeSpacing: 26, rankSpacing: 36, wrappingWidth: 350, inheritDir: true },
          sequence: { useMaxWidth: false, mirrorActors: false, actorMargin: 35, messageMargin: 18, messageFontSize: 22, actorFontSize: 22, noteFontSize: 20 },
          state: { useMaxWidth: false, theme: 'base', look: 'classic', layout: 'dagre', rankSpacing: 22, nodeSpacing: 24, padding: 8, fontSize: 22 }
        }));
      }
      svg = await page.evaluate(async ({ source, index }) => {
        const rendered = (await mermaid.render(`diagram${index}`, source)).svg;
        const xml = new DOMParser().parseFromString(rendered, 'image/svg+xml');
        if (xml.querySelector('parsererror')) throw new Error(`Invalid SVG XML for diagram ${index + 1}`);
        const root = xml.documentElement;
        const [, , width, height] = root.getAttribute('viewBox').split(/\s+/).map(Number);
        root.setAttribute('width', width);
        root.setAttribute('height', height);
        return new XMLSerializer().serializeToString(root);
      }, { source, index });
      await writeFile(cached, svg);
    } else {
      const java = process.env.JAVA_BIN || ['/opt/homebrew/opt/openjdk@21/bin/java',
        '/opt/homebrew/opt/openjdk/bin/java', '/usr/local/opt/openjdk/bin/java'].find(existsSync) || 'java';
      const result = spawnSync(java, ['-Djava.awt.headless=true', '-DPLANTUML_SECURITY_PROFILE=SANDBOX',
        '-jar', plantumlPath, '-tsvg', '-pipe', '-charset', 'UTF-8'], {
        input: source, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 60000
      });
      if (result.error || result.status !== 0 || !result.stdout.includes('<svg') || /Syntax Error|An error has occurred/.test(result.stdout)) {
        throw new Error(`PlantUML diagram ${index + 1}: ${result.error?.message || result.stderr || result.stdout.slice(0, 1000)}`);
      }
      svg = result.stdout.slice(result.stdout.indexOf('<svg'));
      await writeFile(cached, svg);
    }
    if (!svg.includes('<svg') || /Syntax error in text|mermaid-error/.test(svg)) throw new Error(`Invalid ${language} diagram ${index + 1}`);
    const title = source.match(/(?:%%|') title: (.+)/)?.[1] || `${language} diagram ${index + 1}`;
    processed = processed.replace(block[0], () => `<div class="diagram"><img alt="${escape(title)}" data-engine="${language}" data-diagram="${index + 1}" src="data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}"></div>`);
    diagrams.push({ number: index + 1, language, title, sourceHash: key });
  }
} finally {
  await browser?.close();
}
const marp = new Marp({ html: true, inlineSVG: false });
marp.themeSet.add(theme);
const { html, css } = marp.render(processed);
const titles = [...markdown.matchAll(/^#{1,2} (.+)$/gm)].map(match => match[1].replace(/<[^>]+>/g, ' '));
const count = (html.match(/<section\b/g) || []).length;
if (ids.length !== count || titles.length !== count || new Set(ids).size !== count) throw new Error(`Slide metadata mismatch: ids=${ids.length}, titles=${titles.length}, sections=${count}`);
const data = JSON.stringify({ revision, ids, titles, markdown, sources, ...focus }).replaceAll('<', '\\u003c');
const output = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'">
<title>${escape(focus.title)}</title><style>${css}\n${appStyle}</style></head>
<body><header class="chrome"><span class="brand">${escape(focus.title)}</span>
<button id="previous" aria-label="Previous slide">Previous</button><select id="slide-picker" aria-label="Choose slide"></select>
<button id="next" aria-label="Next slide">Next</button><button id="follow" aria-pressed="true">Follow latest: on</button></header>
<main id="stage"><div id="deck">${html}</div></main>
<nav class="chrome" aria-label="Presentation tools"><span id="counter"></span><button id="reading">Read all</button>
<button id="zoom">Zoom diagram</button><button id="source">Download Markdown</button>
<span class="brand"></span><span>Proposal, not implementation authorization</span></nav>
<div class="status"><span id="live-status" role="status">Loading preview</span><span>Arrow keys navigate; manual navigation pauses following</span></div>
<details><summary>About this presentation</summary><p>Diagrams are locally rendered from Mermaid and PlantUML in Marp Markdown. Everything needed to read the deck is embedded. Questions and approvals stay in Chat. Presentation evidence is not product acceptance.</p>
<details><summary>Third-party notices</summary><pre>${escape(marpLicense + '\n\n' + mermaidLicense)}</pre></details></details>
<script id="deck-data" type="application/json">${data}</script><script>${appScript}</script></body></html>`;
const temporary = join(root, `index.${process.pid}.next`);
await writeFile(temporary, output);
await rename(temporary, join(root, 'index.html'));
const manifestTemporary = join(root, `.cache/build.${process.pid}.next`);
await writeFile(manifestTemporary, JSON.stringify({ revision, htmlSha256: hash(output), slides: count, ids, diagrams, ...focus }, null, 2));
await rename(manifestTemporary, join(root, '.cache/build.json'));
console.log(`Built ${count} slides; ${diagrams.length} diagrams; revision ${revision}; focus ${focus.focus}`);
