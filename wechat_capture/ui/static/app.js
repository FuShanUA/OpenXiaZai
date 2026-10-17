/**
 * Web UI for the WeChat Channels capture addon.
 *
 * Deliberately framework-free and build-free. Two rules keep it manageable:
 *   1. one state object
 *   2. one render() entry point — no scattered DOM mutation
 * The only exception is the download progress bar, which is patched in place
 * because it updates several times a second.
 */

// Must match UI_VERSION in api.py. A mismatch means the tab is running an old
// script against a newer backend (or the reverse), which silently produces
// wrong results — so it reloads instead.
const APP_VERSION = 17;

// ---------------------------------------------------------------- state
const state = {
  videos: [],
  lives: [],
  tasks: {},
  selected: null,
  selectedLive: null,
  session: null,
  counters: {},
  hook: null,
  events: [],
  message: null,
  scope: 'watched',
  tab: 'captures',
  playingVariant: null,
  playingLiveTask: null,
  qualityChoices: {},
  previewRequests: {},
  serviceBusy: false,
  liveInitialized: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const el = (id) => document.getElementById(id);
const tpl = (id) => document.getElementById(id).content.firstElementChild.cloneNode(true);

const refreshIcons = () => {
  if (window.lucide?.createIcons) window.lucide.createIcons();
};

refreshIcons();

const list = el('list');
const detail = el('detail');

// ---------------------------------------------------------------- helpers
function humanSize(bytes) {
  if (!bytes && bytes !== 0) return '未知';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = Number(bytes);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return unit === 0 ? `${size} B` : `${size.toFixed(1)} ${units[unit]}`;
}

function shortKey(key) {
  return key.length > 18 ? `${key.slice(0, 10)}…${key.slice(-6)}` : key;
}

/** Titles are captured when the API provides them; fall back to the file key. */
function displayTitle(video) {
  const title = (video.title || '').trim();
  if (title) return title;
  return `(未取到标题) ${shortKey(video.file_key)}`;
}

/**
 * Every list-returning Finder API drags in videos nobody asked for, and the
 * client also fully downloads the *next* video so autoplay is instant — which
 * looks identical on the wire to a video the user actually watched.
 *
 * "Watched" therefore keys off comment activity (see VIEWING_ENDPOINTS), not
 * bytes: a video on screen gets its comment panel fetched, a prebuffered one
 * does not.
 */
function visibleVideos() {
  if (state.tab === 'library') {
    return state.videos.filter((v) => v.downloaded.length > 0);
  }
  if (state.scope === 'all') return state.videos;
  if (state.scope === 'downloaded') {
    return state.videos.filter((v) => v.watched || v.client_downloaded);
  }
  return state.videos.filter((v) => v.watched);
}

const SOURCE_LABEL = { opened: '已打开', profile: '作者主页', flow: '推荐流', other: '其他' };

function shortAge(seconds) {
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  return `${Math.round(seconds / 3600)} 小时`;
}

function formatEta(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  return `${Math.round(seconds / 3600)} 小时`;
}

function progressText(task) {
  if (task.state === 'queued') return '排队中';
  if (task.state === 'decrypting') return '下载完成，正在解密';
  const eta = formatEta(task.eta_seconds);
  const percent = task.total ? `${task.percent ?? 0}%` : '等待文件大小';
  return [
    percent,
    `${humanSize(task.received)} / ${humanSize(task.total)}`,
    task.speed ? `${humanSize(task.speed)}/s` : '',
    eta ? `剩余 ${eta}` : '',
  ].filter(Boolean).join(' · ');
}

function formatTimestamp(seconds) {
  if (!seconds) return '';
  return new Date(seconds * 1000).toLocaleString();
}

function formatSpec(spec) {
  const bits = [];
  if (spec.height) bits.push(`${spec.height}p`);
  if (spec.video_bitrate) bits.push(`${spec.video_bitrate} kbps`);
  return bits.join(' · ') || spec.file_format || '?';
}

function videoById(fileKey) {
  return state.videos.find((v) => v.file_key === fileKey) || null;
}

function activeTask(video) {
  const tasks = (video.tasks || []).concat(
    Object.values(state.tasks).filter((t) => t.file_key === video.file_key),
  );
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const live = [...byId.values()].filter((t) => ['queued', 'downloading', 'decrypting'].includes(t.state));
  if (live.length) return live[live.length - 1];
  return [...byId.values()].pop() || null;
}

function liveById(liveId) {
  return state.lives.find((live) => live.live_id === liveId) || null;
}

function activeLiveTask(live) {
  return live?.task && ['queued', 'recording', 'stopping'].includes(live.task.state)
    ? live.task
    : null;
}

function liveStateLabel(task) {
  if (!task) return '待录制';
  if (task.state === 'queued') return '准备录制';
  if (task.state === 'recording') return '录制中';
  if (task.state === 'stopping') return '正在停止';
  if (task.state === 'done') return '已录制';
  return '录制失败';
}

function liveProgressText(task) {
  if (!task) return '';
  if (task.state === 'failed') return `录制失败:${task.error || '未知原因'}`;
  return [
    task.duration_human || '00:00',
    humanSize(task.received),
    task.speed_human || '',
  ].filter(Boolean).join(' · ');
}

/** Quality entries: what the backend probed, merged with what is on disk. */
function qualities(video) {
  const rows = [];
  const seen = new Set();
  const push = (quality, label) => {
    if (seen.has(quality)) return;
    seen.add(quality);
    const probe = (video.formats || []).find((f) => f.quality === quality) || {};
    const variant = (video.variants || []).find(
      (v) => v.variant === (quality === 'best' ? 'best' : quality),
    );
    rows.push({
      quality,
      label: label || probe.label || quality,
      bytes: probe.bytes || variant?.bytes || null,
      probed: probe.ok === true,
      error: probe.error,
      downloaded: Boolean(variant?.decoded),
      variant: variant?.variant,
    });
  };
  push('best', '原片');
  for (const spec of video.spec || []) {
    if (spec.file_format) push(spec.file_format, formatSpec(spec));
  }
  for (const variant of video.variants || []) {
    if (variant.variant !== 'client' && variant.variant !== 'best') push(variant.variant);
  }
  return rows;
}

function selectedQuality(video) {
  const checked = $('input[name="quality"]:checked', detail);
  if (checked) return checked.value;
  if (!video) return null;
  const preferred = state.qualityChoices[video.file_key] || 'best';
  const rows = qualities(video);
  return rows.find((row) => row.quality === preferred)?.quality
    || rows.find((row) => row.quality === 'best')?.quality
    || rows[0]?.quality
    || null;
}

// ---------------------------------------------------------------- render
function render() {
  renderTabs();
  renderStatus();
  renderBanner();
  renderList();
  renderDetail();
  renderEvents();
}

function renderTabs() {
  el('tab-captures').textContent = state.videos.filter((v) => v.watched).length || '';
  el('tab-live').textContent = state.lives.length || '';
  el('tab-library').textContent = state.videos.filter((v) => v.downloaded.length).length || '';
  for (const button of el('tabs').querySelectorAll('button')) {
    button.classList.toggle('active', button.dataset.tab === state.tab);
  }
}

function renderStatus() {
  const serviceEnabled = state.session?.capture_enabled !== false;
  const hook = state.hook || {};
  const bridgeActive = Boolean(hook.wrapped || state.counters['report:bridge_request']);
  const injected = Boolean(
    state.counters.html_injected
    || state.counters.js_hook_injected
    || bridgeActive,
  );
  const dots = [
    ['代理', serviceEnabled && state.proxyOk],
    ['注入', injected],
    ['桥', bridgeActive],
    ['上报', state.counters['report:media'] || hook.reports],
  ];
  el('dots').replaceChildren(...dots.map(([label, on]) => {
    const span = document.createElement('span');
    span.className = `dot ${on ? 'on' : 'off'}`;
    span.textContent = label;
    span.title = on ? '正常' : '尚无信号';
    return span;
  }));

  const session = state.session || {};
  const backend = state.session?.ui_version;
  el('subtitle').textContent = !serviceEnabled
    ? '捕获已关闭'
    : session.video_dir
    ? `已捕获 ${state.videos.length} 个视频`
    : '连接中';
  el('version-state').lastElementChild.textContent = `UI v${APP_VERSION} / 后端 v${backend ?? '?'}`;
  const captureState = el('capture-state');
  captureState.className = `state-chip ${serviceEnabled && state.proxyOk ? '' : 'muted'}`;
  captureState.lastElementChild.textContent = serviceEnabled
    ? (state.proxyOk ? '捕获服务运行中' : '等待捕获')
    : '捕获已关闭';

  const serviceToggle = el('service-toggle');
  const serviceLabel = el('service-toggle-label');
  serviceToggle.disabled = state.serviceBusy;
  serviceToggle.setAttribute('aria-pressed', String(serviceEnabled));
  serviceToggle.classList.toggle('off', !serviceEnabled);
  serviceLabel.textContent = state.serviceBusy
    ? '处理中…'
    : (serviceEnabled ? '关闭捕获' : '开启捕获');
  serviceToggle.title = serviceEnabled
    ? '关闭后恢复系统代理，不再捕获新视频'
    : '开启系统代理并恢复捕获';
  renderMetrics();
}

function renderMetrics() {
  const active = Object.values(state.tasks).filter(
    (task) => ['queued', 'downloading', 'decrypting'].includes(task.state),
  ).length + state.lives.filter((live) => activeLiveTask(live)).length;
  el('metric-captures').textContent = state.videos.length;
  el('metric-available').textContent = state.videos.filter(
    (video) => !video.link_expired && !video.downloaded.length,
  ).length;
  el('metric-downloads').textContent = state.videos.filter(
    (video) => video.downloaded.length > 0,
  ).length;
  el('metric-active').textContent = active;
}

function renderBanner() {
  const banner = el('banner');
  if (state.message) {
    banner.hidden = false;
    banner.className = `banner ${state.message.level || 'warn'}`;
    banner.textContent = state.message.text;
    return;
  }
  const expired = state.videos.filter((v) => v.link_expired && !v.downloaded.length);
  if (!expired.length) {
    banner.hidden = true;
    return;
  }
  const names = expired.slice(0, 3).map((v) => shortKey(v.file_key)).join('、');
  banner.hidden = false;
  banner.className = 'banner warn';
  banner.textContent =
    `${names}${expired.length > 3 ? ` 等 ${expired.length} 个` : ''} 的下载链接已失效。` +
    '请回微信里重新播放该视频以获取新链接。';
}

function renderList() {
  if (state.tab === 'live') {
    renderLiveList();
    return;
  }
  const rows = visibleVideos();
  el('scope').hidden = false;
  el('empty').hidden = rows.length > 0;
  el('count').textContent = state.videos.length
    ? `(${rows.length}/${state.videos.length})`
    : '';

  const library = state.tab === 'library';
  el('pane-title').innerHTML = library
    ? '下载完成 <span class="muted" id="count"></span>'
    : '捕获队列 <span class="muted" id="count"></span>';
  el('count').textContent = library
    ? (rows.length ? `(${rows.length})` : '')
    : (state.videos.length ? `(${rows.length}/${state.videos.length})` : '');

  const hidden = library ? 0 : state.videos.length - rows.length;
  const note = el('hidden-note');
  note.hidden = hidden <= 0;
  note.textContent = hidden > 0
    ? `已隐藏 ${hidden} 条未观看、客户端自行预取的视频(切到「全部」可见)`
    : '';

  list.replaceChildren(...rows.map((video) => {
    const node = tpl('tpl-row');
    node.dataset.key = video.file_key;
    if (video.file_key === state.selected) node.classList.add('active');

    const title = $('.row-title', node);
    title.textContent = displayTitle(video);
    title.title = video.title || video.file_key;

    const sub = $('.row-sub', node);
    sub.textContent = video.author ? `${video.author}` : shortKey(video.file_key);

    const task = activeTask(video);
    const progress = $('.row-progress', node);
    const progressBar = $('span', progress);
    if (task && ['queued', 'downloading', 'decrypting'].includes(task.state)) {
      progress.hidden = false;
      progressBar.style.width = `${task.state === 'decrypting' ? 100 : task.percent ?? 0}%`;
    } else {
      progress.hidden = true;
    }

    const badge = $('.row-badge', node);
    if (task && task.state === 'downloading') {
      badge.textContent = `下载中 ${task.percent ?? 0}%`;
      badge.classList.add('busy');
    } else if (video.downloaded.length) {
      badge.textContent = `已下载 ${video.downloaded.slice(0, 2).join(' / ')}`;
      badge.classList.add('ok');
    } else if (video.link_expired) {
      badge.textContent = '链接失效';
      badge.classList.add('bad');
    } else if (!video.watched && video.client_downloaded) {
      badge.textContent = '自动播放';
      badge.classList.add('muted');
    } else if (!video.played) {
      badge.textContent = '预取';
      badge.classList.add('muted');
    } else {
      badge.textContent = '待下载';
    }

    const meta = [];
    if (video.height) meta.push(`${video.width}×${video.height}`);
    if (video.client_format) meta.push(`客户端播 ${video.client_format}`);
    meta.push(SOURCE_LABEL[video.source_kind] || '其他');
    meta.push(`链接 ${shortAge(video.link_age_seconds)}前`);
    $('.row-meta', node).textContent = meta.join(' · ');
    return node;
  }));
  refreshIcons();
}

function renderLiveList() {
  const rows = state.lives;
  el('empty').hidden = rows.length > 0;
  el('scope').hidden = true;
  el('pane-title').innerHTML = '直播录制 <span class="muted" id="count"></span>';
  el('count').textContent = rows.length ? `(${rows.length})` : '';
  el('hidden-note').hidden = true;

  list.replaceChildren(...rows.map((live) => {
    const node = tpl('tpl-row');
    node.dataset.key = live.live_id;
    if (live.live_id === state.selectedLive) node.classList.add('active');
    $('.row-media i', node)?.setAttribute('data-lucide', 'radio');

    const title = $('.row-title', node);
    title.textContent = live.title || live.author || `直播 ${shortKey(live.live_id)}`;
    title.title = live.title || live.live_id;

    const sub = $('.row-sub', node);
    sub.textContent = live.author || shortKey(live.live_id);

    const task = live.task;
    const progress = $('.row-progress', node);
    const progressBar = $('span', progress);
    if (task && ['recording', 'stopping'].includes(task.state)) {
      progress.hidden = false;
      progressBar.style.width = '100%';
    } else {
      progress.hidden = true;
    }

    const badge = $('.row-badge', node);
    badge.textContent = live.is_current && !task ? '当前直播' : liveStateLabel(task);
    badge.className = `row-badge badge ${task?.state === 'failed' ? 'bad' : task?.state === 'done' ? 'ok' : task ? 'busy' : ''}`;

    const meta = [
      live.is_current ? '当前直播' : '',
      `开播 ${formatTimestamp(live.start_time) || '未知'}`,
      `捕获 ${formatTimestamp(live.captured_at) || '刚刚'}`,
    ].filter(Boolean);
    $('.row-meta', node).textContent = meta.filter(Boolean).join(' · ');
    return node;
  }));
  refreshIcons();
}

function renderDetail() {
  if (state.tab === 'live') {
    renderLiveDetail();
    return;
  }
  const video = videoById(state.selected);
  if (!video) {
    detail.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.innerHTML = `
      <i data-lucide="mouse-pointer-click"></i>
      <strong>选择一个视频</strong>
      <span>左侧列表会显示最近捕获的视频号内容</span>
    `;
    detail.append(empty);
    refreshIcons();
    return;
  }

  if (state.tab === 'library') {
    renderLibraryDetail(video);
    return;
  }

  const node = tpl('tpl-detail');
  for (const heading of node.querySelectorAll('.d-title')) {
    heading.textContent = displayTitle(video);
    heading.title = video.title || video.file_key;
  }
  const origin = SOURCE_LABEL[video.source_kind] || '其他';
  const stageAuthor = $('.d-author', node);
  stageAuthor.textContent = [
    video.author || shortKey(video.file_key),
    origin,
    video.played ? '' : '未播放(预取)',
  ].filter(Boolean).join(' · ');
  const keyline = $('.d-keyline', node);
  keyline.textContent = `file_key ${video.file_key}`;
  keyline.title = video.file_key;

  const badge = $('.d-badge', node);
  badge.textContent = video.downloaded.length ? `已下载 ${video.downloaded.join(' / ')}` : '待下载';
  badge.classList.add(video.downloaded.length ? 'ok' : 'muted');

  // --- video area: never show an empty player
  const videoEl = $('.d-video', node);
  const noVideo = $('.stage-empty', node);
  const variants = downloadedVariants(video);
  const playable = variants.find((v) => v.variant === state.playingVariant) || variants[0];
  if (playable) {
    videoEl.hidden = false;
    noVideo.hidden = true;
    videoEl.src = `/api/videos/${encodeURIComponent(video.file_key)}/stream?variant=${encodeURIComponent(playable.variant)}`;
  } else {
    videoEl.hidden = true;
    noVideo.hidden = false;
    videoEl.removeAttribute('src');

    const playButton = $('.stage-play', node);
    const playLabel = $('.stage-play span', node);
    const note = $('.stage-note', node);
    const stageProgress = $('.stage-progress', node);
    const stageBar = $('span', stageProgress);
    const previewQuality = state.previewRequests[video.file_key];
    const previewTask = previewQuality ? activeTask(video) : null;
    if (previewTask && ['queued', 'downloading', 'decrypting'].includes(previewTask.state)) {
      playButton.disabled = true;
      playLabel.textContent = '准备播放';
      note.textContent = previewTask.state === 'decrypting'
        ? '下载完成，正在解密'
        : `下载中 ${previewTask.total ? `${previewTask.percent ?? 0}%` : '…'} · ${humanSize(previewTask.received)} / ${humanSize(previewTask.total)}`;
      stageProgress.hidden = false;
      stageBar.style.width = `${previewTask.state === 'decrypting' ? 100 : (previewTask.total ? previewTask.percent ?? 0 : 100)}%`;
    } else {
      playButton.disabled = Boolean(video.link_expired);
      playLabel.textContent = video.link_expired ? '链接已失效' : '点击播放';
      note.textContent = video.link_expired
        ? '回微信重新播放后可再次捕获'
        : '将下载并解密当前选择的画质';
      stageProgress.hidden = true;
      stageBar.style.width = '0%';
    }
  }

  // --- progress
  const task = activeTask(video);
  const wrap = $('.progress-wrap', node);
  const bar = $('.d-progress', node);
  const text = $('.d-progress-text', node);
  const stateLabel = $('.progress-state', node);
  if (task && ['queued', 'downloading', 'decrypting'].includes(task.state)) {
    wrap.hidden = false;
    wrap.classList.remove('failed');
    wrap.classList.toggle('unknown', task.state !== 'decrypting' && !task.total);
    bar.style.width = `${task.state === 'decrypting' ? 100 : (task.total ? task.percent ?? 0 : 100)}%`;
    stateLabel.textContent = task.state === 'decrypting' ? '解密中' : '下载中';
    text.textContent = progressText(task);
  } else if (task && task.state === 'failed') {
    wrap.hidden = false;
    wrap.classList.add('failed');
    wrap.classList.remove('unknown');
    bar.style.width = '0%';
    stateLabel.textContent = '失败';
    text.textContent = `下载失败:${task.error || '未知原因'}`;
  }

  // --- quality list
  const formats = $('.d-formats', node);
  const rows = qualities(video);
  const chosen = rows.find((r) => r.quality === (video.client_format || ''))?.quality;
  const preferred = state.qualityChoices[video.file_key] || 'best';
  for (const row of rows) {
    const item = tpl('tpl-format');
    const input = $('input', item);
    input.value = row.quality;
    input.checked = row.quality === preferred;
    $('.fmt-label', item).textContent = row.label;
    $('.fmt-size', item).textContent = row.bytes
      ? humanSize(row.bytes)
      : (row.error ? '链接失效' : '探测中…');
    const tags = $('.fmt-tags', item);
    if (row.downloaded) tags.append(makeTag('已有', 'ok'));
    if (row.quality === chosen) tags.append(makeTag('客户端正在播', 'muted'));
    formats.append(item);
  }
  if (!rows.length) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = '还没有探测到可用档位。';
    formats.append(p);
  }

  // --- hint about link freshness
  const hint = $('.d-hint', node);
  if (video.link_expired) {
    hint.textContent =
      `下载链接已于 ${shortAge(video.link_age_seconds - video.link_ttl_seconds)}前失效。` +
      '请回微信里重新播放这个视频,然后回到本页。';
    $('.d-download', node).disabled = true;
  } else {
    hint.textContent = `链接签发于 ${shortAge(video.link_age_seconds)}前,约 ${video.link_ttl_seconds / 60} 分钟后失效。`;
  }

  // --- meta footer
  const meta = $('.d-meta', node);
  const lines = [
    `decodeKey ${video.decodeKeys.join(', ') || '—'}`,
    `来源 ${video.source || '—'}`,
    `签名档位 ${video.client_format || '—'}`,
    video.watched
      ? `判定为观看过:${video.watch_evidence || '(未知接口)'}`
      : '未判定为观看过(无评论接口触达)',
  ];
  if (video.fileSize) lines.push(`原片大小 ${humanSize(video.fileSize)}`);
  for (const line of lines) {
    const div = document.createElement('div');
    div.className = 'muted';
    div.textContent = line;
    meta.append(div);
  }

  detail.replaceChildren(node);
  refreshIcons();
}

