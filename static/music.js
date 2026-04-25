const CACHE_NAME = 'cloud-music-cache-v1';
let allSongs = [];
let tagsMap = {};       
let categories = [];    
let currentCategory = '全部';
let currentFilteredSongs = []; 
let cachedSongUrls = new Set();

const MAX_HISTORY = 9; 
let playHistory = [];
let historyIndex = -1;
let currentPlaying = '';

// 将原来的 isLocalMode 替换为完全的 isOfflineMode，并做本地持久化
let isOfflineMode = localStorage.getItem('isOfflineMode') === 'true';
let currentBlobUrl = null;
let isShuffleMode = false;

const SCENARIOS = {
    "🏔️ 爬山": { 
        include: ["史诗", "氛围", "后摇", "纯音乐", "环境", "轻音乐"], 
        exclude: ["重金属", "硬核", "说唱", "电音", "DJ", "快节奏"] 
    },
    "🚴 骑车": { 
        include: ["流行", "轻快", "电音", "EDM", "节奏", "动感健身", "欢快"], 
        exclude: ["舒缓", "悲伤", "助眠", "夜晚", "emo"] 
    },
    "🏋️ 无氧": { 
        include: ["摇滚", "说唱", "重低音", "燃", "金属", "硬核", "动感健身", "高强度"], 
        exclude: ["轻音乐", "古典", "抒情", "民谣", "舒缓", "安静"] 
    }
};

const audio = document.getElementById('audioPlayer');
const plyrInstance = new Plyr('#audioPlayer', {
    controls: ['play', 'progress', 'current-time', 'duration', 'mute', 'volume'],
    keyboard: { focused: true, global: true },
    tooltips: { controls: false, seek: true }
});

let favoriteSongs = new Set();
let deletedSongs = new Set(); 
let playStats = {}; 

window.addEventListener('online', flushSyncQueue);

async function flushSyncQueue() {
    if (isOfflineMode) return; // 明确指定处于离线模式时不尝试同步
    let queue = JSON.parse(localStorage.getItem('playStatsQueue') || '[]');
    if (queue.length === 0) return;

    try {
        const res = await fetch('/api/play_stats/sync', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(queue)
        });

        if (res.ok) {
            localStorage.setItem('playStatsQueue', '[]'); 
            console.log(`📡 成功将 ${queue.length} 条播放记录同步至云端`);
        }
    } catch (e) {
        console.log("📡 网络异常，播放记录缓存保留");
    }
}

async function init() {
    await checkLocalCache();
    updateModeUI();

    let rawSongs = [], tagsData = {}, favData = [], delData = [], serverStats = {};

    try {
        // 若开启离线模式，直接短路网络请求，强制走 catch 载入本地缓存数据
        if (isOfflineMode) throw new Error("Offline Mode Active");

        const [songsRes, tagsRes, favRes, delRes, statsRes] = await Promise.all([
            fetch('/api/proxy/songs').then(r => r.json()), 
            fetch('/api/tags').then(r => r.json()).catch(()=>({})),
            fetch('/api/favorites').then(r => r.json()).catch(()=>([])),
            fetch('/api/deleted_songs').then(r => r.json()).catch(()=>([])),
            fetch('/api/play_stats').then(r => r.json()).catch(()=>({}))
        ]);
        
        rawSongs = songsRes;
        tagsData = tagsRes;
        favData = favRes;
        delData = delRes;
        serverStats = statsRes;

        // 在线加载成功时，顺手写入备用离线全量包
        localStorage.setItem('offline_meta_data', JSON.stringify({
            rawSongs, tagsData, favData, delData, serverStats
        }));
        
        flushSyncQueue();
    } catch (e) {
        console.log("📡 切换至本地离线数据引擎:", e.message);
        const cachedMeta = JSON.parse(localStorage.getItem('offline_meta_data') || '{}');
        rawSongs = cachedMeta.rawSongs || [];
        tagsData = cachedMeta.tagsData || {};
        favData = cachedMeta.favData || [];
        delData = cachedMeta.delData || [];
        serverStats = cachedMeta.serverStats || {};
        
        if (rawSongs.length === 0) {
            document.getElementById('songList').innerHTML = '<div style="text-align:center;color:#ef4444;margin-top:20px;">数据缺失：首次访问需连接网络初始化缓存</div>';
            return;
        }
    }

    tagsMap = {};
    for (const [song, info] of Object.entries(tagsData?.song_tags || {})) {
        tagsMap[song] = Array.isArray(info) ? info : (info.tags || []);
    }
    categories = tagsData?.categories || [];
    
    if (Array.isArray(favData)) {
        favData.forEach(item => { if(item.song_name) favoriteSongs.add(item.song_name); });
    }

    if (Array.isArray(delData)) {
        delData.forEach(item => { if(item.song_name) deletedSongs.add(item.song_name); });
    }

    if (Object.keys(serverStats).length > 0) {
        playStats = serverStats;
    }
    
    allSongs = rawSongs.filter(song => !deletedSongs.has(song));
    
    renderCategories();
    filterSongs();
    setupAudioEngine(); 

    if (currentFilteredSongs.length > 0) {
        generateNewPlay(currentFilteredSongs[0]);
    }
}

