/* ==========================================================================
   1. 全局状态容器
   ========================================================================== */
const AppState = {
    data: [],
    filter: 'all',
    sourceVideo: null,
    tag: '全部',
    pool: localStorage.getItem('videoPoolPref') || 'mixed',
    page: 1,
    hasMore: true,
    isLoading: false,
    currentIndex: 0,
    observer: null,
    DOMFeed: document.getElementById('videoFeed'),
    allStreamCache: { data: [], page: 1, currentIndex: 0, hasMore: true, tag: '全部' }
};

/* ==========================================================================
   2. 🌟 高性能媒体引擎 (核心优化点)
   ========================================================================== */
const MediaEngine = {
    updateWindow: (currentIndex) => {
        const cards = document.querySelectorAll('.video-card');
        cards.forEach(card => {
            const idx = parseInt(card.dataset.index);
            const video = card.querySelector('video');
            const dataSrc = video.getAttribute('data-src');

            // 核心：仅维持窗口大小为 3（上一个，当前，下一个）
            if (Math.abs(idx - currentIndex) <= 1) {
                // 如果尚未挂载源，或者源被清理了，则重新挂载
                if (!video.src || video.src === window.location.href || video.src === '') {
                    video.src = dataSrc;
                }
                // 当前视频全速缓冲，相邻视频仅缓冲元数据，节省带宽
                video.preload = (idx === currentIndex) ? 'auto' : 'metadata';
            } else {
                // 核心：超出窗口的视频，强制卸载源，瞬间释放硬件解码器和几百MB内存
                if (video.src && video.src !== window.location.href) {
                    video.removeAttribute('src');
                    video.load(); 
                }
            }
        });
    }
};

/* ==========================================================================
   3. 业务引擎模块
   ========================================================================== */
const NavigationEngine = {
    scrollToVideo: (index) => {
        const targetCard = document.getElementById(`card-${index}`);
        if (targetCard) {
            AppState.DOMFeed.scrollTo({ top: targetCard.offsetTop, behavior: 'smooth' });
            AppState.currentIndex = index;
            return true;
        }
        return false;
    },
    next: () => NavigationEngine.scrollToVideo(AppState.currentIndex + 1),
    prev: () => NavigationEngine.scrollToVideo(AppState.currentIndex - 1),
    toggleLikeCurrent: () => {
        const card = document.getElementById(`card-${AppState.currentIndex}`);
        if (card) AppActions.toggleLike(card.querySelector('.action-btn:not(.btn-dislike)'), card.querySelector('video').getAttribute('data-src').split('/').pop());
    },
    deleteCurrent: () => {
        const card = document.getElementById(`card-${AppState.currentIndex}`);
        if (card) AppActions.deleteCard(card.querySelector('.btn-dislike, .text-green-400'), card.querySelector('video').getAttribute('data-src').split('/').pop());
    },
    seekCurrent: (direction) => {
        const card = document.getElementById(`card-${AppState.currentIndex}`);
        if (!card) return;
        const video = card.querySelector('video'), toast = card.querySelector('.seek-toast');
        if (video && video.duration) {
            const step = video.duration * 0.2;
            video.currentTime = Math.max(0, Math.min(video.currentTime + (direction === 'right' ? step : -step), video.duration));
            toast.innerText = direction === 'right' ? '前进 20%' : '后退 20%';
            toast.style.opacity = 1;
            if (toast._timer) clearTimeout(toast._timer);
            toast._timer = setTimeout(() => toast.style.opacity = 0, 500);
        }
    }
};