function renderLiveDetail() {
  const live = liveById(state.selectedLive);
  if (!live) {
    detail.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.innerHTML = `
      <i data-lucide="radio"></i>
      <strong>选择一场直播</strong>
      <span>在微信里打开视频号直播后等待捕获</span>
    `;
    detail.append(empty);
    refreshIcons();
    return;
  }

  const node = tpl('tpl-live-detail');
  const task = live.task;
  for (const heading of node.querySelectorAll('.d-title')) {
    heading.textContent = live.title || live.author || `直播 ${shortKey(live.live_id)}`;
    heading.title = live.title || live.live_id;
  }
  $('.d-author', node).textContent = live.author || shortKey(live.live_id);

  const badge = $('.d-badge', node);
  badge.textContent = live.is_current && !task ? '当前直播' : liveStateLabel(task);
  badge.className = `d-badge badge ${task?.state === 'failed' ? 'bad' : task?.state === 'done' ? 'ok' : task ? 'busy' : ''}`;

  const videoEl = $('.d-video', node);
  const stageEmpty = $('.stage-empty', node);
  const stateLabel = $('.live-state-label', node);
  const stageNote = $('.stage-note', node);
  const stageProgress = $('.stage-progress', node);
  const stageBar = $('span', stageProgress);
  if (live.is_current && live.url) {
    videoEl.hidden = false;
    stageEmpty.hidden = true;
    videoEl.src = `/api/lives/${encodeURIComponent(live.live_id)}/preview/index.m3u8`;
    videoEl.autoplay = true;
    videoEl.muted = true;
    videoEl.addEventListener('canplay', () => {
      videoEl.play().catch(() => {});
    });
    videoEl.load();
  } else if (task?.state === 'done' && task.output_exists) {
    videoEl.hidden = false;
    stageEmpty.hidden = true;
    videoEl.src = `/api/live-recordings/${encodeURIComponent(task.id)}/stream`;
    videoEl.autoplay = false;
    videoEl.muted = false;
  } else {
    videoEl.hidden = true;
    videoEl.removeAttribute('src');
    videoEl.autoplay = false;
    videoEl.muted = false;
    stageEmpty.hidden = false;
    stateLabel.textContent = liveStateLabel(task);
    stageNote.textContent = task?.state === 'failed'
      ? (task.error || '录制失败')
      : (task?.state === 'recording' ? liveProgressText(task) : '录制完成后在这里播放');
    stageProgress.hidden = !task || !['queued', 'recording', 'stopping'].includes(task.state);
    stageBar.style.width = '100%';
  }

  const wrap = $('.live-progress', node);
  const bar = $('.d-progress', node);
  const text = $('.d-progress-text', node);
  const progressState = $('.progress-state', node);
  if (task && ['queued', 'recording', 'stopping'].includes(task.state)) {
    wrap.hidden = false;
    wrap.classList.remove('failed');
    wrap.classList.add('unknown');
    bar.style.width = '100%';
    progressState.textContent = liveStateLabel(task);
    text.textContent = liveProgressText(task);
  } else if (task?.state === 'failed') {
    wrap.hidden = false;
    wrap.classList.add('failed');
    wrap.classList.remove('unknown');
    bar.style.width = '0%';
    progressState.textContent = '失败';
    text.textContent = `录制失败:${task.error || '未知原因'}`;
  } else {
    wrap.hidden = true;
  }

  const ffmpegReady = state.session?.ffmpeg_available !== false;
  const recordButton = $('.live-record', node);
  const stopButton = $('.live-stop', node);
  const openButton = $('.live-open', node);
  const revealButton = $('.live-reveal', node);
  recordButton.hidden = Boolean(activeLiveTask(live));
  stopButton.hidden = !activeLiveTask(live);
  openButton.hidden = !(task?.state === 'done' && task.output_exists);
  revealButton.disabled = !(task?.state === 'done' && task.output_exists);
  recordButton.disabled = !ffmpegReady || !live.url;

  const path = $('.finder-path', node);
  path.textContent = task?.output_exists ? task.output : state.session?.video_dir || '';
  path.title = path.textContent;

  const hint = $('.d-hint', node);
  hint.textContent = !ffmpegReady
    ? '未找到 ffmpeg，请先执行 brew install ffmpeg'
    : (!live.url ? '直播地址已过期，回微信重新打开直播后可录制' : (activeLiveTask(live) ? '停止后会自动生成 MP4' : '录制会保存当前直播画面和声音'));

  const meta = $('.live-meta', node);
  const lines = [
    `live_id ${live.live_id}`,
    `来源 ${live.source || '—'}`,
    `开播 ${formatTimestamp(live.start_time) || '未知'}`,
    task?.state === 'done' ? `时长 ${task.duration_human}` : '',
    task?.state === 'done' ? `大小 ${humanSize(task.received)}` : '',
  ].filter(Boolean);
  for (const line of lines) {
    const div = document.createElement('div');
    div.className = 'muted';
    div.textContent = line;
    meta.append(div);
  }

  detail.replaceChildren(node);
  refreshIcons();
  if (live.is_current && live.url) {
    videoEl.play().catch(() => {});
  }
}

