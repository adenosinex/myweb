// dy_main.js

// ================= API 请求封装 =================
async function fetchLatestVideos(state) {
    const params = { 
        latest: state.pageSize,
        page: state.page, 
        page_size: state.pageSize, 
        search: state.searchKeyword,
        score: state.searchScore
    }
    if (state.searchSize && state.searchSize !== 0) {
        params.size = state.searchSize
    }
    const res = await axios.get('/dy/videos', { params })
    return res.data
}

function getVideoUrl(id) {
    return `/dy/videos/${id}/stream`
}

async function updateScore(videoId, score) {
    await axios.post('/dy/videos/update_score', {
        id: videoId,
        score: score  
    })
}

async function searchVideos({ keyword = '', score = 0, size = 0, page = 1 }) {
    const params = { page }
    if (keyword) params.search = keyword
    if (score) params.score = score
    if (size) params.size = size
    const res = await axios.get('/dy/videos', { params })
    return res.data
}

async function getTotalVideos(pageSize, searchKeyword = '', searchScore = 0, searchSize = 0) {
    const params = []
    if (searchKeyword) params.push(`search=${encodeURIComponent(searchKeyword)}`)
    if (searchScore && searchScore > 0) params.push(`score=${searchScore}`)
    if (searchSize && searchSize > 0) params.push(`size=${searchSize}`)
    const url = `/dy/videos/count${params.length ? '?' + params.join('&') : ''}`
    const resp = await axios.get(url)
    return resp.data.total || 0
}

