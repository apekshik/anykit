// synced from github.com/apekshik/anyscene generate/fal.mjs
// fal client for AnyScene: GPT Image 2.5 references + Tripo / Meshy image-to-3D, with a spend log.
// Adapted from character-lab/generate/fal.mjs.
//
// The key comes from the user environment variable FAL_KEY (machine-wide; never print it):
//   node generate/fal.mjs <command> [flags]
//
// Commands
//   image --out refs/tokyo-electronics-repair/concept_a --prompt "..." [--variant flare|sunburst] [--size landscape_16_9] [--quality high] [--n 1]
//         -> <out>_<i>.png + <out>.json (keeps the fal URL so 3D endpoints can reuse it)
//   3d    --model tripo-h31|tripo-p2|meshy --ref refs/props/x.json --out assets/gen/<model>/<id> [--face-limit 150000]
//         -> <out>/model.glb + result.json
//   spend -> totals so far
//
// All paid calls append {time, endpoint, est_usd, request_id, ...} to generate/spend.jsonl.

import { appendFile, mkdir, readFile, unlink, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

// The machine-wide key lives in the user environment (FAL_KEY). Read it from the registry so shells started
// before it was set still pick it up; fall back to the process env / --env-file.
function globalKey() {
  if (process.platform !== 'win32') return undefined;
  try {
    const out = execFileSync('reg', ['query', 'HKCU\\Environment', '/v', 'FAL_KEY'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    return out.match(/FAL_KEY\s+REG_\w+\s+(\S+)/)?.[1];
  } catch {
    return undefined;
  }
}
const KEY = globalKey() || process.env.FAL_KEY || process.env.FAL_API_KEY;
const HERE = dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Z]:)/, '$1'));
const REPO = resolve(HERE, '..');
const SPEND_LOG = join(REPO, 'generate', 'spend.jsonl');

/** Budget is unlimited per the user; this only guards against runaway loops. */
const TOTAL_CAP = 1000;

/** Rough per-call estimates for the log (fal pricing API, 2026-10-27; GPT Image is token-billed, so estimated). */
const EST = { image: 0.25, 'tripo-h31': 0.6, 'tripo-p2': 0.6, meshy: 0.8 };

const MODELS3D = {
  'tripo-h31': {
    endpoint: 'tripo3d/h3.1/image-to-3d',
    input: { pbr: true, texture: true, geometry_quality: 'detailed', texture_quality: 'detailed', auto_size: true, orientation: 'align_image' },
  },
  'tripo-p2': {
    endpoint: 'tripo3d/p2/image-to-3d',
    input: { pbr: true, texture: true, texture_quality: 'extreme', texture_version: 'v3.5-20260815', delight: true, auto_size: true, export_uv: true },
  },
  meshy: {
    endpoint: 'meshy/v7.1/image-to-3d',
    input: { enable_pbr: true, should_texture: true, geometry_resolution: '4k', topology: 'triangle', should_remesh: true, target_polycount: 150000, symmetry_mode: 'auto' },
  },
};

const { positionals, values: a } = parseArgs({
  allowPositionals: true,
  options: {
    out: { type: 'string' }, prompt: { type: 'string' }, variant: { type: 'string', default: 'flare' },
    size: { type: 'string', default: 'landscape_16_9' }, quality: { type: 'string', default: 'high' }, n: { type: 'string', default: '1' },
    model: { type: 'string' }, ref: { type: 'string' }, 'face-limit': { type: 'string' }, force: { type: 'boolean' },
  },
});

async function readSpend() {
  if (!existsSync(SPEND_LOG)) return [];
  return (await readFile(SPEND_LOG, 'utf8')).split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

async function logSpend(row) {
  await mkdir(dirname(SPEND_LOG), { recursive: true });
  await appendFile(SPEND_LOG, JSON.stringify({ time: new Date().toISOString(), ...row }) + '\n');
}

async function call(url, init = {}) {
  const res = await fetch(url, { ...init, headers: { authorization: `Key ${KEY}`, 'content-type': 'application/json' } });
  const text = await res.text();
  if (!res.ok) throw new Error(`${res.status} ${url}: ${text.slice(0, 600)}`);
  return text ? JSON.parse(text) : null;
}

/** Queue-submit, poll, return result. Logs spend as soon as the request is accepted.
 *  With `pending` (a file path), the submitted job is recorded there until its result is in. If a run is cut off (the
 *  process or the whole worker dies) and the same command runs again, it picks the recorded job back up instead of
 *  paying for a new one. */
async function run(endpoint, input, { est, meta = {}, timeoutMin = 30, pending = null }) {
  if (!KEY) throw new Error('FAL_KEY is not set (user environment variable)');
  const t0 = performance.now();
  let job = null;
  if (pending && existsSync(pending)) {
    try {
      job = JSON.parse(await readFile(pending, 'utf8'));
      await call(job.status_url);  // still known to the queue?
      console.log(`resuming ${endpoint} ${job.request_id}`);
    } catch {
      job = null;  // expired or unknown: submit again
    }
  }
  if (!job) {
    const spent = (await readSpend()).reduce((s, r) => s + r.est_usd, 0);
    if (spent + est > TOTAL_CAP) throw new Error(`BUDGET: total would reach $${(spent + est).toFixed(2)} > $${TOTAL_CAP}. Stop and report.`);
    job = await call(`https://queue.fal.run/${endpoint}`, { method: 'POST', body: JSON.stringify(input) });
    await logSpend({ endpoint, est_usd: est, request_id: job.request_id, ...meta });
    if (pending) {
      await mkdir(dirname(pending), { recursive: true });
      await writeFile(pending, JSON.stringify(job));
    }
    console.log(`queued ${endpoint} ${job.request_id}`);
  }
  for (;;) {
    await new Promise((r) => setTimeout(r, 4000));
    const st = await call(job.status_url);
    if (st.status === 'COMPLETED') break;
    if (performance.now() - t0 > timeoutMin * 60_000) throw new Error(`timed out; request ${job.request_id}`);
  }
  const result = await call(job.response_url);
  const seconds = (performance.now() - t0) / 1000;
  console.log(`done ${endpoint} in ${seconds.toFixed(0)} s`);
  return { result, request_id: job.request_id, seconds };
}

async function download(url, out) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`download ${res.status} ${url}`);
  await mkdir(dirname(out), { recursive: true });
  await writeFile(out, Buffer.from(await res.arrayBuffer()));
  return out;
}

