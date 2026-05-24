const { createApp, ref, computed, watch, onMounted, nextTick } = Vue;

const DocsApp = {
    setup() {
        const currentMode = ref('view'); 
        const previousMode = ref('view'); 
        const isSidebarOpen = ref(window.innerWidth >= 768);
        const rawDocs = ref([]);
        const activeDoc = ref(null);
        const isCreating = ref(false); 
        const maxLevel = 2;
        const abortController = ref(null);

        const dragOverTarget = ref(null);
        const searchQuery = ref('');
        const isSearching = ref(false);
        const searchResults = ref(null);
        let searchTimeout = null;
        let chartInstance = null;

        const folderState = ref(JSON.parse(localStorage.getItem('docFolderState') || '{}'));
        watch(folderState, (newVal) => { localStorage.setItem('docFolderState', JSON.stringify(newVal)); }, { deep: true });

        const isPreview = ref(false);
        const saveSuccess = ref(false);
        const docTitle = ref('');
        const oldDocTitle = ref('');
        const editContent = ref('');
        const editPreviewHtml = ref('');
        const editorRef = ref(null);
        
        const selectedDocs = ref([]);
        const showMoveModal = ref(false);

        const viewHtml = ref('');
        const viewScrollContainer = ref(null);
        const sidebarScroll = ref(null);
        const csvFileRef = ref(null); 

        // =============== 核心模式切换 ===============
        const switchMode = async (mode) => {
            if (currentMode.value !== mode) {
                previousMode.value = currentMode.value;
            }
            currentMode.value = mode;
            selectedDocs.value = [];
            
            if (mode === 'graph') {
                activeDoc.value = null; // 确保进入图谱时没有文章处于激活态
                nextTick(() => renderGraph());
                return;
            }
            
            if (activeDoc.value) {
                await loadDoc(activeDoc.value);
            } else if (mode === 'edit') {
                createNew();
            } else {
                isCreating.value = false;
            }
        };

        const allFolderPaths = computed(() => {
            let paths = [];
            for (let f1 in tree.value.folders) {
                paths.push(tree.value.folders[f1].path);
                for (let f2 in tree.value.folders[f1].folders) paths.push(tree.value.folders[f1].folders[f2].path);
            }
            return paths;
        });

        const tree = computed(() => {
            let root = { items: [], folders: {} };
            rawDocs.value.forEach(docObj => {
                let docName = docObj.id;
                let parts = docName.split('_');
                let depth = Math.min(parts.length - 1, maxLevel);
                let folders = parts.slice(0, depth);
                
                let currentLevel = root;
                let currentPath = '';

                folders.forEach(f => {
                    currentPath = currentPath ? `${currentPath}_${f}` : f;
                    if (!currentLevel.folders[f]) {
                        if (folderState.value[currentPath] === undefined) folderState.value[currentPath] = false; 
                        currentLevel.folders[f] = { name: f, path: currentPath, items: [], folders: {} };
                    }
                    currentLevel = currentLevel.folders[f];
                });
                currentLevel.items.push(docObj);
            });
            return root;
        });

        const getTitle = (docId) => {
            const doc = rawDocs.value.find(d => d.id === docId);
            return doc ? doc.title : docId;
        };

        const loadList = async () => {
            try {
                const res = await fetch('/api/docs/list');
                const data = await res.json();
                rawDocs.value = data.docs;
                
                if(currentMode.value === 'graph' && chartInstance) {
                    renderGraph();
                }
            } catch (e) { console.error('列表获取失败', e); }
        };

        // =============== API 与加载逻辑 ===============
        const loadDoc = async (docId) => {
            activeDoc.value = docId;
            isCreating.value = false;
            docTitle.value = docId;
            oldDocTitle.value = docId;

            // 移动端处理：不论原先是什么模式，只要加载文档，就关闭侧边栏
            if (window.innerWidth < 768) isSidebarOpen.value = false;
            
            if (abortController.value) abortController.value.abort();
            abortController.value = new AbortController();
            const signal = abortController.value.signal;

            NProgress.start();
            try {
                if (currentMode.value === 'edit') {
                    const res = await fetch(`/api/docs/raw/${encodeURIComponent(docId)}`, { signal });
                    if (!res.ok) throw new Error('读取失败');
                    const data = await res.json();
                    editContent.value = data.content;
                    if (isPreview.value) await refreshPreview();
                } else {
                    viewHtml.value = ''; // 先清空，给 Vue 明确的渲染信号
                    const resFast = await fetch(`/api/docs/content/${encodeURIComponent(docId)}`, { signal });
                    const dataFast = await resFast.json();
                    if (dataFast.error) throw new Error(dataFast.error);
                    
                    viewHtml.value = dataFast.html;
                    
                    if (viewScrollContainer.value) viewScrollContainer.value.scrollTop = 0;
                    NProgress.set(0.6);
                    
                    const resFresh = await fetch(`/api/docs/refresh/${encodeURIComponent(docId)}`, { signal });
                    const dataFresh = await resFresh.json();
                    if (!dataFresh.error && dataFresh.html !== dataFast.html) {
                        viewHtml.value = dataFresh.html;
                    }
                }
            } catch (e) {
                if (e.name !== 'AbortError') {
                    viewHtml.value = `<div class="text-red-500 p-5 text-center">⚠️ 加载失败: ${e.message}</div>`;
                }
            } finally { 
                NProgress.done(); 
            }
        };

        // =============== 交互事件 =================
        const handleDocClick = (e, docId) => {
            if (currentMode.value === 'edit' && (e.ctrlKey || e.metaKey)) {
                toggleSelect(docId);
            } else {
                if (!selectedDocs.value.includes(docId) || selectedDocs.value.length > 1) {
                    selectedDocs.value = [];
                }
                if (currentMode.value === 'graph') {
                    // 如果身处图谱，点击侧边栏意味着要退回阅读状态并加载文章
                    switchMode('view').then(() => {
                        loadDoc(docId);
                    });
                } else {
                    loadDoc(docId);
                }
            }
        };

        // =============== 图表与 CSV 逻辑 ===============
        const triggerFileInput = () => {
            if (csvFileRef.value) csvFileRef.value.click(); 
        };

        const exportCSV = () => { window.location.href = `/api/docs/export_untagged`; };

        const importCSV = (event) => {
            const file = event.target.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            fetch(`/api/docs/import_tags`, { method: 'POST', body: formData })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    alert(`✅ 成功更新了 ${data.updated} 个文档标签`);
                    loadList();
                } else alert(data.error);
            }).catch(err => alert("上传处理失败"));
            event.target.value = '';
        };

        const renderGraph = () => {
            const container = document.getElementById('md-graph-container');
            if (!container) return;
            
            // 每次重新渲染图谱时，最好 dispose 掉旧实例防止内存泄露和事件绑定堆叠
            if (chartInstance) {
                chartInstance.dispose();
            }
            chartInstance = echarts.init(container);

            const nodes = [];
            const links = [];
            const categoriesSet = new Set();
            const tagMap = new Map(); 
            const tagCounts = {}; 

            rawDocs.value.forEach(page => {
                let pTags = page.tags ? page.tags.split(/[,，]/).map(t => t.trim()).filter(t => t) : ['未分类'];
                if(pTags.length === 0) pTags = ['未分类'];
                pTags.forEach(tag => tagCounts[tag] = (tagCounts[tag] || 0) + 1);
            });

            // 核心修复点：为标签节点显式指定 Category 且使其与子节点挂钩
            rawDocs.value.forEach(page => {
                let pTags = page.tags ? page.tags.split(/[,，]/).map(t => t.trim()).filter(t => t) : ['未分类'];
                if(pTags.length === 0) pTags = ['未分类'];
                
                pTags.forEach(tag => {
                    categoriesSet.add(tag);
                    if (!tagMap.has(tag)) {
                        const count = tagCounts[tag];
                        const nodeSize = Math.min(100, 40 + count * 5); 
                        nodes.push({
                            id: 'TAG_' + tag,
                            name: tag, 
                            value: count,
                            symbolSize: nodeSize,
                            category: tag, // 这里必须是真实的分类名称
                            itemStyle: { borderWidth: 3, borderColor: '#fff', shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' },
                            label: { show: true, formatter: '{b}\n({c})', fontSize: 14, fontWeight: 'bold' }
                        });
                        tagMap.set(tag, true);
                    }
                    links.push({ source: page.id, target: 'TAG_' + tag });
                });

                // 文章节点
                nodes.push({
                    id: page.id,
                    name: page.title,
                    symbolSize: 18,
                    category: pTags[0], // 子节点继承第一个 Tag 作为颜色分类
                    label: { show: true, position: 'right', formatter: '{b}' }
                });
            });

            // 生成完整的分类对象数组
            const categories = Array.from(categoriesSet).map(name => ({ name: name }));
            
            const option = {
                tooltip: { 
                    formatter: function(p) {
                        if(p.data.id && p.data.id.startsWith('TAG_')) return `🏷️ <b>${p.data.name}</b><br/>共聚集了 ${p.data.value} 个项目`;
                        return `📄 ${p.data.name}`;
                    } 
                },
                legend: { data: categories.map(c => c.name), type: 'scroll', bottom: 10 },
                series: [{
                    type: 'graph', layout: 'force',
                    force: { repulsion: 300, edgeLength: [40, 90] },
                    data: nodes, links: links, categories: categories,
                    roam: true, label: { position: 'right' },
                    lineStyle: { color: 'source', curveness: 0.1 },
                    // 关闭 ECharts 自动的高亮功能，由外部 JS 事件去调度
                    emphasis: {
                        focus: 'none',
                        lineStyle: { width: 3 }
                    }
                }]
            };

            chartInstance.setOption(option);

            let hoverTimer = null;

            chartInstance.on('mouseover', function (params) {
                if (params.dataType === 'node') {
                    hoverTimer = setTimeout(() => {
                        chartInstance.dispatchAction({
                            type: 'highlight',
                            seriesIndex: 0,
                            dataIndex: params.dataIndex // dispatch 需要的是在数组中的绝对索引
                        });
                    }, 500); // 防抖 500ms
                }
            });

            chartInstance.on('mouseout', function (params) {
                if (hoverTimer) {
                    clearTimeout(hoverTimer);
                    hoverTimer = null;
                }
                chartInstance.dispatchAction({
                    type: 'downplay',
                    seriesIndex: 0
                });
            });

            chartInstance.on('click', function (params) {
                if (params.data) {
                    chartInstance.dispatchAction({
                        type: 'highlight',
                        seriesIndex: 0,
                        dataIndex: params.dataIndex
                    });

                    // 核心修复：如果是文档节点，执行页面状态转换并加载文章
                    if (!params.data.id.startsWith('TAG_')) {
                        // 在异步处理中等待视图切换，再拉取文档
                        switchMode('view').then(() => {
                            loadDoc(params.data.id);
                        });
                    }
                }
            });
        };

        // =============== 其它基础功能 =================
        const createNew = () => {
            if (currentMode.value !== 'edit') switchMode('edit');
            activeDoc.value = null;
            isCreating.value = true;
            docTitle.value = '';
            oldDocTitle.value = '';
            editContent.value = '---\ntags: 新建标签\n---\n# 新文档\n\n正文内容';
            if (window.innerWidth < 768) isSidebarOpen.value = false;
            if (isPreview.value) togglePreview();
            setTimeout(() => editorRef.value?.focus(), 50);
        };

        const saveDoc = async () => {
            let name = docTitle.value.trim();
            const old_name = oldDocTitle.value.trim();
            const docContent = editContent.value;
            if (!name) return alert('请手动输入文件名');

            NProgress.start();
            try {
                const res = await fetch('/api/docs/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, old_name, content: docContent })
                });
                if (res.ok) {
                    oldDocTitle.value = name;
                    activeDoc.value = name;
                    await loadList();
                    saveSuccess.value = true;
                    setTimeout(() => saveSuccess.value = false, 1500);
                    if (isPreview.value) await refreshPreview();
                } else {
                    const err = await res.json();
                    alert('保存失败: ' + err.error);
                }
            } catch (e) { alert('网络错误，保存失败');
            } finally { NProgress.done(); }
        };

        const deleteDoc = async (docId) => {
            if (!docId) return;
            if (!confirm(`确定要删除文档 "${docId}" 吗？`)) return;
            NProgress.start();
            try {
                const res = await fetch(`/api/docs/delete/${encodeURIComponent(docId)}`, { method: 'DELETE' });
                if (res.ok) {
                    if (oldDocTitle.value === docId) activeDoc.value = null;
                    await loadList();
                } else alert('删除失败');
            } catch (e) { alert('网络错误'); } finally { NProgress.done(); }
        };

        const togglePreview = async () => {
            isPreview.value = !isPreview.value;
            if (isPreview.value) await refreshPreview();
        };

        const refreshPreview = async () => {
            const name = oldDocTitle.value;
            if (name) {
                try {
                    const res = await fetch(`/api/docs/content/${encodeURIComponent(name)}`);
                    if (res.ok) {
                        const data = await res.json();
                        editPreviewHtml.value = data.html;
                        return;
                    }
                } catch (e) {}
            }
            editPreviewHtml.value = '<div class="text-slate-400 pt-5">* 需先点击“保存修改”。</div>';
        };

        const debouncedSearch = () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(async () => {
                if (!searchQuery.value.trim()) { searchResults.value = null; return; }
                isSearching.value = true;
                try {
                    const res = await fetch(`/api/docs/search?q=${encodeURIComponent(searchQuery.value)}`);
                    searchResults.value = (await res.json()).results || [];
                } catch(e) { searchResults.value = []; } finally { isSearching.value = false; }
            }, 300);
        };

        const expandAll = () => { for (let key in folderState.value) folderState.value[key] = true; };
        const collapseAll = () => { for (let key in folderState.value) folderState.value[key] = false; };
        const toggleFolder = (path) => { folderState.value[path] = !folderState.value[path]; };
        const toggleSelect = (docId) => {
            if (currentMode.value !== 'edit') return;
            const idx = selectedDocs.value.indexOf(docId);
            if (idx > -1) selectedDocs.value.splice(idx, 1);
            else selectedDocs.value.push(docId);
        };

        const autoScroll = (e) => {
            if (!sidebarScroll.value) return;
            const threshold = 60; 
            const rect = sidebarScroll.value.getBoundingClientRect();
            if (e.clientY - rect.top < threshold) sidebarScroll.value.scrollTop -= 15;
            else if (rect.bottom - e.clientY < threshold) sidebarScroll.value.scrollTop += 15;
        };

        const dragStart = (e, docId, currentPath) => {
            if (currentMode.value !== 'edit') {
                e.preventDefault();
                return;
            }
            let dragItems = [];
            if (selectedDocs.value.includes(docId)) {
                dragItems = selectedDocs.value.map(id => {
                    let parts = id.split('_');
                    let depth = Math.min(parts.length - 1, maxLevel);
                    return { docId: id, currentPath: parts.slice(0, depth).join('_') };
                });
            } else {
                dragItems = [{ docId, currentPath }];
            }
            e.dataTransfer.setData('application/json', JSON.stringify(dragItems));
            e.dataTransfer.effectAllowed = 'move';
        };

        const handleDrop = async (e, targetPath) => {
            dragOverTarget.value = null;
            const dataStr = e.dataTransfer.getData('application/json');
            if (!dataStr) return;

            try {
                const dragItems = JSON.parse(dataStr);
                NProgress.start();
                
                let hasChanges = false;
                for (const item of dragItems) {
                    const oldId = item.docId;
                    const currentPath = item.currentPath;
                    
                    let pureFileName = oldId;
                    if (currentPath && oldId.startsWith(currentPath + '_')) {
                        pureFileName = oldId.substring(currentPath.length + 1);
                    }

                    const newId = targetPath ? `${targetPath}_${pureFileName}` : pureFileName;
                    if (oldId === newId) continue;
                    
                    hasChanges = true;
                    await fetch('/api/docs/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ oldName: oldId, newName: newId })
                    });

                    if (activeDoc.value === oldId) {
                        activeDoc.value = newId;
                        oldDocTitle.value = newId;
                        docTitle.value = newId;
                    }
                }
                
                if (hasChanges) {
                    selectedDocs.value = [];
                    await loadList();
                }
            } catch (err) {
                alert(`⚠️ 文件移动失败: ${err.message}`);
            } finally {
                NProgress.done();
            }
        };

        const executeBulkMove = async (targetPath) => {
            showMoveModal.value = false;
            NProgress.start();
            try {
                for (let oldId of selectedDocs.value) {
                    let parts = oldId.split('_');
                    let depth = Math.min(parts.length - 1, maxLevel);
                    let currentPath = parts.slice(0, depth).join('_');
                    
                    let pureFileName = oldId;
                    if (currentPath && oldId.startsWith(currentPath + '_')) {
                        pureFileName = oldId.substring(currentPath.length + 1);
                    }
                    const newId = targetPath ? `${targetPath}_${pureFileName}` : pureFileName;
                    if (oldId === newId) continue;

                    await fetch('/api/docs/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ oldName: oldId, newName: newId })
                    });

                    if (activeDoc.value === oldId) {
                        activeDoc.value = newId;
                        oldDocTitle.value = newId;
                        docTitle.value = newId;
                    }
                }
                selectedDocs.value = [];
                await loadList();
            } catch (err) {
                alert("部分文件移动失败");
            } finally {
                NProgress.done();
            }
        };

        const renameFolder = async (folderPath) => {
            const newName = prompt(`重命名文件夹 "${folderPath}"\n请输入新名称 (请勿包含下划线):`);
            if (!newName || newName.includes('_')) return;

            const pathParts = folderPath.split('_');
            pathParts[pathParts.length - 1] = newName;
            const newPath = pathParts.join('_');

            if (folderPath === newPath) return;

            NProgress.start();
            const filesToRename = rawDocs.value.filter(doc => doc.id.startsWith(folderPath + '_'));

            try {
                for (const docObj of filesToRename) {
                    const oldId = docObj.id;
                    const pureName = oldId.substring(folderPath.length + 1);
                    const newId = `${newPath}_${pureName}`;
                    
                    await fetch('/api/docs/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ oldName: oldId, newName: newId })
                    });

                    if (activeDoc.value === oldId) {
                        activeDoc.value = newId;
                        oldDocTitle.value = newId;
                        docTitle.value = newId;
                    }
                }
                if (folderState.value[folderPath] !== undefined) {
                    folderState.value[newPath] = folderState.value[folderPath];
                }
                await loadList();
            } catch(err) {
                alert("部分文件重命名失败，请检查网络");
            } finally {
                NProgress.done();
            }
        };

        window.addEventListener('resize', () => { if (chartInstance && currentMode.value === 'graph') chartInstance.resize(); });
        
        onMounted(() => { 
            loadList(); 
        });

        return {
            currentMode, previousMode, switchMode, isSidebarOpen, 
            tree, folderState, activeDoc, isCreating, dragOverTarget, isPreview, saveSuccess,
            docTitle, editContent, editPreviewHtml, editorRef, sidebarScroll, viewScrollContainer, csvFileRef,
            selectedDocs, showMoveModal, allFolderPaths,
            toggleFolder, createNew, loadDoc, saveDoc, deleteDoc, togglePreview, renameFolder,
            handleDocClick, toggleSelect, expandAll, collapseAll, viewHtml, getTitle,
            searchQuery, isSearching, searchResults, debouncedSearch, 
            exportCSV, importCSV, triggerFileInput, executeBulkMove, autoScroll, dragStart, handleDrop
        };
    }
};

createApp(DocsApp).mount('#app');