const SyncEngine = {
    init: () => window.addEventListener('online', SyncEngine.flush),
    flush: async () => {
        if (!navigator.onLine) return;
        let queue = JSON.parse(localStorage.getItem('videoActionsQueue') || '[]');
        if (queue.length === 0) return;
        try {
            const res = await fetch('/api/video/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(queue) });
            if (res.ok) localStorage.setItem('videoActionsQueue', '[]');
        } catch (e) {}
    },
    record: (filename, action) => {
        let queue = JSON.parse(localStorage.getItem('videoActionsQueue') || '[]');
        queue.push({ filename, action, timestamp: Date.now() });
        localStorage.setItem('videoActionsQueue', JSON.stringify(queue));
        SyncEngine.flush();
    }
};

const AppActions = {
    switchPool: (poolType) => {
        AppState.pool = poolType;
        localStorage.setItem('videoPoolPref', poolType);
        AppState.tag = '全部';
        AppState.allStreamCache.data = [];
        document.getElementById('searchInput').value = '';
        DataLoader.fetchNextPage(true);
    },
    switchFilter: (type) => {
        const oldType = AppState.filter;
        if (oldType === type && AppState.data.length > 0) return;

        document.querySelectorAll('video').forEach(v => v.pause());

        if (oldType === 'all') {
            AppState.allStreamCache = { data: [...AppState.data], page: AppState.page, currentIndex: AppState.currentIndex, hasMore: AppState.hasMore, tag: AppState.tag };
        }

        AppState.filter = type;

        const els = { 'all': 'nav-all', 'liked': 'nav-liked', 'similar': 'nav-similar', 'similar_vision': 'nav-similar-vision' };
        const indicator = document.getElementById('navIndicator');
        
        Object.keys(els).forEach(key => {
            const el = document.getElementById(els[key]);
            if (!el) return;
            if (key === type) {
                el.classList.add('active');
                requestAnimationFrame(() => {
                    if (indicator) {
                        indicator.style.left = `${el.offsetLeft}px`;
                        indicator.style.width = `${el.offsetWidth}px`;
                        indicator.style.background = key === 'similar_vision' 
                            ? 'linear-gradient(135deg, #10b981 0%, #059669 100%)' 
                            : 'linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)';
                    }
                });
            } else {
                el.classList.remove('active');
            }
        });

        if (type === 'similar' || type === 'similar_vision') {
            if (!AppState.sourceVideo) { alert("请先选择视频"); AppActions.switchFilter(oldType); return; }
            DataLoader[type === 'similar' ? 'fetchSimilarPage' : 'fetchVisionSimilarPage'](true);
        } else if (type === 'all' && AppState.allStreamCache.data.length > 0) {
            const c = AppState.allStreamCache;
            AppState.data = c.data; AppState.page = c.page; AppState.hasMore = c.hasMore; AppState.tag = c.tag;
            AppState.DOMFeed.innerHTML = '';
            AppState.data.forEach((v, idx) => UIRenderer.buildCardDOM(v, idx));
            NavigationEngine.scrollToVideo(c.currentIndex);
            setTimeout(() => InteractionEngine.initIntersectionObserver(), 100);
        } else {
            AppState.tag = '全部';
            DataLoader.fetchNextPage(true);
        }
    },
    setTag: (tag) => {
        AppState.tag = tag;
        const searchInput = document.getElementById('searchInput');
        if (searchInput && tag !== '全部') searchInput.value = tag;
        if (searchInput && tag === '全部') searchInput.value = '';
        if (AppState.filter === 'all') AppState.allStreamCache.data = [];
        DataLoader.fetchNextPage(true);
    },
    handleSearch: (e) => { if (e.key === 'Enter') { AppActions.setTag(e.target.value.trim() || '全部'); e.target.blur(); } },
    toggleDrawer: () => document.getElementById('drawer').classList.toggle('open'),
    toggleLike: (btn, encodedName) => {
        const isLiked = btn.classList.toggle('liked');
        btn.querySelector('.icon-circle').innerText = isLiked ? '❤️' : '🤍';
        SyncEngine.record(decodeURIComponent(encodedName), isLiked ? 'like' : 'unlike');
    },
    deleteCard: (btn, encodedName) => {
        const isDislikeMode = AppState.filter === 'disliked';
        const isActioned = btn.classList.toggle('actioned');
        const icon = btn.querySelector('.icon-circle');
        if (isDislikeMode) {
            SyncEngine.record(decodeURIComponent(encodedName), isActioned ? 'undelete' : 'delete');
            icon.innerText = isActioned ? '✅' : '♻️';
            btn.querySelector('span').innerText = isActioned ? '已恢复' : '恢复';
            btn.classList.toggle('text-gray-400', isActioned); btn.classList.toggle('text-green-400', !isActioned);
        } else {
            SyncEngine.record(decodeURIComponent(encodedName), isActioned ? 'delete' : 'undelete');
            icon.innerText = isActioned ? '🖤' : '💔';
            btn.querySelector('span').innerText = isActioned ? '已踩' : '不喜欢';
            icon.style.animation = 'none'; void icon.offsetWidth; icon.style.animation = 'heartPop 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
            NavigationEngine.next();
        }
    }
};

const DataLoader = {
    bootstrap: async () => {
        SyncEngine.init();
        const poolSelect = document.getElementById('poolSelect');
        if (poolSelect) poolSelect.value = AppState.pool;
        fetch('/api/video/scan', { method: 'POST' }).catch(() => { });
        await DataLoader.fetchNextPage(true);
    },
    fetchNextPage: async (reset = false) => {
        if (AppState.isLoading || (!AppState.hasMore && !reset)) return;
        AppState.isLoading = true;
        if (reset) { AppState.page = 1; AppState.data = []; AppState.currentIndex = 0; AppState.DOMFeed.innerHTML = ''; }
        try {
            const needTagsParam = reset ? '&need_tags=1' : '&need_tags=0';
            let reqUrl = `/api/video/list?filter=${AppState.filter}&tag=${encodeURIComponent(AppState.tag)}&pool=${AppState.pool}&page=${AppState.page}&limit=5${needTagsParam}`;
            let res = await fetch(reqUrl); let data = await res.json();
            
            const countEl = document.getElementById('searchCount');
            if (countEl) { countEl.innerText = data.total; countEl.style.color = data.total === 0 ? '#ef4444' : '#fff'; }

            if (reset && AppState.filter === 'all' && AppState.tag === '全部' && data.total > 5) {
                const maxPage = Math.ceil(data.total / 10);
                AppState.page = Math.floor(Math.random() * maxPage) + 1; 
                if (AppState.page > 1) {
                    reqUrl = `/api/video/list?filter=${AppState.filter}&tag=${encodeURIComponent(AppState.tag)}&pool=${AppState.pool}&page=${AppState.page}&limit=10&need_tags=1`;
                    res = await fetch(reqUrl); data = await res.json();
                }
            }

            if (reset && data.tags_count && Object.keys(data.tags_count).length > 0) UIRenderer.renderCategoryPanels(data.tags_count, data.total);

            const newItems = data.items; const startIndex = AppState.data.length;
            AppState.data = [...AppState.data, ...newItems]; AppState.hasMore = data.has_more; AppState.page++;
            newItems.forEach((v, idx) => UIRenderer.buildCardDOM(v, startIndex + idx));

            if (reset && AppState.data.length === 0) AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-gray-500 font-bold">${AppState.filter === 'disliked' ? '回收站空' : '无匹配内容'}</div>`;
            InteractionEngine.initIntersectionObserver();
        } catch (e) {} finally { AppState.isLoading = false; }
    },
    fetchSimilarPage: async (reset = false) => {
        if (!AppState.sourceVideo) return;
        AppState.isLoading = true;
        if (reset) { AppState.page = 1; AppState.data = []; AppState.currentIndex = 0; AppState.DOMFeed.innerHTML = '<div class="h-full w-full flex items-center justify-center text-blue-400">正在联想文本内容...</div>'; }
        try {
            const res = await fetch(`/api/video/recommend?name=${encodeURIComponent(AppState.sourceVideo)}&k=15`);
            const simData = await res.json();
            if (!simData.recommendations || simData.recommendations.length === 0) { AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-gray-500">未发现相近内容</div>`; return; }
            
            const fileNameList = simData.recommendations.map(item => item.filename);
            const detailRes = await fetch(`/api/video/list`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ names: fileNameList, need_tags: 1 }) });
            const finalData = await detailRes.json();
            
            if (reset) AppState.DOMFeed.innerHTML = '';
            const simMap = {}; simData.recommendations.forEach(r => simMap[r.filename] = r.similarity);
            const items = (finalData.items || []).map(item => ({ ...item, similarity: simMap[item.filename] || 0 }));
            
            AppState.data = items; AppState.hasMore = false;
            items.forEach((v, idx) => UIRenderer.buildCardDOM(v, idx));
            setTimeout(() => InteractionEngine.initIntersectionObserver(), 100);
        } catch (e) { AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-red-500">连接失败</div>`; } finally { AppState.isLoading = false; }
    },
    fetchVisionSimilarPage: async (reset = false) => {
        if (!AppState.sourceVideo) return;
        AppState.isLoading = true;
        if (reset) { AppState.page = 1; AppState.data = []; AppState.currentIndex = 0; AppState.DOMFeed.innerHTML = '<div class="h-full w-full flex items-center justify-center text-green-400">分析画面特征寻找视频...</div>'; }
        try {
            const res = await fetch(`/api/video/vision_recommend?name=${encodeURIComponent(AppState.sourceVideo)}&k=15`);
            if (res.status === 404) { AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex flex-col items-center justify-center text-gray-500"><p>该视频暂未生成视觉特征</p></div>`; return; }
            
            const simData = await res.json();
            if (!simData.recommendations || simData.recommendations.length === 0) { AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-gray-500">未发现相近画面</div>`; return; }
            
            const fileNameList = simData.recommendations.map(item => item.filename);
            const detailRes = await fetch(`/api/video/list`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ names: fileNameList, need_tags: 1 }) });
            const finalData = await detailRes.json();
            
            if (reset) AppState.DOMFeed.innerHTML = '';
            const simMap = {}; simData.recommendations.forEach(r => simMap[r.filename] = r.similarity);
            const items = (finalData.items || []).map(item => ({ ...item, similarity: simMap[item.filename] || 0 }));
            
            AppState.data = items; AppState.hasMore = false;
            items.forEach((v, idx) => UIRenderer.buildCardDOM(v, idx));
            setTimeout(() => InteractionEngine.initIntersectionObserver(), 100);
        } catch (e) { AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-red-500">视觉引擎连接失败</div>`; } finally { AppState.isLoading = false; }
    }
};

