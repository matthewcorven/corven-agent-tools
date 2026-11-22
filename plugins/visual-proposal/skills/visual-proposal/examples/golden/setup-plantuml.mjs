import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { plantumlVersion, plantumlPath, verifyJar, readVerifiedJar } from './plantuml.mjs';

if (existsSync(plantumlPath)) {
  await readVerifiedJar();
  console.log('Existing PlantUML jar verified.');
} else {
  const response = await fetch(`https://github.com/plantuml/plantuml/releases/download/v${plantumlVersion}/plantuml.jar`);
  if (!response.ok) throw new Error(`PlantUML download failed: HTTP ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  verifyJar(bytes);
  await mkdir(dirname(plantumlPath), { recursive: true });
  await writeFile(plantumlPath, bytes, { flag: 'wx' });
  console.log('Pinned PlantUML jar downloaded and verified.');
}
