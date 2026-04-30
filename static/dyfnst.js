// stats-panel.js
const StatsPanel = {
    template: `
        <van-popup v-model:show="showStats" position="bottom" :style="{ height: '90%' }" round closeable @closed="resetData">
            <div style="padding: 20px; color: #eee; background: #1a1a1a; height: 100%; box-sizing: border-box; overflow-y: auto;">
                <h3 style="margin-top: 0; color: #fff; border-bottom: 1px solid #333; padding-bottom: 10px;">
                    📊 全局资产分析与 Tag 诊断
                </h3>

                <div v-if="loading" style="text-align: center; margin-top: 50px; color: #888;">
                    <van-loading type="spinner" color="#1989fa" />
                    <p>正在拉取全库数据并进行清洗分析，请稍候...</p>
                </div>

                <div v-else-if="!loading && totalVideos > 0">
                    <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                        <div class="stat-card">
                            <div class="stat-num">{{ totalVideos }}</div>
                            <div class="stat-label">总视频数</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-num">{{ manualTagsList.length }}</div>
                            <div class="stat-label">人工 Tag 总量</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-num">{{ autoTagsList.length }}</div>
                            <div class="stat-label">虚拟 Tag 总量</div>
                        </div>
                    </div>

                    <div style="display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 20px;">
                        <div style="flex: 1; min-width: 300px; background: rgba(0,0,0,0.3); border: 1px solid #333; border-radius: 8px; padding: 15px;">
                            <h4 style="margin-top: 0; color: #4fc08d;">🏆 人工 Tag 分布 (Top 30)</h4>
                            <div v-for="(tag, idx) in topManualTags" :key="tag.name" style="display: flex; align-items: center; margin-bottom: 8px;">
                                <div style="width: 20px; font-size: 12px; color: #888;">{{ idx + 1 }}.</div>
                                <div style="flex: 1; font-size: 13px;">#{{ tag.name }}</div>
                                <div style="width: 150px; background: #222; border-radius: 4px; height: 10px; margin: 0 10px; overflow: hidden;">
                                    <div :style="{ width: (tag.count / maxManualCount * 100) + '%', background: '#4fc08d', height: '100%' }"></div>
                                </div>
                                <div style="font-size: 12px; color: #aaa; width: 40px; text-align: right;">{{ tag.count }} 次</div>
                            </div>
                        </div>

                        <div style="flex: 1; min-width: 300px; background: rgba(0,0,0,0.3); border: 1px solid #333; border-radius: 8px; padding: 15px;">
                            <h4 style="margin-top: 0; color: #c39bd3;">🤖 机器虚拟 Tag 分布 (Top 30)</h4>
                            <div v-for="(tag, idx) in topAutoTags" :key="tag.name" style="display: flex; align-items: center; margin-bottom: 8px;">
                                <div style="width: 20px; font-size: 12px; color: #888;">{{ idx + 1 }}.</div>
                                <div style="flex: 1; font-size: 13px; color: #c39bd3;">#a_{{ tag.name }}</div>
                                <div style="width: 150px; background: #222; border-radius: 4px; height: 10px; margin: 0 10px; overflow: hidden;">
                                    <div :style="{ width: (tag.count / maxAutoCount * 100) + '%', background: '#8e44ad', height: '100%' }"></div>
                                </div>
                                <div style="font-size: 12px; color: #aaa; width: 40px; text-align: right;">{{ tag.count }} 次</div>
                            </div>
                        </div>
                    </div>

                    <div v-if="abnormalTags.length > 0" style="background: rgba(255, 77, 79, 0.05); border: 1px solid #552222; border-radius: 8px; padding: 15px; margin-bottom: 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <h4 style="margin: 0; color: #ff4d4f;">⚠️ 低频/孤立 Tag 诊断</h4>
                            <van-button size="mini" type="default" style="background: transparent; color: #ccc; border-color: #555;" @click="expandedAbnormal = !expandedAbnormal">
                                {{ expandedAbnormal ? '收起' : '展开全部 (' + abnormalTags.length + ')' }}
                            </van-button>
                        </div>
                        <p style="font-size: 12px; color: #bbb; margin-top: 0;">以下标签在全库中仅出现 1 次。注意：自动提取的特定人物名出现一次属正常现象；若是错别字或长句，建议返回主界面拉黑剔除。</p>
                        <div style="display: flex; flex-wrap: wrap; gap: 6px;">
                            <span v-for="tag in displayedAbnormalTags" :key="tag.name" style="background: #222; color: #ff4d4f; padding: 4px 8px; border-radius: 4px; font-size: 12px; border: 1px solid #442222;">
                                #{{ tag.type === 'auto' ? 'a_' : '' }}{{ tag.name }}
                            </span>
                            <span v-if="!expandedAbnormal && abnormalTags.length > displayLimit" style="color: #888; font-size: 12px; padding: 4px;">... 等 {{ abnormalTags.length - displayLimit }} 个</span>
                        </div>
                    </div>

                </div>

                <div v-else-if="!loading && totalVideos === 0" style="text-align: center; margin-top: 50px; color: #888;">
                    暂无视频数据
                </div>
            </div>

            <component is="style">
                .stat-card { flex: 1; background: rgba(255,255,255,0.05); border: 1px solid #333; border-radius: 8px; padding: 15px; text-align: center; }
                .stat-num { font-size: 24px; font-weight: bold; color: #1989fa; margin-bottom: 5px; }
                .stat-label { font-size: 12px; color: #888; }
            </component>
        </van-popup>
    `,
    setup() {
        const { ref, computed } = Vue;
        const showStats = ref(false);
        const loading = ref(false);
        const totalVideos = ref(0);
        
        const manualTagsMap = ref({});
        const autoTagsMap = ref({});
        
        const expandedAbnormal = ref(false);
        const displayLimit = 15;

        const resetData = () => {
            manualTagsMap.value = {};
            autoTagsMap.value = {};
            totalVideos.value = 0;
            expandedAbnormal.value = false;
        };

        const analyzeData = async () => {
            showStats.value = true;
            loading.value = true;
            resetData();

            try {
                const videos = await window.DyAPI.fetchVideos({ page_size: 0 });
                totalVideos.value = videos.length;

                const mTags = {};
                const aTags = {};

                videos.forEach(v => {
                    if (!v.filename) return;
                    const extracted = v.filename.match(/(#a_|#)[\u4e00-\u9fa5a-zA-Z0-9_]+/g);
                    if (extracted) {
                        extracted.forEach(t => {
                            if (t.startsWith('#a_')) {
                                const name = t.substring(3).trim();
                                if(name) aTags[name] = (aTags[name] || 0) + 1;
                            } else if (t.startsWith('#')) {
                                const name = t.substring(1).trim();
                                if(name) mTags[name] = (mTags[name] || 0) + 1;
                            }
                        });
                    }
                });

                manualTagsMap.value = mTags;
                autoTagsMap.value = aTags;
            } catch (error) {
                vant.showFailToast('获取全库数据失败');
            } finally {
                loading.value = false;
            }
        };

        const formatTags = (tagMap) => {
            return Object.entries(tagMap)
                .map(([name, count]) => ({ name, count }))
                .sort((a, b) => b.count - a.count);
        };

        const manualTagsList = computed(() => formatTags(manualTagsMap.value));
        const autoTagsList = computed(() => formatTags(autoTagsMap.value));

        const topManualTags = computed(() => manualTagsList.value.slice(0, 30));
        const topAutoTags = computed(() => autoTagsList.value.slice(0, 30));

        const maxManualCount = computed(() => topManualTags.value[0]?.count || 1);
        const maxAutoCount = computed(() => topAutoTags.value[0]?.count || 1);

        const abnormalTags = computed(() => {
            const abnormal = [];
            manualTagsList.value.forEach(t => { if (t.count === 1) abnormal.push({...t, type: 'manual'}); });
            autoTagsList.value.forEach(t => { if (t.count === 1) abnormal.push({...t, type: 'auto'}); });
            return abnormal.sort((a, b) => b.name.length - a.name.length);
        });

        const displayedAbnormalTags = computed(() => {
            if (expandedAbnormal.value) return abnormalTags.value;
            return abnormalTags.value.slice(0, displayLimit);
        });

        return { 
            showStats, loading, analyzeData, resetData, totalVideos,
            manualTagsList, autoTagsList, topManualTags, topAutoTags,
            maxManualCount, maxAutoCount, abnormalTags, displayedAbnormalTags,
            expandedAbnormal, displayLimit
        };
    }
};