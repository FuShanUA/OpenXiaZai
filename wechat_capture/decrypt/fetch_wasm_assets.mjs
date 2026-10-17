#!/usr/bin/env node
/**
 * Fetch the official WeChat video-decode WASM assets the decryptor drives.
 *
 * These assets are *not* vendored in the repository: they are WeChat's
 * proprietary build, and shipping them would mean redistributing someone else's
 * code. Clone-and-run therefore has exactly one extra step, and this is it.
 *
 * The CDN serves gzip-compressed bodies without a reliable Content-Encoding
 * header, so every download is sniffed and decompressed here. The decompressed
 * assets — the ones the decryptor actually loads — land in
 * vendor/wechat_files/raw; the untouched downloads are kept alongside them so a
 * "the bytes differ" bug can be told apart from "the download was wrong".
 *
 * File names come from wasm_assets.mjs, which the decryptor imports too, so the
 * two can never disagree about what to write and what to read.
 *
 * Usage:
 *   node work/decrypt/fetch_wasm_assets.mjs              # fetch what is missing
 *   node work/decrypt/fetch_wasm_assets.mjs --force      # re-download everything
 *   node work/decrypt/fetch_wasm_assets.mjs --check      # verify only, no network
 *   node work/decrypt/fetch_wasm_assets.mjs --version 1.2.46
 *
 * Needs network access to aladin.wxqcloud.qq.com. Everything runs offline after
 * this; vendor/ is ignored by git, so the files stay local to your machine.
 */

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

import { ASSETS, CDN_VERSION, REQUIRED_FILES, WASM_FILE } from './wasm_assets.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const RAW_DIR = process.env.WX_WASM_ASSETS
  ? path.resolve(process.env.WX_WASM_ASSETS)
  : path.join(ROOT, 'vendor', 'wechat_files', 'raw');
const OUT_DIR = path.dirname(RAW_DIR);

const args = process.argv.slice(2);
const version = args.includes('--version') ? args[args.indexOf('--version') + 1] : CDN_VERSION;
const force = args.includes('--force');
const BASE = `https://aladin.wxqcloud.qq.com/aladin/ffmepeg/video-decode/${version}`;
const FETCH_COMMAND = 'node work/decrypt/fetch_wasm_assets.mjs';

const USAGE = `Fetch the official WeChat WASM assets used by the decryptor.

  ${FETCH_COMMAND} [--force] [--check] [--version ${CDN_VERSION}]

  (no flag)   download anything that is missing, then verify
  --force     re-download every asset, even ones already present
  --check     verify the local assets only; never touches the network
  --version   fetch a different official build (default ${CDN_VERSION})

Assets land in vendor/wechat_files/, which git ignores — they are never
committed, so this is a one-time step per clone.
`;

function maybeGunzip(buffer) {
  if (buffer.length > 2 && buffer[0] === 0x1f && buffer[1] === 0x8b) {
    return { data: zlib.gunzipSync(buffer), compressed: true };
  }
  return { data: buffer, compressed: false };
}

async function download(asset) {
  const target = path.join(RAW_DIR, asset.local);
  if (!force && fs.existsSync(target) && fs.statSync(target).size > 0) {
    console.log(`skip   ${asset.local} (already present, use --force to refresh)`);
    return false;
  }
  const url = `${BASE}/${asset.remote}`;
  let raw;
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}`.trim());
    }
    raw = Buffer.from(await response.arrayBuffer());
  } catch (error) {
    throw new Error(
      `cannot download ${url}\n` +
        `  reason: ${error?.message ?? error}\n` +
        `  this step needs network access to aladin.wxqcloud.qq.com\n` +
        `  if you are behind a proxy, make sure node can see it (HTTPS_PROXY)`,
    );
  }
  // Keep the untouched download for reference next to the decompressed asset.
  fs.writeFileSync(path.join(OUT_DIR, asset.remote), raw);
  const { data, compressed } = maybeGunzip(raw);
  fs.writeFileSync(target, data);
  console.log(
    `ok     ${asset.remote} -> raw/${asset.local} ` +
      `(${raw.length} bytes${compressed ? ` gzip -> ${data.length}` : ''})`,
  );
  return true;
}

function verify() {
  const missing = REQUIRED_FILES.filter((name) => {
    const file = path.join(RAW_DIR, name);
    return !fs.existsSync(file) || fs.statSync(file).size === 0;
  });
  if (missing.length) {
    throw new Error(
      `missing required asset(s): ${missing.join(', ')}\n` +
        `  looked in: ${RAW_DIR}\n` +
        `  fix: ${FETCH_COMMAND}          (downloads from WeChat's official CDN)`,
    );
  }
  const wasm = fs.readFileSync(path.join(RAW_DIR, WASM_FILE));
  const magic = wasm.subarray(0, 4).toString('hex');
  if (magic !== '0061736d') {
    throw new Error(
      `raw/${WASM_FILE} is not a WebAssembly module (magic ${magic})\n` +
        `  fix: ${FETCH_COMMAND} --force`,
    );
  }
  console.log(
    `verified ${REQUIRED_FILES.length} required assets in ${path.relative(ROOT, RAW_DIR)}; ` +
      'WASM magic header is intact',
  );
}

async function main() {
  if (args.includes('--help') || args.includes('-h')) {
    console.log(USAGE);
    return;
  }
  // --check verifies the local assets without touching the network; run.sh uses
  // it so a naming drift between this script and the decryptor fails fast.
  if (args.includes('--check')) {
    verify();
    return;
  }
  fs.mkdirSync(RAW_DIR, { recursive: true });
  console.log(`source: ${BASE}`);
  let downloaded = 0;
  for (const asset of ASSETS) {
    if (await download(asset)) downloaded += 1;
  }
  verify();
  if (downloaded === 0) {
    console.log('\nnothing to do: every asset was already present (use --force to re-download)');
    if (version !== CDN_VERSION) {
      console.warn(
        `warning: --version ${version} had no effect — the files on disk are whatever was\n` +
          '         fetched before. Add --force to actually replace them.',
      );
    }
  } else {
    console.log(`\ndownloaded ${downloaded} asset(s) from the official ${version} build`);
  }
  console.log('assets are local only: git ignores vendor/, so this stays on your machine');
  console.log('next: node work/decrypt/wechat_video_decrypt.mjs selftest');
}

main().catch((error) => {
  console.error(`error: ${error?.message ?? error}`);
  process.exitCode = 1;
});
