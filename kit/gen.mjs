// Style-conditioned item references for AnyKit: GPT Image 2.5 edit with the customer's style reference(s) as input
// images, so every item is drawn in the same world. Writes the same <out>.json as generate/fal.mjs `image`, so
// `node generate/fal.mjs 3d --ref <out>.json ...` takes it straight to Tripo / Meshy. Spend goes to generate/spend.jsonl.
//
//   node kit/gen.mjs edit --out refs/kit/<id> --prompt "..." --image input/reference_1.png [--image <url|path> ...]
//        [--variant sunburst|flare] [--quality high] [--n 1] [--size auto|square_hd|...] [--force]
//
// Re-running is free: an existing <out>.json is skipped (--force regenerates), and a job submitted by a run that
// was cut off (<out>.pending.json) is polled to the end instead of paid for again.
//
// --image takes URLs or local paths (sent inline as data URIs). input/style_urls.json holds the customer's
// references as fal URLs; prefer those.

import { access, appendFile, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { dirname, extname, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

const KEY = process.env.FAL_KEY || process.env.FAL_API_KEY;
const HERE = dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Z]:)/, '$1'));
const REPO = resolve(HERE, '..');
const SPEND_LOG = join(REPO, 'generate', 'spend.jsonl');
const EST = 0.3;

const { positionals, values: a } = parseArgs({
  allowPositionals: true,
  options: {
    out: { type: 'string' }, prompt: { type: 'string' }, image: { type: 'string', multiple: true },
    variant: { type: 'string', default: 'sunburst' }, quality: { type: 'string', default: 'high' },
    n: { type: 'string', default: '1' }, size: { type: 'string' }, force: { type: 'boolean', default: false },
  },
});

async function call(url, init = {}) {
  const res = await fetch(url, { ...init, headers: { authorization: `Key ${KEY}`, 'content-type': 'application/json' } });
  const text = await res.text();
  if (!res.ok) throw new Error(`${res.status} ${url}: ${text.slice(0, 600)}`);
  return text ? JSON.parse(text) : null;
}

async function asUrl(s) {
  if (/^(https?|data):/.test(s)) return s;
  const buf = await readFile(resolve(REPO, s));
  const mime = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp' }[extname(s).toLowerCase()] || 'image/png';
  return `data:${mime};base64,${buf.toString('base64')}`;
}

async function edit() {
  if (!KEY) throw new Error('FAL_KEY is not set');
  if (!a.out || !a.prompt || !a.image?.length) throw new Error('--out, --prompt and at least one --image are required');
  const out = resolve(REPO, a.out);
  const exists = (f) => access(f).then(() => true, () => false);
  if (!a.force && await exists(`${out}.json`)) {
    console.log(`exists ${a.out}.json (skipped; --force to regenerate)`);
    return;
  }
  const endpoint = `openai/gpt-image-2.5/${a.variant}/edit`;
  const input = { prompt: a.prompt, image_urls: await Promise.all(a.image.map(asUrl)), quality: a.quality,
    num_images: +a.n, output_format: 'png', ...(a.size ? { image_size: a.size } : {}) };
  const t0 = performance.now();
  const pending = `${out}.pending.json`;
  let job;
  if (!a.force && await exists(pending)) {
    job = JSON.parse(await readFile(pending, 'utf8'));
    console.log(`resuming ${endpoint} ${job.request_id}`);
  } else {
    job = await call(`https://queue.fal.run/${endpoint}`, { method: 'POST', body: JSON.stringify(input) });
    await mkdir(dirname(out), { recursive: true });
    await writeFile(pending, JSON.stringify(job));
    await mkdir(dirname(SPEND_LOG), { recursive: true });
    await appendFile(SPEND_LOG, JSON.stringify({ time: new Date().toISOString(), endpoint, est_usd: EST * +a.n,
      request_id: job.request_id, kind: 'image', out: a.out }) + '\n');
    console.log(`queued ${endpoint} ${job.request_id}`);
  }
  for (;;) {
    await new Promise((r) => setTimeout(r, 3000));
    const st = await call(job.status_url);
    if (st.status === 'COMPLETED') break;
    if (performance.now() - t0 > 15 * 60_000) throw new Error(`timed out; request ${job.request_id}`);
  }
  const result = await call(job.response_url);
  const files = [];
  await mkdir(dirname(out), { recursive: true });
  for (const [i, img] of (result.images ?? []).entries()) {
    const f = `${out}_${i}.png`;
    const r = await fetch(img.url);
    await writeFile(f, Buffer.from(await r.arrayBuffer()));
    files.push({ file: f, url: img.url, width: img.width, height: img.height });
  }
  if (!files.length) throw new Error('no images in response: ' + JSON.stringify(result).slice(0, 400));
  const seconds = (performance.now() - t0) / 1000;
  // inputs can be multi-MB data URIs: keep only their kind in the record
  const record = { ...input, image_urls: input.image_urls.map((u) => (u.startsWith('data:') ? 'data:(inline)' : u)) };
  await writeFile(`${out}.json`, JSON.stringify({ endpoint, request_id: job.request_id, seconds, input: record, files }, null, 2));
  await rm(pending, { force: true });
  console.log(`saved ${files.map((f) => f.file).join(', ')} (${seconds.toFixed(0)} s)`);
}

if (positionals[0] !== 'edit') {
  console.error('usage: node kit/gen.mjs edit --out ... --prompt ... --image ... (see header)');
  process.exit(1);
}
await edit();
