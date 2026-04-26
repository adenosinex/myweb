 // ==========================================
// dyfn.js - 底层请求与数据管理逻辑 (无 Vue 挂载代码)
// ==========================================

async function fetchLatestVideos(state) {
    const params = { latest: state.pageSize, page: state.page, page_size: state.pageSize, search: state.searchKeyword, score: state.searchScore }
    if (state.excludeKeyword) params.exclude = state.excludeKeyword
    if (state.searchSize && state.searchSize !== 0) params.size = state.searchSize
    if (state.sortBy) params.sort_by = state.sortBy
    const res = await axios.get('/dyfn/videos', { params })
    return res.data
}

function getVideoUrl(id) { return `/dyfn/videos/${id}/stream` }

async function updateScore(videoId, score) {
    await axios.post('/dyfn/videos/update_score', { id: videoId, score: score })
}

class VideoPlayer {
    constructor(state, refs) { this.state = state; this.refs = refs; this.touchStartY = 0; this.touchActive = false; this.lastRatios = []; this.isSwitching = false; }
    initializeVideoQueue() { this.refreshQueue(); }
    updateVideoClass(videoEl = null) {
        if (!videoEl) {
            const videos = document.querySelectorAll('video');
            const domIndex = this.refs.activeVideoIndex.value;
            if (!videos.length || !videos[domIndex]) return;
            videoEl = videos[domIndex];
        }
        if (!videoEl) return;
        const width = videoEl.videoWidth, height = videoEl.videoHeight;
        if (width && height) {
            const isLandscape = (height / width) <= 1.1;
            this.lastRatios.push(isLandscape);
            if (this.lastRatios.length > 3) this.lastRatios.shift();
            if (this.lastRatios.length === 3 && this.lastRatios.every(v => v)) this.refs.videoClass.value = 'video-landscape';
            else if (!isLandscape) this.refs.videoClass.value = 'video-portrait';
            else this.refs.videoClass.value = 'video-landscape';
        }
    }
    refreshQueue() {
        if (!this.state.videos || this.state.videos.length === 0) return;
        const totalVideos = this.state.videos.length;
        const currentGlobalIndex = this.refs.currentIndex.value;
        this.refs.videoQueue.value = [];
        this.refs.activeVideoIndex.value = 1; 
        this.refs.videoLoadStates.value = [false, false, false];
        for (let i = -1; i <= 1; i++) {
            let targetIndex = (currentGlobalIndex + i + totalVideos) % totalVideos;
            if (this.state.videos[targetIndex]) this.refs.videoQueue.value.push(this.state.videos[targetIndex]);
        }
        document.querySelectorAll('video').forEach(v => { v.pause(); v.muted = true; });
    }
    updateVideoQueue(direction = 'next') {
        if (this.state.videos.length === 0) return;
        const totalVideos = this.state.videos.length;
        if (direction === 'next') {
            this.refs.videoQueue.value.shift();
            let nextVideoIndex = (this.refs.currentIndex.value + 1) % totalVideos;
            this.refs.videoQueue.value.push(this.state.videos[nextVideoIndex]);
        } else {
            this.refs.videoQueue.value.pop();
            let prevVideoIndex = (this.refs.currentIndex.value - 1 + totalVideos) % totalVideos;
            this.refs.videoQueue.value.unshift(this.state.videos[prevVideoIndex]);
        }
        this.refs.videoLoadStates.value = [false, false, false];
    }
    prevVideo() {
        if (this.isSwitching || this.state.videos.length === 0) return;
        this.isSwitching = true;
        document.querySelectorAll('video').forEach(v => v.pause());
        this.refs.currentIndex.value--;
        if (this.refs.currentIndex.value < 0) this.refs.currentIndex.value = this.state.videos.length - 1;
        this.updateVideoQueue('prev');
        setTimeout(() => { this.playActiveVideo(); this.isSwitching = false; }, 200);
    }
    async nextVideo() {
        if (this.isSwitching || this.state.videos.length === 0) return;
        this.isSwitching = true;
        try {
            document.querySelectorAll('video').forEach(v => v.pause());
            this.exitFullscreen();
            let nextIndex = this.refs.currentIndex.value + 1;
            let isPageChange = false;
            if (nextIndex >= this.state.videos.length) {
                if (this.state.page * this.state.pageSize < this.state.totalVideos && typeof this.loadMoreVideos === 'function') {
                    this.state.page++; await this.loadMoreVideos(); isPageChange = true;
                }
                nextIndex = nextIndex % this.state.videos.length;
            }
            this.refs.currentIndex.value = nextIndex;
            if (isPageChange) this.refreshQueue(); else this.updateVideoQueue('next');
            setTimeout(() => { this.playActiveVideo(); this.isSwitching = false; }, 250);
        } catch (e) { console.error(e); this.isSwitching = false; }
    }
    playActiveVideo() {
        const domIndex = this.refs.activeVideoIndex.value;
        const videos = document.querySelectorAll('video');
        if (!videos.length || !videos[domIndex]) return;
        videos.forEach((v, i) => { if (i !== domIndex) { v.pause(); v.muted = true; v.currentTime = 0; } });
        const activeVideo = videos[domIndex];
        activeVideo.muted = false;
        if (activeVideo.ended) activeVideo.currentTime = 0;
        const playPromise = activeVideo.play();
        if (playPromise !== undefined) {
            playPromise.then(() => {}).catch(() => { activeVideo.muted = true; activeVideo.play().catch(() => {}); });
        }
        this.checkAndSetVideoClass(activeVideo);
    }
    checkAndSetVideoClass(video) {
        if (!video.hasAttribute('data-class-updated')) {
            const updateClass = () => { this.updateVideoClass(video); video.setAttribute('data-class-updated', 'true'); };
            if (video.readyState >= 1) updateClass();
            else video.addEventListener('loadedmetadata', updateClass, { once: true });
        }
    }
    onVideoReady(index) {
        this.refs.videoLoadStates.value[index] = true;
        const video = document.querySelectorAll('video')[index];
        if (!video) return;
        if (index === this.refs.activeVideoIndex.value) { if (video.paused) video.play().catch(() => {}); } 
        else { video.pause(); video.muted = true; }
    }
    onVideoLoaded(index) { this.refs.videoLoadStates.value[index] = true; }
    handleTouchStart(e) { if (this.isSwitching) return; this.touchActive = true; this.touchStartY = e.touches[0].clientY; }
    handleTouchEnd(e) {
        if (!this.touchActive) return;
        const deltaY = e.changedTouches[0].clientY - this.touchStartY;
        if (Math.abs(deltaY) > 60) { if (deltaY < 0) this.nextVideo(); else this.prevVideo(); }
        this.touchActive = false;
    }
    getCurrentlyPlayingVideo() { const videos = document.querySelectorAll('video'); return videos[this.refs.activeVideoIndex.value] || null; }
    setLoadMoreVideos(callback) { this.loadMoreVideos = callback; }
    exitFullscreen() { if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); }
    previewForward() { const v = this.getCurrentlyPlayingVideo(); if (v && v.duration) { const step = Math.max(5, v.duration * 0.05); v.currentTime = Math.min(v.duration, v.currentTime + step); } }
    previewBackward() { const v = this.getCurrentlyPlayingVideo(); if (v && v.duration) { const step = Math.max(5, v.duration * 0.05); v.currentTime = Math.max(0, v.currentTime - step); } }
    seekTo(ratio) { const v = this.getCurrentlyPlayingVideo(); if (v && v.duration) v.currentTime = v.duration * ratio; }
    initEventListeners() {
        window.addEventListener('wheel', (e) => { if (this.isSwitching || Math.abs(e.deltaY) < 30) return; if (e.deltaY > 0) this.nextVideo(); else if (e.deltaY < 0) this.prevVideo(); });
        window.addEventListener('keydown', (e) => { if (e.target.tagName === 'INPUT') return; if (this.isSwitching) return; if (e.key === 'ArrowDown') this.nextVideo(); else if (e.key === 'ArrowUp') this.prevVideo(); });
        window.addEventListener('dblclick', (e) => {
            if (e.target.tagName === 'BUTTON' || e.target.closest('button')) return;
            const v = this.getCurrentlyPlayingVideo();
            if (!document.fullscreenElement && v) { if (v.requestFullscreen) v.requestFullscreen().catch(() => {}); } 
            else if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); }
        });
    }
}