// ================= 核心业务类 =================
class VideoPlayer {
    constructor(state, refs) {
        this.state = state;
        this.refs = refs;
        this.touchStartY = 0;
        this.touchActive = false;
        this.lastRatios = [];
        this.isSwitching = false;
    }
    initializeVideoQueue() {
        this.refreshQueue();
    }
    forceStopOthers(activeIndex) {
        const videos = document.querySelectorAll('video');
        videos.forEach((video, index) => {
            if (index !== activeIndex) {
                video.pause();
                video.muted = true;
                video.removeAttribute('src');
                video.load();
                video.removeAttribute('data-class-updated');
            }
        });
    }
    updateVideoClass(videoEl = null) {
        if (!videoEl) {
            const videos = document.querySelectorAll('video');
            const domIndex = this.refs.activeVideoIndex.value;
            if (!videos.length || !videos[domIndex]) return;
            videoEl = videos[domIndex];
        }
        if (!videoEl) return;
        const width = videoEl.videoWidth;
        const height = videoEl.videoHeight;
        if (width && height) {
            const ratio = height / width;
            const isLandscape = ratio <= 1.1;
            this.lastRatios.push(isLandscape);
            if (this.lastRatios.length > 3) this.lastRatios.shift();
            if (this.lastRatios.length === 3 && this.lastRatios.every(v => v)) {
                this.refs.videoClass.value = 'video-landscape';
            } else if (!isLandscape) {
                this.refs.videoClass.value = 'video-portrait';
            } else {
                this.refs.videoClass.value = 'video-landscape';
            }
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
            if (this.state.videos[targetIndex]) {
                this.refs.videoQueue.value.push(this.state.videos[targetIndex]);
            }
        }
        const videos = document.querySelectorAll('video');
        videos.forEach(v => { v.pause(); v.muted = true; });
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
        const videos = document.querySelectorAll('video');
        videos.forEach(v => v.pause());
        this.refs.currentIndex.value--;
        if (this.refs.currentIndex.value < 0) {
            this.refs.currentIndex.value = this.state.videos.length - 1;
        }
        this.updateVideoQueue('prev');
        setTimeout(() => {
            this.playActiveVideo();
            this.isSwitching = false;
        }, 200);
    }
    async nextVideo() {
        if (this.isSwitching || this.state.videos.length === 0) return;
        this.isSwitching = true;
        try {
            const videos = document.querySelectorAll('video');
            videos.forEach(v => v.pause());
            this.exitFullscreen();
            let nextIndex = this.refs.currentIndex.value + 1;
            let isPageChange = false;
            if (nextIndex >= this.state.videos.length) {
                const loadedCount = this.state.page * this.state.pageSize;
                if (loadedCount < this.state.totalVideos && typeof this.loadMoreVideos === 'function') {
                    this.state.page++;
                    await this.loadMoreVideos();
                    isPageChange = true;
                }
                nextIndex = nextIndex % this.state.videos.length;
            }
            this.refs.currentIndex.value = nextIndex;
            if (isPageChange) {
                this.refreshQueue();
            } else {
                this.updateVideoQueue('next');
            }
            setTimeout(() => {
                this.playActiveVideo();
                this.isSwitching = false;
            }, 250);
        } catch (e) {
            console.error('切换异常:', e);
            this.isSwitching = false;
        }
    }
    playActiveVideo() {
        const domIndex = this.refs.activeVideoIndex.value;
        const videos = document.querySelectorAll('video');
        if (!videos.length || !videos[domIndex]) return;
        videos.forEach((v, i) => {
            if (i !== domIndex) {
                v.pause();
                v.muted = true;
                v.currentTime = 0;
            }
        });
        const activeVideo = videos[domIndex];
        activeVideo.muted = false;
        if (activeVideo.ended) activeVideo.currentTime = 0;
        const playPromise = activeVideo.play();
        if (playPromise !== undefined) {
            playPromise.then(() => {}).catch(() => {
                activeVideo.muted = true;
                activeVideo.play().catch(() => {});
            });
        }
        this.checkAndSetVideoClass(activeVideo);
    }
    checkAndSetVideoClass(video) {
        if (!video.hasAttribute('data-class-updated')) {
            const updateClass = () => {
                this.updateVideoClass(video);
                video.setAttribute('data-class-updated', 'true');
            };
            if (video.readyState >= 1) {
                updateClass();
            } else {
                video.addEventListener('loadedmetadata', updateClass, { once: true });
            }
        }
    }
    autoPlayVideo() {
        setTimeout(() => {
            if (this.refs.videoQueue.value.length === 0) {
                this.refreshQueue();
            }
            this.playActiveVideo();
        }, 500);
    }
    onVideoReady(index) {
        this.refs.videoLoadStates.value[index] = true;
        const domIndex = this.refs.activeVideoIndex.value;
        const videos = document.querySelectorAll('video');
        const video = videos[index];
        if (!video) return;
        if (index === domIndex) {
            if (video.paused) {
                video.play().catch(() => {});
            }
        } else {
            video.pause();
            video.muted = true;
        }
    }
    onVideoLoaded(index) {
        this.refs.videoLoadStates.value[index] = true;
    }
    handleTouchStart(e) {
        if (this.isSwitching) return;
        this.touchActive = true;
        this.touchStartY = e.touches[0].clientY;
    }
    handleTouchEnd(e) {
        if (!this.touchActive) return;
        const deltaY = e.changedTouches[0].clientY - this.touchStartY;
        if (Math.abs(deltaY) > 60) {
            if (deltaY < 0) this.nextVideo();
            else this.prevVideo();
        }
        this.touchActive = false;
    }
    getCurrentlyPlayingVideo() {
        const videos = document.querySelectorAll('video');
        const domIndex = this.refs.activeVideoIndex.value;
        if (domIndex < videos.length) return videos[domIndex];
        return null;
    }
    setLoadMoreVideos(callback) {
        this.loadMoreVideos = callback;
    }
    exitFullscreen() {
        if (document.fullscreenElement) {
            document.exitFullscreen().catch(() => {});
        }
    }
    getActiveVideoVolume() {
        const video = this.getCurrentlyPlayingVideo();
        return video ? video.volume : 1;
    }
    setActiveVideoVolume(val) {
        const videos = document.querySelectorAll('video');
        videos.forEach(v => v.volume = val);
    }
    previewForward() {
        const video = this.getCurrentlyPlayingVideo();
        if(video) video.currentTime += 5;
    }
    previewBackward() {
         const video = this.getCurrentlyPlayingVideo();
        if(video) video.currentTime -= 5;
    }
    initEventListeners() {
        window.addEventListener('wheel', (e) => {
            if (this.isSwitching) return;
            if (Math.abs(e.deltaY) < 30) return;
            if (e.deltaY > 0) this.nextVideo();
            else if (e.deltaY < 0) this.prevVideo();
        });
        window.addEventListener('keydown', (e) => {
            if (this.isSwitching) return;
            if (e.key === 'ArrowDown') this.nextVideo();
            else if (e.key === 'ArrowUp') this.prevVideo();
        });
        window.addEventListener('dblclick', (e) => {
             if (e.target.tagName === 'BUTTON' || e.target.closest('button')) return;
            const videoElement = this.getCurrentlyPlayingVideo();
            if (!document.fullscreenElement && videoElement) {
                if (videoElement.requestFullscreen) {
                    videoElement.requestFullscreen().catch(() => {});
                }
            } else if (document.fullscreenElement) {
                 document.exitFullscreen().catch(() => {});
            }
        });
    }
}

