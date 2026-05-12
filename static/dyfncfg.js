// tag-panel.js
const TagPanel = {
    template: `
        <div style="padding: 0 15px 15px 15px;">
            <div class="dynamic-sections" v-show="videos?.length > 0">
                <div v-for="(item, idx) in panelOrder" :key="item"
                     class="drag-section"
                     :draggable="dragEnabledId === item"
                     @dragstart="onDragStart($event, idx)"
                     @dragend="onDragEnd"
                     @dragover.prevent
                     @dragenter.prevent
                     @drop="onDrop($event, idx)">
                     
                    <div class="drag-handle" @mousedown="enableDrag(item)" @mouseup="disableDrag" @mouseleave="disableDrag" title="按住拖拽排版">⋮⋮</div>

                    <div class="section-content">
                        <div v-if="item === 'tokens'">
                            <div class="group-title">1. 文件名词块 (点字黑化剔除，双击[全]撤销独占)</div>
                            <div style="min-height: 30px;">
                                <span v-if="filenameTokens.length === 0" class="empty-hint">分词中或无核心词...</span>
                                <span v-for="t in filenameTokens" :key="t.text" class="token-chip" :class="{'token-off': !t.active}">
                                    <span class="chip-text" @click="toggleToken(t)">{{ t.text }}</span>
                                    <span class="chip-action" @click.stop="setAsOnlyToken(t.text)" title="设为独占">[全]</span>
                                    <span class="chip-del" @click.stop="quickBlacklistToken(t.text)" title="全局黑名单">[黑]</span>
                                </span>
                            </div>
                        </div>
                        
                        <div v-else-if="item === 'auto_tags'">
                            <div class="group-title"><span>2. 机器提取虚拟 Tag (独立分区，点击一键注销)</span></div>
                            <div style="background: rgba(0,0,0,0.2); border: 1px dashed #553366; border-radius: 6px; padding: 10px; margin-bottom: 10px; min-height: 35px; transition: all 0.2s;">
                                <span v-if="autoTags.length === 0" class="empty-hint" style="color:#775588;">未检测到 #a_ 开头的虚拟标签...</span>
                                <span v-for="t in autoTags" :key="t.text" class="auto-tag-chip" :class="{'tag-off': !t.active}" @click="toggleAutoTag(t)">
                                    #a_{{ t.text }}
                                </span>
                            </div>
                        </div>

                        <div v-else-if="item === 'tags'">
                            <div class="group-title" style="margin-bottom:12px;">3. 人工预设 Tag 分区 (长按拖拽)</div>

                            <div style="background: rgba(25, 137, 250, 0.08); border: 1px solid rgba(25, 137, 250, 0.3); border-radius: 6px; padding: 10px; margin-bottom: 12px; min-height: 30px;">
                                <span style="font-size: 12px; color: #1989fa; font-weight: bold; margin-bottom: 6px; display: block;">即将保存的 Tag 预览:</span>
                                <span v-if="activeAutoTags.length === 0 && currentCustomTags.length === 0" class="empty-hint" style="color:#1989fa; opacity: 0.6;">暂无标签...</span>
                                <span v-for="t in activeAutoTags" :key="'auto'+t" class="auto-tag-chip" style="margin: 2px 4px 2px 0; cursor: default; padding: 2px 8px;">#a_{{ t }}</span>
                                <span v-for="t in currentCustomTags" :key="'custom'+t" class="quick-tag-chip tag-on" style="margin: 2px 4px 2px 0; cursor: default; padding: 2px 8px;">#{{ t }}</span>
                            </div>

                            <div v-for="(group, gIdx) in tagGroups" :key="group.id" class="tag-group-card" :class="{'drag-over-group': dragType === 'group' && dragOverGroupIdx === gIdx, 'drag-over-empty': dragType === 'tag' && dragOverGroupIdx === gIdx && group.tags.length === 0}" :draggable="dragGroupEnabledId === group.id" @dragstart.stop="onGroupDragStart($event, gIdx)" @dragend.stop="onGroupDragEnd" @dragover.prevent.stop="onGroupDragOver($event, gIdx)" @dragenter.prevent="onTagDragEnter(gIdx)" @dragleave.prevent="onTagDragLeave(gIdx)" @drop.prevent.stop="onGroupDrop($event, gIdx)">
                                <div style="display:flex; justify-content:space-between; margin-bottom:4px; align-items:center;">
                                    <div style="display:flex; align-items:center; gap:10px;">
                                        <div class="drag-handle-group" @mousedown="enableGroupDrag(group.id)" @mouseup="disableGroupDrag" @mouseleave="disableGroupDrag">⋮⋮ 分区</div>
                                        <label style="font-size:11px; color:#e6a23c; cursor:pointer; display:flex; align-items:center; gap:4px;" title="勾选后，该分区的Tag将在同名人物(正则提取)文件间自动传染合并">
                                            <input type="checkbox" v-model="group.is_person" /> 标记为特征类(扩散)
                                        </label>
                                    </div>
                                    <span style="color:#ff4d4f; font-size:11px; cursor:pointer;" @click="removeTagGroup(gIdx)">删区</span>
                                </div>
                                <div style="min-height: 35px; padding-bottom: 5px;">
                                    <span v-if="group.tags.length === 0" class="empty-hint" style="pointer-events: none;">拖拽 Tag 到此...</span>
                                    <span v-for="(tag, tIdx) in group.tags" :key="tag" class="quick-tag-chip" :class="{'tag-on': isTagActive(tag), 'drag-over-tag-left': dragType === 'tag' && dragOverTagInfo?.gIdx === gIdx && dragOverTagInfo?.tIdx === tIdx && dragDropPosition === 'left', 'drag-over-tag-right': dragType === 'tag' && dragOverTagInfo?.gIdx === gIdx && dragOverTagInfo?.tIdx === tIdx && dragDropPosition === 'right'}" draggable="true" @dragstart.stop="onTagDragStart($event, gIdx, tIdx)" @dragend.stop="onTagDragEnd" @dragover.prevent.stop="onTagDragOver($event, gIdx, tIdx)" @drop.prevent.stop="onTagDrop($event, gIdx, tIdx)">
                                        <span class="chip-text" @click="handlePresetTagClick(tag)">#{{ tag }}</span>
                                        <span class="chip-del" @click.stop="removeSavedTagFromGroup(gIdx, tIdx)">×</span>
                                    </span>
                                </div>
                            </div>

                            <div class="tag-input-row" style="margin-top:12px; margin-bottom:12px;">
                                <input type="text" v-model="globalTagInput" placeholder="输入新 Tag (首区)..." @keyup.enter="addGlobalTag" />
                                <van-button type="primary" size="small" @click="addGlobalTag">添加</van-button>
                                <van-button type="default" size="small" @click="addEmptyGroup" style="padding: 0 10px;">+ 新区</van-button>
                            </div>

                            <div style="display:flex; justify-content:space-between; align-items:center; background: rgba(0,0,0,0.2); padding: 8px 10px; border-radius: 6px; border: 1px dashed #444;">
                                <span style="font-size: 12px; color: #888;">多方案模板管理</span>
                                <div style="display:flex; gap:6px; align-items:center; background: rgba(0,0,0,0.4); padding: 2px 6px; border-radius: 4px; border: 1px solid #444;">
                                    <span style="font-size: 12px; color: #888;">模板:</span>
                                    <select v-model="currentProfileName" @change="switchProfile" style="background: transparent; border: none; color: #e6a23c; font-size: 12px; outline: none; max-width: 90px; cursor: pointer;">
                                        <option v-for="(val, key) in tagProfiles" :key="key" :value="key" style="color: #000;">{{ key }}</option>
                                    </select>
                                    <span @click="createNewProfile" style="cursor:pointer; color:#4fc08d; font-size:12px; padding: 0 4px; border-left: 1px solid #333;" title="创建全新空模板">➕</span>
                                    <span v-if="Object.keys(tagProfiles).length > 1" @click="deleteCurrentProfile" style="cursor:pointer; color:#ff4d4f; font-size:12px; padding: 0 4px;" title="删除当前模板">✖</span>
                                </div>
                            </div>
                        </div>

                        <div v-else-if="item === 'blacklist'">
                            <div class="group-title">4. 快捷拉黑干扰词 (全局生效)</div>
                            <div class="tag-input-row" style="margin-bottom:0;">
                                <input type="text" v-model="quickBlacklistInput" placeholder="输入符号或干扰词..." @keyup.enter="addQuickBlacklist" />
                                <van-button type="danger" size="small" @click="addQuickBlacklist">拉黑</van-button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <div v-show="showConfig" class="search-modal" @click.self="showConfig=false">
                <div class="search-modal-content" style="max-width: 650px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <h4>系统配置与智能归档</h4>
                        <van-button size="mini" @click="showConfig=false">关闭</van-button>
                    </div>

                    <div style="background: rgba(103, 194, 58, 0.05); border: 1px solid rgba(103, 194, 58, 0.3); padding: 15px; border-radius: 12px; margin-bottom: 20px; margin-top: 15px;">
                        <h5 style="margin:0 0 10px 0; color:#67c23a;">📦 智能洗库归档引擎 (稀有优先 + 特征传染)</h5>
                        
                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            <input type="text" v-model="archiveRootDir" placeholder="处理源路径 (如 D:/videos)" class="search-input" style="margin-bottom:0;" />
                            
                            <div style="display: flex; gap: 10px;">
                                <div style="flex:1;">
                                    <span style="font-size:11px; color:#888;">人物名正则 (留空则不识别，默认提首词):</span>
                                    <input type="text" v-model="archivePersonRegex" placeholder="如 ^(\\S+)" class="search-input" style="padding:4px 8px; font-size:13px; margin-bottom:0;" />
                                </div>
                                <div style="flex:0 0 80px;">
                                    <span style="font-size:11px; color:#888;">归档阈值:</span>
                                    <input type="number" v-model="archiveThreshold" class="search-input" style="padding:4px 8px; font-size:13px; margin-bottom:0;" />
                                </div>
                                <div style="flex:0 0 80px;">
                                    <span style="font-size:11px; color:#888;">数量上限:</span>
                                    <input type="number" v-model="archiveMaxPer" class="search-input" style="padding:4px 8px; font-size:13px; margin-bottom:0;" />
                                </div>
                            </div>

                            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 5px;">
                                <label style="display:flex; align-items:center; gap:6px; cursor:pointer; color:#e6a23c; font-size:12px; font-weight:bold;">
                                    <input type="checkbox" v-model="forceRearchive" /> 强制全量重归档 (含已归档文件)
                                </label>
                                <van-button type="success" size="small" @click="getArchivePlan" :loading="isArchiving" style="height:32px; padding:0 20px; box-shadow: 0 4px 10px rgba(103,194,58,0.3);">生成预览</van-button>
                            </div>
                        </div>

                        <div v-if="archivePlan.length > 0" style="margin-top:15px; border-top:1px dashed #444; padding-top:15px;">
                            <div style="max-height: 250px; overflow-y: auto; background: #000; border-radius: 6px; padding: 5px;">
                                <table style="width:100%; font-size:12px; color:#ccc; border-collapse: collapse;">
                                    <thead style="position: sticky; top: 0; background: #222;">
                                        <tr>
                                            <th style="text-align:left; padding:8px;">拟建文件夹</th>
                                            <th style="text-align:left; padding:8px;">包含Tag</th>
                                            <th style="text-align:center; padding:8px;">文件数</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        <tr v-for="p in archivePlan" :key="p.folder_name" style="border-bottom: 1px solid #222;">
                                            <td style="padding:8px; color:#e6a23c;">{{ p.folder_name }}</td>
                                            <td style="padding:8px;">{{ p.display_tags.join(', ') }}</td>
                                            <td style="padding:8px; text-align:center;">{{ p.files.length }}</td>
                                        </tr>
                                    </tbody>
                                </table>
                            </div>
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:10px;">
                                <span style="font-size:12px; color:#888;">共计 {{ archivePlan.length }} 个文件夹</span>
                                <van-button type="danger" size="small" block @click="executeArchive" style="flex:0 0 150px;">确认物理移动</van-button>
                            </div>
                        </div>
                        
                        <van-button type="warning" size="small" @click="fixLegacyFilenames" style="margin-top: 15px; width: 100%; border-radius: 6px; background: rgba(230,162,60,0.1); border: 1px dashed #e6a23c; color: #e6a23c;">🔧 急救：一键修复历史错误篡改的文件名</van-button>
                    </div>

                    <div style="opacity: 0.8; border-top: 1px dashed #333; padding-top: 15px;">
                        <div style="display: flex; gap: 8px;">
                            <input type="text" v-model="newPath" placeholder="输入绝对路径如 D:/videos" class="search-input" @keyup.enter="addPathLocal" style="margin-bottom:0;" />
                            <button class="search-btn-confirm" style="margin:0; width:auto;" @click="addPathLocal">添加</button>
                        </div>
                        
                        <div style="margin-top:16px; background: rgba(255,255,255,0.02); padding: 10px; border-radius: 6px;">
                            <div v-for="(path, idx) in pathList" :key="path" style="display:flex;align-items:center;margin-bottom:8px; border-bottom: 1px dashed #333; padding-bottom: 8px;">
                                <button @click="indexPath_del(path)" style="background:transparent; border:none; color:#ff4d4f; cursor:pointer;">✖</button>
                                <span style="flex:1;font-size:12px;word-break:break-all;margin:0 8px; color: #bbb;">{{ path }}</span>
                                <button @click="indexPath(path)" style="background: rgba(25,137,250,0.2); border:1px solid #1989fa; color:#1989fa; border-radius:4px; padding: 2px 6px; font-size:12px; cursor:pointer;">索引</button>
                            </div>
                        </div>

                        <div style="margin-top: 25px; padding-top: 15px; border-top: 1px dashed #444;">
                            <h4>全局自动黑名单</h4>
                            <div style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom: 12px; max-height: 100px; overflow-y: auto;">
                                <span v-for="(word, idx) in blacklist" :key="idx" style="background:#333; border: 1px solid #444; color:#ddd; font-size:12px; padding:4px 10px; border-radius:12px; cursor:pointer;" @click="removeBlacklistWord(idx)">{{ word }} ×</span>
                            </div>
                            <div style="display: flex; gap: 8px;">
                                <input type="text" v-model="newBlacklistWord" placeholder="输入需自动屏蔽的词汇" class="search-input" style="flex:1; margin-bottom:0;" @keyup.enter="addBlacklistWord" />
                                <button class="search-btn-confirm" style="width: auto; padding: 0 15px; margin: 0;" @click="addBlacklistWord">添加</button>
                            </div>
                        </div>
                        
                        <div style="display: flex; flex-direction: column; gap: 8px; margin-top:20px; background: rgba(0,0,0,0.2); padding: 12px; border-radius: 8px; border: 1px solid #222;">
                            <span style="font-size: 12px; color: #888; margin-bottom: 4px;">重命名与洗库控制台</span>
                            <div style="display: flex; gap: 8px;">
                                <button class="search-btn-confirm" style="margin: 0; flex: 1; background: transparent; border: 1px solid #e6a23c; color: #e6a23c;" @click="executeRenameQueue('queue_only')" :disabled="isExecuting">▶ 执行最新打标</button>
                                <button class="search-btn-confirm" style="margin: 0; flex: 1; background: transparent; border: 1px solid #67c23a; color: #67c23a;" @click="executeRenameQueue('full')" :disabled="isExecuting">▶ 全库应用黑名单</button>
                            </div>
                            <button class="search-btn-confirm" style="margin: 0; background: transparent; border: 1px dashed #f56c6c; color: #f56c6c;" @click="retryFailedQueue" :disabled="isExecuting">↻ 重试失败的改名任务</button>
                            <div style="text-align: center; height: 20px; margin-top: 4px;">
                                <span v-if="isExecuting" style="font-size: 12px; color: #e6a23c;">处理中，请稍候...</span>
                                <span v-if="executeSuccess && !isExecuting" style="color:#4fc08d; font-size:12px; font-weight:bold;">✔ {{ executeMsg }}</span>
                            </div>
                        </div>

                        <div style="margin-top: 25px; padding-top: 15px; border-top: 1px dashed #444;">
                            <h4>高级数据操作 (CSV & JSON)</h4>
                            <div style="display: flex; gap: 10px; margin-bottom: 12px; align-items: center;">
                                <select v-model="exportTarget" class="search-select" style="flex: 1; padding: 4px 8px; margin-bottom:0;">
                                    <option value="ALL">全部模板</option>
                                    <option v-for="(val, key) in tagProfiles" :key="key" :value="key">仅: {{key}}</option>
                                </select>
                                <van-button type="primary" size="small" @click="exportTags" style="flex:1; background: #2b2b2b; border-color: #444;">⬇ 导出模板</van-button>
                                <van-button type="warning" size="small" @click="triggerImportTags" style="flex:1; background: #2b2b2b; border-color: #444; color: #e6a23c;">⬆ 导入模板</van-button>
                            </div>
                            <div style="background: rgba(25,137,250,0.05); border: 1px solid rgba(25,137,250,0.2); padding: 12px; border-radius: 8px; margin-bottom: 12px;">
                                <p style="font-size: 12px; color: #888; margin-top: 0; margin-bottom: 10px;">批量打标：导出无Tag文件列表，在CSV第三列添加Tag后即可一键导入。</p>
                                <div style="display: flex; gap: 8px; align-items: center;">
                                    <select v-model="exportLimit" class="search-select" style="flex: 1; margin-bottom:0;">
                                        <option value="0">全部导出</option>
                                        <option value="100">前 100 条</option>
                                        <option value="500">前 500 条</option>
                                        <option value="1000">前 1000 条</option>
                                    </select>
                                    <button class="search-btn-confirm" style="margin:0; width:auto; background: #1989fa;" @click="exportCSV">导出打标底表</button>
                                </div>
                                <button class="search-btn-confirm" style="margin-top:8px; width:100%; background: #e6a23c;" @click="triggerImportCSV">上传 CSV 批量导入打标</button>
                            </div>
                            <div style="background: rgba(142,68,173,0.05); border: 1px solid rgba(142,68,173,0.2); padding: 12px; border-radius: 8px;">
                                <p style="font-size: 12px; color: #888; margin-top: 0; margin-bottom: 10px;">防丢核对：导出“原始名”与“最新物理名”对比表。</p>
                                <div style="display: flex; gap: 8px; align-items: center;">
                                    <select v-model="exportLogLimit" class="search-select" style="flex: 1; margin-bottom:0;">
                                        <option value="0">全部导出</option>
                                        <option value="100">前 100 条</option>
                                        <option value="500">前 500 条</option>
                                        <option value="1000">前 1000 条</option>
                                    </select>
                                    <button class="search-btn-confirm" style="margin:0; width:auto; background: #8e44ad;" @click="exportRenameLogCSV">导出改名核对表</button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div v-if="showHistory" class="search-modal" @click.self="showHistory=false">
                <div class="search-modal-content">
                    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 15px;">
                        <h4 style="margin:0;">文件名修改日志</h4>
                        <van-button size="mini" @click="showHistory=false">关闭</van-button>
                    </div>
                    <div style="max-height: 50vh; overflow-y: auto; margin-bottom: 10px;">
                        <div v-for="log in renameHistory" :key="log.id" style="background: rgba(255,255,255,0.05); border: 1px solid #333; border-radius: 6px; padding: 10px; margin-bottom: 10px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <span style="font-size: 12px; color: #888;">{{ log.create_time }}</span>
                                <span v-if="log.status === 'restored'" style="font-size: 12px; color: #999;">已撤销</span>
                                <van-button v-if="log.status === 'success'" type="primary" size="mini" @click="handleRestore(log.id)">撤销恢复</van-button>
                            </div>
                            <div style="font-size: 13px; color: #ff4d4f; text-decoration: line-through; word-break: break-all;">{{ log.original_name }}</div>
                            <div style="font-size: 13px; color: #4fc08d; font-weight: bold; margin-top: 4px; word-break: break-all;">{{ log.target_name }}</div>
                        </div>
                    </div>
                </div>
            </div>

            <input type="file" id="import-tags-file" style="display:none" accept=".json" @change="importTags" />
            <input type="file" id="import-csv-data-file" style="display:none" accept=".csv" @change="importCSVFile" />
        </div>
    `,
    props: ['currentVideo', 'videos', 'blacklist', 'currentIndex', 'searchState', 'pathList', 'syncEnabled', 'syncMemory'],
    emits: ['reload-videos', 'add-path', 'index-path', 'index-path-del', 'index-all', 'add-blacklist', 'remove-blacklist', 'update-sync'],
    setup(props, { emit, expose }) {
        const { ref, computed, watch, onMounted } = Vue;

        const panelOrder = ref(JSON.parse(localStorage.getItem('dy_panel_order')) || ['tokens', 'auto_tags', 'tags', 'blacklist']);
        
        const tagProfiles = ref({});
        const currentProfileName = ref('');
        const exportTarget = ref('ALL');
        let isSwitchingProfile = false;

        const showConfig = ref(false);
        const archiveRootDir = ref(localStorage.getItem('dy_archive_root') || '');
        const archiveThreshold = ref(parseInt(localStorage.getItem('dy_archive_threshold')) || 10);
        const archiveMaxPer = ref(parseInt(localStorage.getItem('dy_archive_max_per')) || 50);
        const forceRearchive = ref(localStorage.getItem('dy_force_rearchive') === 'true');
        
        // 🌟 初始化为默认提取首词正则
        const storedRegex = localStorage.getItem('dy_archive_person_regex');
        const archivePersonRegex = ref(storedRegex !== null ? storedRegex : '^(\\S+)');
        
        const archivePlan = ref([]);
        const isArchiving = ref(false);

        watch(archiveRootDir, (val) => localStorage.setItem('dy_archive_root', val));
        watch(archiveThreshold, (val) => localStorage.setItem('dy_archive_threshold', val));
        watch(archiveMaxPer, (val) => localStorage.setItem('dy_archive_max_per', val));
        watch(forceRearchive, (val) => localStorage.setItem('dy_force_rearchive', val));
        watch(archivePersonRegex, (val) => localStorage.setItem('dy_archive_person_regex', val));

        onMounted(async () => {
            if (panelOrder.value.includes('preview')) {
                panelOrder.value = panelOrder.value.filter(item => item !== 'preview');
                localStorage.setItem('dy_panel_order', JSON.stringify(panelOrder.value));
            }

            const tgRes = await window.DyAPI.getTagGroups();
            let backendGroups = (tgRes && tgRes.groups && tgRes.groups.length > 0) ? tgRes.groups : [{ id: 'g_1', tags: [], is_person: false }];

            let savedProfiles = JSON.parse(localStorage.getItem('dy_tag_profiles'));
            let savedCurrentName = localStorage.getItem('dy_current_profile_name');

            if (!savedProfiles || Object.keys(savedProfiles).length === 0) {
                tagProfiles.value = { '默认方案': backendGroups };
                currentProfileName.value = '默认方案';
            } else {
                tagProfiles.value = savedProfiles;
                if (savedCurrentName && tagProfiles.value[savedCurrentName]) {
                    currentProfileName.value = savedCurrentName;
                } else {
                    currentProfileName.value = Object.keys(tagProfiles.value)[0];
                }
            }
            
            tagGroups.value = JSON.parse(JSON.stringify(tagProfiles.value[currentProfileName.value]));
        });

        async function fixLegacyFilenames() {
            if (!archiveRootDir.value) return vant.showFailToast('请先输入处理源路径');
            if (!confirm('确定扫描并修复该路径下所有错误带有 arc_ 前缀的归档文件吗？')) return;
            
            vant.showLoadingToast({ message: '修复中...', forbidClick: true, duration: 0 });
            try {
                const res = await axios.post('/dyfn/sys_tags/fix_legacy_filenames', { root_dir: archiveRootDir.value });
                vant.closeToast();
                if (res.data.success) {
                    vant.showSuccessToast(res.data.msg);
                    emit('reload-videos', false, props.currentIndex);
                } else {
                    vant.showFailToast(res.data.msg);
                }
            } catch (e) {
                vant.closeToast();
                vant.showFailToast('请求失败');
            }
        }

        async function getArchivePlan() {
            if (!archiveRootDir.value) return vant.showFailToast('请输入路径');
            isArchiving.value = true;
            try {
                const res = await axios.post('/dyfn/sys_tags/archive_plan', {
                    root_dir: archiveRootDir.value,
                    tag_groups: props.searchState.tagGroups || JSON.parse(localStorage.getItem('dy_tag_profiles'))[localStorage.getItem('dy_current_profile_name')],
                    threshold: archiveThreshold.value,
                    max_per_folder: archiveMaxPer.value,
                    force_rearchive: forceRearchive.value,
                    person_regex: archivePersonRegex.value
                });
                if (res.data.success) {
                    archivePlan.value = res.data.plan;
                } else {
                    vant.showFailToast(res.data.msg);
                }
            } catch (e) {
                vant.showFailToast('请求失败');
            } finally {
                isArchiving.value = false;
            }
        }

        async function executeArchive() {
            if (archivePlan.value.length === 0) return;
            try {
                const res = await axios.post('/dyfn/sys_tags/archive_execute', {
                    plan: archivePlan.value,
                    root_dir: archiveRootDir.value
                });
                if (res.data.success) {
                    vant.showSuccessToast(res.data.msg);
                    archivePlan.value = [];
                    emit('reload-videos', false, props.currentIndex);
                } else {
                    vant.showFailToast(res.data.msg);
                }
            } catch (e) {
                vant.showFailToast('执行异常');
            }
        }

        function switchProfile() {
            isSwitchingProfile = true;
            tagGroups.value = JSON.parse(JSON.stringify(tagProfiles.value[currentProfileName.value]));
            localStorage.setItem('dy_current_profile_name', currentProfileName.value);
            vant.showToast({ message: `已切换至: ${currentProfileName.value}`, position: 'top', duration: 1000 });
            setTimeout(() => { isSwitchingProfile = false; }, 200);
        }

        function createNewProfile() {
            const name = prompt('请输入新模板名称：\\n(将创建一个干净的空模板)', '新模板');
            if (name && name.trim()) {
                const cleanName = name.trim();
                if (tagProfiles.value[cleanName]) {
                    vant.showFailToast('模板名称已存在！'); return;
                }
                tagProfiles.value[cleanName] = [{ id: 'g_' + Date.now(), tags: [], is_person: false }];
                currentProfileName.value = cleanName;
                switchProfile();
            }
        }

        function deleteCurrentProfile() {
            if (Object.keys(tagProfiles.value).length <= 1) {
                vant.showFailToast('至少保留一个模板方案！'); return;
            }
            if (confirm(`确定要删除模板 [${currentProfileName.value}] 吗？`)) {
                delete tagProfiles.value[currentProfileName.value];
                currentProfileName.value = Object.keys(tagProfiles.value)[0];
                switchProfile();
            }
        }

        const dragEnabledId = ref(null); let dragIndex = null;
        function enableDrag(item) { dragEnabledId.value = item; }
        function disableDrag() { dragEnabledId.value = null; }
        function onDragStart(e, idx) { dragIndex = idx; e.dataTransfer.effectAllowed = 'move'; setTimeout(() => e.target.classList.add('is-dragging'), 0); }
        function onDragEnd(e) { e.target.classList.remove('is-dragging'); dragIndex = null; disableDrag(); }
        function onDrop(e, dropIndex) {
            if (dragIndex !== null && dragIndex !== dropIndex) {
                const item = panelOrder.value.splice(dragIndex, 1)[0];
                panelOrder.value.splice(dropIndex, 0, item);
                localStorage.setItem('dy_panel_order', JSON.stringify(panelOrder.value));
            }
        }

        const tagGroups = ref([]);
        const globalTagInput = ref(''), dragGroupEnabledId = ref(null);
        const filenameTokens = ref([]), currentCustomTags = ref([]), quickBlacklistInput = ref('');
        const autoTags = ref([]); 
        const autoSaveStatus = ref(''); 
        let renameTimeoutId = null, pendingRenameData = null, tagSaveTimeout = null;
        const dragType = ref(null), draggedGroupIdx = ref(null), dragOverGroupIdx = ref(null), draggedTagInfo = ref(null), dragOverTagInfo = ref(null), dragDropPosition = ref('');
        
        const showHistory = ref(false), renameHistory = ref([]);
        const newPath = ref(''), newBlacklistWord = ref(''), exportLimit = ref('0'), exportLogLimit = ref('0');
        const isExecuting = ref(false), executeSuccess = ref(false), executeMsg = ref('');

        const activeAutoTags = computed(() => {
            return autoTags.value.filter(t => t.active).map(t => t.text);
        });

        function getSyncKey(filename) {
            if (!filename) return '';
            let s = filename.replace(/\.[^.]+$/, ''); 
            s = s.replace(/(#a_|#)[\u4e00-\u9fa5a-zA-Z0-9_]+/g, ''); 
            s = s.replace(/\d+/g, ''); 
            s = s.replace(/[【】\[\]\(\)\-_，。！!]/g, ' '); 
            return s.trim().replace(/\s{2,}/g, ' ');
        }

        function recordSyncAction(tag, action) {
            if (!props.syncEnabled || !props.currentVideo) return;
            const key = getSyncKey(props.currentVideo.filename);
            if (!key) return;
            
            let mem = [...props.syncMemory];
            let recordIdx = mem.findIndex(r => r.key === key);
            let record;
            if (recordIdx === -1) {
                record = { key, added: [], removed: [], ts: Date.now() };
                mem.unshift(record);
            } else {
                record = { ...mem[recordIdx], ts: Date.now() };
                mem.splice(recordIdx, 1);
                mem.unshift(record);
            }
            
            if (action === 'add') {
                if (!record.added.includes(tag)) record.added.push(tag);
                record.removed = record.removed.filter(t => t !== tag);
            } else if (action === 'remove') {
                if (!record.removed.includes(tag)) record.removed.push(tag);
                record.added = record.added.filter(t => t !== tag);
            }
            
            if (mem.length > 5) mem = mem.slice(0, 5);
            emit('update-sync', mem);
        }

        watch(tagGroups, (newVal) => { 
            if (isSwitchingProfile) return; 

            if (tagGroups.value.length > 0) { 
                if(currentProfileName.value && tagProfiles.value[currentProfileName.value]) {
                    tagProfiles.value[currentProfileName.value] = JSON.parse(JSON.stringify(newVal));
                    localStorage.setItem('dy_tag_profiles', JSON.stringify(tagProfiles.value));
                    localStorage.setItem('dy_current_profile_name', currentProfileName.value);
                }

                clearTimeout(tagSaveTimeout); 
                tagSaveTimeout = setTimeout(() => { window.DyAPI.saveTagGroups(newVal); }, 1500); 
            }
        }, { deep: true });

        const realFilename = computed(() => {
            if (!props.currentVideo) return '';
            const dbName = props.currentVideo.filename || '';
            const physicalName = props.currentVideo.detail ? props.currentVideo.detail.split(/[\\/]/).pop() : '';
            if (physicalName.includes('#a_') && !dbName.includes('#a_')) return physicalName;
            return dbName;
        });

        watch(() => props.currentVideo?.id, async (newId) => {
            if (pendingRenameData) executePendingRename(); 
            if(newId) {
                currentCustomTags.value = []; autoTags.value = []; filenameTokens.value = []; autoSaveStatus.value = '';
                
                if (props.currentVideo) {
                    const rawFileName = realFilename.value;
                    const extractedTags = rawFileName.match(/#a_[\u4e00-\u9fa5a-zA-Z0-9_]+|#[\u4e00-\u9fa5a-zA-Z0-9_]+/g);
                    if (extractedTags) {
                        extractedTags.forEach(t => {
                            if (t.startsWith('#a_')) {
                                const tagBody = t.substring(3).trim();
                                if (tagBody && !autoTags.value.find(x => x.text === tagBody)) autoTags.value.push({ text: tagBody, active: true });
                            } else if (t.startsWith('#')) {
                                let tagBody = t.substring(1).trim(); 
                                if (tagBody && !currentCustomTags.value.includes(tagBody)) currentCustomTags.value.push(tagBody);
                            }
                        });
                    }

                    if (props.syncEnabled) {
                        const key = getSyncKey(rawFileName);
                        const record = props.syncMemory.find(r => r.key === key);
                        if (record) {
                            let isApplied = false;
                            record.added.forEach(t => {
                                if (!currentCustomTags.value.includes(t)) {
                                    currentCustomTags.value.push(t);
                                    isApplied = true;
                                }
                            });
                            record.removed.forEach(t => {
                                const oldLen = currentCustomTags.value.length;
                                currentCustomTags.value = currentCustomTags.value.filter(ct => ct !== t);
                                if(oldLen !== currentCustomTags.value.length) isApplied = true;
                                
                                const autoT = autoTags.value.find(at => at.text === t);
                                if (autoT && autoT.active) {
                                    autoT.active = false;
                                    isApplied = true;
                                }
                            });
                            if (isApplied) vant.showToast({message: '⚡ 已套用同源修改', position: 'top', duration: 1000});
                        }
                    }
                    
                    const cleanForTokenize = rawFileName.replace(/(#a_|#)[\u4e00-\u9fa5a-zA-Z0-9_]+/g, '').replace(/\s{2,}/g, ' ').trim();
                    try {
                        const res = await window.DyAPI.tokenize(cleanForTokenize);
                        if(res.success) { 
                            const validWords = res.words.filter(w => !props.blacklist.includes(w)); 
                            filenameTokens.value = validWords.map(w => ({ text: w, active: true, isOnly: false })); 
                        }
                    } catch(e) {}
                }
            }
        });

        function isTagActive(tag) { return currentCustomTags.value.includes(tag) || autoTags.value.some(at => at.text === tag && at.active); }

        function handlePresetTagClick(tag) {
            const autoTag = autoTags.value.find(at => at.text === tag);
            if (autoTag && autoTag.active) {
                autoTag.active = false; 
                recordSyncAction(tag, 'remove');
                return;
            }
            const idx = currentCustomTags.value.indexOf(tag);
            if (idx > -1) {
                currentCustomTags.value.splice(idx, 1);
                recordSyncAction(tag, 'remove');
            } else {
                currentCustomTags.value.push(tag);
                recordSyncAction(tag, 'add');
            }
        }
        
        function toggleAutoTag(t) { 
            t.active = !t.active; 
            recordSyncAction(t.text, t.active ? 'add' : 'remove');
        }

        const generatedFilename = computed(() => {
            if (!props.currentVideo) return '';
            let ext = ''; const match = (props.currentVideo.detail || '').match(/\.[^.]+$/); if (match) ext = match[0];
            let originalNameBody = realFilename.value;
            if (originalNameBody.endsWith(ext)) originalNameBody = originalNameBody.slice(0, -ext.length);

            let cleanBase = originalNameBody.replace(/(#a_|#)[\u4e00-\u9fa5a-zA-Z0-9_]+/g, '').replace(/\s{2,}/g, ' ').trim();
            const activeTokens = filenameTokens.value.filter(t => t.active);
            const isOnlyMode = (activeTokens.length === 1 && activeTokens[0].isOnly);
            
            let base = '';
            if (isOnlyMode) base = activeTokens[0].text;
            else {
                base = cleanBase;
                props.blacklist.forEach(bw => { if (bw.trim()) base = base.split(bw).join(''); });
                filenameTokens.value.forEach(t => { if (!t.active) base = base.split(t.text).join(''); });
                base = base.replace(/\[\s*\]|\(\s*\)|【\s*】/g, '').replace(/\.{2,}/g, '.').replace(/\s{2,}/g, ' ').replace(/^[.\-_ ]+|[.\-_ ]+$/g, ''); 
            }
            
            const activeAutoTagsStr = autoTags.value.filter(t => t.active).map(t => '#a_' + t.text).join(' ');
            const activeTagsStr = currentCustomTags.value.map(tag => '#' + tag).join(' ');
            let finalBody = [base, activeAutoTagsStr, activeTagsStr].filter(Boolean).join(' ');
            
            if (finalBody) {
                const words = finalBody.split(/\s+/); const seenTags = new Set(); const deduplicatedWords = [];
                for (let w of words) { 
                    if (w.startsWith('#a_') || w.startsWith('#')) { 
                        const baseTag = w.replace(/^(#a_|#)/, '');
                        if (!seenTags.has(baseTag)) { seenTags.add(baseTag); deduplicatedWords.push(w); } 
                    } else { deduplicatedWords.push(w); } 
                }
                finalBody = deduplicatedWords.join(' ');
            }
            if (!finalBody) return props.currentVideo.filename; 
            return finalBody + ext;
        });

        watch(generatedFilename, (newName) => {
            if (!props.currentVideo || !props.currentVideo.id) return;
            if (newName === props.currentVideo.filename) { 
                if (renameTimeoutId) clearTimeout(renameTimeoutId); 
                pendingRenameData = null; return; 
            }
            if (renameTimeoutId) clearTimeout(renameTimeoutId);
            pendingRenameData = { video_id: props.currentVideo.id, new_filename: newName };
            autoSaveStatus.value = 'pending'; 
            renameTimeoutId = setTimeout(() => { executePendingRename(); }, 2000);
        });

        function setAsOnlyToken(word) { 
            const target = filenameTokens.value.find(t => t.text === word);
            if (target && target.isOnly) { filenameTokens.value.forEach(t => { t.active = true; t.isOnly = false; }); } 
            else { filenameTokens.value.forEach(t => { t.active = (t.text === word); t.isOnly = (t.text === word); }); currentCustomTags.value = []; autoTags.value.forEach(at => at.active = false); }
        }
        function toggleToken(t) { t.active = !t.active; filenameTokens.value.forEach(tok => tok.isOnly = false); }
        function restoreOriginalName() { filenameTokens.value.forEach(t => { t.active = true; t.isOnly = false; }); currentCustomTags.value = []; autoTags.value.forEach(at => at.active = true); }
        async function addQuickBlacklist() { const kw = quickBlacklistInput.value.trim(); if(kw) { emit('add-blacklist', kw); filenameTokens.value = filenameTokens.value.filter(t => t.text !== kw); quickBlacklistInput.value = ''; } }
        async function quickBlacklistToken(kw) { emit('add-blacklist', kw); filenameTokens.value = filenameTokens.value.filter(t => t.text !== kw); }

        function enableGroupDrag(id) { dragGroupEnabledId.value = id; }
        function disableGroupDrag() { dragGroupEnabledId.value = null; }
        function onGroupDragStart(e, idx) { dragType.value = 'group'; draggedGroupIdx.value = idx; e.dataTransfer.effectAllowed = 'move'; setTimeout(() => e.target.classList.add('is-dragging-group'), 0); }
        function onGroupDragEnd(e) { e.target.classList.remove('is-dragging-group'); dragType.value = null; draggedGroupIdx.value = null; disableGroupDrag(); }
        function onTagDragStart(e, gIdx, tIdx) { dragType.value = 'tag'; draggedTagInfo.value = { gIdx, tIdx }; e.dataTransfer.effectAllowed = 'move'; setTimeout(() => e.target.classList.add('is-dragging-tag'), 0); }
        function onTagDragEnd(e) { e.target.classList.remove('is-dragging-tag'); dragType.value = null; draggedTagInfo.value = null; dragOverTagInfo.value = null; }
        function onTagDragOver(e, gIdx, tIdx) { if (dragType.value !== 'tag') return; dragOverGroupIdx.value = null; dragOverTagInfo.value = { gIdx, tIdx }; const rect = e.target.getBoundingClientRect(); dragDropPosition.value = e.clientX < (rect.left + rect.width / 2) ? 'left' : 'right'; }
        function onGroupDragOver(e, gIdx) { if (dragType.value === 'group') dragOverGroupIdx.value = gIdx; else if (dragType.value === 'tag') { dragOverGroupIdx.value = gIdx; dragOverTagInfo.value = null; } }
        function onTagDragEnter(gIdx) { dragOverGroupIdx.value = gIdx; }
        function onTagDragLeave(gIdx) { if(dragOverGroupIdx.value === gIdx) dragOverGroupIdx.value = null; }
        function onGroupDrop(e, targetGIdx) { dragOverGroupIdx.value = null; if (dragType.value === 'group' && draggedGroupIdx.value !== null) { const sourceIdx = draggedGroupIdx.value; if (sourceIdx !== targetGIdx) { const item = tagGroups.value.splice(sourceIdx, 1)[0]; tagGroups.value.splice(targetGIdx, 0, item); } } else if (dragType.value === 'tag' && draggedTagInfo.value) { const { gIdx: sourceGIdx, tIdx: sourceTIdx } = draggedTagInfo.value; if (sourceGIdx !== targetGIdx) { const tag = tagGroups.value[sourceGIdx].tags.splice(sourceTIdx, 1)[0]; tagGroups.value[targetGIdx].tags.push(tag); } } dragType.value = null; }
        function onTagDrop(e, targetGIdx, targetTIdx) { if (dragType.value === 'tag' && draggedTagInfo.value) { const { gIdx: sourceGIdx, tIdx: sourceTIdx } = draggedTagInfo.value; if (sourceGIdx === targetGIdx && sourceTIdx === targetTIdx) return; const tag = tagGroups.value[sourceGIdx].tags.splice(sourceTIdx, 1)[0]; let insertIdx = targetTIdx; if (sourceGIdx === targetGIdx && sourceTIdx < targetTIdx) insertIdx -= 1; if (dragDropPosition.value === 'right') insertIdx += 1; tagGroups.value[targetGIdx].tags.splice(insertIdx, 0, tag); } dragType.value = null; dragOverTagInfo.value = null; }

        function addEmptyGroup() { tagGroups.value.push({ id: 'g_' + Date.now(), tags: [], is_person: false }); }
        function removeTagGroup(idx) { if (confirm('确定删除此分区及内部所有Tag吗？')) { tagGroups.value.splice(idx, 1); } }
        function addGlobalTag() { const tag = globalTagInput.value.replace(/#/g, '').trim(); if (!tag) return; if (!currentCustomTags.value.includes(tag)) currentCustomTags.value.push(tag); if (tagGroups.value.length === 0) tagGroups.value.push({ id: 'g_' + Date.now(), tags: [], is_person: false }); let exists = false; for(let g of tagGroups.value) { if(g.tags.includes(tag)) { exists = true; break; } } if(!exists) { tagGroups.value[0].tags.push(tag); } globalTagInput.value = ''; recordSyncAction(tag, 'add'); }
        function removeSavedTagFromGroup(gIdx, tIdx) { const tag = tagGroups.value[gIdx].tags[tIdx]; tagGroups.value[gIdx].tags.splice(tIdx, 1); const cIdx = currentCustomTags.value.indexOf(tag); if (cIdx > -1) { currentCustomTags.value.splice(cIdx, 1); recordSyncAction(tag, 'remove'); } }

        async function executePendingRename() {
            if (!pendingRenameData) return;
            const dataToSave = { ...pendingRenameData }; pendingRenameData = null;
            if (renameTimeoutId) { clearTimeout(renameTimeoutId); renameTimeoutId = null; }
            if (props.currentVideo && props.currentVideo.id === dataToSave.video_id) props.currentVideo.filename = dataToSave.new_filename;
            else { const v = props.videos.find(vid => vid.id === dataToSave.video_id); if (v) v.filename = dataToSave.new_filename; }
            autoSaveStatus.value = 'pending';
            try {
                const res = await window.DyAPI.queueRename(dataToSave);
                if (res.success && props.currentVideo && props.currentVideo.id === dataToSave.video_id) { autoSaveStatus.value = 'success'; setTimeout(() => { if(autoSaveStatus.value === 'success') autoSaveStatus.value = ''; }, 2000); } 
                else { autoSaveStatus.value = ''; }
            } catch (e) { autoSaveStatus.value = ''; }
        }

        async function executeRenameQueue(mode = 'queue_only') {
            if (pendingRenameData) { clearTimeout(renameTimeoutId); await executePendingRename(); }
            if (isExecuting.value) return;
            try {
                isExecuting.value = true; executeSuccess.value = false; executeMsg.value = '执行中...';
                const res = await window.DyAPI.executeRenames(mode);
                isExecuting.value = false;
                if (res.success) { executeMsg.value = res.msg; executeSuccess.value = true; emit('reload-videos', false, props.currentIndex); } 
                else { executeMsg.value = '执行异常'; executeSuccess.value = true; }
            } catch (e) { isExecuting.value = false; executeMsg.value = '网络异常'; executeSuccess.value = true; }
        }
        
        async function retryFailedQueue() {
            if (isExecuting.value) return;
            try {
                isExecuting.value = true; executeSuccess.value = false; executeMsg.value = '重置中...';
                const res = await window.DyAPI.retryFailed();
                isExecuting.value = false;
                if (res.success) { executeMsg.value = res.msg; executeSuccess.value = true; } else { executeMsg.value = '重置失败'; executeSuccess.value = true; }
            } catch (e) { isExecuting.value = false; executeMsg.value = '网络异常'; executeSuccess.value = true; }
        }

        const addPathLocal = () => emit('add-path', newPath.value);
        const indexPath = (p) => emit('index-path', p);
        const indexPath_del = (p) => emit('index-path-del', p);
        const addBlacklistWord = () => emit('add-blacklist', newBlacklistWord.value);
        const removeBlacklistWord = (idx) => emit('remove-blacklist', idx);
        const openConfigPanel = async () => { showConfig.value = true; };
        const openHistoryPanel = async () => { showHistory.value = true; const res = await window.DyAPI.getRenameHistory(); if (res && res.success) renameHistory.value = res.history; }
        
        async function handleRestore(logId) {
            vant.showLoadingToast({ message: '正在撤销恢复...', forbidClick: true, duration: 0 });
            const res = await window.DyAPI.restoreRename(logId); vant.closeToast();
            if (res && res.success) { vant.showSuccessToast('文件已恢复'); await openHistoryPanel(); emit('reload-videos', false, props.currentIndex); } else { vant.showFailToast(res?.msg || '恢复失败'); }
        }

        function exportTags() { 
            let payload;
            if (exportTarget.value === 'ALL') {
                payload = { version: 2, current: currentProfileName.value, profiles: tagProfiles.value };
            } else {
                payload = { version: 2, current: exportTarget.value, profiles: { [exportTarget.value]: tagProfiles.value[exportTarget.value] } };
            }
            const dataStr = JSON.stringify(payload, null, 2); 
            const blob = new Blob([dataStr], { type: "application/json" }); 
            const url = URL.createObjectURL(blob); 
            const a = document.createElement('a'); a.href = url; a.download = `dy_tags_${exportTarget.value === 'ALL' ? 'all' : exportTarget.value}_${new Date().getTime()}.json`; a.click(); 
            URL.revokeObjectURL(url); 
            vant.showSuccessToast('配置导出成功'); 
        }

        function triggerImportTags() { document.getElementById('import-tags-file').click(); }
        
        function importTags(event) {
            const file = event.target.files[0]; if (!file) return; const reader = new FileReader();
            reader.onload = (e) => { 
                try { 
                    const parsed = JSON.parse(e.target.result); 
                    if (parsed.version === 2 && parsed.profiles) {
                        for (const key in parsed.profiles) {
                            tagProfiles.value[key] = parsed.profiles[key];
                        }
                        currentProfileName.value = parsed.current || Object.keys(parsed.profiles)[0];
                        localStorage.setItem('dy_tag_profiles', JSON.stringify(tagProfiles.value));
                        localStorage.setItem('dy_current_profile_name', currentProfileName.value);
                        switchProfile();
                        vant.showSuccessToast('模板导入并合并成功');
                    } else if (Array.isArray(parsed)) { 
                        const newName = '导入模板_' + new Date().getTime().toString().slice(-4);
                        tagProfiles.value[newName] = parsed;
                        currentProfileName.value = newName;
                        localStorage.setItem('dy_tag_profiles', JSON.stringify(tagProfiles.value));
                        localStorage.setItem('dy_current_profile_name', currentProfileName.value);
                        switchProfile();
                        vant.showSuccessToast('旧版配置已导入为新模板'); 
                    } else {
                        vant.showFailToast('格式不正确'); 
                    }
                } catch(err) { vant.showFailToast('解析失败'); } 
                event.target.value = ''; 
            };
            reader.readAsText(file);
        }

        function exportCSV() {
            const params = [];
            if (props.searchState.searchKeyword) params.push(`search=${encodeURIComponent(props.searchState.searchKeyword)}`);
            if (props.searchState.excludeKeyword) params.push(`exclude=${encodeURIComponent(props.searchState.excludeKeyword)}`);
            if (props.searchState.searchScore > 0) params.push(`score=${props.searchState.searchScore}`);
            if (props.searchState.searchSize !== 0) params.push(`size=${props.searchState.searchSize}`);
            if (props.searchState.sortBy) params.push(`sort_by=${props.searchState.sortBy}`);
            if (exportLimit.value !== '0') params.push(`limit=${exportLimit.value}`);
            window.open(`/dyfn/sys_tags/export_csv?${params.join('&')}`, '_blank');
        }
        function triggerImportCSV() { document.getElementById('import-csv-data-file').click(); }
        async function importCSVFile(event) {
            const file = event.target.files[0]; if (!file) return; const formData = new FormData(); formData.append('file', file);
            vant.showLoadingToast({ message: '正在导入...', forbidClick: true, duration: 0 });
            try { const res = await axios.post('/dyfn/sys_tags/import_csv', formData, { headers: { 'Content-Type': 'multipart/form-data' } }); vant.closeToast(); if (res.data.success) vant.showSuccessToast(res.data.msg); else vant.showFailToast(res.data.msg || '导入失败'); } catch (err) { vant.closeToast(); vant.showFailToast('网络或服务器异常'); }
            event.target.value = '';
        }

        function exportRenameLogCSV() {
            const params = [];
            if (props.searchState.searchKeyword) params.push(`search=${encodeURIComponent(props.searchState.searchKeyword)}`);
            if (props.searchState.excludeKeyword) params.push(`exclude=${encodeURIComponent(props.searchState.excludeKeyword)}`);
            if (props.searchState.searchScore > 0) params.push(`score=${props.searchState.searchScore}`);
            if (props.searchState.searchSize !== 0) params.push(`size=${props.searchState.searchSize}`);
            if (props.searchState.sortBy) params.push(`sort_by=${props.searchState.sortBy}`);
            if (exportLogLimit.value !== '0') params.push(`limit=${exportLogLimit.value}`);
            window.open(`/dyfn/sys_tags/export_rename_compare_csv?${params.join('&')}`, '_blank');
        }

        expose({ openConfigPanel, openHistoryPanel, autoSaveStatus, restoreOriginalName });

        return {
            panelOrder, dragEnabledId, onDragStart, onDragEnd, onDrop, enableDrag, disableDrag,
            autoTags, toggleAutoTag, tagGroups, globalTagInput, dragGroupEnabledId, filenameTokens, currentCustomTags, quickBlacklistInput, autoSaveStatus, dragType, draggedGroupIdx, dragOverGroupIdx, dragOverTagInfo, dragDropPosition,
            generatedFilename, setAsOnlyToken, toggleToken, restoreOriginalName, addQuickBlacklist, quickBlacklistToken, enableGroupDrag, disableGroupDrag, onGroupDragStart, onGroupDragEnd, onTagDragStart, onTagDragEnd, onTagDragOver, onGroupDragOver, onTagDragEnter, onTagDragLeave, onGroupDrop, onTagDrop, addEmptyGroup, removeTagGroup, addGlobalTag, removeSavedTagFromGroup, 
            isTagActive, handlePresetTagClick,
            showConfig, showHistory, renameHistory, handleRestore, newPath, newBlacklistWord, exportLimit, exportLogLimit, isExecuting, executeSuccess, executeMsg,
            addPathLocal, indexPath, indexPath_del, addBlacklistWord, removeBlacklistWord, executeRenameQueue, retryFailedQueue, triggerImportTags, importTags, exportCSV, triggerImportCSV, importCSVFile, exportRenameLogCSV,
            tagProfiles, currentProfileName, switchProfile, createNewProfile, deleteCurrentProfile, exportTarget, exportTags,
            activeAutoTags, archiveRootDir, archiveThreshold, archiveMaxPer, archivePlan, isArchiving, forceRearchive, archivePersonRegex, getArchivePlan, executeArchive, fixLegacyFilenames
        };
    }
};