function downloadedVariants(video) {
  return (video.variants || [])
    .filter((variant) => variant.decoded && variant.path)
    .sort((a, b) => qualityRank(b.variant) - qualityRank(a.variant));
}

function qualityRank(variant) {
  if (variant === 'best') return Number.MAX_SAFE_INTEGER;
  if (variant === 'client') return Number.MAX_SAFE_INTEGER - 1;
  const height = Number.parseInt(String(variant).replace(/\D/g, ''), 10);
  return Number.isFinite(height) ? height : 0;
}

function renderLibraryDetail(video) {
  const node = tpl('tpl-library-detail');
  for (const heading of node.querySelectorAll('.d-title')) {
    heading.textContent = displayTitle(video);
    heading.title = video.title || video.file_key;
  }
  $('.d-author', node).textContent = video.author || shortKey(video.file_key);

  const variants = downloadedVariants(video);
  const active = variants.find((v) => v.variant === state.playingVariant) || variants[0];
  const videoEl = $('.d-video', node);
  const noVideo = $('.stage-empty', node);
  if (active) {
    videoEl.hidden = false;
    noVideo.hidden = true;
    videoEl.src = `/api/videos/${encodeURIComponent(video.file_key)}/stream?variant=${encodeURIComponent(active.variant)}`;
  } else {
    videoEl.hidden = true;
    noVideo.hidden = false;
    videoEl.removeAttribute('src');
  }

  const directory = active?.directory || state.session?.video_dir || '';
  const path = $('.finder-path', node);
  path.textContent = directory;
  path.title = directory;

  const list = $('.finder-list', node);
  for (const variant of variants) {
    const item = tpl('tpl-finder-file');
    item.dataset.variant = variant.variant;
    item.classList.toggle('active', variant.variant === active?.variant);
    $('.finder-name', item).textContent = variant.filename || `${video.file_key}.${variant.variant}.mp4`;
    $('.finder-name', item).title = variant.path || '';
    $('.finder-meta', item).textContent = [
      humanSize(variant.size ?? variant.bytes),
      formatTimestamp(variant.modified),
      variant.variant === 'best' ? '最佳分辨率' : variant.variant,
    ].filter(Boolean).join(' · ');
    list.append(item);
  }

  detail.replaceChildren(node);
  refreshIcons();
}