class SearchManager {
    constructor(state, refs) {
        this.state = state
        this.refs = refs
    }
    async doSearch() {
        const start = performance.now()
        this.state.searchKeyword = this.refs.searchKeyword.value.trim()
        this.state.searchScore = this.refs.searchScore.value
        if (this.refs.sizeValue.value && this.refs.sizeValue.value > 0) {
            this.state.searchSize = `${this.refs.sizeOperator.value}:${this.refs.sizeValue.value}`
        } else {
            this.state.searchSize = 0
        }
        this.state.page = 1
        this.refs.currentIndex.value = 0
        this.refs.showSearch.value = false
        const total = await getTotalVideos(this.state.pageSize, this.state.searchKeyword, this.state.searchScore, this.state.searchSize)
        this.state.totalVideos = total
        this.state.totalpage = total
        this.updateUrlParams()
        this.showSearchHint()
        await this.loadVideosCallback(false)
        const ms = performance.now() - start
        this.refs.searchTimeMsg.value = '搜索完成，用时 ' + (ms / 1000).toFixed(2) + ' 秒'
        setTimeout(() => {
            this.refs.searchTimeMsg.value = ''
        }, 3000)
    }
    showSearchHint() {
        const conditions = []
        if (this.state.searchKeyword) {
            conditions.push(`关键词: "${this.state.searchKeyword}"`)
        }
        if (this.state.searchScore > 0) {
            conditions.push(`评分: ${this.state.searchScore}分`)
        }
        if (this.state.searchSize && this.state.searchSize !== 0) {
            const [operator, value] = this.state.searchSize.split(':')
            const operatorText = operator === 'lte' ? '≤' : operator === 'gte' ? '≥' : '='
            conditions.push(`大小: ${operatorText}${value}MB`)
        }
        if (conditions.length > 0) {
            this.refs.searchTimeMsg.value = `应用筛选条件: ${conditions.join(', ')}`
        }
    }
    async autoApplySearch() {
        const hasChanges = this.hasSearchChanges()
        if (hasChanges) {
            this.refs.searchTimeMsg.value = '检测到搜索条件变化，正在自动应用...'
            await this.doSearch()
        }
    }
    hasSearchChanges() {
        const currentKeyword = this.refs.searchKeyword.value.trim()
        const currentScore = this.refs.searchScore.value
        const currentSize = this.refs.sizeValue.value && this.refs.sizeValue.value > 0 
            ? `${this.refs.sizeOperator.value}:${this.refs.sizeValue.value}` : 0
        return currentKeyword !== this.state.searchKeyword ||
               currentScore !== this.state.searchScore ||
               currentSize !== this.state.searchSize
    }
    updateUrlParams() {
        const url = new URL(window.location)
        if (this.state.searchKeyword) {
            url.searchParams.set('search', this.state.searchKeyword)
        } else {
            url.searchParams.delete('search')
        }
        if (this.state.searchScore && this.state.searchScore > 0) {
            url.searchParams.set('score', this.state.searchScore)
        } else {
            url.searchParams.delete('score')
        }
        if (this.state.searchSize && this.state.searchSize !== 0) {
            url.searchParams.set('size', this.state.searchSize)
        } else {
            url.searchParams.delete('size')
        }
        window.history.pushState({}, '', url)
    }
    loadFromUrlParams() {
        const urlParams = new URLSearchParams(window.location.search)
        const searchParam = urlParams.get('search')
        const scoreParam = urlParams.get('score')
        const sizeParam = urlParams.get('size')
        if (searchParam) {
            this.refs.searchKeyword.value = searchParam
            this.state.searchKeyword = searchParam
        }
        if (scoreParam) {
            const score = parseInt(scoreParam)
            if (!isNaN(score) && score > 0) {
                this.refs.searchScore.value = score
                this.state.searchScore = score
            }
        }
        if (sizeParam) {
            if (sizeParam.includes(':')) {
                const [operator, value] = sizeParam.split(':')
                const sizeValue = parseInt(value)
                if (!isNaN(sizeValue) && sizeValue > 0) {
                    this.refs.sizeOperator.value = operator
                    this.refs.sizeValue.value = sizeValue
                    this.state.searchSize = sizeParam
                }
            } else {
                const size = parseInt(sizeParam)
                if (!isNaN(size) && size > 0) {
                    this.refs.sizeOperator.value = 'lte'
                    this.refs.sizeValue.value = size
                    this.state.searchSize = `lte:${size}`
                }
            }
        }
        if (searchParam || scoreParam || sizeParam) {
            return true
        }
        return false
    }
    clearSearch() {
        this.refs.searchKeyword.value = ''
        this.refs.searchScore.value = 0
        this.refs.sizeOperator.value = 'lte'
        this.refs.sizeValue.value = ''
        this.state.searchKeyword = ''
        this.state.searchScore = 0
        this.state.searchSize = 0
        this.state.page = 1
        this.refs.currentIndex.value = 0
        const url = new URL(window.location)
        url.searchParams.delete('search')
        url.searchParams.delete('score')
        url.searchParams.delete('size')
        window.history.pushState({}, '', url)
    }
    setLoadVideosCallback(callback) {
        this.loadVideosCallback = callback
    }
    initPopstateListener() {
        window.addEventListener('popstate', () => {
            const shouldSearch = this.loadFromUrlParams()
            if (shouldSearch) {
                this.doSearch()
            } else {
                this.clearSearch()
                this.loadVideosCallback(false)
            }
        })
    }
    async getSearchSuggestions(keyword) {
        return []
    }
    saveSearchHistory(keyword) {
        if (!keyword.trim()) return
        const history = this.getSearchHistory()
        const newHistory = [keyword, ...history.filter(item => item !== keyword)].slice(0, 10)
        localStorage.setItem('searchHistory', JSON.stringify(newHistory))
    }
    getSearchHistory() {
        try {
            return JSON.parse(localStorage.getItem('searchHistory') || '[]')
        } catch {
            return []
        }
    }
}