async function checkLocalCache() {
    const cache = await caches.open(CACHE_NAME);
    const requests = await cache.keys();
    cachedSongUrls = new Set(requests.map(req => new URL(req.url).pathname));
}

function updateModeUI() {
    const btn = document.getElementById('modeBtn');
    if (btn) {
        btn.innerHTML = isOfflineMode ? '📦 离线' : '☁️ 在线';
        if (isOfflineMode) btn.classList.add('active-local');
        else btn.classList.remove('active-local');
    }
}

function toggleMode() {
    isOfflineMode = !isOfflineMode;
    localStorage.setItem('isOfflineMode', isOfflineMode);
    updateModeUI();
    
    if (!isOfflineMode) {
        init(); // 恢复在线，重新获取最新的库状态
    } else {
        filterSongs();
    }
}

let isDrawerOpen = false;
function toggleDrawer() {
    isDrawerOpen = !isDrawerOpen;
    const appWrapper = document.getElementById('appWrapper');
    if (isDrawerOpen) appWrapper.classList.add('squeezed');
    else appWrapper.classList.remove('squeezed');
}

function renderCategories() {
    const bar = document.getElementById('categoryBar');
    const grid = document.getElementById('drawerGrid');
    bar.innerHTML = ''; grid.innerHTML = '';

    const categoryClicks = JSON.parse(localStorage.getItem('categoryClicks') || '{}');

    const allHtml = `<div class="cat-pill ${currentCategory === '全部' ? 'active' : ''}" data-cat="全部" onclick="handleCategoryClick('全部')">全部 <span style="font-size:0.85em;opacity:0.6;">${allSongs.length}</span></div>`;
    bar.innerHTML += allHtml; grid.innerHTML += allHtml;

    Object.keys(SCENARIOS).forEach(scene => {
        const isActive = currentCategory === scene ? 'active' : '';
        const html = `<div class="cat-pill ${isActive}" style="color: #60a5fa; border-color: rgba(96, 165, 250, 0.4);" data-cat="${scene}" onclick="handleCategoryClick('${scene}')">${scene}</div>`;
        bar.innerHTML += html; grid.innerHTML += html;
    });

    const categoryCounts = {};
    categories.forEach(cat => categoryCounts[cat] = 0);
    allSongs.forEach(song => {
        (tagsMap[song] || []).forEach(tag => {
            if (categoryCounts[tag] !== undefined) categoryCounts[tag]++;
        });
    });

    categories.sort((a, b) => {
        const clicksA = categoryClicks[a] || 0;
        const clicksB = categoryClicks[b] || 0;
        if (clicksB !== clicksA) return clicksB - clicksA; 
        return categoryCounts[b] - categoryCounts[a]; 
    });

    categories.forEach(cat => {
        const count = categoryCounts[cat] || 0;
        if (count === 0) return; 
        const isActive = currentCategory === cat ? 'active' : '';
        const html = `<div class="cat-pill ${isActive}" data-cat="${cat}" onclick="handleCategoryClick('${cat}')">${cat} <span style="font-size:0.85em;opacity:0.6;">${count}</span></div>`;
        bar.innerHTML += html; grid.innerHTML += html;
    });
}

