 
        /* ==========================================================================
           1. 全局状态容器 (Centralized State)
           ========================================================================== */
        /* ==========================================================================
    1. 全局状态容器 (Centralized State)
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
            isFirstLoad: true,
            observer: null,
            DOMFeed: document.getElementById('videoFeed'),

            // 🌟 新增：持久化保存“推荐流”的状态
            allStreamCache: {
                data: [],
                page: 1,
                currentIndex: 0,
                hasMore: true,
                tag: '全部'
            }
        };

        /* ==========================================================================
           2. 视频导航与操作引擎
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
                if (card) {
                    const likeBtn = card.querySelector('.action-btn:not(.btn-dislike)');
                    const encodedName = card.querySelector('video').src.split('/').pop();
                    AppActions.toggleLike(likeBtn, encodedName);
                }
            },
            deleteCurrent: () => {
                const card = document.getElementById(`card-${AppState.currentIndex}`);
                if (card) {
                    const deleteBtn = card.querySelector('.btn-dislike, .text-green-400');
                    const encodedName = card.querySelector('video').src.split('/').pop();
                    AppActions.deleteCard(deleteBtn, encodedName, AppState.currentIndex);
                }
            },
            seekCurrent: (direction) => {
                const card = document.getElementById(`card-${AppState.currentIndex}`);
                if (!card) return;

                const video = card.querySelector('video');
                const toast = card.querySelector('.seek-toast');

                if (video && video.duration) {
                    const step = video.duration * 0.2;
                    const isForward = direction === 'right';

                    video.currentTime = Math.max(0, Math.min(
                        video.currentTime + (isForward ? step : -step),
                        video.duration
                    ));

                    toast.innerText = isForward ? '前进 20%' : '后退 20%';
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
                } catch (e) { console.error('同步失败', e); }
            },
            record: (filename, action) => {
                let queue = JSON.parse(localStorage.getItem('videoActionsQueue') || '[]');
                queue.push({ filename, action, timestamp: Date.now() });
                localStorage.setItem('videoActionsQueue', JSON.stringify(queue));
                SyncEngine.flush();
            }
        };

        const AppActions = {
            parseTags: (filename) => {
                const matches = filename.match(/#([^#\s.]+)/g);
                return matches ? matches.map(m => m.substring(1)) : [];
            },
            switchPool: (poolType) => {
                AppState.pool = poolType;
                localStorage.setItem('videoPoolPref', poolType);
                AppState.tag = '全部';
                // 🌟 清空“推荐流”缓存，确保切换模式时强制触发 fetchNextPage 里的随机逻辑
    AppState.allStreamCache.data = [];
                const searchInput = document.getElementById('searchInput');
                if (searchInput) searchInput.value = '';
                DataLoader.fetchNextPage(true);
            },
            // 修复语法错误：在对象字面量内正确声明方法
            switchFilter: (type) => {
                const oldType = AppState.filter;
                if (oldType === type) return;

                // 🌟 1. 如果离开的是“推荐流(all)”，保存当前状态
                if (oldType === 'all') {
                    AppState.allStreamCache = {
                        data: [...AppState.data],
                        page: AppState.page,
                        currentIndex: AppState.currentIndex,
                        hasMore: AppState.hasMore,
                        tag: AppState.tag
                    };
                }

                AppState.filter = type;

                // UI 状态切换
                document.getElementById('nav-all').classList.toggle('active', type === 'all');
                document.getElementById('nav-liked').classList.toggle('active', type === 'liked');
                document.getElementById('nav-similar').classList.toggle('active', type === 'similar');

                // 🌟 2. 处理进入逻辑
                if (type === 'similar') {
                    if (!AppState.sourceVideo) {
                        alert("请先在推荐流中找到感兴趣的视频");
                        AppActions.switchFilter(oldType); // 滚回之前的状态
                        return;
                    }
                    DataLoader.fetchSimilarPage(true);
                }
                else if (type === 'all') {
                    // 🌟 3. 核心：如果缓存里有数据，直接恢复
                    if (AppState.allStreamCache.data.length > 0) {
                        const cache = AppState.allStreamCache;
                        AppState.data = cache.data;
                        AppState.page = cache.page;
                        AppState.hasMore = cache.hasMore;
                        AppState.tag = cache.tag;

                        // 重绘 DOM
                        AppState.DOMFeed.innerHTML = '';
                        AppState.data.forEach((v, idx) => UIRenderer.buildCardDOM(v, idx));

                        // 滚动到离开时的那个视频
                        NavigationEngine.scrollToVideo(cache.currentIndex);
                        InteractionEngine.initIntersectionObserver();
                    } else {
                        AppState.tag = '全部';
                        DataLoader.fetchNextPage(true);
                    }
                }
                else {
                    // 其他模式(liked/disliked) 依然正常重置加载
                    AppState.tag = '全部';
                    DataLoader.fetchNextPage(true);
                }
            },

            setTag: (tag) => {
                AppState.tag = tag;
                const searchInput = document.getElementById('searchInput');
                if (searchInput && tag !== '全部') searchInput.value = tag;
                if (searchInput && tag === '全部') searchInput.value = '';

                // 🌟 如果当前是推荐流模式，清空缓存，让它重新加载新标签内容
                if (AppState.filter === 'all') {
                    AppState.allStreamCache.data = [];
                }

                DataLoader.fetchNextPage(true);
            },
            handleSearch: (e) => {
                if (e.key === 'Enter') {
                    const val = e.target.value.trim();
                    AppActions.setTag(val ? val : '全部');
                    e.target.blur();
                }
            },
            toggleDrawer: () => document.getElementById('drawer').classList.toggle('open'),
            toggleLike: (btn, encodedName) => {
                const isLiked = btn.classList.toggle('liked');
                btn.querySelector('.icon-circle').innerText = isLiked ? '❤️' : '🤍';
                SyncEngine.record(decodeURIComponent(encodedName), isLiked ? 'like' : 'unlike');
            },
            deleteCard: (btn, encodedName) => {
                const isDislikeMode = AppState.filter === 'disliked';
                const isActioned = btn.classList.toggle('actioned');
                const card = btn.closest('.video-card');
                const video = card.querySelector('.video-player');
                const icon = btn.querySelector('.icon-circle');

                if (isDislikeMode) {
                    SyncEngine.record(decodeURIComponent(encodedName), isActioned ? 'undelete' : 'delete');
                    icon.innerText = isActioned ? '✅' : '♻️';
                    btn.querySelector('span').innerText = isActioned ? '已恢复' : '恢复';
                    btn.classList.toggle('text-gray-400', isActioned);
                    btn.classList.toggle('text-green-400', !isActioned);
                } else {
                    SyncEngine.record(decodeURIComponent(encodedName), isActioned ? 'delete' : 'undelete');
                    icon.innerText = isActioned ? '🖤' : '💔';
                    btn.querySelector('span').innerText = isActioned ? '已踩' : '不喜欢';

                    icon.style.animation = 'none';
                    void icon.offsetWidth;
                    icon.style.animation = 'heartPop 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
                    NavigationEngine.next();
                }
            }
        };

        /* ==========================================================================
           3. 数据加载与生命周期模块 (Data Loader)
           ========================================================================== */
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

                if (reset) {
                    AppState.page = 1;
                    AppState.data = [];
                    AppState.currentIndex = 0;
                    AppState.DOMFeed.innerHTML = '';
                }

                try {
                    const needTagsParam = reset ? '&need_tags=1' : '&need_tags=0';
                    let reqUrl = `/api/video/list?filter=${AppState.filter}&tag=${encodeURIComponent(AppState.tag)}&pool=${AppState.pool}&page=${AppState.page}&limit=5${needTagsParam}`;
                    let res = await fetch(reqUrl);
                    let data = await res.json();

                    const countEl = document.getElementById('searchCount');
                    if (countEl) {
                        countEl.innerText = data.total;
                        countEl.style.color = data.total === 0 ? '#ef4444' : '#fff';
                    }

                  // 🌟 修改点：随机播放逻辑扩展
        // 只要是 reset（切换模式/标签） 且 处于推荐模式 且 数据量够大
        if (reset && AppState.filter === 'all' && AppState.tag === '全部' && data.total > 5) {
            const maxPage = Math.ceil(data.total / 10);
            // 生成随机页码
            AppState.page = Math.floor(Math.random() * maxPage) + 1; 
            
            if (AppState.page > 1) {
                // 立即重新请求随机页的数据
                reqUrl = `/api/video/list?filter=${AppState.filter}&tag=${encodeURIComponent(AppState.tag)}&pool=${AppState.pool}&page=${AppState.page}&limit=10&need_tags=1`;
                res = await fetch(reqUrl);
                data = await res.json();
            }
        }
                    if (reset && data.tags_count && Object.keys(data.tags_count).length > 0) {
                        UIRenderer.renderCategoryPanels(data.tags_count, data.total);
                    }

                    const newItems = data.items;
                    const startIndex = AppState.data.length;
                    AppState.data = [...AppState.data, ...newItems];
                    AppState.hasMore = data.has_more;
                    AppState.page++;

                    newItems.forEach((v, idx) => UIRenderer.buildCardDOM(v, startIndex + idx));

                    if (reset && AppState.data.length === 0) {
                        AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-gray-500 font-bold">${AppState.filter === 'disliked' ? '回收站空' : (AppState.filter === 'liked' ? '暂无喜欢的视频' : '无匹配视频')}</div>`;
                    }

                    InteractionEngine.initIntersectionObserver();
                } catch (e) {
                    console.error("加载数据异常:", e);
                } finally {
                    AppState.isLoading = false;
                }
            },
            fetchSimilarPage: async (reset = false) => {
                if (!AppState.sourceVideo) return;
                AppState.isLoading = true;

                if (reset) {
                    AppState.page = 1;
                    AppState.data = [];
                    AppState.currentIndex = 0;
                    AppState.DOMFeed.innerHTML = '<div class="h-full w-full flex items-center justify-center text-blue-400">AI 正在联想相似内容...</div>';
                }

                try {
                    // 第一步：获取相似视频的名字列表
                    const res = await fetch(`/api/video/recommend?name=${encodeURIComponent(AppState.sourceVideo)}&k=15`);
                    const simData = await res.json();
                    const recommendations = simData.recommendations || [];

                    if (recommendations.length === 0) {
                        AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-gray-500">未发现语义相近的内容</div>`;
                        return;
                    }

                    // 第二步：将名字列表转为逗号分隔字符串，向核心服务器请求完整元数据
                    // 假设你的核心接口支持通过 filenames 过滤，或者我们模拟推荐流的布局
                    // 3. 改为 POST 请求核心服务器
                    const fileNameList = recommendations.map(item => item.filename);
                    const detailRes = await fetch(`/api/video/list`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            names: fileNameList, // 发送数组
                            need_tags: 1
                        })
                    });

                    const finalData = await detailRes.json();

                    if (reset) AppState.DOMFeed.innerHTML = '';
                    // 🌟 新增：建立相似度映射表，以便回填
                    const simMap = {};
                    recommendations.forEach(r => { simMap[r.filename] = r.similarity; });

                    const items = (finalData.items || []).map(item => ({
                        ...item,
                        similarity: simMap[item.filename] || 0 // 将数值存入 item 对象
                    }));
                    AppState.data = items;
                    AppState.hasMore = false;

                    // 第三步：使用标准的渲染器，这样布局和标签就和推荐流完全一样了
                    items.forEach((v, idx) => UIRenderer.buildCardDOM(v, idx));

                    InteractionEngine.initIntersectionObserver();
                } catch (e) {
                    console.error("加载相似度数据失败:", e);
                    AppState.DOMFeed.innerHTML = `<div class="h-full w-full flex items-center justify-center text-red-500">相似引擎连接失败</div>`;
                } finally {
                    AppState.isLoading = false;
                }
            }
        };

        /* ==========================================================================
           4. 界面渲染引擎 (UI Renderer)
           ========================================================================== */
        const UIRenderer = {
            renderCategoryPanels: (countsMap, total) => {
                const sorted = Object.entries(countsMap || {}).sort((a, b) => b[1] - a[1]);
                let topTags = sorted.slice(0, 10).map(item => item[0]);

                if (AppState.tag !== '全部' && !topTags.includes(AppState.tag)) {
                    topTags.unshift(AppState.tag);
                    if (topTags.length > 10) topTags.pop();
                }

                const bar = document.getElementById('categoryBar');
                bar.innerHTML = `<div class="cat-pill ${AppState.tag === '全部' ? 'active' : ''}" onclick="AppActions.setTag('全部')">全部${AppState.tag === '全部' ? ` ${total || 0}` : ''}</div>` +
                    topTags.map(tag => `<div class="cat-pill ${AppState.tag === tag ? 'active' : ''}" onclick="AppActions.setTag('${tag}')">${tag}${AppState.tag === tag ? ` ${countsMap[tag] || 0}` : ''}</div>`).join('');

                const grid = document.getElementById('drawerGrid');
                grid.innerHTML = `
                    <div class="grid-item border border-red-500/30" onclick="AppActions.switchFilter('disliked'); AppActions.toggleDrawer()">
                        <span class="text-sm font-bold text-red-400">已不喜欢 🗑️</span>
                        <span class="text-[10px] opacity-50 text-red-200">误触可找回</span>
                    </div>
                    <div class="grid-item ${AppState.tag === '全部' && AppState.filter !== 'disliked' ? 'active' : ''}" onclick="AppActions.switchFilter('all'); AppActions.setTag('全部'); AppActions.toggleDrawer()">
                        <span class="text-sm font-bold">全部视频</span>
                        <span class="text-[10px] opacity-50">${total || 0} 视频</span>
                    </div>` +
                    sorted.map(([tag, count]) => `
                    <div class="grid-item ${AppState.tag === tag && AppState.filter !== 'disliked' ? 'active' : ''}" onclick="AppActions.switchFilter('all'); AppActions.setTag('${tag}'); AppActions.toggleDrawer()">
                        <span class="text-sm font-bold">${tag}</span><span class="text-[10px] opacity-50">${count} 视频</span>
                    </div>`).join('');
            },
            buildCardDOM: (v, index) => {
                if (document.getElementById(`card-${index}`)) return;

                const card = document.createElement('div');
                card.className = 'video-card paused';
                card.id = `card-${index}`;
                card.dataset.index = index;

                const jsSafeName = encodeURIComponent(v.filename);
                const isDislikeMode = AppState.filter === 'disliked';

                const deleteBtnHtml = isDislikeMode
                    ? `<button class="action-btn text-green-400" onclick="AppActions.deleteCard(this, '${jsSafeName}')"><div class="icon-circle">♻️</div><span class="text-[10px] font-bold shadow-black">恢复</span></button>`
                    : `<button class="action-btn btn-dislike" onclick="AppActions.deleteCard(this, '${jsSafeName}')"><div class="icon-circle">💔</div><span class="text-[10px] font-bold shadow-black">不喜欢</span></button>`;

                let cleanTitle = v.filename.replace(/^\[NEW\]_/i, '').replace(/\.(mp4|mov|mkv|webm|avi)$/i, '').replace(/#([^#\s.]+)/g, '').trim();

                // 🌟 计算相似度显示
                let simBadgeHtml = '';
                if (v.similarity && AppState.filter === 'similar') {
                    const score = (v.similarity * 100).toFixed(0);
                    let colorClass = 'sim-low';
                    if (score >= 90) colorClass = 'sim-high';
                    else if (score >= 80) colorClass = 'sim-med';
                    simBadgeHtml = `<span class="similarity-badge ${colorClass} mr-2 px-1.5 py-0.5 rounded text-[10px] italic shadow-sm">${score}%</span>`;
                }

                let aiTagsHtml = '';
                if (v.category && v.category !== '未分类') aiTagsHtml += `<span class="ai-tag px-2 py-0.5 rounded text-[11px] cursor-pointer" onclick="event.stopPropagation(); AppActions.setTag('${v.category}')">#${v.category}</span>`;
                (v.ai_tags || []).forEach(t => aiTagsHtml += `<span class="ai-tag px-2 py-0.5 rounded text-[11px] cursor-pointer" onclick="event.stopPropagation(); AppActions.setTag('${t}')">#${t}</span>`);

                let fnTagsHtml = '';
                (v.filename_tags || []).forEach(t => fnTagsHtml += `<span class="file-tag px-2 py-0.5 rounded text-[10px] cursor-pointer" onclick="event.stopPropagation(); AppActions.setTag('${t}')"><small class='opacity-50'>[F]</small> ${t}</span>`);

                card.innerHTML = `
        <div class="blur-bg" style="background-image: url('${v.url}')"></div>
        <video src="${v.url}" class="video-player" loop playsinline preload="metadata"></video>
        <div class="click-area" id="area-${index}"></div>
        <div class="play-icon"><svg class="w-20 h-20 drop-shadow-lg" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div>
        <div class="seek-toast" id="toast-${index}"></div>
        <div class="action-sidebar">
            <button class="action-btn ${v.is_liked ? 'liked' : ''}" onclick="AppActions.toggleLike(this, '${jsSafeName}')">
                <div class="icon-circle">${v.is_liked ? '❤️' : '🤍'}</div>
                <span class="text-[10px] font-bold shadow-black">喜欢</span>
            </button>
            ${deleteBtnHtml}
        </div>
        <div class="info-bottom pointer-events-auto">
            <h2 class="text-[15px] font-bold mb-3 line-clamp-2 drop-shadow-md flex items-center">
                ${simBadgeHtml}<span>${cleanTitle}</span>
            </h2>
            ${aiTagsHtml ? `<div class="tag-group mb-2">${aiTagsHtml}</div>` : ''}
            ${fnTagsHtml ? `<div class="tag-group pt-2 border-t border-white/10">${fnTagsHtml}</div>` : ''}
        </div>
        <div class="progress-container"><div class="progress-bar" id="prog-${index}"></div></div>
    `;
                AppState.DOMFeed.appendChild(card);

                const video = card.querySelector('video');
                const prog = card.querySelector(`#prog-${index}`);
                video.addEventListener('timeupdate', () => {
                    if (video.duration) prog.style.width = `${(video.currentTime / video.duration) * 100}%`;
                });

                InteractionEngine.bindGestures(card.querySelector(`#area-${index}`), video, card.querySelector(`#toast-${index}`));
                if (AppState.observer) AppState.observer.observe(card);
            }
        };

        /* ==========================================================================
           5. 物理交互与观察引擎 (Interaction Engine)
           ========================================================================== */
        const InteractionEngine = {
            initIntersectionObserver: () => {
                if (AppState.observer) AppState.observer.disconnect();

                AppState.observer = new IntersectionObserver((entries) => {
                    entries.forEach(entry => {
                        const card = entry.target;
                        const video = card.querySelector('video');
                        const idx = parseInt(card.dataset.index);

                        if (entry.isIntersecting) {
                            // 修复重复变量声明：移除此处多余的 const card 与 const video
                            const fname = decodeURIComponent(video.src.split('/').pop());

                            if (AppState.filter !== 'similar') {
                                AppState.sourceVideo = fname;
                                document.getElementById('nav-similar').classList.remove('hidden');
                                document.getElementById('nav-similar').innerText = '相似';
                            }

                            AppState.currentIndex = idx;
                            video.play().then(() => {
                                card.classList.remove('paused');
                                if (AppState.filter !== 'disliked') SyncEngine.record(decodeURIComponent(video.src.split('/').pop()), 'play');
                            }).catch(() => card.classList.add('paused'));

                            if (idx >= AppState.data.length - 3 && AppState.hasMore) {
                                DataLoader.fetchNextPage(false);
                            }
                        } else {
                            video.pause();
                            video.currentTime = 0;
                            card.classList.add('paused');
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
                        e.preventDefault(); isSeeking = true;
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
            }
        };

        /* ==========================================================================
           6. 全局键盘事件监听器 (规整至末尾)
           ========================================================================== */
        window.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT') return;

            switch (e.key.toLowerCase()) {
                case 'z':
                    e.preventDefault();
                    NavigationEngine.toggleLikeCurrent();
                    break;
                case 'x':
                    e.preventDefault();
                    NavigationEngine.deleteCurrent();
                    break;
                case ' ':
                    e.preventDefault();
                    const card = document.getElementById(`card-${AppState.currentIndex}`);
                    if (card) card.querySelector('.click-area').click();
                    break;
                case 'arrowdown':
                    e.preventDefault();
                    NavigationEngine.next();
                    break;
                case 'arrowup':
                    e.preventDefault();
                    NavigationEngine.prev();
                    break;
                case 'arrowleft':
                    e.preventDefault();
                    NavigationEngine.seekCurrent('left');
                    break;
                case 'arrowright':
                    e.preventDefault();
                    NavigationEngine.seekCurrent('right');
                    break;
            }
        });

        // 启动引擎
        InteractionEngine.setupGlobalListeners();
        DataLoader.bootstrap();
 