const UIRenderer = {
    renderCategoryPanels: (countsMap, total) => {
        const sorted = Object.entries(countsMap || {}).sort((a, b) => b[1] - a[1]);
        let topTags = sorted.slice(0, 10).map(item => item[0]);
        if (AppState.tag !== '全部' && !topTags.includes(AppState.tag)) { topTags.unshift(AppState.tag); if (topTags.length > 10) topTags.pop(); }

        const bar = document.getElementById('categoryBar');
        bar.innerHTML = `<div class="cat-pill ${AppState.tag === '全部' ? 'active' : ''}" onclick="AppActions.setTag('全部')">全部${AppState.tag === '全部' ? ` ${total || 0}` : ''}</div>` +
            topTags.map(tag => `<div class="cat-pill ${AppState.tag === tag ? 'active' : ''}" onclick="AppActions.setTag('${tag}')">${tag}${AppState.tag === tag ? ` ${countsMap[tag] || 0}` : ''}</div>`).join('');

        const grid = document.getElementById('drawerGrid');
        grid.innerHTML = `<div class="grid-item border border-red-500/30" onclick="AppActions.switchFilter('disliked'); AppActions.toggleDrawer()"><span class="text-sm font-bold text-red-400">已不喜欢 🗑️</span></div>` +
            `<div class="grid-item ${AppState.tag === '全部' && AppState.filter !== 'disliked' ? 'active' : ''}" onclick="AppActions.switchFilter('all'); AppActions.setTag('全部'); AppActions.toggleDrawer()"><span class="text-sm font-bold">全部</span><span class="text-[10px] opacity-50">${total || 0}</span></div>` +
            sorted.map(([tag, count]) => `<div class="grid-item ${AppState.tag === tag && AppState.filter !== 'disliked' ? 'active' : ''}" onclick="AppActions.switchFilter('all'); AppActions.setTag('${tag}'); AppActions.toggleDrawer()"><span class="text-sm font-bold">${tag}</span><span class="text-[10px] opacity-50">${count}</span></div>`).join('');
    },
    buildCardDOM: (v, index) => {
        if (document.getElementById(`card-${index}`)) return;
        const card = document.createElement('div');
        // 添加背板颜色 bg-black
        card.className = 'video-card paused relative w-full h-full overflow-hidden bg-black'; 
        card.id = `card-${index}`; card.dataset.index = index;
        const jsSafeName = encodeURIComponent(v.filename), isDislikeMode = AppState.filter === 'disliked';
        const deleteBtnHtml = isDislikeMode ? `<button class="action-btn text-green-400" onclick="AppActions.deleteCard(this, '${jsSafeName}')"><div class="icon-circle">♻️</div><span class="text-[10px] font-bold shadow-black">恢复</span></button>` : `<button class="action-btn btn-dislike" onclick="AppActions.deleteCard(this, '${jsSafeName}')"><div class="icon-circle">💔</div><span class="text-[10px] font-bold shadow-black">不喜欢</span></button>`;
        let cleanTitle = v.filename.replace(/^\[NEW\]_/i, '').replace(/\.(mp4|mov|mkv|webm|avi)$/i, '').replace(/#([^#\s.]+)/g, '').trim();

        let simBadgeHtml = '';
        if (v.similarity && (AppState.filter === 'similar' || AppState.filter === 'similar_vision')) {
            const score = (v.similarity * 100).toFixed(0);
            let colorClass = 'sim-low';
            if (AppState.filter === 'similar_vision') { if (score >= 90) colorClass = 'sim-high'; else if (score >= 80) colorClass = 'sim-med'; } 
            else { if (score >= 85) colorClass = 'sim-high'; else if (score >= 75) colorClass = 'sim-med'; }
            simBadgeHtml = `<span class="similarity-badge ${colorClass} mr-2 px-1.5 py-0.5 rounded text-[10px] italic shadow-sm">${AppState.filter === 'similar_vision'?'👁️ ':''}${score}%</span>`;
        }

        let aiTagsHtml = '';
        if (v.category && v.category !== '未分类') aiTagsHtml += `<span class="ai-tag px-2 py-0.5 rounded text-[11px] cursor-pointer" onclick="event.stopPropagation(); AppActions.setTag('${v.category}')">#${v.category}</span>`;
        (v.ai_tags || []).forEach(t => aiTagsHtml += `<span class="ai-tag px-2 py-0.5 rounded text-[11px] cursor-pointer" onclick="event.stopPropagation(); AppActions.setTag('${t}')">#${t}</span>`);

        // 🌟 性能优化：去除了 blur-bg，极大地释放了 GPU 压力
        card.innerHTML = `
            <video data-src="${v.url}" class="video-player absolute inset-0 w-full h-full object-contain z-10" loop playsinline preload="none" disablePictureInPicture></video>
            <div class="click-area absolute inset-0 z-20" id="area-${index}"></div>
            <div class="play-icon absolute z-30"><svg class="w-20 h-20 drop-shadow-lg" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div>
            <div class="seek-toast absolute z-30" id="toast-${index}"></div>
            <div class="action-sidebar"><button class="action-btn ${v.is_liked ? 'liked' : ''}" onclick="AppActions.toggleLike(this, '${jsSafeName}')"><div class="icon-circle">${v.is_liked ? '❤️' : '🤍'}</div><span class="text-[10px] font-bold shadow-black">喜欢</span></button>${deleteBtnHtml}</div>
            <div class="info-bottom pointer-events-auto"><h2 class="text-[15px] font-bold mb-3 line-clamp-2 drop-shadow-md flex items-center">${simBadgeHtml}<span>${cleanTitle}</span></h2>${aiTagsHtml ? `<div class="tag-group mb-2">${aiTagsHtml}</div>` : ''}</div>
            <div class="progress-container"><div class="progress-bar" id="prog-${index}"></div></div>
        `;
        AppState.DOMFeed.appendChild(card);

        const video = card.querySelector('video'), prog = card.querySelector(`#prog-${index}`);
        video.addEventListener('timeupdate', () => { if (video.duration) prog.style.width = `${(video.currentTime / video.duration) * 100}%`; });
        InteractionEngine.bindGestures(card.querySelector(`#area-${index}`), video, card.querySelector(`#toast-${index}`));
        if (AppState.observer) AppState.observer.observe(card);
    }
};

const InteractionEngine = {
    initIntersectionObserver: () => {
        if (AppState.observer) AppState.observer.disconnect();
        AppState.observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                const card = entry.target, video = card.querySelector('video'), idx = parseInt(card.dataset.index);
                if (entry.isIntersecting) {
                    const fname = decodeURIComponent(video.getAttribute('data-src').split('/').pop());
                    if (AppState.filter !== 'similar' && AppState.filter !== 'similar_vision') {
                        AppState.sourceVideo = fname;
                        document.getElementById('nav-similar')?.classList.remove('hidden');
                        document.getElementById('nav-similar-vision')?.classList.remove('hidden');
                    }
                    AppState.currentIndex = idx;
                    
                    // 🌟 核心触发：调度媒体引擎进行资源挂载/卸载
                    MediaEngine.updateWindow(idx);

                    video.muted = false;
                    video.play().then(() => {
                        card.classList.remove('paused');
                        if (AppState.filter !== 'disliked') SyncEngine.record(fname, 'play');
                    }).catch((err) => {
                        video.muted = true;
                        video.play().then(() => {
                            card.classList.remove('paused');
                            if (AppState.filter !== 'disliked') SyncEngine.record(fname, 'play');
                        }).catch(() => {
                            card.classList.add('paused');
                        });
                    });

                    if (idx >= AppState.data.length - 3 && AppState.hasMore) DataLoader.fetchNextPage(false);
                } else {
                    video.pause(); video.currentTime = 0; card.classList.add('paused');
                }
            });
        }, { root: AppState.DOMFeed, threshold: 0.6 });
        document.querySelectorAll('.video-card').forEach(card => AppState.observer.observe(card));
    },
    
    bindGestures: (touchArea, video, toastEl) => {
        let startX = 0, startY = 0, isSeeking = false, toastTimer, justSeeked = false;
        
        const handleStart = (x, y) => { startX = x; startY = y; isSeeking = false; };
        
        const handleMove = (x, y, e) => { 
            if (!startX || !startY) return; 
            const diffX = x - startX, diffY = y - startY; 
            if (Math.abs(diffX) > Math.abs(diffY) && Math.abs(diffX) > 15) { 
                e.preventDefault(); 
                isSeeking = true; 
            } 
        };
        
        const handleEnd = (x) => {
            if (!isSeeking || !video.duration) { startX = 0; startY = 0; return; }
            const diffX = x - startX;
            if (Math.abs(diffX) > 40) {
                const step = video.duration * 0.2;
                video.currentTime = Math.max(0, Math.min(video.currentTime + (diffX > 0 ? step : -step), video.duration));
                toastEl.innerText = diffX > 0 ? '前进 20%' : '后退 20%'; 
                toastEl.style.opacity = 1;
                clearTimeout(toastTimer); 
                toastTimer = setTimeout(() => toastEl.style.opacity = 0, 500);
                justSeeked = true; 
                setTimeout(() => { justSeeked = false; }, 200);
            }
            startX = 0; startY = 0; isSeeking = false;
        };

        touchArea.addEventListener('touchstart', e => handleStart(e.touches[0].clientX, e.touches[0].clientY), { passive: true });
        touchArea.addEventListener('touchmove', e => handleMove(e.touches[0].clientX, e.touches[0].clientY, e), { passive: false });
        touchArea.addEventListener('touchend', e => handleEnd(e.changedTouches[0].clientX));
        
        let isMouseDown = false;
        touchArea.addEventListener('mousedown', e => { isMouseDown = true; handleStart(e.clientX, e.clientY); });
        touchArea.addEventListener('mousemove', e => { if (isMouseDown) handleMove(e.clientX, e.clientY, e); });
        const mouseEnd = e => { if (isMouseDown) { isMouseDown = false; handleEnd(e.clientX); } };
        touchArea.addEventListener('mouseup', mouseEnd);
        touchArea.addEventListener('mouseleave', mouseEnd);

        touchArea.addEventListener('click', (e) => {
            if (justSeeked) { e.preventDefault(); return; }
            const card = touchArea.closest('.video-card');
            if (video.muted) video.muted = false;
            video.paused ? (video.play(), card.classList.remove('paused')) : (video.pause(), card.classList.add('paused'));
        });
    },

    setupGlobalListeners: () => {
        let wheelLock = false;
        
        AppState.DOMFeed.addEventListener('wheel', (e) => {
            if (window.matchMedia("(hover: none)").matches) return;
            e.preventDefault(); 
            if (wheelLock) return; 
            wheelLock = true;
            const targetIndex = Math.max(0, Math.min(e.deltaY > 0 ? AppState.currentIndex + 1 : AppState.currentIndex - 1, AppState.data.length - 1));
            const targetCard = document.getElementById(`card-${targetIndex}`);
            if (targetCard) AppState.DOMFeed.scrollTo({ top: targetCard.offsetTop, behavior: 'smooth' });
            setTimeout(() => { wheelLock = false; }, 500);
        }, { passive: false });

        window.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT') return;
            switch (e.key.toLowerCase()) {
                case 'z': e.preventDefault(); NavigationEngine.toggleLikeCurrent(); break;
                case 'x': e.preventDefault(); NavigationEngine.deleteCurrent(); break;
                case ' ': 
                    e.preventDefault(); 
                    document.getElementById(`card-${AppState.currentIndex}`)?.querySelector('.click-area')?.click(); 
                    break;
                case 'arrowdown': e.preventDefault(); NavigationEngine.next(); break;
                case 'arrowup': e.preventDefault(); NavigationEngine.prev(); break;
                case 'arrowleft': e.preventDefault(); NavigationEngine.seekCurrent('left'); break;
                case 'arrowright': e.preventDefault(); NavigationEngine.seekCurrent('right'); break;
            }
        });
    }
};

// 启动绑定
InteractionEngine.setupGlobalListeners();
DataLoader.bootstrap();