function handleCategoryClick(cat) {
    currentCategory = cat;
    if (cat !== '全部' && !SCENARIOS[cat]) {
        const categoryClicks = JSON.parse(localStorage.getItem('categoryClicks') || '{}');
        categoryClicks[cat] = (categoryClicks[cat] || 0) + 1;
        localStorage.setItem('categoryClicks', JSON.stringify(categoryClicks));
    }

    document.querySelectorAll('.cat-pill').forEach(el => {
        if (el.getAttribute('data-cat') === cat) el.classList.add('active');
        else el.classList.remove('active');
    });
    
    filterSongs();
    if(isDrawerOpen) toggleDrawer();
    if (currentFilteredSongs.length > 0 && currentFilteredSongs[0] !== currentPlaying) {
        generateNewPlay(currentFilteredSongs[0]);
    }
}

function calculateSongWeight(songName) {
    const stats = playStats[songName];
    let weight = 10; 

    if (stats) {
        weight += (stats.accumulatedTime / 60) * 2;
        const recentSkips = stats.recentSkipCount || 0;
        if (recentSkips > 0) {
            weight = weight * Math.pow(0.3, recentSkips); 
        }
    }
    
    if (favoriteSongs.has(songName)) weight += 30; 
    return weight;
}

function filterSongs() {
    const keyword = document.getElementById('searchInput').value.toLowerCase();
    
    currentFilteredSongs = allSongs.filter(song => {
        const matchKeyword = song.toLowerCase().includes(keyword);
        const matchLocal = isOfflineMode ? cachedSongUrls.has(`/stream/${encodeURIComponent(song)}`) : true;
        
        let matchCategory = false;
        if (currentCategory === '全部') {
            matchCategory = true;
        } else if (SCENARIOS[currentCategory]) {
            const songTags = tagsMap[song] || [];
            const config = SCENARIOS[currentCategory];
            const hasInclude = config.include.some(t => songTags.includes(t));
            const hasExclude = config.exclude.some(t => songTags.includes(t));
            matchCategory = hasInclude && !hasExclude;
        } else {
            matchCategory = (tagsMap[song] || []).includes(currentCategory);
        }
        
        return matchKeyword && matchCategory && matchLocal;
    });

    currentFilteredSongs.sort((a, b) => calculateSongWeight(b) - calculateSongWeight(a));
    renderList();
}

function toggleShuffle() {
    isShuffleMode = !isShuffleMode;
    const btn = document.getElementById('shuffleBtn');
    btn.innerText = isShuffleMode ? '🔀' : '🔁';
    btn.style.opacity = isShuffleMode ? '1' : '0.5';
    btn.style.color = isShuffleMode ? 'var(--accent)' : 'white';
}

function renderList() {
    const listEl = document.getElementById('songList');
    listEl.innerHTML = '';
    if (currentFilteredSongs.length === 0) {
        listEl.innerHTML = `<div style="text-align:center; color:var(--text-sub); margin-top: 20px;">无匹配歌曲</div>`;
        return;
    }

    currentFilteredSongs.forEach(song => {
        const item = document.createElement('div');
        item.className = `song-item ${song === currentPlaying ? 'playing' : ''}`;
        
        const tags = tagsMap[song] || [];
        let tagsHtml = tags.map(t => `<span class="song-tag">${t}</span>`).join('');
        if (cachedSongUrls.has(`/stream/${encodeURIComponent(song)}`)) {
            tagsHtml = `<span class="cached-badge">✓ 离线</span>` + tagsHtml;
        } else if (tags.length === 0) tagsHtml = `<span class="song-tag" style="opacity:0.5">未分类</span>`;

        const isFav = favoriteSongs.has(song);

        item.innerHTML = `
            <div class="song-row-content">
                <div class="song-main" onclick="generateNewPlay('${song.replace(/'/g, "\\'")}')">
                    <div class="song-name" style="color:${song === currentPlaying ? 'var(--accent)' : 'inherit'}">
                        ${song === currentPlaying ? (audio.paused ? '⏸ ' : '🎶 ') : ''}${song.replace('.mp3', '')}
                    </div>
                    <div class="song-tags">${tagsHtml}</div>
                </div>
                <div class="song-actions">
                    <button class="action-btn ${isFav ? 'fav-active' : ''}" onclick="toggleFav(event, '${song.replace(/'/g, "\\'")}')">
                        ${isFav ? '❤️' : '🤍'}
                    </button>
                    <button class="action-btn" onclick="editTags(event, '${song.replace(/'/g, "\\'")}')">🏷️</button>
                    <button class="action-btn" onclick="deleteSong(event, '${song.replace(/'/g, "\\'")}')">🗑️</button>
                </div>
            </div>
        `;
        listEl.appendChild(item);
    });
}