function makeTag(text, kind) {
  const span = document.createElement('span');
  span.className = `tag ${kind}`;
  span.textContent = text;
  return span;
}

function renderEvents() {
  el('event-count').textContent = state.events.length ? `(${state.events.length})` : '';
  const box = el('events');
  box.replaceChildren(...state.events.slice(-60).reverse().map((event) => {
    const line = document.createElement('div');
    line.className = 'event-line';
    const stamp = new Date(event.at * 1000).toLocaleTimeString();
    line.dataset.time = stamp;
    line.textContent = event.text;
    return line;
  }));
}

/** Patch the progress bar without a full re-render (it updates several times a second). */
function patchProgress(task) {
  const video = videoById(task.file_key);
  if (!video || task.file_key !== state.selected) return;
  const preview = state.previewRequests[task.file_key] === task.quality;
  const wrap = $('.progress-wrap', detail);
  const bar = $('.d-progress', detail);
  const text = $('.d-progress-text', detail);
  const stateLabel = $('.progress-state', detail);
  const stageButton = $('.stage-play', detail);
  const stageLabel = $('.stage-play span', detail);
  const stageNote = $('.stage-note', detail);
  const stageProgress = $('.stage-progress', detail);
  const stageBar = stageProgress ? $('span', stageProgress) : null;
  if (!wrap || !bar || !text || !stateLabel) return;
  const settled = task.state === 'done' || task.state === 'failed';
  if (settled) {
    if (task.state === 'failed') {
      wrap.hidden = false;
      wrap.classList.add('failed');
      wrap.classList.remove('unknown');
      bar.style.width = '0%';
      stateLabel.textContent = '失败';
      text.textContent = `下载失败:${task.error || '未知原因'}`;
      if (preview && stageButton && stageLabel && stageNote && stageProgress) {
        stageButton.disabled = false;
        stageLabel.textContent = '点击重试';
        stageNote.textContent = `预览失败:${task.error || '未知原因'}`;
        stageProgress.hidden = true;
      }
    } else {
      wrap.hidden = true;
      wrap.classList.remove('failed');
      wrap.classList.remove('unknown');
      if (preview && stageButton && stageLabel && stageNote && stageProgress && stageBar) {
        stageButton.disabled = true;
        stageLabel.textContent = '即将播放';
        stageNote.textContent = '正在载入视频';
        stageProgress.hidden = false;
        stageBar.style.width = '100%';
      }
    }
    return;
  }
  wrap.hidden = false;
  wrap.classList.remove('failed');
  wrap.classList.toggle('unknown', task.state !== 'decrypting' && !task.total);
  bar.style.width = `${task.state === 'decrypting' ? 100 : (task.total ? task.percent ?? 0 : 100)}%`;
  stateLabel.textContent = task.state === 'decrypting' ? '解密中' : '下载中';
  text.textContent = progressText(task);
  if (preview && stageButton && stageLabel && stageNote && stageProgress && stageBar) {
    stageButton.disabled = true;
    stageLabel.textContent = '准备播放';
    stageNote.textContent = task.state === 'decrypting'
      ? '下载完成，正在解密'
      : `下载中 ${task.total ? `${task.percent ?? 0}%` : '…'} · ${humanSize(task.received)} / ${humanSize(task.total)}`;
    stageProgress.hidden = false;
    stageBar.style.width = `${task.state === 'decrypting' ? 100 : (task.total ? task.percent ?? 0 : 100)}%`;
  }
}

