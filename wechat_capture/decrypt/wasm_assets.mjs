/**
 * Single source of truth for the vendored WeChat WASM assets.
 *
 * Both the downloader (`fetch_wasm_assets.mjs`) and the decryptor
 * (`wechat_video_decrypt.mjs`) import this, so the names they write and read
 * can never drift apart again — a mismatch here silently produced a vendor
 * directory that "downloaded fine" but could not be loaded.
 *
 * Local names mirror the CDN names on purpose: searching for a file by the
 * name it has upstream is one less translation step when debugging.
 */

export const CDN_VERSION = '1.2.46';
export const CDN_BASE = `https://aladin.wxqcloud.qq.com/aladin/ffmepeg/video-decode/${CDN_VERSION}`;

/** `remote` is the CDN path segment, `local` is the file under vendor/.../raw. */
export const ASSETS = [
  { remote: 'wasm_video_decode.js', local: 'wasm_video_decode.js', required: true },
  { remote: 'wasm_video_decode.wasm', local: 'wasm_video_decode.wasm', required: true },
  { remote: 'worker_release.js', local: 'worker_release.js', required: false },
];

/** Emscripten glue the decryptor evaluates. */
export const GLUE_FILE = 'wasm_video_decode.js';

/** Compiled WASM module the glue instantiates. */
export const WASM_FILE = 'wasm_video_decode.wasm';

/** Every file `loadWasmModule()` needs before it can run. */
export const REQUIRED_FILES = ASSETS.filter((asset) => asset.required).map((asset) => asset.local);