function need(name) {
  if (!a[name]) throw new Error(`--${name} is required`);
  return a[name];
}

async function cmdImage() {
  const out = resolve(REPO, need('out'));
  // already made (a resumed build re-running its batch): never pay twice. --force regenerates.
  if (!a.force && existsSync(`${out}.json`) && [...Array(+a.n).keys()].every((i) => existsSync(`${out}_${i}.png`))) {
    console.log(`exists ${a.out}_0.png (skipped; --force to regenerate)`);
    return;
  }
  const endpoint = `openai/gpt-image-2.5/${a.variant}/text-to-image`;
  const input = { prompt: need('prompt'), image_size: a.size, quality: a.quality, num_images: +a.n, output_format: 'png' };
  const { result, request_id, seconds } = await run(endpoint, input, { est: EST.image * +a.n, meta: { kind: 'image', out: a.out },
                                                                   pending: `${out}.pending.json` });
  const files = [];
  for (const [i, img] of (result.images ?? []).entries()) {
    const f = `${out}_${i}.png`;
    await download(img.url, f);
    files.push({ file: f, url: img.url, width: img.width, height: img.height });
  }
  await writeFile(`${out}.json`, JSON.stringify({ endpoint, request_id, seconds, input, files }, null, 2));
  await unlink(`${out}.pending.json`).catch(() => {});
  console.log(`saved ${files.map((f) => f.file).join(', ')}`);
}

async function cmd3d() {
  const spec = MODELS3D[need('model')];
  if (!spec) throw new Error(`--model must be one of ${Object.keys(MODELS3D).join(', ')}`);
  const ref = JSON.parse(await readFile(resolve(REPO, need('ref')), 'utf8'));
  const out = resolve(REPO, need('out'));
  if (!a.force && existsSync(join(out, 'model.glb'))) {
    console.log(`exists ${a.out}/model.glb (skipped; --force to regenerate)`);
    return;
  }
  const input = { ...spec.input, image_url: ref.files[0].url };
  // Tripo otherwise returns ~2M triangles per prop: slow to import, compare and decimate. Meshy ignores it.
  if (a['face-limit'] && a.model.startsWith('tripo')) input.face_limit = Number(a['face-limit']);
  const { result, request_id, seconds } = await run(spec.endpoint, input, { est: EST[a.model], meta: { kind: '3d', model: a.model, out: a.out },
                                                                         pending: join(out, 'pending.json') });
  await mkdir(out, { recursive: true });
  await writeFile(join(out, 'result.json'), JSON.stringify({ endpoint: spec.endpoint, request_id, seconds, input, result }, null, 2));
  // Tripo returns model_mesh / pbr_model, Meshy returns model_glb; take the first GLB we find.
  const glb = JSON.stringify(result).match(/https:\/\/[^"]+?\.glb[^"]*/)?.[0];
  if (!glb) throw new Error('no GLB in response: ' + JSON.stringify(result).slice(0, 500));
  await download(glb, join(out, 'model.glb'));
  await unlink(join(out, 'pending.json')).catch(() => {});
  console.log(`saved ${a.out}/model.glb (${seconds.toFixed(0)} s)`);
}

async function cmdSpend() {
  const rows = await readSpend();
  const by = {};
  for (const r of rows) by[r.endpoint] = (by[r.endpoint] ?? 0) + r.est_usd;
  for (const [e, v] of Object.entries(by)) console.log(`${e.padEnd(48)} $${v.toFixed(2)}`);
  console.log(`${'total'.padEnd(48)} $${rows.reduce((s, r) => s + r.est_usd, 0).toFixed(2)}  (${SPEND_LOG})`);
}

const cmds = { image: cmdImage, '3d': cmd3d, spend: cmdSpend };
const cmd = cmds[positionals[0]];
if (!cmd) {
  console.error(`usage: fal.mjs <${Object.keys(cmds).join('|')}> [flags]  (see header)`);
  process.exit(1);
}
await cmd();