function patchLiveProgress(task) {
  const live = liveById(task.live_id);
  if (!live || live.live_id !== state.selectedLive || state.tab !== 'live') return;
  const wrap = $('.live-progress', detail);
  const bar = $('.d-progress', detail);
  const text = $('.d-progress-text', detail);
  const stateLabel = $('.progress-state', detail);
  const stageLabel = $('.live-state-label', detail);
  const stageNote = $('.stage-note', detail);
  if (!wrap || !bar || !text || !stateLabel) return;
  if (task.state === 'failed') {
    wrap.hidden = false;
    wrap.classList.add('failed');
    wrap.classList.remove('unknown');
    bar.style.width = '0%';
    stateLabel.textContent = '失败';
    text.textContent = `录制失败:${task.error || '未知原因'}`;
    if (stageLabel && stageNote) {
      stageLabel.textContent = '录制失败';
      stageNote.textContent = task.error || '录制失败';
    }
    return;
  }
  if (task.state === 'done') {
    wrap.hidden = true;
    wrap.classList.remove('failed', 'unknown');
    if (stageLabel && stageNote) {
      stageLabel.textContent = '已录制';
      stageNote.textContent = '正在载入视频';
    }
    return;
  }
  wrap.hidden = false;
  wrap.classList.remove('failed');
  wrap.classList.add('unknown');
  bar.style.width = '100%';
  stateLabel.textContent = liveStateLabel(task);
  text.textContent = liveProgressText(task);
  if (stageLabel && stageNote) {
    stageLabel.textContent = liveStateLabel(task);
    stageNote.textContent = liveProgressText(task);
  }
}

