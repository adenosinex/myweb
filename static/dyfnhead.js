// header-panel.js
const HeaderPanel = {
    template: `
        <div>
            <teleport to=".video-side" v-if="isMounted">
                <div class="video-overlay-header">
                    <div class="video-title-clickable" @click="showDetail = true" title="点击查看详情">
                        {{ videoIndex ? videoIndex + '. ' : '' }}/{{ totalpage ?? 0 }}  {{ currentVideo?.filename }}
                    </div>
                    
                    <div class="status-controls">
                        <div style="display:flex; align-items:center; gap:6px; margin-right: 8px; padding-right: 8px; border-right: 1px solid rgba(255,255,255,0.2);">
                            <label style="display:flex; align-items:center; gap:4px; cursor:pointer; color:#e6a23c; font-size:12px; user-select:none;">
                                <input type="checkbox" :checked="syncEnabled" @change="$emit('toggle-sync', $event.target.checked)" /> 同源同步
                            </label>
                            <span v-if="syncEnabled" @click.stop="$emit('clear-sync')" title="清空同步缓存" style="color:#ff4d4f; font-size:11px; cursor:pointer; padding:2px 6px; background:rgba(255,77,79,0.15); border-radius:4px;">
                                清空({{ syncCount }})
                            </span>
                        </div>

                        <van-button type="default" size="mini" @click="$emit('restore-name')" class="restore-btn" title="取消所有修改，恢复初始名">恢复原名</van-button>
                        
                        <div class="save-status-indicator">
                            <transition name="van-fade">
                                <span v-if="saveStatus === 'pending'" class="status-pending">待保存...</span>
                            </transition>
                            <transition name="van-fade">
                                <span v-if="saveStatus === 'success'" class="status-success">✔</span>
                            </transition>
                        </div>
                    </div>
                </div>
            </teleport>

            <van-popup v-model:show="showDetail" position="bottom" round>
                <div style="padding:20px;">
                    <h4>视频详情</h4>
                    <p style="color: #ccc;">文件名: <span v-for="keyword in currentVideo?.filename?.split(' ')" :key="keyword" @click="searchDouyin(keyword)" style="cursor:pointer;color:#1989fa;margin-right:5px;">{{ keyword }}</span></p>
                    <p style="color: #ccc;">文件路径: <span style="font-size:12px;color:#888;word-break:break-all;">{{ currentVideo?.detail }}</span></p>
                    <p style="color: #ccc; display: flex; align-items: center; gap: 10px;">评分: <van-rate v-model="localScore" @change="$emit('update-score', localScore)" /></p>
                    <div style="display:flex; gap: 10px; margin-top: 20px;">
                        <van-button type="success" style="flex:1;" block :url="getVideoUrl(currentVideo?.id)" download>下载到本地</van-button>
                    </div>
                </div>
            </van-popup>

            <div class="panel-header">
                <van-button type="primary" icon="search" size="small" @click="showSearch = true">搜索</van-button>
                <div style="display:flex; gap:6px;">
                    <van-button type="success" icon="bar-chart-o" size="small" @click="$emit('open-stats')">统计</van-button>
                    <van-button type="warning" icon="clock-o" size="small" @click="$emit('open-history')">日志</van-button>
                    <van-button type="info" icon="clipboard" size="small" @click="searchFromClipboard">剪贴板</van-button>
                    <van-button type="default" icon="setting" size="small" @click="$emit('open-config')">配置</van-button>
                    <van-button type="default" icon="replay" size="small" @click="$emit('refresh-all')">更新</van-button>
                </div>
            </div>

            <div style="padding: 15px 15px 0 15px;">
                <div style="display: flex; gap: 8px; margin-bottom: 12px; align-items: center; background: rgba(0,0,0,0.3); padding: 8px 12px; border-radius: 6px; border: 1px solid #333;">
                    <span style="font-size: 13px; color: #888;">进度定位:</span>
                    <input type="number" v-model="localJumpTarget" placeholder="序号" style="width: 70px; background: rgba(255,255,255,0.05); border: 1px solid #444; color: #fff; padding: 4px 6px; border-radius: 4px; text-align: center; font-size: 13px; outline: none;" @keyup.enter="handleJump" />
                    <van-button type="primary" size="small" @click="handleJump" style="padding: 0 12px;">跳转</van-button>
                    <span style="font-size: 12px; color: #666; margin-left: auto;">当前: {{ videoIndex }} / {{ totalpage }}</span>
                </div>

                <div class="main-action-btns" style="display: flex; gap: 8px;">
                    <van-button class="icon-btn" id="dislike-button" title="不喜欢 (快捷键 X)" type="default" :class="{ 'btn-active': lastLikeType === 'dislike' }" @click="$emit('action', 'dislike')">
                        <span v-if="currentVideoScore === 1" style="color:#222;">&#x1F5A4;</span>
                        <span v-else style="color:#888;">&#x1F5A4;</span>
                    </van-button>
                    <van-button type="primary" @click="$emit('action', 'prev')" style="flex:1;">上一个</van-button>
                    <van-button type="primary" @click="$emit('action', 'next')" style="flex:1;">下一个</van-button>
                    <van-button class="icon-btn" id="like-button" title="喜欢 (快捷键 Z)" type="default" :class="{ 'btn-active': lastLikeType === 'like' }" @click="$emit('action', 'like')">
                        <span v-if="currentVideoScore === 5" style="color:#ff3b30;">&#x2764;</span>
                        <span v-else-if="currentVideoScore === 1" style="color:#170201;">&#x2764;</span>
                        <span v-else style="color:#888;">&#x2764;</span>
                    </van-button>
                </div>
            </div>

            <div v-if="showSearch" class="search-modal" @click.self="showSearch=false">
                <div class="search-modal-content">
                    <h4>搜索与筛选</h4>
                    <div style="margin-bottom:15px;">
                        <input type="text" v-model="searchKeyword" placeholder="包含关键词 (多词空格分隔)" class="search-input" @keyup.enter="doSearch" />
                        <input type="text" v-model="excludeKeyword" placeholder="排除关键词 (多词空格分隔)" class="search-input" @keyup.enter="doSearch" style="margin-top:8px;" />
                        <div style="text-align:right; margin-top:6px;"><a href="/?search=random" style="font-size:12px;color:#1989fa;text-decoration:none;">↻ 使用 random 随机打乱</a></div>
                    </div>
                    <div style="margin:15px 0;">
                        <span style="font-size: 14px; color: #ccc;">排序方式：</span>
                        <select v-model="sortBy" class="search-select" style="margin-top: 5px;">
                            <option value="">默认 (入库倒序)</option>
                            <option value="filename">纯文件名正序 (A-Z)</option>
                            <option value="path">文件路径正序 (按目录结构)</option>
                        </select>
                    </div>
                    <div style="margin:15px 0;">
                        <span style="font-size: 14px; color: #ccc;">评分筛选：</span>
                        <select v-model="searchScore" class="search-select" style="margin-top: 5px;">
                            <option value="0">不限 (自动过滤1星)</option>
                            <option value="1">只看1星 (不喜欢)</option>
                            <option value="2">2分及以上</option>
                            <option value="3">3分及以上</option>
                            <option value="4">4分及以上</option>
                            <option value="5">仅5分</option>
                        </select>
                    </div>
                    <div style="margin:15px 0;">
                        <span style="font-size: 14px; color: #ccc;">文件大小筛选：</span>
                        <div style="display: flex; gap: 8px; margin-top:5px;">
                            <select v-model="sizeOperator" class="search-select" style="width:auto; flex: 0 0 80px;">
                                <option value="lte">≤</option>
                                <option value="gte">≥</option>
                                <option value="eq">=</option>
                            </select>
                            <div style="display: flex; align-items: center; background: #0a0a0a; border: 1px solid #444; border-radius: 6px; flex: 1;">
                                <input type="number" v-model="sizeValue" placeholder="大小" class="search-input" style="border:none !important; flex:1;" min="1" />
                                <span style="color:#888; padding-right:12px; font-size: 14px;">MB</span>
                            </div>
                        </div>
                    </div>
                    <div style="display: flex; gap: 10px; margin-top: 20px;">
                        <button class="search-btn-confirm" style="flex:1;" @click="doSearch">确认查询</button>
                        <button class="search-btn-cancel" style="flex:1; margin-top:0;" @click="showSearch=false">取消</button>
                    </div>
                </div>
            </div>

            <component is="style">
                .video-overlay-header {
                    position: absolute;
                    top: 15px; 
                    left: 15px;
                    right: 15px;
                    z-index: 100;
                    display: flex;
                    align-items: flex-start;
                    gap: 12px;
                    pointer-events: none;
                }
                .video-title-clickable {
                    background: rgba(0, 0, 0, 0.6);
                    color: #fff;
                    padding: 8px 12px;
                    border-radius: 6px;
                    font-size: 14px;
                    max-width: 60%;
                    pointer-events: auto;
                    backdrop-filter: blur(4px);
                    border: 1px solid rgba(255,255,255,0.1);
                    line-height: 1.4;
                    word-break: break-all;
                }
                .video-title-clickable:hover {
                    background: rgba(0, 0, 0, 0.8);
                    border-color: rgba(255,255,255,0.3);
                }
                .status-controls {
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    background: rgba(0, 0, 0, 0.6);
                    padding: 6px 10px;
                    border-radius: 20px;
                    pointer-events: auto;
                    backdrop-filter: blur(4px);
                    border: 1px solid rgba(255,255,255,0.1);
                    height: 24px;
                    margin-top: 4px;
                }
                .restore-btn {
                    padding: 0 8px;
                    height: 22px;
                    line-height: 20px;
                    color: #ccc;
                    background: rgba(255,255,255,0.1);
                    border: 1px solid #555;
                    border-radius: 4px;
                }
                .restore-btn:active { background: rgba(255,255,255,0.2); }
                .save-status-indicator {
                    width: 50px;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                }
                .status-pending { color: #e6a23c; font-size: 12px; font-weight: bold; }
                .status-success { color: #4fc08d; font-size: 16px; font-weight: bold; }
            </component>
        </div>
    `,
    props: ['videoIndex', 'totalpage', 'currentVideoScore', 'lastLikeType', 'state', 'currentVideo', 'saveStatus', 'syncEnabled', 'syncCount'],
    // 🌟 这里也确保补回了 'open-stats' 事件声明 🌟
    emits: ['action', 'jump', 'open-history', 'open-config', 'open-stats', 'refresh-all', 'trigger-search', 'update-score', 'restore-name', 'toggle-sync', 'clear-sync'],
    setup(props, { emit, expose }) {
        const { ref, computed, onMounted } = Vue;
        const localJumpTarget = ref('');
        const showSearch = ref(false);
        const showDetail = ref(false);
        const isMounted = ref(false);

        onMounted(() => { isMounted.value = true; });
        
        const searchKeyword = ref(''), excludeKeyword = ref(''), searchScore = ref(0), sortBy = ref('');
        const sizeOperator = ref('lte'), sizeValue = ref('');

        const localScore = computed({
            get: () => props.currentVideoScore || 0,
            set: (val) => emit('update-score', val)
        });

        function getVideoUrl(id) { return id ? `/dyfn/videos/${id}/stream` : ''; }
        function searchDouyin(kw) { window.open(`https://www.douyin.com/search/${encodeURIComponent(kw)}`, '_blank'); }

        function handleJump() {
            if(localJumpTarget.value) { emit('jump', parseInt(localJumpTarget.value)); localJumpTarget.value = ''; }
        }

        async function doSearch() {
            props.state.searchKeyword = searchKeyword.value.trim();
            props.state.excludeKeyword = excludeKeyword.value.trim();
            props.state.searchScore = searchScore.value;
            props.state.sortBy = sortBy.value;
            props.state.searchSize = (sizeValue.value && sizeValue.value > 0) ? `${sizeOperator.value}:${sizeValue.value}` : 0;
            props.state.page = 1; 
            showSearch.value = false;

            props.state.totalVideos = props.state.totalpage = await window.DyAPI.fetchVideoCount({ 
                search: props.state.searchKeyword, exclude: props.state.excludeKeyword, score: props.state.searchScore, size: props.state.searchSize 
            });
            
            const url = new URL(window.location);
            const mapping = { 'search': 'searchKeyword', 'exclude': 'excludeKeyword', 'score': 'searchScore', 'size': 'searchSize', 'sort_by': 'sortBy' };
            for (let [k, stateKey] of Object.entries(mapping)) { if (props.state[stateKey] && props.state[stateKey] !== 0) url.searchParams.set(k, props.state[stateKey]); else url.searchParams.delete(k); }
            window.history.pushState({}, '', url);
            
            emit('trigger-search');
        }

        function loadFromUrlParams() {
            const urlParams = new URLSearchParams(window.location.search);
            let hasParams = false;
            if (urlParams.get('search')) { searchKeyword.value = props.state.searchKeyword = urlParams.get('search'); hasParams = true; }
            if (urlParams.get('exclude')) { excludeKeyword.value = props.state.excludeKeyword = urlParams.get('exclude'); hasParams = true; }
            if (urlParams.get('score')) { searchScore.value = props.state.searchScore = parseInt(urlParams.get('score')); hasParams = true; }
            if (urlParams.get('sort_by')) { sortBy.value = props.state.sortBy = urlParams.get('sort_by'); hasParams = true; }
            if (urlParams.get('size')) {
                const sizeParam = urlParams.get('size');
                if (sizeParam.includes(':')) { const [op, val] = sizeParam.split(':'); sizeOperator.value = op; sizeValue.value = parseInt(val); props.state.searchSize = sizeParam; } 
                else { sizeOperator.value = 'lte'; sizeValue.value = parseInt(sizeParam); props.state.searchSize = `lte:${sizeParam}`; }
                hasParams = true;
            }
            return hasParams;
        }

        function clearSearch() {
            searchKeyword.value = props.state.searchKeyword = ''; excludeKeyword.value = props.state.excludeKeyword = '';
            searchScore.value = props.state.searchScore = 0; sortBy.value = props.state.sortBy = '';
            sizeOperator.value = 'lte'; sizeValue.value = ''; props.state.searchSize = 0; props.state.page = 1;
        }

        const searchFromClipboard = async () => {
            if (!navigator.clipboard || !window.isSecureContext) { alert('当前环境不支持或未授权 HTTPS'); return; }
            try { const text = await navigator.clipboard.readText(); if (!text.trim()) { alert('剪贴板为空'); return; } searchKeyword.value = text.trim(); showSearch.value = false; await doSearch(); } catch (err) { alert('读取失败，检查权限'); }
        };

        expose({ loadFromUrlParams, clearSearch, doSearch }); 

        return { localJumpTarget, showSearch, showDetail, isMounted, localScore, searchKeyword, excludeKeyword, searchScore, sortBy, sizeOperator, sizeValue, handleJump, doSearch, searchFromClipboard, getVideoUrl, searchDouyin };
    }
};