function evaluateCurrentSong() {
    if (!currentPlaying || !audio.duration) return;
    
    let playedTime = 0;
    for (let i = 0; i < audio.played.length; i++) {
        playedTime += (audio.played.end(i) - audio.played.start(i));
    }
    
    if (playedTime < 3) return; 

    const ratio = playedTime / audio.duration;
    let stats = playStats[currentPlaying] || { accumulatedTime: 0, recentSkipCount: 0, lastPlayedAt: 0 };
    
    stats.accumulatedTime += playedTime;
    stats.lastPlayedAt = Date.now();

    let isSkip = false;
    let isComplete = false;

    if (ratio < 0.15 && playedTime < 30) { 
        stats.recentSkipCount += 1; 
        isSkip = true;
    } else if (ratio >= 0.8) {
        stats.recentSkipCount = 0; 
        isComplete = true;
    } else if (ratio > 0.5) {
        stats.recentSkipCount = Math.max(0, stats.recentSkipCount - 1);
    }
    
    playStats[currentPlaying] = stats;

    let queue = JSON.parse(localStorage.getItem('playStatsQueue') || '[]');
    queue.push({
        song_name: currentPlaying,
        played_time: playedTime,
        is_skip: isSkip,
        is_complete: isComplete,
        timestamp: Date.now()
    });
    localStorage.setItem('playStatsQueue', JSON.stringify(queue));

    flushSyncQueue();
}

async function executePlay(song) {
    evaluateCurrentSong(); 

    currentPlaying = song;
    const cleanSongName = song.replace(/\.(mp3|flac|wav)$/i, ''); 
    document.getElementById('nowPlayingText').innerText = cleanSongName;
    const urlPath = `/stream/${encodeURIComponent(song)}`;

    if (currentBlobUrl) { URL.revokeObjectURL(currentBlobUrl); currentBlobUrl = null; }

    try {
        if (cachedSongUrls.has(urlPath)) {
            const cache = await caches.open(CACHE_NAME);
            const res = await cache.match(urlPath);
            if (res) {
                currentBlobUrl = URL.createObjectURL(await res.blob());
                audio.src = currentBlobUrl;
            } else {
                throw new Error("缓存未命中或受损");
            }
        } else {
            // 离线模式下强制要求只能播放已缓存内容
            if (isOfflineMode) throw new Error("离线模式截断非本地资源请求");
            audio.src = urlPath;
        }
        
        if ('mediaSession' in navigator) {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: cleanSongName,
                artist: '云端幻境',
                artwork: [{ src: '/static/music.svg', sizes: '512x512', type: 'image/svg+xml' }]
            });
        }

        await audio.play();
    } catch(err) {
        // 规避用户未交互（NotAllowedError），其余加载问题（断网/无缓存）自动静默跳过
        console.log("音频加载中断，跳跃至下一首:", err.message);
        if (err.name !== 'NotAllowedError') {
            setTimeout(playNext, 800);
        }
    }
    renderList(); 
}

function generateNewPlay(song) {
    if (historyIndex < playHistory.length - 1) {
        playHistory = playHistory.slice(0, historyIndex + 1);
    }
    playHistory.push(song);
    if (playHistory.length > MAX_HISTORY) playHistory.shift(); 
    historyIndex = playHistory.length - 1;
    executePlay(song);
}

function playPrev() {
    if (historyIndex > 0) {
        historyIndex--;
        executePlay(playHistory[historyIndex]);
    } else {
        if (currentFilteredSongs.length === 0 || !currentPlaying) return;
        let idx = currentFilteredSongs.indexOf(currentPlaying) - 1;
        generateNewPlay(currentFilteredSongs[idx < 0 ? currentFilteredSongs.length - 1 : idx]);
    }
}

