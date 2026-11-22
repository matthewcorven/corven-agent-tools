import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

export const plantumlVersion = '1.2026.9';
export const plantumlSha256 = 'aa4a1d06f8911c4b842beff4828dd955be431692b631a312d04c0e9fab9581de';
export const plantumlPath = process.env.PLANTUML_JAR || fileURLToPath(new URL('.tools/plantuml.jar', import.meta.url));
export function verifyJar(bytes) {
  if (createHash('sha256').update(bytes).digest('hex') !== plantumlSha256) {
    throw new Error('PlantUML SHA-256 mismatch; use the pinned official jar, never an unverified replacement.');
  }
}
export async function readVerifiedJar() {
  let bytes;
  try { bytes = await readFile(plantumlPath); }
  catch (error) {
    if (error.code === 'ENOENT') throw new Error('PlantUML jar missing; run npm run setup:plantuml or set PLANTUML_JAR.');
    throw error;
  }
  verifyJar(bytes);
}
