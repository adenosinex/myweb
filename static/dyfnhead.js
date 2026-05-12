// dyfnhead.js (或 header-panel.js)
const HeaderPanel = {
    template: `
        <div>
            <teleport to=".video-side" v-if="isMounted">
                <div class="video-overlay-header">
                    
                    <div class="status-controls-row">
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
                            
                            <div style="width: 1px; height: 12px; background: rgba(255,255,255,0.3); margin: 0 4px;"></div>
                            
                            <van-button type="default" size="mini" icon="keyboard-o" class="restore-btn" @click="openKeyBind" title="设置按键绑定Tag">快捷键</van-button>
                            
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

                    <div class="title-row-container">
                        <div class="title-row-main">
                            <van-button size="small" type="primary" class="author-btn" @click="copyAuthorName" title="复制文件名空格前第一个词">@作者</van-button>
                            
                            <div class="video-title-clickable" @click="showDetail = true" title="点击查看详情">
                               {{ videoIndex ? videoIndex + '. ' : '' }}/{{ totalpage ?? 0 }}  {{ currentVideo?.filename }}
                            </div>
                        </div>
                    </div>

                </div>
            </teleport>

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

            <teleport to=".panel-side" v-if="isMounted">
                <div style="flex-shrink: 0; padding: 15px; background: #141414; border-top: 1px solid #333; z-index: 10;">
                    <div style="display: flex; gap: 8px; margin-bottom: 12px; align-items: center; background: rgba(0,0,0,0.3); padding: 8px 12px; border-radius: 6px; border: 1px solid #333;">
                        <span style="font-size: 13px; color: #888;">进度定位:</span>
                        <input type="number" v-model="localJumpTarget" placeholder="序号" style="width: 70px; background: rgba(255,255,255,0.05); border: 1px solid #444; color: #fff; padding: 4px 6px; border-radius: 4px; text-align: center; font-size: 13px; outline: none;" @keyup.enter="handleJump" />
                        <van-button type="primary" size="small" @click="handleJump" style="padding: 0 12px;">跳转</van-button>
                        <span style="font-size: 12px; color: #666; margin-left: auto;">当前: {{ videoIndex }} / {{ totalpage }}</span>
                    </div>

                    <div class="main-action-btns" style="display: flex; gap: 8px;">
                        <van-button class="icon-btn" id="dislike-button" type="default" :class="{ 'btn-active': lastLikeType === 'dislike' }" @click="$emit('action', 'dislike')">
                            <span v-if="currentVideoScore === 1" style="color:#222;">🖤</span>
                            <span v-else style="color:#888;">🖤</span>
                        </van-button>
                        <van-button type="primary" @click="$emit('action', 'prev')" style="flex:1;">上一个</van-button>
                        <van-button type="primary" @click="$emit('action', 'next')" style="flex:1;">下一个</van-button>
                        <van-button class="icon-btn" id="like-button" type="default" :class="{ 'btn-active': lastLikeType === 'like' }" @click="$emit('action', 'like')">
                            <span v-if="currentVideoScore === 5" style="color:#ff3b30;">❤</span>
                            <span v-else-if="currentVideoScore === 1" style="color:#170201;">❤</span>
                            <span v-else style="color:#888;">❤</span>
                        </van-button>
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

            <div v-if="showSearch" class="search-modal" @click.self="showSearch=false">
                <div class="search-modal-content">
                    <h4>搜索与筛选</h4>
                    <div style="margin-bottom:15px;">
                        <input type="text" v-model="searchKeyword" placeholder="包含关键词 (多词空格分隔)" class="search-input" @keyup.enter="doSearch" />
                        <input type="text" v-model="excludeKeyword" placeholder="排除关键词 (多词空格分隔)" class="search-input" @keyup.enter="doSearch" style="margin-top:8px;" />
                        <div style="text-align:right; margin-top:6px;"><a href="/?search=random" style="font-size:12px;color:#1989fa;text-decoration:none;">↻ 使用 random 随机打乱</a></div>
                    </div>
                    
                    <div style="margin:15px 0;">
                        <span style="font-size: 14px; color: #ccc;">打标状态：</span>
                        <select v-model="searchUntagged" class="search-select" style="margin-top: 5px; color: #e6a23c !important;">
                            <option value="0">全部文件</option>
                            <option value="1">仅看未打标 (无用户Tag)</option>
                            <option value="2">仅看已打标 (含用户Tag)</option>
                        </select>
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

            <div v-if="showKeyBind" class="search-modal" @click.self="closeKeyBind">
                <div class="search-modal-content" style="max-width: 600px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom: 1px dashed #444; padding-bottom:12px; margin-bottom:15px;">
                        <h4 style="margin:0; border:none; padding:0;">键盘组合连击映射</h4>
                        <div style="display:flex; gap:8px;">
                            <van-button size="mini" type="warning" plain @click="autoExtractTags">自动提取现有Tag</van-button>
                            <van-button size="mini" type="primary" plain @click="addKeyBindRow">+ 手动新增</van-button>
                        </div>
                    </div>
                    
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 10px; background: rgba(0,0,0,0.3); padding: 8px 12px; border-radius: 6px;">
                        <span style="font-size: 12px; color: #aaa;">连击判定有效时间 (毫秒)：</span>
                        <input type="number" v-model="keyTimeout" style="width: 80px; background: #222; border: 1px solid #444; color: #fff; padding: 4px; border-radius: 4px; text-align: center; font-size: 13px; outline: none;" />
                    </div>
                    <p style="font-size: 11px; color: #666; margin-top:-4px; margin-bottom:12px;">注：如果在判定时间内未输入完整的组合，输入池将清空。建议 600 - 1500 之间。</p>

                    <div style="max-height: 45vh; overflow-y: auto; padding-right: 5px;">
                        <div v-for="(item, idx) in keyBinds" :key="idx" 
                             class="keybind-row" :class="{'has-conflict': isConflict(item.key)}">
                            <div style="flex: 0 0 100px;">
                                <input type="text" v-model="item.key" placeholder="字母组合" class="search-input" style="text-align:center; text-transform:lowercase; font-weight:bold; color:#e6a23c; margin-bottom:0;" />
                            </div>
                            <span style="color:#666;">➡</span>
                            <div style="flex: 1;">
                                <input type="text" v-model="item.tag" placeholder="要绑定的 Tag (不带#)" class="search-input" style="margin-bottom:0;" />
                            </div>
                            <van-button size="small" type="danger" plain @click="removeKeyBindRow(idx)" style="padding:0 12px; border:none;">删</van-button>
                        </div>
                        <div v-if="keyBinds.length === 0" style="text-align:center; color:#666; font-size:13px; margin:20px 0;">暂无映射，请点击右上角提取或添加</div>
                    </div>

                    <div style="display: flex; gap: 10px; margin-top: 20px;">
                        <button class="search-btn-confirm" style="flex:1;" @click="saveKeyBind">保存并生效</button>
                        <button class="search-btn-cancel" style="flex:1; margin-top:0;" @click="closeKeyBind">取消</button>
                    </div>
                </div>
            </div>

            <component is="style">
                .video-overlay-header {
                    position: absolute;
                    top: 20px; 
                    left: 20px; 
                    right: 20px;
                    z-index: 100;
                    display: flex;
                    flex-direction: column;
                    align-items: flex-start;
                    gap: 10px;
                    pointer-events: none;
                }
                
                .status-controls-row {
                    display: flex;
                    justify-content: flex-start;
                    width: 100%;
                }
                .status-controls {
                    display: flex;
                    align-items: center;
                    background: rgba(0, 0, 0, 0.7);
                    padding: 6px 14px;
                    border-radius: 20px;
                    pointer-events: auto;
                    backdrop-filter: blur(6px);
                    border: 1px solid rgba(255,255,255,0.15);
                    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                }
                
                .restore-btn {
                    padding: 2px 10px;
                    color: #ddd;
                    background: rgba(255,255,255,0.1);
                    border: 1px solid #666;
                    border-radius: 4px;
                    font-size: 11px;
                    cursor: pointer;
                    transition: background 0.2s;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                }
                .restore-btn:active { background: rgba(255,255,255,0.3); }

                .title-row-container {
                    display: flex;
                    justify-content: flex-start;
                    width: 100%;
                }
                .title-row-main {
                    display: flex;
                    align-items: stretch; 
                    gap: 8px;
                    pointer-events: none;
                    max-width: 90%;
                }
                
                .author-btn {
                    pointer-events: auto;
                    background: #1989fa;
                    color: #fff;
                    border: none;
                    border-radius: 6px;
                    padding: 0 16px;
                    font-size: 14px;
                    font-weight: bold;
                    cursor: pointer;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
                    transition: opacity 0.2s;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .author-btn:active { opacity: 0.7; }
                
                .video-title-clickable {
                    background: rgba(0, 0, 0, 0.75);
                    color: #fff;
                    padding: 10px 18px;
                    border-radius: 6px;
                    font-size: 17px; 
                    font-weight: bold;
                    pointer-events: auto;
                    backdrop-filter: blur(8px);
                    border: 1px solid rgba(255,255,255,0.15);
                    line-height: 1.4;
                    word-break: break-all;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.4);
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                }
                .video-title-clickable:hover {
                    background: rgba(0, 0, 0, 0.9);
                    border-color: rgba(255,255,255,0.3);
                }
                .index-tag {
                    color: #e6a23c;
                    margin-right: 8px;
                    font-family: monospace;
                    font-size: 16px;
                    white-space: nowrap;
                }

                .keybind-row {
                    display: flex; 
                    gap: 10px; 
                    margin-bottom: 10px; 
                    align-items: center; 
                    background: rgba(255,255,255,0.03); 
                    padding: 8px 12px; 
                    border-radius: 6px; 
                    border: 1px solid #333;
                    transition: all 0.2s;
                }
                .keybind-row.has-conflict {
                    border: 1px solid #ff4d4f !important;
                    box-shadow: 0 0 8px rgba(255,77,79,0.3);
                    background: rgba(255,77,79,0.05);
                }
            </component>
        </div>
    `,
    props: ['videoIndex', 'totalpage', 'currentVideoScore', 'lastLikeType', 'state', 'currentVideo', 'saveStatus', 'syncEnabled', 'syncCount'],
    emits: ['action', 'jump', 'open-history', 'open-config', 'open-stats', 'refresh-all', 'trigger-search', 'update-score', 'restore-name', 'toggle-sync', 'clear-sync'],
    setup(props, { emit, expose }) {
        const { ref, computed, onMounted } = Vue;
        const localJumpTarget = ref('');
        const showSearch = ref(false);
        const showDetail = ref(false);
        const isMounted = ref(false);

        const showKeyBind = ref(false);
        const keyBinds = ref([]);
        const keyTimeout = ref(parseInt(localStorage.getItem('dy_key_timeout')) || 1000);
        let keyBuffer = '';
        let keyTimer = null;

        function initKeyBinds() {
            const saved = localStorage.getItem('dy_key_binds');
            if (saved) {
                try { keyBinds.value = JSON.parse(saved); } catch(e) { keyBinds.value = []; }
            }
            if (keyBinds.value.length === 0) keyBinds.value.push({ key: '', tag: '' });
        }

        const conflictKeys = computed(() => {
            const counts = {};
            const conflicts = new Set();
            keyBinds.value.forEach(item => {
                const k = item.key?.toLowerCase();
                if (k) {
                    counts[k] = (counts[k] || 0) + 1;
                    if (counts[k] > 1) conflicts.add(k);
                }
            });
            return conflicts;
        });
        function isConflict(key) {
            return key && conflictKeys.value.has(key.toLowerCase());
        }

        function getAutoKey(str) {
            if (!str) return '';
            if (/^[a-zA-Z]+$/.test(str)) return str.substring(0, 2).toLowerCase(); 
            
            const borders = "啊芭擦搭蛾发噶哈击喀垃妈拿哦啪期然撒塌挖昔压匝".split('');
            const initials = "abcdefghjklmnopqrstwxyz".split('');
            let key = '';
            
            for(let i = 0; i < str.length; i++) {
                const char = str[i];
                if(/[a-zA-Z0-9]/.test(char)) {
                    key += char.toLowerCase();
                } else if(/[\u4e00-\u9fa5]/.test(char)) {
                    let match = 'a'; 
                    for(let k = borders.length - 1; k >= 0; k--) {
                        if(char.localeCompare(borders[k], 'zh-CN') >= 0) {
                            match = initials[k];
                            break;
                        }
                    }
                    key += match;
                }
            }
            return key.substring(0, 3).toLowerCase(); 
        }

        async function autoExtractTags() {
            try {
                const res = await window.DyAPI.getTagGroups();
                if(!res || !res.groups) return;
                
                const existingTags = new Set(keyBinds.value.map(i => i.tag));
                let addedCount = 0;

                res.groups.forEach(g => {
                    g.tags.forEach(tag => {
                        if(existingTags.has(tag)) return; 
                        keyBinds.value.push({ key: getAutoKey(tag), tag: tag });
                        existingTags.add(tag);
                        addedCount++;
                    });
                });
                
                if (addedCount > 0) {
                    vant.showToast(`成功提取 ${addedCount} 个映射。红框表示存在按键冲突，请修改`);
                } else {
                    vant.showToast('没有发现新的Tag可提取');
                }
            } catch (e) {
                vant.showFailToast('提取失败');
            }
        }

        onMounted(() => { 
            isMounted.value = true; 
            initKeyBinds();

            setTimeout(() => {
                const panel = document.querySelector('.panel-content');
                if (panel) {
                    const savedScroll = localStorage.getItem('dy_panel_scroll');
                    if (savedScroll) panel.scrollTop = parseInt(savedScroll, 10);
                    let scrollTimeout = null;
                    panel.addEventListener('scroll', (e) => {
                        if (!scrollTimeout) {
                            scrollTimeout = setTimeout(() => {
                                localStorage.setItem('dy_panel_scroll', e.target.scrollTop);
                                scrollTimeout = null;
                            }, 300);
                        }
                    });
                }
            }, 800);

            window.addEventListener('keydown', (e) => {
                const activeTag = document.activeElement ? document.activeElement.tagName : '';
                if (activeTag === 'INPUT' || activeTag === 'TEXTAREA') return;
                
                if (/^[a-z0-9]$/i.test(e.key)) {
                    keyBuffer += e.key.toLowerCase();
                    clearTimeout(keyTimer);

                    const bindItem = keyBinds.value.find(item => item.key && item.key.toLowerCase() === keyBuffer);
                    
                    if (bindItem && bindItem.tag) {
                        e.preventDefault(); 
                        applyBindTag(bindItem.tag);
                        keyBuffer = ''; 
                    } else {
                        keyTimer = setTimeout(() => { 
                            keyBuffer = ''; 
                        }, keyTimeout.value);
                    }
                }
            });
        });

        function applyBindTag(targetTag) {
            const chipTexts = Array.from(document.querySelectorAll('.chip-text'));
            const targetChip = chipTexts.find(el => el.textContent.trim() === '#' + targetTag);
            
            if (targetChip) {
                targetChip.click();
                vant.showSuccessToast({ message: `自动反选: #${targetTag}`, duration: 500, position: 'top' });
                return;
            }

            const tagInput = document.querySelector('.tag-input-row input[placeholder*="输入新 Tag"]');
            const addBtn = document.querySelector('.tag-input-row .van-button--primary');
            
            if (tagInput && addBtn) {
                tagInput.value = targetTag;
                tagInput.dispatchEvent(new Event('input'));
                addBtn.click();
                vant.showSuccessToast({ message: `自动打标: #${targetTag}`, duration: 500, position: 'top' });
            } else {
                vant.showFailToast('打标面板未就绪');
            }
        }

        function openKeyBind() { initKeyBinds(); showKeyBind.value = true; }
        function closeKeyBind() { initKeyBinds(); showKeyBind.value = false; }
        function addKeyBindRow() { keyBinds.value.unshift({ key: '', tag: '' }); } 
        function removeKeyBindRow(idx) { keyBinds.value.splice(idx, 1); }
        function saveKeyBind() {
            if (conflictKeys.value.size > 0) {
                vant.showFailToast('存在冲突的键位，请修改红框项');
                return;
            }
            const validBinds = keyBinds.value.filter(item => item.key && item.tag);
            localStorage.setItem('dy_key_binds', JSON.stringify(validBinds));
            localStorage.setItem('dy_key_timeout', keyTimeout.value);
            keyBinds.value = validBinds;
            showKeyBind.value = false;
            vant.showSuccessToast('配置保存并生效');
        }
        
        const searchKeyword = ref('');
        const excludeKeyword = ref('');
        const searchScore = ref(0);
        const sortBy = ref('');
        const searchUntagged = ref('0');
        const sizeOperator = ref('lte');
        const sizeValue = ref('');
        
        const localScore = computed({ get: () => props.currentVideoScore || 0, set: (val) => emit('update-score', val) });

        async function copyAuthorName() {
            if (!props.currentVideo || !props.currentVideo.filename) return vant.showFailToast('无视频信息');
            const name = props.currentVideo.filename.split(' ')[0].trim();
            if (!name) return vant.showFailToast('提取失败');
            if (navigator.clipboard && window.isSecureContext) {
                try { await navigator.clipboard.writeText(name); vant.showSuccessToast({ message: `已复制: ${name}`, duration: 800 }); } 
                catch (err) { fallbackCopy(name); }
            } else { fallbackCopy(name); }
        }

        function fallbackCopy(text) {
            const textArea = document.createElement("textarea"); textArea.value = text;
            textArea.style.top = "0"; textArea.style.left = "0"; textArea.style.position = "fixed";
            document.body.appendChild(textArea); textArea.focus(); textArea.select();
            try { document.execCommand('copy') ? vant.showSuccessToast({ message: `已复制: ${text}`, duration: 800 }) : vant.showFailToast('浏览器不支持'); } 
            catch (err) { vant.showFailToast('复制失败'); }
            document.body.removeChild(textArea);
        }

        function getVideoUrl(id) { return id ? `/dyfn/videos/${id}/stream` : ''; }
        function searchDouyin(kw) { window.open(`https://www.douyin.com/search/${encodeURIComponent(kw)}`, '_blank'); }
        function handleJump() { if(localJumpTarget.value) { emit('jump', parseInt(localJumpTarget.value)); localJumpTarget.value = ''; } }

        async function doSearch() {
            props.state.searchKeyword = searchKeyword.value.trim(); 
            props.state.excludeKeyword = excludeKeyword.value.trim();
            props.state.searchScore = searchScore.value; 
            props.state.sortBy = sortBy.value;
            props.state.searchUntagged = searchUntagged.value;
            props.state.searchSize = (sizeValue.value && sizeValue.value > 0) ? `${sizeOperator.value}:${sizeValue.value}` : 0;
            props.state.page = 1; 
            showSearch.value = false;
            
            // 🌟 核心修改：使用统一的装载器获取所有参数
            props.state.totalVideos = props.state.totalpage = await window.DyAPI.fetchVideoCount(window.DyAPI.getSearchParams(props.state));
            
            const url = new URL(window.location);
            const mapping = { 
                'search': 'searchKeyword', 
                'exclude': 'excludeKeyword', 
                'score': 'searchScore', 
                'size': 'searchSize', 
                'sort_by': 'sortBy',
                'untagged': 'searchUntagged' 
            };
            for (let [k, stateKey] of Object.entries(mapping)) { 
                if (props.state[stateKey] && props.state[stateKey] !== 0 && props.state[stateKey] !== '0') {
                    url.searchParams.set(k, props.state[stateKey]); 
                } else { 
                    url.searchParams.delete(k); 
                } 
            }
            window.history.pushState({}, '', url); 
            emit('trigger-search');
        }

        function loadFromUrlParams() {
            const urlParams = new URLSearchParams(window.location.search); let hasParams = false;
            if (urlParams.get('search')) { searchKeyword.value = props.state.searchKeyword = urlParams.get('search'); hasParams = true; }
            if (urlParams.get('exclude')) { excludeKeyword.value = props.state.excludeKeyword = urlParams.get('exclude'); hasParams = true; }
            if (urlParams.get('score')) { searchScore.value = props.state.searchScore = parseInt(urlParams.get('score')); hasParams = true; }
            if (urlParams.get('sort_by')) { sortBy.value = props.state.sortBy = urlParams.get('sort_by'); hasParams = true; }
            if (urlParams.get('untagged')) { searchUntagged.value = props.state.searchUntagged = urlParams.get('untagged'); hasParams = true; } 
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
            searchUntagged.value = '0'; props.state.searchUntagged = '0'; 
            sizeOperator.value = 'lte'; sizeValue.value = ''; props.state.searchSize = 0; props.state.page = 1;
        }

        const searchFromClipboard = async () => {
            if (!navigator.clipboard || !window.isSecureContext) { alert('当前环境不支持或未授权 HTTPS'); return; }
            try { const text = await navigator.clipboard.readText(); if (!text.trim()) return alert('剪贴板为空'); searchKeyword.value = text.trim(); showSearch.value = false; await doSearch(); } catch (err) { alert('读取失败，检查权限'); }
        };

        expose({ loadFromUrlParams, clearSearch, doSearch }); 

        return { 
            localJumpTarget, showSearch, showDetail, isMounted, localScore, 
            searchKeyword, excludeKeyword, searchScore, sortBy, searchUntagged, sizeOperator, sizeValue, 
            handleJump, doSearch, searchFromClipboard, getVideoUrl, searchDouyin, copyAuthorName,
            showKeyBind, keyBinds, keyTimeout, isConflict, openKeyBind, closeKeyBind, addKeyBindRow, removeKeyBindRow, saveKeyBind, autoExtractTags
        };
    }
};