// ---------------------------------------------------------------- api
async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

async function loadState() {
  const [statePayload, sessionStart] = await Promise.all([
    api('/api/state'),
    api('/api/log?tail=1').catch(() => ({ records: [] })),
  ]);
  state.session = statePayload.session;
  state.counters = statePayload.counters || {};
  state.proxyOk = statePayload.session?.proxy_configured === true;
  render();
}

async function loadVideos() {
  const payload = await api('/api/videos');
  state.videos = payload.videos || [];
  const stale = state.videos.length > 0 && !('watched' in state.videos[0]);
  if (stale) {
    state.message = {
      level: 'bad',
      text: '后端进程是旧版本(没有 watched 字段),过滤结果不可信。请重启 mitmdump 后刷新本页。',
    };
    renderBanner();
  }
  if (!state.selected || !videoById(state.selected)) {
    state.selected = state.videos[0]?.file_key || null;
  }
  render();
}

async function loadTasks() {
  const payload = await api('/api/tasks');
  for (const task of payload.tasks || []) state.tasks[task.id] = task;
  renderMetrics();
}

async function loadLives() {
  const payload = await api('/api/lives');
  const lives = payload.lives || [];
  const previousCurrent = state.lives.find((live) => live.is_current)?.live_id;
  const signature = JSON.stringify(lives.map((live) => [
    live.live_id,
    live.is_current,
    live.task?.id,
    live.task?.state,
  ]));
  const previousSignature = JSON.stringify(state.lives.map((live) => [
    live.live_id,
    live.is_current,
    live.task?.id,
    live.task?.state,
  ]));
  state.lives = lives;
  const current = state.lives.find((live) => live.is_current);
  const currentChanged = Boolean(current && current.live_id !== previousCurrent);
  if (current) {
    state.selectedLive = current.live_id;
  } else if (!state.selectedLive || !liveById(state.selectedLive)) {
    state.selectedLive = state.lives[0]?.live_id || null;
  }
  if ((currentChanged || !state.liveInitialized) && current) {
    state.tab = 'live';
  }
  state.liveInitialized = true;
  if (signature !== previousSignature) {
    render();
  }
}