class SearchManager {
    constructor(state, refs) { this.state = state; this.refs = refs; }
    async doSearch() {
        const start = performance.now()
        this.state.searchKeyword = this.refs.searchKeyword.value.trim()
        this.state.excludeKeyword = this.refs.excludeKeyword.value.trim()
        this.state.searchScore = this.refs.searchScore.value
        this.state.sortBy = this.refs.sortBy.value
        this.state.searchSize = (this.refs.sizeValue.value && this.refs.sizeValue.value > 0) ? `${this.refs.sizeOperator.value}:${this.refs.sizeValue.value}` : 0
        this.state.page = 1; this.refs.currentIndex.value = 0; this.refs.showSearch.value = false;
        
        const params = []
        if (this.state.searchKeyword) params.push(`search=${encodeURIComponent(this.state.searchKeyword)}`)
        if (this.state.excludeKeyword) params.push(`exclude=${encodeURIComponent(this.state.excludeKeyword)}`)
        if (this.state.searchScore > 0) params.push(`score=${this.state.searchScore}`)
        if (this.state.searchSize !== 0) params.push(`size=${this.state.searchSize}`)
        
        const resp = await axios.get(`/dyfn/videos/count${params.length ? '?' + params.join('&') : ''}`)
        this.state.totalVideos = resp.data.total || 0; this.state.totalpage = this.state.totalVideos;
        
        this.updateUrlParams(); this.showSearchHint();
        await this.loadVideosCallback(false, 0);
        this.refs.searchTimeMsg.value = '筛选完成，用时 ' + ((performance.now() - start) / 1000).toFixed(2) + ' 秒'
        setTimeout(() => { this.refs.searchTimeMsg.value = '' }, 3000)
    }
    showSearchHint() {
        const conditions = []
        if (this.state.searchKeyword) conditions.push(`包含: "${this.state.searchKeyword}"`)
        if (this.state.excludeKeyword) conditions.push(`排除: "${this.state.excludeKeyword}"`)
        if (this.state.sortBy === 'filename') conditions.push(`纯文件名排序`)
        if (this.state.sortBy === 'path') conditions.push(`绝对路径排序`)
        if (this.state.searchScore > 0) conditions.push(`评分: ${this.state.searchScore}分`)
        if (this.state.searchSize !== 0) {
            const [op, val] = this.state.searchSize.split(':');
            conditions.push(`大小: ${op === 'lte' ? '≤' : op === 'gte' ? '≥' : '='}${val}MB`)
        }
        if (conditions.length > 0) this.refs.searchTimeMsg.value = `当前条件: ${conditions.join(', ')}`
    }
    updateUrlParams() {
        const url = new URL(window.location);
        const mapping = { 'search': 'searchKeyword', 'exclude': 'excludeKeyword', 'score': 'searchScore', 'size': 'searchSize', 'sort_by': 'sortBy' };
        for (let [k, stateKey] of Object.entries(mapping)) {
            if (this.state[stateKey] && this.state[stateKey] !== 0) url.searchParams.set(k, this.state[stateKey]);
            else url.searchParams.delete(k);
        }
        window.history.pushState({}, '', url);
    }
    loadFromUrlParams() {
        const urlParams = new URLSearchParams(window.location.search);
        let hasParams = false;
        if (urlParams.get('search')) { this.refs.searchKeyword.value = this.state.searchKeyword = urlParams.get('search'); hasParams = true; }
        if (urlParams.get('exclude')) { this.refs.excludeKeyword.value = this.state.excludeKeyword = urlParams.get('exclude'); hasParams = true; }
        if (urlParams.get('score')) { this.refs.searchScore.value = this.state.searchScore = parseInt(urlParams.get('score')); hasParams = true; }
        if (urlParams.get('sort_by')) { this.refs.sortBy.value = this.state.sortBy = urlParams.get('sort_by'); hasParams = true; }
        if (urlParams.get('size')) {
            const sizeParam = urlParams.get('size');
            if (sizeParam.includes(':')) {
                const [op, val] = sizeParam.split(':');
                this.refs.sizeOperator.value = op; this.refs.sizeValue.value = parseInt(val); this.state.searchSize = sizeParam;
            } else {
                this.refs.sizeOperator.value = 'lte'; this.refs.sizeValue.value = parseInt(sizeParam); this.state.searchSize = `lte:${sizeParam}`;
            }
            hasParams = true;
        }
        return hasParams;
    }
    clearSearch() {
        this.refs.searchKeyword.value = this.state.searchKeyword = ''; this.refs.excludeKeyword.value = this.state.excludeKeyword = '';
        this.refs.searchScore.value = this.state.searchScore = 0; this.refs.sortBy.value = this.state.sortBy = '';
        this.refs.sizeOperator.value = 'lte'; this.refs.sizeValue.value = '';
        this.state.searchSize = 0; this.state.page = 1; this.refs.currentIndex.value = 0;
        const url = new URL(window.location);
        ['search', 'exclude', 'score', 'size', 'sort_by'].forEach(k => url.searchParams.delete(k));
        window.history.pushState({}, '', url);
    }
    setLoadVideosCallback(cb) { this.loadVideosCallback = cb; }
    initPopstateListener() {
        window.addEventListener('popstate', () => {
            if (this.loadFromUrlParams()) this.doSearch();
            else { this.clearSearch(); this.loadVideosCallback(false, 0); }
        })
    }
}

class ConfigManager {
    constructor(refs) { this.refs = refs; }
    async fetchPaths() { const resp = await axios.get('/dyfn/video-paths'); this.refs.pathList.value = resp.data.paths || []; }
}