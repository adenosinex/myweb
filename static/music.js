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
    
    let isLocalMode = false;
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
    
    // 全局统计字典
    let playStats = {}; 

    // ================= 新增：离线状态与队列同步机制 =================
    window.addEventListener('online', flushSyncQueue); // 一旦恢复网络，立刻尝试同步

    async function flushSyncQueue() {
        let queue = JSON.parse(localStorage.getItem('playStatsQueue') || '[]');
        if (queue.length === 0) return;

        try {
            const res = await fetch('/api/play_stats/sync', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(queue) // 将离线积累的所有听歌记录发给后端
            });

            if (res.ok) {
                localStorage.setItem('playStatsQueue', '[]'); // 同步成功，清空离线队列
                console.log(`📡 成功将 ${queue.length} 条播放记录同步至云端`);
            }
        } catch (e) {
            console.log("📡 处于离线模式，播放记录已缓存，待下次同步");
        }
    }
    // =============================================================

    async function init() {
        await checkLocalCache();
        try {
            // 并行拉取所有基础数据，包含后端的“权威统计记录”
            const [songsRes, tagsRes, favRes, delRes, statsRes] = await Promise.all([
                fetch('/api/proxy/songs'), 
                fetch('/api/tags').catch(()=>({json:()=>({})})),
                fetch('/api/favorites').catch(()=>({json:()=>([])})),
                fetch('/api/deleted_songs').catch(()=>({json:()=>([])})),
                fetch('/api/play_stats').catch(()=>({json:()=>({})})) // 获取后端统计
            ]);
            
            let rawSongs = await songsRes.json();
            const tagsData = await tagsRes.json();
            
            // ================= 核心修复：兼容新版后端返回的带统计信息的结构 =================
            tagsMap = {};
            for (const [song, info] of Object.entries(tagsData?.song_tags || {})) {
                // 如果是新版对象则取 .tags，旧版数组则直接使用
                tagsMap[song] = Array.isArray(info) ? info : (info.tags || []);
            }
            categories = tagsData?.categories || [];
            
            const favData = await favRes.json();
            if (Array.isArray(favData)) {
                favData.forEach(item => { if(item.song_name) favoriteSongs.add(item.song_name); });
            }

            const delData = await delRes.json();
            if (Array.isArray(delData)) {
                delData.forEach(item => { if(item.song_name) deletedSongs.add(item.song_name); });
            }

            // 载入后端云同步的播放统计，作为基准
            const serverStats = await statsRes.json();
            if (Object.keys(serverStats).length > 0) {
                playStats = serverStats;
            }
            
            allSongs = rawSongs.filter(song => !deletedSongs.has(song));
            
            renderCategories();
            filterSongs();
            setupAudioEngine(); 
            
            // 页面一打开，顺手尝试清一下上次断网遗留的队列
            flushSyncQueue();

            if (currentFilteredSongs.length > 0) {
                generateNewPlay(currentFilteredSongs[0]);
            }
        } catch (e) {
            console.error(e); // 在控制台打印真实错误，避免被掩盖
            document.getElementById('songList').innerHTML = '<div style="text-align:center;color:#ef4444;">网络或解析异常，无法获取数据</div>';
        }
    }

    async function checkLocalCache() {
        const cache = await caches.open(CACHE_NAME);
        const requests = await cache.keys();
        cachedSongUrls = new Set(requests.map(req => new URL(req.url).pathname));
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

    // ================= 核心：极度敏感的权重算法 =================
    function calculateSongWeight(songName) {
        const stats = playStats[songName];
        let weight = 10; // 基础分：所有歌就算没听过，至少给10分起步

        if (stats) {
            // 奖赏：累计听歌时长（秒）。每一分钟时长，增加 2 点基础权重（无上限）
            weight += (stats.accumulatedTime / 60) * 2;
            
            // 惩罚：近期敏感度暴跌
            const recentSkips = stats.recentSkipCount || 0;
            if (recentSkips > 0) {
                // 指数级惩罚：连切 1 次权重打 3 折；连切 2 次打 0.09 折；极速沉底
                weight = weight * Math.pow(0.3, recentSkips); 
            }
        }
        
        // 收藏自带保底光环
        if (favoriteSongs.has(songName)) weight += 30; 
        
        return weight;
    }

    function filterSongs() {
        const keyword = document.getElementById('searchInput').value.toLowerCase();
        
        currentFilteredSongs = allSongs.filter(song => {
            const matchKeyword = song.toLowerCase().includes(keyword);
            const matchLocal = isLocalMode ? cachedSongUrls.has(`/stream/${encodeURIComponent(song)}`) : true;
            
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

        // 每次点击分类或打开页面，严格按照听歌行为权重降序重排列表
        currentFilteredSongs.sort((a, b) => calculateSongWeight(b) - calculateSongWeight(a));
        renderList();
    }

    function toggleMode() {
        isLocalMode = !isLocalMode;
        document.getElementById('modeBtn').classList.toggle('active-local');
        filterSongs();
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

    // ================= 核心：精准追踪并分发至队列 =================
    function evaluateCurrentSong() {
        if (!currentPlaying || !audio.duration) return;
        
        let playedTime = 0;
        // 使用底层的 played 属性，无论怎么拖拽进度条，只算耳朵真实听到的秒数
        for (let i = 0; i < audio.played.length; i++) {
            playedTime += (audio.played.end(i) - audio.played.start(i));
        }
        
        if (playedTime < 3) return; // 播放少于3秒视为失误，不计入统计

        const ratio = playedTime / audio.duration;
        let stats = playStats[currentPlaying] || { accumulatedTime: 0, recentSkipCount: 0, lastPlayedAt: 0 };
        
        stats.accumulatedTime += playedTime;
        stats.lastPlayedAt = Date.now();

        let isSkip = false;
        let isComplete = false;

        if (ratio < 0.15 && playedTime < 30) { 
            // 极其讨厌：没到高潮就立刻切歌
            stats.recentSkipCount += 1; 
            isSkip = true;
        } else if (ratio >= 0.8) {
            // 完整听完（超80%）：原谅一切过去的秒切历史，彻底洗白！
            stats.recentSkipCount = 0; 
            isComplete = true;
        } else if (ratio > 0.5) {
            // 听到一半以上：酌情减轻一次惩罚
            stats.recentSkipCount = Math.max(0, stats.recentSkipCount - 1);
        }
        
        // 1. 更新前端展示基准
        playStats[currentPlaying] = stats;

        // 2. 将本次动作打包推入“待同步队列”
        let queue = JSON.parse(localStorage.getItem('playStatsQueue') || '[]');
        queue.push({
            song_name: currentPlaying,
            played_time: playedTime,
            is_skip: isSkip,
            is_complete: isComplete,
            timestamp: Date.now()
        });
        localStorage.setItem('playStatsQueue', JSON.stringify(queue));

        // 3. 触发发送指令
        flushSyncQueue();
    }

    async function executePlay(song) {
        // 先结算上一首
        evaluateCurrentSong(); 

        currentPlaying = song;
        const cleanSongName = song.replace(/\.(mp3|flac|wav)$/i, ''); 
        document.getElementById('nowPlayingText').innerText = cleanSongName;
        const urlPath = `/stream/${encodeURIComponent(song)}`;

        if (currentBlobUrl) { URL.revokeObjectURL(currentBlobUrl); currentBlobUrl = null; }

        if (cachedSongUrls.has(urlPath)) {
            const cache = await caches.open(CACHE_NAME);
            const res = await cache.match(urlPath);
            if (res) {
                currentBlobUrl = URL.createObjectURL(await res.blob());
                audio.src = currentBlobUrl;
            }
        } else {
            audio.src = urlPath;
        }
        
        if ('mediaSession' in navigator) {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: cleanSongName,
                artist: '云端幻境',
                artwork: [{ src: '/static/music.svg', sizes: '512x512', type: 'image/svg+xml' }]
            });
        }

        try {
            await audio.play();
        } catch(err) {
            console.log("等待用户首次交互", err);
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
            fetch('/api/favorites', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ song_name: songName, action: 'add' })
            });
        }
        filterSongs(); 
    }

    async function editTags(event, songName) {
        event.stopPropagation();
        const currentTags = tagsMap[songName] ? tagsMap[songName].join('，') : '';
        const input = prompt(`请输入《${songName}》的标签\n多个标签请用逗号分隔：`, currentTags);
        
        if (input !== null) {
            const newTags = input.replace(/,/g, '，').split('，').map(t => t.trim()).filter(t => t);
            try {
                const res = await fetch('/api/tags', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ song_name: songName, tags: newTags })
                });
                if (res.ok) {
                    tagsMap[songName] = newTags;
                    filterSongs(); 
                    if(!categories.includes(...newTags)) {
                        newTags.forEach(t => { if(!categories.includes(t)) categories.push(t); });
                        renderCategories();
                    }
                }
            } catch(e) { alert("保存标签失败！"); }
        }
    }

    async function deleteSong(event, songName) {
        event.stopPropagation(); 

        deletedSongs.add(songName);
        allSongs = allSongs.filter(s => s !== songName);
        
        renderCategories(); 
        filterSongs();
        
        try {
            fetch('/api/deleted_songs', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ song_name: songName, action: 'delete' })
            });
        } catch(e) {
            console.error("隐藏同步失败");
        }
    }

    function setupAudioEngine() {
        audio.addEventListener('play', () => renderList());
        audio.addEventListener('pause', () => renderList());
        audio.addEventListener('ended', playNext);
        
        window.addEventListener('beforeunload', () => evaluateCurrentSong());

        if ('mediaSession' in navigator) {
            navigator.mediaSession.setActionHandler('play', () => audio.play());
            navigator.mediaSession.setActionHandler('pause', () => audio.pause());
            navigator.mediaSession.setActionHandler('previoustrack', () => playPrev());
            navigator.mediaSession.setActionHandler('nexttrack', () => playNext());
        }
    }

    window.onload = init;