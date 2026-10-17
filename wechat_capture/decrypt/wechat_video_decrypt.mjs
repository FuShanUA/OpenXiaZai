#!/usr/bin/env node
/**
 * WeChat Channels (微信视频号) video decryption.
 *
 * WeChat encrypts only the first 131072 bytes (128 KiB) of the media file by
 * XOR-ing it with an Isaac64 PRNG keystream seeded by the per-response
 * `decodeKey`. The keystream produced by the official WASM module must be
 * reversed before use.
 *
 * This tool drives the official `wasm_video_decode.wasm` (loaded from
 * vendor/wechat_files) so the keystream matches the client byte for byte.
 *
 * Usage:
 *   node wechat_video_decrypt.mjs keystream --key <decodeKey> [-o keystream.bin]
 *   node wechat_video_decrypt.mjs decrypt   --key <decodeKey> -i in.mp4 -o out.mp4
 *   node wechat_video_decrypt.mjs info      -i in.mp4
 *   node wechat_video_decrypt.mjs selftest
 *
 * Common options:
 *   --assets <dir>   Directory holding the decompressed WASM assets
 *                    (default: <repo>/vendor/wechat_files/raw)
 *   --json           Emit a machine readable result on stdout
 *   --quiet          Suppress the human readable report
 */

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { GLUE_FILE, WASM_FILE } from './wasm_assets.mjs';

const KEYSTREAM_SIZE = 131072; // 128 KiB, the only encrypted region
const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_ASSETS = process.env.WX_WASM_ASSETS
  ? path.resolve(process.env.WX_WASM_ASSETS)
  : path.resolve(HERE, '..', '..', 'vendor', 'wechat_files', 'raw');

// Test vector derived from the published sample of
// https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption:
// decodeKey "2136343393" with a known encrypted/plaintext header pair. XOR-ing
// the two gives the first 16 keystream bytes, which is the oracle below.
const SELFTEST_SEED = '2136343393';
const SELFTEST_KEYSTREAM_PREFIX = Buffer.from(
  '23766a3699fb876a75d5a232994844ab',
  'hex',
);

function parseArgs(argv) {
  const options = { command: argv[0], assets: DEFAULT_ASSETS, json: false, quiet: false };
  for (let i = 1; i < argv.length; i += 1) {
    const token = argv[i];
    const next = () => {
      i += 1;
      if (i >= argv.length) throw new Error(`Missing value for ${token}`);
      return argv[i];
    };
    switch (token) {
      case '--key':
      case '-k':
        options.key = next();
        break;
      case '--input':
      case '-i':
        options.input = next();
        break;
      case '--output':
      case '-o':
        options.output = next();
        break;
      case '--assets':
        options.assets = path.resolve(next());
        break;
      case '--json':
        options.json = true;
        break;
      case '--quiet':
      case '-q':
        options.quiet = true;
        break;
      case '--help':
      case '-h':
        options.help = true;
        break;
      default:
        if (token.startsWith('-')) throw new Error(`Unknown option: ${token}`);
        if (!options.key) options.key = token;
        else throw new Error(`Unexpected argument: ${token}`);
    }
  }
  return options;
}

/**
 * Instantiate the official Emscripten module.
 *
 * The build was produced for the Web Worker environment, so it expects
 * `self.location` and XHR-based loading. Supplying `Module.wasmBinary` keeps
 * the whole thing offline: no fetch, no XHR, no network.
 */