async function recordLive(liveId) {
  try {
    const payload = await api(`/api/lives/${encodeURIComponent(liveId)}/record`, { method: 'POST' });
    const live = liveById(liveId);
    if (live) live.task = payload.task;
    pushEvent('开始直播录制');
    render();
  } catch (error) {
    state.message = { level: 'bad', text: `开始录制失败:${error.message}` };
    render();
  }
}

async function stopLive(liveId) {
  try {
    const payload = await api(`/api/lives/${encodeURIComponent(liveId)}/stop`, { method: 'POST' });
    const live = liveById(liveId);
    if (live) live.task = payload.task;
    pushEvent('停止直播录制');
    render();
  } catch (error) {
    state.message = { level: 'bad', text: `停止录制失败:${error.message}` };
    render();
  }
}

setInterval(() => {
  if (document.visibilityState === 'visible') {
    loadLives().catch(() => {});
  }
}, 2000);

async function download(fileKey, quality, { preview = false } = {}) {
  if (preview) state.previewRequests[fileKey] = quality;
  else delete state.previewRequests[fileKey];

  try {
    const payload = await api(`/api/videos/${encodeURIComponent(fileKey)}/download`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ quality }),
    });
    state.tasks[payload.task.id] = payload.task;
    pushEvent(preview ? `准备播放 ${quality}` : `开始下载 ${quality}`);
    renderMetrics();
    render();
    if (payload.task.state === 'done') {
      await loadVideos();
      const video = videoById(fileKey);
      if (preview && !downloadedVariants(video || {}).length) {
        delete state.previewRequests[fileKey];
        state.message = { level: 'bad', text: '预览失败:任务已完成但没有可播放文件' };
      }
      render();
    }
  } catch (error) {
    if (preview) delete state.previewRequests[fileKey];
    state.message = { level: 'bad', text: `下载失败:${error.message}` };
    render();
  }
}

async function probe(fileKey) {
  pushEvent('重新探测档位…');
  try {
    await api(`/api/videos/${encodeURIComponent(fileKey)}/probe`);
    await loadVideos();
  } catch (error) {
    state.message = { level: 'bad', text: `探测失败:${error.message}` };
    render();
  }
}

async function setService(enabled) {
  state.serviceBusy = true;
  renderStatus();
  try {
    const payload = await api('/api/service', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    state.session = {
      ...(state.session || {}),
      capture_enabled: payload.service.capture_enabled,
      proxy_configured: payload.service.proxy_configured,
    };
    state.proxyOk = payload.service.proxy_configured;
    state.message = {
      level: 'ok',
      text: payload.service.capture_enabled
        ? '捕获已开启，系统代理已指向本地服务'
        : '捕获已关闭，系统代理已恢复',
    };
    pushEvent(payload.service.capture_enabled ? '开启捕获' : '关闭捕获');
  } catch (error) {
    state.message = { level: 'bad', text: `切换捕获失败:${error.message}` };
  } finally {
    state.serviceBusy = false;
  }
  render();
}

function pushEvent(text) {
  state.events.push({ at: Date.now() / 1000, text });
  renderEvents();
}

// ---------------------------------------------------------------- events
list.addEventListener('click', (event) => {
  const row = event.target.closest('.row');
  if (!row) return;
  if (state.tab === 'live') {
    state.selectedLive = row.dataset.key;
    render();
    return;
  }
  if (state.selected && state.selected !== row.dataset.key) {
    delete state.previewRequests[state.selected];
  }
  state.selected = row.dataset.key;
  const video = videoById(state.selected);
  if (video && !(video.formats || []).length) probe(state.selected);
  render();
});

detail.addEventListener('click', (event) => {
  if (state.tab === 'live') {
    const live = liveById(state.selectedLive);
    if (!live) return;
    if (event.target.closest('.live-record')) recordLive(live.live_id);
    if (event.target.closest('.live-stop')) stopLive(live.live_id);
    if (event.target.closest('.live-reveal') && live.task?.output_exists) {
      api(`/api/live-recordings/${encodeURIComponent(live.task.id)}/reveal`)
        .catch((error) => pushEvent(`打开 Finder 失败:${error.message}`));
    }
    if (event.target.closest('.live-open') && live.task?.output_exists) {
      api(`/api/live-recordings/${encodeURIComponent(live.task.id)}/open`)
        .catch((error) => pushEvent(`打开视频失败:${error.message}`));
    }
    return;
  }
  const video = videoById(state.selected);
  if (!video) return;
  if (event.target.closest('.d-download')) {
    const quality = selectedQuality(video);
    if (quality) download(video.file_key, quality);
  }
  if (event.target.closest('.d-probe')) probe(video.file_key);
  if (event.target.closest('.stage-play')) {
    const quality = selectedQuality(video);
    const task = activeTask(video);
    if (quality && !(task && ['queued', 'downloading', 'decrypting'].includes(task.state))) {
      download(video.file_key, quality, { preview: true });
    } else if (quality) {
      state.previewRequests[video.file_key] = quality;
      render();
    }
  }
  if (event.target.closest('.fmt input')) {
    state.qualityChoices[video.file_key] = event.target.value;
  }
  if (event.target.closest('.finder-file')) {
    const file = event.target.closest('.finder-file');
    state.playingVariant = file.dataset.variant;
    render();
    const player = $('.d-video', detail);
    player?.play().catch(() => {});
  }
  if (event.target.closest('.d-reveal')) {
    const variant = state.playingVariant || downloadedVariants(video)[0]?.variant || '';
    api(`/api/videos/${encodeURIComponent(video.file_key)}/reveal?variant=${encodeURIComponent(variant)}`)
      .catch((error) => pushEvent(`打开 Finder 失败:${error.message}`));
  }
  if (event.target.closest('.d-open')) {
    const variant = state.playingVariant || downloadedVariants(video)[0]?.variant || '';
    api(`/api/videos/${encodeURIComponent(video.file_key)}/open?variant=${encodeURIComponent(variant)}`)
      .catch((error) => pushEvent(`打开视频失败:${error.message}`));
  }
});

el('selftest').addEventListener('click', async () => {
  pushEvent('运行解密器自检…');
  try {
    const payload = await api('/api/selftest', { method: 'POST' });
    state.message = {
      level: payload.ok ? 'ok' : 'bad',
      text: payload.ok ? '解密器自检通过' : `自检失败:${JSON.stringify(payload).slice(0, 200)}`,
    };
  } catch (error) {
    state.message = { level: 'bad', text: `自检失败:${error.message}` };
  }
  render();
});

el('service-toggle').addEventListener('click', () => {
  if (state.serviceBusy) return;
  setService(state.session?.capture_enabled === false);
});

el('banner').addEventListener('click', () => { state.message = null; renderBanner(); });

el('tabs').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-tab]');
  if (!button) return;
  if (state.tab === 'captures' && button.dataset.tab !== 'captures') {
    delete state.previewRequests[state.selected];
  }
  state.tab = button.dataset.tab;
  if (state.tab === 'live') {
    if (!state.selectedLive || !liveById(state.selectedLive)) {
      state.selectedLive = state.lives[0]?.live_id || null;
    }
    render();
    return;
  }
  if (!visibleVideos().some((v) => v.file_key === state.selected)) {
    state.selected = visibleVideos()[0]?.file_key || null;
  }
  render();
});