function playNext() {
    if (historyIndex < playHistory.length - 1) {
        historyIndex++;
        executePlay(playHistory[historyIndex]);
        return;
    }

    if (currentFilteredSongs.length === 0 || !currentPlaying) return;
    let nextIndex;
    
    if (isShuffleMode && currentFilteredSongs.length > 1) {
        const candidates = currentFilteredSongs.filter(s => s !== currentPlaying);
        let totalWeight = 0;
        const weights = candidates.map(song => {
            const w = calculateSongWeight(song);
            totalWeight += w;
            return w;
        });

        let randomNum = Math.random() * totalWeight;
        let selectedSong = candidates[candidates.length - 1]; 
        for (let i = 0; i < candidates.length; i++) {
            if (randomNum < weights[i]) {
                selectedSong = candidates[i];
                break;
            }
            randomNum -= weights[i];
        }
        
        nextIndex = currentFilteredSongs.indexOf(selectedSong);
    } else {
        nextIndex = currentFilteredSongs.indexOf(currentPlaying) + 1;
        if (nextIndex >= currentFilteredSongs.length) nextIndex = 0;
    }
    
    generateNewPlay(currentFilteredSongs[nextIndex]);
}

async function toggleFav(event, songName) {
    event.stopPropagation(); 
    const btn = event.currentTarget;
    
    if (favoriteSongs.has(songName)) {
        favoriteSongs.delete(songName);
        btn.classList.remove('fav-active');
        btn.innerText = '🤍';
    } else {
        favoriteSongs.add(songName);
        btn.classList.add('fav-active');
        btn.innerText = '❤️';
    }
    filterSongs(); 
    
    // 静默阻断，防止控制台抛红
    if (!isOfflineMode) {
        fetch('/api/favorites', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ song_name: songName, action: favoriteSongs.has(songName) ? 'add' : 'remove' })
        }).catch(() => {});
    }
}

async function editTags(event, songName) {
    event.stopPropagation();
    const currentTags = tagsMap[songName] ? tagsMap[songName].join('，') : '';
    const input = prompt(`请输入《${songName}》的标签\n多个标签请用逗号分隔：`, currentTags);
    
    if (input !== null) {
        const newTags = input.replace(/,/g, '，').split('，').map(t => t.trim()).filter(t => t);
        
        // 先走前端渲染逻辑以保证不影响体验
        tagsMap[songName] = newTags;
        filterSongs(); 
        if(!categories.includes(...newTags)) {
            newTags.forEach(t => { if(!categories.includes(t)) categories.push(t); });
            renderCategories();
        }

        if (!isOfflineMode) {
            fetch('/api/tags', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ song_name: songName, tags: newTags })
            }).catch(() => console.warn("离线模式，标签修改将在刷新后丢失"));
        }
    }
}

async function deleteSong(event, songName) {
    event.stopPropagation(); 

    deletedSongs.add(songName);
    allSongs = allSongs.filter(s => s !== songName);
    
    renderCategories(); 
    filterSongs();
    
    if (!isOfflineMode) {
        fetch('/api/deleted_songs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ song_name: songName, action: 'delete' })
        }).catch(() => {});
    }
}

function setupAudioEngine() {
    audio.addEventListener('play', () => renderList());
    audio.addEventListener('pause', () => renderList());
    audio.addEventListener('ended', playNext);
    
    // 监听突发网络异常，导致未命中 Blob 的文件播放中断
    audio.addEventListener('error', () => {
        if (isOfflineMode || !navigator.onLine) {
            console.warn("网络连接断开导致流加载失败，跳过该曲");
            setTimeout(playNext, 1000);
        }
    });

    window.addEventListener('beforeunload', () => evaluateCurrentSong());

    if ('mediaSession' in navigator) {
        navigator.mediaSession.setActionHandler('play', () => audio.play());
        navigator.mediaSession.setActionHandler('pause', () => audio.pause());
        navigator.mediaSession.setActionHandler('previoustrack', () => playPrev());
        navigator.mediaSession.setActionHandler('nexttrack', () => playNext());
    }
}

window.onload = init;