function loadWasmModule(assetsDir) {
  const gluePath = path.join(assetsDir, GLUE_FILE);
  const wasmPath = path.join(assetsDir, WASM_FILE);
  for (const file of [gluePath, wasmPath]) {
    if (!fs.existsSync(file)) {
      throw new Error(
        `Missing WASM asset: ${file}\n` +
          'Fetch it once with: node work/decrypt/fetch_wasm_assets.mjs\n' +
          "(downloads the official build from WeChat's CDN into vendor/wechat_files/)",
      );
    }
  }

  const captured = { keystream: null, calls: 0 };
  const Module = {
    wasmBinary: fs.readFileSync(wasmPath),
    locateFile: (name) => path.join(assetsDir, name),
    noExitRuntime: true,
    print: () => {},
    printErr: (...args) => {
      if (process.env.WX_DECRYPT_DEBUG) console.error('[wasm]', ...args);
    },
  };

  const sandbox = {
    Module,
    console,
    WebAssembly,
    TextDecoder,
    TextEncoder,
    performance,
    process,
    Buffer,
    URL,
    Promise,
    // The glue resolves `var wasmBinaryFile = VTS_WASM_URL`, injected by the page.
    VTS_WASM_URL: wasmPath,
    queueMicrotask,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    // The wasm calls this bare global (ASM_CONSTS) when the keystream is ready.
    wasm_isaac_generate(ptr, size) {
      captured.calls += 1;
      const view = new Uint8Array(Module.HEAPU8.buffer, ptr, size);
      // The client reverses the generated stream in place before XOR-ing.
      view.reverse();
      captured.keystream = Uint8Array.from(view);
    },
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.location = { href: `file://${gluePath}` };
  sandbox.self.location = sandbox.location;
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(gluePath, 'utf8'), sandbox, { filename: gluePath });

  // The wasm instance is created asynchronously, so the embind exports only
  // appear once the runtime is ready.
  const ready = (async () => {
    const deadline = Date.now() + 30000;
    while (typeof Module.WxIsaac64 !== 'function') {
      if (Date.now() > deadline) throw new Error('Timed out waiting for the WASM runtime to initialise');
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    return Module;
  })();

  return {
    Module,
    captured,
    ready,
    async generate(decodeKey, size) {
      await ready;
      captured.keystream = null;
      if (typeof Module.WxIsaac64 !== 'function') {
        throw new Error('WxIsaac64 export is unavailable; the WASM module did not initialise');
      }
      const decryptor = await new Module.WxIsaac64(String(decodeKey));
      try {
        await decryptor.generate(size);
      } finally {
        try {
          await decryptor.delete();
        } catch {
          /* the module is single use anyway */
        }
      }
      if (!captured.keystream || captured.keystream.length !== size) {
        throw new Error(
          `Keystream generation returned ${captured.keystream?.length ?? 0} bytes, expected ${size}`,
        );
      }
      return captured.keystream;
    },
  };
}

function hexPreview(buffer, length = 32) {
  return Array.from(buffer.subarray(0, length))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join(' ');
}

function describeMp4(buffer) {
  if (buffer.length < 16) return { valid: false, reason: 'file is shorter than 16 bytes' };
  const boxSize = buffer.readUInt32BE(0);
  const boxType = buffer.toString('latin1', 4, 8);
  const majorBrand = buffer.toString('latin1', 8, 12);
  const compatibleBrands = [];
  for (let offset = 16; offset + 4 <= Math.min(buffer.length, 64); offset += 4) {
    const brand = buffer.toString('latin1', offset, offset + 4);
    if (/^[\x20-\x7e]{4}$/.test(brand)) compatibleBrands.push(brand);
    else break;
  }
  return {
    valid: boxType === 'ftyp',
    boxSize,
    boxType,
    majorBrand,
    compatibleBrands,
    reason: boxType === 'ftyp' ? null : `expected 'ftyp' at offset 4, found ${JSON.stringify(boxType)}`,
  };
}

function decryptBuffer(encrypted, keystream) {
  const output = Buffer.from(encrypted);
  const length = Math.min(keystream.length, output.length);
  for (let i = 0; i < length; i += 1) output[i] ^= keystream[i];
  return output;
}

function report(options, payload, lines) {
  if (options.json) {
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    return;
  }
  if (options.quiet) return;
  for (const line of lines) console.log(line);
}

function fail(options, message) {
  if (options.json) {
    process.stdout.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
  } else {
    console.error(`error: ${message}`);
  }
  process.exitCode = 1;
}

async function cmdKeystream(options) {
  if (!options.key) return fail(options, 'keystream requires --key <decodeKey>');
  const { generate } = loadWasmModule(options.assets);
  const keystream = await generate(options.key, KEYSTREAM_SIZE);
  if (options.output) fs.writeFileSync(options.output, keystream);
  report(
    options,
    {
      ok: true,
      command: 'keystream',
      decodeKey: options.key,
      bytes: keystream.length,
      sha256: (await import('node:crypto')).createHash('sha256').update(keystream).digest('hex'),
      preview: hexPreview(keystream, 32),
      output: options.output ?? null,
    },
    [
      `decodeKey      : ${options.key}`,
      `keystream      : ${keystream.length} bytes (reversed 128 KiB)`,
      `preview        : ${hexPreview(keystream, 32)}`,
      options.output ? `written to     : ${options.output}` : '(--output not given, nothing written)',
    ],
  );
}

async function cmdDecrypt(options) {
  if (!options.key) return fail(options, 'decrypt requires --key <decodeKey>');
  if (!options.input) return fail(options, 'decrypt requires --input <encrypted.mp4>');
  const encrypted = fs.readFileSync(options.input);
  const { generate } = loadWasmModule(options.assets);
  const keystream = await generate(options.key, KEYSTREAM_SIZE);
  const decrypted = decryptBuffer(encrypted, keystream);
  const mp4 = describeMp4(decrypted);
  const output = options.output ?? `${options.input.replace(/\.mp4$/i, '')}.decrypted.mp4`;
  fs.writeFileSync(output, decrypted);
  if (!options.json && !options.quiet) {
    console.log(`input          : ${options.input} (${encrypted.length} bytes)`);
    console.log(`decodeKey      : ${options.key}`);
    console.log(`encrypted head : ${hexPreview(encrypted, 16)}`);
    console.log(`keystream head : ${hexPreview(keystream, 16)}`);
    console.log(`decrypted head : ${hexPreview(decrypted, 16)}`);
    console.log(`type           : ${mp4.boxType} / brand ${mp4.majorBrand}`);
    console.log(mp4.valid ? 'result         : OK, valid MP4' : `result         : FAILED (${mp4.reason})`);
    console.log(`written to     : ${output}`);
  }
  report(
    options,
    {
      ok: mp4.valid,
      command: 'decrypt',
      input: options.input,
      output,
      decodeKey: options.key,
      bytes: decrypted.length,
      encryptedHead: hexPreview(encrypted, 16),
      keystreamHead: hexPreview(keystream, 16),
      decryptedHead: hexPreview(decrypted, 16),
      mp4,
    },
    [],
  );
  if (!mp4.valid) process.exitCode = 2;
}

function cmdInfo(options) {
  if (!options.input) return fail(options, 'info requires --input <file>');
  const data = fs.readFileSync(options.input);
  const mp4 = describeMp4(data);
  report(
    options,
    { ok: true, command: 'info', input: options.input, bytes: data.length, head: hexPreview(data, 32), mp4 },
    [
      `input : ${options.input} (${data.length} bytes)`,
      `head  : ${hexPreview(data, 32)}`,
      `state : ${mp4.valid ? 'looks like a plain MP4' : 'looks encrypted / not an MP4'}`,
    ],
  );
}

async function cmdSelftest(options) {
  const { generate } = loadWasmModule(options.assets);
  const keystream = await generate(SELFTEST_SEED, KEYSTREAM_SIZE);
  const actual = Buffer.from(keystream.subarray(0, SELFTEST_KEYSTREAM_PREFIX.length));
  const pass = actual.equals(SELFTEST_KEYSTREAM_PREFIX);

  // Full pipeline check: build a fake "encrypted" payload from a real MP4
  // header, run it through the same XOR path used for captures, and confirm the
  // MP4 signature survives the roundtrip.
  const plaintext = Buffer.alloc(KEYSTREAM_SIZE + 64);
  Buffer.from('000000206674797069736f6d0000020069736f6d69736f32617663316d703431', 'hex').copy(plaintext);
  plaintext.fill(0xab, 32, KEYSTREAM_SIZE + 64);
  const encrypted = decryptBuffer(plaintext, keystream); // XOR is its own inverse
  const recovered = decryptBuffer(encrypted, keystream);
  const roundtrip = recovered.equals(plaintext) && describeMp4(recovered).valid;

  report(
    options,
    {
      ok: pass && roundtrip,
      command: 'selftest',
      decodeKey: SELFTEST_SEED,
      keystream: { expected: SELFTEST_KEYSTREAM_PREFIX.toString('hex'), actual: actual.toString('hex'), pass },
      roundtrip: {
        pass: roundtrip,
        encryptedHead: hexPreview(encrypted, 16),
        recoveredHead: hexPreview(recovered, 16),
      },
    },
    [
      `selftest decodeKey : ${SELFTEST_SEED}`,
      `expected prefix    : ${SELFTEST_KEYSTREAM_PREFIX.toString('hex')}`,
      `actual prefix      : ${actual.toString('hex')}`,
      pass ? 'keystream          : PASS (matches the official client)' : 'keystream          : FAIL',
      `roundtrip          : ${roundtrip ? 'PASS' : 'FAIL'} (encrypted head ${hexPreview(encrypted, 16)})`,
      pass && roundtrip ? 'result             : PASS' : 'result             : FAIL',
    ],
  );
  if (!pass || !roundtrip) process.exitCode = 3;
}

const USAGE = `WeChat Channels video decryption

  keystream --key <decodeKey> [-o keystream.bin]
  decrypt   --key <decodeKey> -i <encrypted> [-o <output>]
  info      -i <file>
  selftest

options: --assets <dir>  --json  --quiet
`;

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help || !options.command) {
    process.stdout.write(USAGE);
    return;
  }
  switch (options.command) {
    case 'keystream':
      return cmdKeystream(options);
    case 'decrypt':
      return cmdDecrypt(options);
    case 'info':
      return cmdInfo(options);
    case 'selftest':
      return cmdSelftest(options);
    default:
      process.stdout.write(USAGE);
      return fail(options, `Unknown command: ${options.command}`);
  }
}

main().catch((error) => {
  console.error(`error: ${error?.message ?? error}`);
  process.exitCode = 1;
});