el('scope').addEventListener('change', (event) => {
  state.scope = event.target.value;
  if (state.selected && !visibleVideos().some((v) => v.file_key === state.selected)) {
    state.selected = visibleVideos()[0]?.file_key || null;
  }
  render();
});

// ---------------------------------------------------------------- realtime
function connect() {
  const source = new EventSource('/api/events');
  source.onmessage = (message) => {
    let payload;
    try { payload = JSON.parse(message.data); } catch { return; }
    switch (payload.type) {
      case 'videos_changed':
        loadVideos();
        break;
      case 'lives_changed':
        loadLives();
        break;
      case 'hook':
        state.hook = payload.hook;
        renderStatus();
        break;
      case 'download': {
        const task = payload.task;
        state.tasks[task.id] = task;
        patchProgress(task);
        if (task.state === 'done' || task.state === 'failed') {
          pushEvent(task.state === 'done' ? `下载完成 ${task.quality}` : `下载失败 ${task.quality}`);
          const preview = state.previewRequests[task.file_key] === task.quality;
          if (task.state === 'failed' && preview) {
            delete state.previewRequests[task.file_key];
          }
          if (task.state === 'done' && preview) {
            state.playingVariant = task.quality;
            loadVideos().then(() => {
              delete state.previewRequests[task.file_key];
              render();
              if (state.selected === task.file_key && state.tab === 'captures') {
                const player = $('.d-video', detail);
                player?.play().catch(() => {});
              }
            });
          } else if (task.state === 'done') {
            state.tab = 'library';
            state.selected = task.file_key;
            state.playingVariant = task.quality;
            loadVideos().then(() => {
              render();
              const player = $('.d-video', detail);
              player?.play().catch(() => {});
            });
          }
        }
        renderList();
        renderMetrics();
        break;
      }
      case 'live_recording': {
        const task = payload.task;
        const live = liveById(task.live_id);
        if (live) live.task = task;
        patchLiveProgress(task);
        if (task.state === 'done' || task.state === 'failed') {
          pushEvent(task.state === 'done' ? '直播录制完成' : '直播录制失败');
          loadLives().then(() => {
            if (task.state === 'done' && state.selectedLive === task.live_id && state.tab === 'live') {
              const player = $('.d-video', detail);
              player?.play().catch(() => {});
            }
          });
        }
        renderList();
        renderMetrics();
        break;
      }
      case 'hello':
        if (payload.version && payload.version !== APP_VERSION) {
          state.message = {
            level: 'warn',
            text: `页面脚本 v${APP_VERSION} 与后端 v${payload.version} 不一致,正在重新加载…`,
          };
          renderBanner();
          setTimeout(() => window.location.reload(), 800);
        }
        break;
      case 'session':
        state.session = { ...(state.session || {}), ...payload.session };
        renderStatus();
        break;
      case 'service':
        state.session = {
          ...(state.session || {}),
          capture_enabled: payload.service.capture_enabled,
          proxy_configured: payload.service.proxy_configured,
        };
        state.proxyOk = payload.service.proxy_configured;
        renderStatus();
        break;
      default:
        break;
    }
  };
  source.onerror = () => {
    el('subtitle').textContent = '与后端断开,重连中…';
  };
  source.onopen = () => { loadState(); loadVideos(); };
}

loadTasks()
  .then(loadState)
  .then(() => Promise.all([loadVideos(), loadLives()]))
  .then(connect)
  .catch((error) => {
  el('subtitle').textContent = `初始化失败:${error.message}`;
});