class ConfigManager {
    constructor(refs) {
        this.refs = refs
    }
    async fetchPaths() {
        const resp = await axios.get('/dy/video-paths')
        this.refs.pathList.value = resp.data.paths || []
    }
    async addPath() {
        if (!this.refs.newPath.value.trim()) return
        await axios.post('/dy/video-paths', { path: this.refs.newPath.value.trim() })
        this.refs.newPath.value = ''
        this.fetchPaths()
    }
    async indexPath(path) {
        const start = performance.now()
        await axios.post('/dy/video-paths/index?all=1', { path })
        const ms = Math.round(performance.now() - start)
        alert('索引完成，用时 ' + (ms / 1000).toFixed(2) + ' 秒')
    }
    async indexPathq( ) {
        const start = performance.now()
        await axios.get('/dy/video-paths/updatefile' )
        const ms = Math.round(performance.now() - start)
        alert('更新完成，用时 ' + (ms / 1000).toFixed(2) + ' 秒')
    }
    async indexPath_del(path) {
        const start = performance.now()
        await axios.post('/dy/video-paths/index?del=1', { path })
        const ms = Math.round(performance.now() - start)
        alert('索引完成，用时 ' + (ms / 1000).toFixed(2) + ' 秒')
    }
    async deletePath(path) {
        if (!confirm(`确定要删除路径 "${path}" 吗？`)) return
        try {
            await axios.delete('/dy/video-paths', { data: { path } })
            this.fetchPaths()
        } catch (error) {
            console.error('删除路径失败:', error)
            alert('删除路径失败')
        }
    }
    async getSystemConfig() {
        try {
            const resp = await axios.get('/dy/config')
            return resp.data
        } catch (error) {
            return null
        }
    }
}

class UrlParamsManager {
    constructor() {
        this.params = new URLSearchParams(window.location.search)
    }
    getParam(key) {
        return this.params.get(key)
    }
    setParam(key, value) {
        if (value === null || value === undefined || value === '') {
            this.params.delete(key)
        } else {
            this.params.set(key, value)
        }
        this.updateUrl()
    }
    deleteParam(key) {
        this.params.delete(key)
        this.updateUrl()
    }
    setParams(paramsObj) {
        Object.entries(paramsObj).forEach(([key, value]) => {
            if (value === null || value === undefined || value === '') {
                this.params.delete(key)
            } else {
                this.params.set(key, value)
            }
        })
        this.updateUrl()
    }
    getAllParams() {
        const result = {}
        for (const [key, value] of this.params) {
            result[key] = value
        }
        return result
    }
    clearAllParams() {
        this.params = new URLSearchParams()
        this.updateUrl()
    }
    updateUrl() {
        const url = new URL(window.location)
        url.search = this.params.toString()
        window.history.pushState({}, '', url)
    }
}