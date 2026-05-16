import os
import re
import json
import random 
import datetime
import csv
import io
import traceback
import shutil  # 🌟 新增：用于跨盘/同盘移动文件
from flask import Flask, Blueprint, request, jsonify, Response, stream_with_context, send_file, abort, redirect, make_response
from sqlalchemy import create_engine, Column, Integer, String, BigInteger, DateTime, func, not_, asc, or_, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

try:
    import jieba
except ImportError:
    jieba = None

# ================= 本地扁平化路径配置 =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(CURRENT_DIR, 'db')
DB_PATH = f"sqlite:///{os.path.join(DB_DIR, 'videos.db')}"
PATHS_FILE = os.path.join(DB_DIR, 'video_paths.json')
BLACKLIST_FILE = os.path.join(DB_DIR, 'blacklist.json') # 仅用作历史迁移兜底
INDEX_FILE = os.path.join(CURRENT_DIR, 'index.html')
# ====================================================

Base = declarative_base()

# ================= 数据模型层 (Models) =================
class Video(Base):
    __tablename__ = 'videos'
    id = Column(Integer, primary_key=True)
    filename = Column(String)
    detail = Column(String)
    tags = Column(String)
    score = Column(Integer)
    file_size = Column(BigInteger, default=0)
    cp = Column(Integer, default=0)
    updatetime = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

class VideoRandomOrder(Base):
    __tablename__ = 'video_random_order'
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True)
    order_index = Column(Integer, index=True)

class RenameLog(Base):
    __tablename__ = 'rename_logs'
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True)
    original_path = Column(String)
    target_path = Column(String)
    status = Column(String, default='pending') 
    error_msg = Column(String, default='')
    create_time = Column(DateTime, default=datetime.datetime.now)

class BlacklistRule(Base):
    __tablename__ = 'blacklist_rules'
    id = Column(Integer, primary_key=True)
    word = Column(String, unique=True, nullable=False)

class TagPartition(Base):
    __tablename__ = 'tag_partitions'
    id = Column(Integer, primary_key=True)
    partition_id = Column(String, unique=True)
    order_index = Column(Integer, default=0)

class CustomTag(Base):
    __tablename__ = 'custom_tags'
    id = Column(Integer, primary_key=True)
    partition_id = Column(String, index=True)
    name = Column(String, nullable=False)
    order_index = Column(Integer, default=0)


# ================= 🌟 核心提取引擎 (下沉至 SQLite 原生) =================
class TagFilter:
    # 自动 tag：#a_xxx
    _AUTO_TAG = re.compile(r'#a_[A-Za-z0-9_\u4e00-\u9fa5]+')

    # 人工 tag：只允许 # 后面紧跟纯中文或纯英文。不允许带下划线、数字等废符号。
    _MANUAL_TAG = re.compile(r'#[A-Za-z\u4e00-\u9fa5]+(?![_0-9A-Za-z\u4e00-\u9fa5])')

    @classmethod
    def is_untagged_or_auto(cls, filename: str) -> bool:
        """
        True = 没有有效人工tag（即忽略自动tag后，剩下的全是不合格杂碎标签，视为未打标）
        False = 存在符合严格规定的人工tag
        """
        if not filename: return True
        tmp = cls._AUTO_TAG.sub('', filename)
        return not bool(cls._MANUAL_TAG.search(tmp))


engine = create_engine(DB_PATH, echo=False)

@event.listens_for(Engine, "connect")
def sqlite_engine_connect(dbapi_connection, connection_record):
    """🌟 黑科技：将 Python 正则打包成底层 SQLite 函数，打通跨层面的高速 SQL 搜索过滤"""
    def _is_untagged(filename):
        return 1 if TagFilter.is_untagged_or_auto(filename) else 0
        
    try:
        # 在 SQLite 引擎中直接注册 `is_untagged` 供 SQL 语句使用
        dbapi_connection.create_function("is_untagged", 1, _is_untagged)
    except Exception:
        pass

Session = sessionmaker(bind=engine)

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    Base.metadata.create_all(engine)


# ================= 核心逻辑服务层 (Services) =================
class BlacklistService:
    @staticmethod
    def get_all(session):
        records = session.query(BlacklistRule).all()
        return [r.word for r in records if r.word.strip()]

    @staticmethod
    def sync_list(session, words):
        session.query(BlacklistRule).delete()
        for w in set(words):
            if w.strip():
                session.add(BlacklistRule(word=w.strip()))
        session.commit()

class TagService:
    @staticmethod
    def get_all_groups(session):
        partitions = session.query(TagPartition).order_by(TagPartition.order_index).all()
        result = []
        for p in partitions:
            tags = session.query(CustomTag).filter(CustomTag.partition_id == p.partition_id).order_by(CustomTag.order_index).all()
            result.append({
                "id": p.partition_id,
                "tags": [t.name for t in tags]
            })
        return result

    @staticmethod
    def sync_groups(session, groups_data):
        session.query(TagPartition).delete()
        session.query(CustomTag).delete()
        for p_idx, group in enumerate(groups_data):
            p_id = group.get('id', f'g_{p_idx}')
            session.add(TagPartition(partition_id=p_id, order_index=p_idx))
            for t_idx, t_name in enumerate(group.get('tags', [])):
                session.add(CustomTag(partition_id=p_id, name=t_name, order_index=t_idx))
        session.commit()

class RenameService:
    @staticmethod
    def extract_tags(filename):
        # 1. 提取系统自动生成的 tag (保留完整的 a_xxx)
        auto_tags = [m.group(1) for m in re.finditer(r'#(a_[A-Za-z0-9_\u4e00-\u9fa5]+)', filename)]
        # 2. 从文件名中暂时移除自动 tag，防止干扰人工 tag 提取
        tmp = re.sub(r'#a_[A-Za-z0-9_\u4e00-\u9fa5]+', '', filename)
        # 3. 提取严格规则的人工 tag (只能是纯中文或纯英文，不能有数字、下划线等符号跟随或夹杂)
        manual_tags = re.findall(r'#([A-Za-z\u4e00-\u9fa5]+)(?![_0-9A-Za-z\u4e00-\u9fa5])', tmp)
        return auto_tags + manual_tags

    @staticmethod
    def clean_filename_base(base_name, blacklist_words):
        """核心重构：在执行黑名单清洗前，将有效 Tag 隔离保护"""
        protected_tags = []
        def protect_tag(match):
            protected_tags.append(match.group(0))
            return f" __TAG_{len(protected_tags)-1}__ "
        
        # 1. 匹配 # 号后紧跟的非空字符，将其隔离保护
        temp_base = re.sub(r'#\S+', protect_tag, base_name)
        
        # 2. 对非 Tag 区域执行黑名单清洗
        needs_clean = False
        for bw in blacklist_words:
            if bw.strip() and re.search(re.escape(bw), temp_base, re.IGNORECASE):
                temp_base = re.sub(re.escape(bw), '', temp_base, flags=re.IGNORECASE)
                needs_clean = True
                
        # 3. 常规无效符号清洗
        temp_base = re.sub(r'\[\s*\]|\(\s*\)|【\s*】', '', temp_base)
        temp_base = re.sub(r'\.{2,}', '.', temp_base)
        
        # 4. 彻底清理孤立的 # 号 (因为有效的 tag 已被隔离，剩下的独立 # 号均为废弃残留)
        if re.search(r'#+', temp_base):
            temp_base = re.sub(r'#+', '', temp_base)
            needs_clean = True

        # 5. 还原被保护的标签
        for i, tag in enumerate(protected_tags):
            temp_base = temp_base.replace(f"__TAG_{i}__", tag)
            
        temp_base = re.sub(r'\s{2,}', ' ', temp_base).strip(' .-_')
        return temp_base, needs_clean

    @staticmethod
    def process_renames(session, mode='queue_only'):
        blacklist_words = BlacklistService.get_all(session)
        success_count, failed_count = 0, 0
        
        # 1. 处理待执行队列
        logs = session.query(RenameLog).filter(RenameLog.status.in_(['pending', 'retry'])).all()
        for log in logs:
            video = session.query(Video).filter(Video.id == log.video_id).first()
            if not video:
                log.status, log.error_msg = 'failed', '视频记录丢失'
                failed_count += 1; continue
                
            old_path = video.detail
            dir_name = os.path.dirname(old_path)
            ext = os.path.splitext(old_path)[1]
            
            target_base = os.path.basename(log.target_path)
            if target_base.lower().endswith(ext.lower()): 
                target_base = target_base[:-len(ext)]
            
            # 🌟 调用隔离清洗算法，保护 Tag 文本不被破坏
            target_base, _ = RenameService.clean_filename_base(target_base, blacklist_words)
            
            if not target_base: 
                target_base = f"video_{video.id}"
                
            new_filename = target_base + ext
            new_full_path = os.path.join(dir_name, new_filename)
            
            try:
                if os.path.exists(old_path) and old_path != new_full_path:
                    os.rename(old_path, new_full_path)
                elif not os.path.exists(old_path) and not os.path.exists(new_full_path):
                    log.status, log.error_msg = 'failed', '找不到物理文件'
                    failed_count += 1; continue
                
                video.filename = new_filename
                video.detail = new_full_path
                video.tags = ','.join(RenameService.extract_tags(new_filename))
                
                log.status = 'success'
                log.target_path = new_full_path 
                success_count += 1
            except Exception as e:
                log.status, log.error_msg = 'failed', str(e)
                failed_count += 1

        # 2. 全库洗牌模式
        if mode == 'full' and blacklist_words:
            for video in session.query(Video).all():
                old_path = video.detail
                if not os.path.exists(old_path): continue
                
                ext = os.path.splitext(old_path)[1]
                base_name = video.filename[:-len(ext)] if video.filename.lower().endswith(ext.lower()) else video.filename
                
                # 同步使用隔离清洗算法
                clean_base, needs_clean = RenameService.clean_filename_base(base_name, blacklist_words)
                
                if needs_clean:
                    new_filename = clean_base + ext
                    new_path = os.path.join(os.path.dirname(old_path), new_filename)
                    
                    if new_path != old_path:
                        try:
                            os.rename(old_path, new_path)
                            video.filename = new_filename
                            video.detail = new_path 
                            video.tags = ','.join(RenameService.extract_tags(new_filename))
                            success_count += 1
                        except: failed_count += 1

        session.commit()
        return success_count, failed_count
def load_paths():
    if os.path.exists(PATHS_FILE):
        with open(PATHS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_paths(paths):
    with open(PATHS_FILE, 'w', encoding='utf-8') as f:
        json.dump(paths, f, ensure_ascii=False)

def scan_directory_for_videos(path, session, existing_videos):
    valid_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.wmv', '.m4v'}
    added_count = 0
    target_files = []
    for root, dirs, files in os.walk(path):
        for file in files:
            if os.path.splitext(file)[1].lower() in valid_exts:
                target_files.append(os.path.abspath(os.path.join(root, file)))
                    
    for full_path in target_files:
        if full_path not in existing_videos:
            size = os.path.getsize(full_path) if os.path.exists(full_path) else 0
            file_name = os.path.basename(full_path)
            v = Video(filename=file_name, detail=full_path, score=0, file_size=size)
            session.add(v)
            existing_videos.add(full_path)
            added_count += 1
    return added_count

def clean_invalid_videos(session, path=None):
    query = session.query(Video)
    if path: query = query.filter(Video.detail.like(f"{os.path.abspath(path)}%"))
    invalid_ids = [video.id for video in query.all() if not os.path.isfile(video.detail)]
    removed_count = len(invalid_ids)
    if invalid_ids:
        chunk_size = 500
        for i in range(0, removed_count, chunk_size):
            chunk_ids = invalid_ids[i:i+chunk_size]
            session.query(VideoRandomOrder).filter(VideoRandomOrder.video_id.in_(chunk_ids)).delete(synchronize_session=False)
            session.query(RenameLog).filter(RenameLog.video_id.in_(chunk_ids)).delete(synchronize_session=False)
            session.query(Video).filter(Video.id.in_(chunk_ids)).delete(synchronize_session=False)
    return removed_count

def apply_size_filter(query, size_param):
    if ':' in size_param:
        operator, value = size_param.split(':', 1)
        size_bytes = int(value) * 1024 * 1024
        if operator == 'lte': query = query.filter(Video.file_size <= size_bytes)
        elif operator == 'gte': query = query.filter(Video.file_size >= size_bytes)
        elif operator == 'eq':
            margin = 10 * 1024 * 1024
            query = query.filter(Video.file_size.between(size_bytes - margin, size_bytes + margin))
    else:
        query = query.filter(Video.file_size <= int(size_param) * 1024 * 1024)
    return query

        
def apply_video_filters(query, request_args, session=None):
    score = request_args.get('score')
    search = request_args.get('search')
    exclude = request_args.get('exclude')
    tags = request_args.get('tags')
    size = request_args.get('size')
    
    # 兼容前端传入的 1/2 或 true/false
    untag_raw = str(request_args.get('untagged', '0')).lower()
    if untag_raw in ('1', 'true'): untagged = '1'
    elif untag_raw in ('2', 'false'): untagged = '2'
    else: untagged = '0'
    
    use_random = False
    
    if search and 'random' in search.lower():
        use_random = True
        search = search.replace('random', '').strip()
        
    if score and str(score) != '0': 
        query = query.filter(Video.score == int(score))
    else: 
        from sqlalchemy import or_
        query = query.filter(or_(Video.score != 1, Video.score.is_(None)))

    if tags:
        for tag in tags.split(','): 
            if tag.strip():
                query = query.filter(Video.tags.like(f"%{tag}%"))
                
    if search:
        for kw in search.strip().split(): 
            # 🌟 优化点：文本搜索同时应用到 文件名 和 路径，防止遗漏
            query = query.filter(or_(Video.detail.like(f"%{kw}%"), Video.filename.like(f"%{kw}%")))
            
    if exclude:
        for kw in exclude.strip().split(): 
            query = query.filter(~Video.detail.like(f"%{kw}%"))

    if size and str(size) != '0': 
        query = apply_size_filter(query, str(size))
        
    # 🌟 核心过滤：直接呼叫底层 SQLite 绑定的原生正则函数，不再依赖内存，分页完美生效！
    if untagged == '1':
        query = query.filter(func.is_untagged(Video.filename) == 1)
    elif untagged == '2':
        query = query.filter(func.is_untagged(Video.filename) == 0)
        
    return query, use_random
    
# ================= 视图控制与路由分配 (Views) =================
dy_bp = Blueprint('dyfn', __name__, url_prefix='/dyfn')

@dy_bp.before_request 
def setup(): init_db()

@dy_bp.route('/sys_tags/migrate_legacy', methods=['POST'])
def migrate_legacy_data():
    session = Session()
    try:
        data = request.get_json() or {}
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                legacy_bl = json.load(f)
                if legacy_bl: BlacklistService.sync_list(session, legacy_bl)
        legacy_groups = data.get('tag_groups', [])
        if legacy_groups: TagService.sync_groups(session, legacy_groups)
        return jsonify({'success': True, 'msg': '历史数据迁移成功！'})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})
    finally:
        session.close()

@dy_bp.route('/sys_tags/blacklist', methods=['GET', 'POST'])
def api_blacklist():
    session = Session()
    if request.method == 'GET':
        words = BlacklistService.get_all(session)
        session.close()
        return jsonify({'blacklist': words})
    new_bl = request.get_json().get('blacklist', [])
    BlacklistService.sync_list(session, new_bl)
    session.close()
    return jsonify({'success': True, 'msg': '黑名单更新成功'})

@dy_bp.route('/sys_tags/tag_groups', methods=['GET', 'POST'])
def api_tag_groups():
    session = Session()
    if request.method == 'GET':
        groups = TagService.get_all_groups(session)
        session.close()
        return jsonify({'success': True, 'groups': groups})
    groups_data = request.get_json().get('groups', [])
    TagService.sync_groups(session, groups_data)
    session.close()
    return jsonify({'success': True, 'msg': 'Tag分区已更新'})

@dy_bp.route('/sys_tags/tokenize', methods=['POST'])
def api_tokenize():
    if not jieba: return jsonify({'success': False, 'msg': '未安装 jieba 库'}), 500
    filename = request.get_json().get('filename', '')
    if not filename: return jsonify({'success': False, 'msg': '缺少文件名'})
    name_without_ext = os.path.splitext(filename)[0]
    words = list(jieba.cut(name_without_ext))
    core_words = [w for w in words if len(w.strip()) > 1 and not bool(re.match(r'^[^\w\u4e00-\u9fa5]+$', w))]
    return jsonify({'success': True, 'words': core_words})

@dy_bp.route('/sys_tags/queue_rename', methods=['POST'])
def queue_rename():
    data = request.json
    video_id = data.get('video_id')
    new_filename = data.get('new_filename')
    
    session = Session()
    try:
        video = session.query(Video).filter(Video.id == video_id).first()
        if not video:
            return jsonify({"success": False, "msg": "视频不存在"})
            
        target_path = os.path.join(os.path.dirname(video.detail), new_filename)
        
        existing_log = session.query(RenameLog).filter(
            RenameLog.video_id == video_id,
            RenameLog.status.in_(['pending', 'retry', 'failed'])
        ).first()
        
        if existing_log:
            existing_log.target_path = target_path
            existing_log.status = 'pending'
            existing_log.error_msg = None
            existing_log.create_time = datetime.datetime.now()
        else:
            new_log = RenameLog(
                video_id=video_id,
                original_path=video.detail,
                target_path=target_path,
                status='pending',
                create_time=datetime.datetime.now()
            )
            session.add(new_log)
            
        session.commit()
        return jsonify({"success": True})
    except Exception as e:
        session.rollback()
        return jsonify({"success": False, "msg": str(e)})
    finally:
        session.close()

@dy_bp.route('/sys_tags/execute_renames', methods=['POST'])
def api_execute_renames():
    mode = request.args.get('mode', 'queue_only')
    session = Session()
    sc, fc = RenameService.process_renames(session, mode)
    session.close()
    prefix = "全量洗库：" if mode == 'full' else "执行最新："
    return jsonify({'success': True, 'msg': f'{prefix}成功 {sc} 个，失败 {fc} 个'})

@dy_bp.route('/sys_tags/retry_failed', methods=['POST'])
def api_retry_failed():
    session = Session()
    logs = session.query(RenameLog).filter(RenameLog.status == 'failed').all()
    count = len(logs)
    if count > 0:
        for log in logs: log.status = 'retry'
        session.commit()
    session.close()
    return jsonify({'success': True, 'msg': f'已重置 {count} 个失败任务'})

@dy_bp.route('/sys_tags/rename_history', methods=['GET'])
def api_rename_history():
    session = Session()
    logs = session.query(RenameLog).filter(RenameLog.status.in_(['success', 'restored'])).order_by(RenameLog.create_time.desc()).limit(100).all()
    history = [{
        'id': log.id, 'video_id': log.video_id,
        'original_name': os.path.basename(log.original_path),
        'target_name': os.path.basename(log.target_path),
        'status': log.status,
        'create_time': log.create_time.strftime('%Y-%m-%d %H:%M:%S')
    } for log in logs]
    session.close()
    return jsonify({'success': True, 'history': history})

@dy_bp.route('/sys_tags/restore_rename', methods=['POST'])
def api_restore_rename():
    log_id = request.get_json().get('log_id')
    session = Session()
    log = session.query(RenameLog).filter(RenameLog.id == log_id).first()
    if not log or log.status != 'success':
        session.close(); return jsonify({'success': False, 'msg': '无效记录'}), 404
    try:
        if os.path.exists(log.target_path) and not os.path.exists(log.original_path):
            os.rename(log.target_path, log.original_path)
            video = session.query(Video).filter(Video.id == log.video_id).first()
            if video:
                video.filename, video.detail = os.path.basename(log.original_path), log.original_path
                video.tags = ','.join(RenameService.extract_tags(video.filename))
            log.status = 'restored'
            session.commit()
            session.close()
            return jsonify({'success': True, 'msg': '恢复成功'})
        else:
            session.close(); return jsonify({'success': False, 'msg': '环境已不支持恢复'}), 400
    except OSError as e:
        session.close(); return jsonify({'success': False, 'msg': str(e)}), 500

@dy_bp.route('/sys_tags/archive_plan', methods=['POST'])
def api_archive_plan():
    data = request.get_json() or {}
    root_dir = os.path.abspath(data.get('root_dir', '').strip())
    tag_groups = data.get('tag_groups', [])
    threshold = int(data.get('threshold', 10))
    max_per_folder = int(data.get('max_per_folder', 50))
    force_rearchive = data.get('force_rearchive', False)
    person_regex = data.get('person_regex', '').strip() 

    if not os.path.exists(root_dir):
        return jsonify({'success': False, 'msg': '路径不存在'})

    valid_tags_map = {}
    person_tags_set = set()
    
    for g in tag_groups:
        g_name = g.get('id', '未分类')
        is_person = g.get('is_person', False)
        for t in g.get('tags', []):
            valid_tags_map[t] = g_name
            if is_person:
                person_tags_set.add(t)

    all_files_raw = []
    person_dict = {}
    
    for current_root, dirs, files in os.walk(root_dir):
        if not force_rearchive and "_arc_" in current_root: 
            continue 
            
        for f in files:
            f_path = os.path.join(current_root, f)
            explicit_tags = [t for t in valid_tags_map.keys() if f'#{t}' in f]
            
            clean_f = os.path.splitext(f)[0]
            person_name = ""
            
            if person_regex:
                try:
                    import re
                    m = re.search(person_regex, clean_f)
                    if m:
                        person_name = m.group(1).strip() if len(m.groups()) > 0 else m.group(0).strip()
                except Exception:
                    pass
            
            all_files_raw.append({
                'path': f_path, 
                'name': f, 
                'explicit_tags': explicit_tags,
                'person': person_name
            })
            
            if person_name:
                for t in explicit_tags:
                    if t in person_tags_set:
                        if person_name not in person_dict:
                            person_dict[person_name] = set()
                        person_dict[person_name].add(t)

    all_files = []
    tag_counts = {t: 0 for t in valid_tags_map.keys()}
    
    for file_info in all_files_raw:
        p_name = file_info['person']
        e_tags = set(file_info['explicit_tags'])
        
        if p_name in person_dict:
            e_tags.update(person_dict[p_name])
            
        final_tags = list(e_tags)
        if final_tags:
            all_files.append({'path': file_info['path'], 'name': file_info['name'], 'tags': final_tags})
            for t in final_tags: tag_counts[t] += 1

    buckets = {t: [] for t in valid_tags_map.keys()}
    for file_info in all_files:
        rarest_tag = min(file_info['tags'], key=lambda t: tag_counts[t])
        buckets[rarest_tag].append(file_info['path'])

    final_plan = []
    catch_all_bucket = []
    
    for group in tag_groups:
        group_tags = group.get('tags', [])
        small_tags_in_group = []
        combined_files = []

        for t in group_tags:
            files = buckets.get(t, [])
            if len(files) >= threshold:
                for i in range(0, len(files), max_per_folder):
                    chunk = files[i:i + max_per_folder]
                    suffix = f"_{i//max_per_folder + 1}" if len(files) > max_per_folder else ""
                    final_plan.append({
                        'folder_name': f"_arc_{t}{suffix}",
                        'display_tags': [t],
                        'files': chunk
                    })
            elif len(files) > 0:
                small_tags_in_group.append(t)
                combined_files.extend(files)
        
        if len(combined_files) >= threshold:
            folder_name = f"_arc_" + "_".join(small_tags_in_group[:3]) 
            final_plan.append({
                'folder_name': folder_name,
                'display_tags': small_tags_in_group,
                'files': combined_files
            })
        else:
            catch_all_bucket.extend(combined_files)

    if catch_all_bucket:
        final_plan.append({
            'folder_name': "_arc_未分类_零碎收集",
            'display_tags': ["多种兜底"],
            'files': catch_all_bucket
        })

    return jsonify({'success': True, 'plan': final_plan})

@dy_bp.route('/sys_tags/archive_execute', methods=['POST'])
def api_archive_execute():
    data = request.get_json() or {}
    plan = data.get('plan', [])
    root_dir = os.path.abspath(data.get('root_dir', '').strip())
    
    moved_count = 0
    session = Session()
    try:
        for item in plan:
            target_dir = os.path.join(root_dir, item['folder_name'])
            os.makedirs(target_dir, exist_ok=True)
            
            for old_path in item['files']:
                if not os.path.exists(old_path): continue
                
                filename = os.path.basename(old_path)
                new_path = os.path.join(target_dir, filename)
                
                if old_path != new_path:
                    if os.path.exists(new_path):
                        os.remove(new_path)
                    shutil.move(old_path, new_path)
                    
                    video = session.query(Video).filter(
                        or_(Video.detail == old_path, Video.detail == os.path.abspath(old_path))
                    ).first()
                    
                    if video: 
                        video.detail = os.path.abspath(new_path)
                moved_count += 1
        
        session.commit()
        
        for current_root, dirs, files in os.walk(root_dir, topdown=False):
            if "_arc_" in current_root and not dirs and not files:
                try: os.rmdir(current_root)
                except: pass
                
        return jsonify({'success': True, 'msg': f'执行完毕，成功归档 {moved_count} 个文件'})
    except Exception as e:
        session.rollback()
        return jsonify({'success': False, 'msg': str(e)})
    finally:
        session.close()

def is_meaningful_filename(filename):
    name = os.path.splitext(filename)[0].lower()
    name = re.sub(r'^(video|v|mp4|hd)_+', '', name)
    name = re.sub(r'video|mp4', '', name)
    name = re.sub(r'\d{4}-\d{2}-\d{2}', '', name)
    name = re.sub(r'[a-f0-9]{8,}', '', name)
    name = re.sub(r'[\d\W_]+', '', name)
    if len(name) < 2 and not re.search(r'[\u4e00-\u9fa5]', name):
        return False
    return True

@dy_bp.route('/sys_tags/export_csv', methods=['GET'])
def export_csv():
    session = Session()  
    try:
        query = session.query(Video)
        query, _ = apply_video_filters(query, request.args, session)
        
        # 兜底：强制要求数据库层必须是未打标的
        query = query.filter(func.is_untagged(Video.filename) == 1)
        
        pending_subquery = session.query(RenameLog.video_id).filter(
            RenameLog.status.in_(['pending', 'retry'])
        ).subquery()
        query = query.filter(not_(Video.id.in_(pending_subquery)))
        
        sort_by = request.args.get('sort_by')
        if sort_by == 'filename':
            query = query.order_by(Video.filename.asc())
        elif sort_by == 'path':
            query = query.order_by(Video.detail.asc())
        else:
            query = query.order_by(Video.id.desc())
            
        limit = request.args.get('limit', type=int, default=0)
        if limit > 0: query = query.limit(limit)
        
        valid_videos = []
        for v in query.all():
            # 这里只需把无意义如 111.mp4 这种过滤掉即可
            if is_meaningful_filename(v.filename):
                valid_videos.append(v)
                    
        output = io.StringIO()
        output.write('\ufeff') 
        writer = csv.writer(output)
        writer.writerow(['id', 'filename', 'tags'])
        
        for v in valid_videos:
            writer.writerow([v.id, v.filename, ''])
            
        output.seek(0)
        mem_file = io.BytesIO(output.getvalue().encode('utf-8'))
        return send_file(
            mem_file,
            mimetype='text/csv',
            as_attachment=True,
            download_name='untagged_videos.csv'
        )
    finally:
        session.close()

@dy_bp.route('/sys_tags/import_csv', methods=['POST'])
def api_import_csv():
    if 'file' not in request.files: return jsonify({'success': False, 'msg': '无文件'})
    file = request.files['file']
    try:
        stream = io.StringIO(file.stream.read().decode('utf-8-sig'), newline=None)
        csv_input = csv.reader(stream)
        next(csv_input)
        
        session, count = Session(), 0
        for row in csv_input:
            if len(row) < 3: continue
            vid_id, tags_str = row[0].strip(), row[2].strip()
            if not tags_str or not vid_id.isdigit(): continue
                
            video = session.query(Video).filter(Video.id == int(vid_id)).first()
            if not video: continue
                
            user_tags = [t.strip() for t in tags_str.split() if t.strip()]
            if not user_tags: continue
            
            formatted_tags = [f"#a_{t}" for t in user_tags]
            ext = os.path.splitext(video.detail)[1]
            base_name = video.filename[:-len(ext)] if video.filename.lower().endswith(ext.lower()) else video.filename
            
            new_filename = f"{base_name} {' '.join(formatted_tags)}{ext}"
            new_target_path = os.path.join(os.path.dirname(video.detail), new_filename)
            
            session.add(RenameLog(video_id=video.id, original_path=video.detail, target_path=new_target_path, status='pending'))
            count += 1
            
        session.commit()
        session.close()
        return jsonify({'success': True, 'msg': f'已将 {count} 个任务加入队列。'})
    except Exception as e:
        return jsonify({'success': False, 'msg': f'CSV 异常: {str(e)}'})

@dy_bp.route('/video-paths', methods=['GET', 'POST'])
def api_video_paths():
    if request.method == 'GET': return jsonify({'paths': load_paths()})
    path = request.get_json().get('path', '').strip()
    if not path or not os.path.isdir(path): return jsonify({'error': '路径无效'}), 400
    paths = load_paths()
    if path not in paths:
        paths.append(path)
        save_paths(paths)
    return jsonify({'success': True})

@dy_bp.route('/video-paths/index', methods=['POST'])
def api_index_path():
    path = request.get_json().get('path', '').strip()
    if request.args.get('del') == '1':
        paths = load_paths()
        if path in paths:
            paths.remove(path)
            save_paths(paths)
            session = Session()
            query = session.query(Video).filter(Video.detail.like(f"{os.path.abspath(path)}%"))
            delete_ids = [v.id for v in query.all()]
            removed_count = len(delete_ids)
            if delete_ids:
                for i in range(0, removed_count, 500):
                    chunk = delete_ids[i:i+500]
                    session.query(VideoRandomOrder).filter(VideoRandomOrder.video_id.in_(chunk)).delete(synchronize_session=False)
                    session.query(RenameLog).filter(RenameLog.video_id.in_(chunk)).delete(synchronize_session=False)
                    session.query(Video).filter(Video.id.in_(chunk)).delete(synchronize_session=False)
            session.commit()
            session.close()
        return jsonify({'success': True, 'msg': f'删除了目录及 {removed_count} 条记录'})
        
    if request.args.get('all') == '1':
        session = Session()
        removed = clean_invalid_videos(session, path)
        existing_videos = {os.path.abspath(v.detail) for v in session.query(Video.detail).all()}
        added = scan_directory_for_videos(path, session, existing_videos)
        session.commit()
        session.close()
        return jsonify({'success': True, 'added': added, 'removed': removed})
    return jsonify({'success': True})

@dy_bp.route('/video-paths/updatefile')
def api_update_files():
    paths = load_paths()
    session = Session()
    for v in session.query(Video).all():
        v.tags = ','.join(RenameService.extract_tags(v.filename))
    total_removed = clean_invalid_videos(session)
    existing_videos = {os.path.abspath(v.detail) for v in session.query(Video.detail).all()}
    total_added = sum(scan_directory_for_videos(p, session, existing_videos) for p in paths if os.path.isdir(p))
    session.commit()
    session.close()
    return jsonify({'success': True, 'added': total_added, 'removed': total_removed})

@dy_bp.route('/videos', methods=['GET'])
def api_get_videos():
    session = Session()
    try:
        query = session.query(Video)
        page, page_size = int(request.args.get('page', 1)), int(request.args.get('page_size', 5))
        latest = int(request.args.get('latest', 0))
        stream, sort_by = request.args.get('stream', 'false').lower() == 'true', request.args.get('sort_by', '')
        
        query, use_random = apply_video_filters(query, request.args, session)
        
        if use_random:
            all_video_ids = {vid for (vid,) in session.query(Video.id).all()}
            existing_ids = {vid for (vid,) in session.query(VideoRandomOrder.video_id).all()}
            missing_ids = list(all_video_ids - existing_ids)
            if missing_ids:
                random.shuffle(missing_ids)
                session.query(VideoRandomOrder).update({VideoRandomOrder.order_index: VideoRandomOrder.order_index + len(missing_ids)})
                for i, vid in enumerate(missing_ids): session.add(VideoRandomOrder(video_id=vid, order_index=i))
                session.commit()
                
            random_subq = session.query(VideoRandomOrder).order_by(VideoRandomOrder.order_index).with_entities(
                VideoRandomOrder.video_id.label('video_id'), VideoRandomOrder.order_index.label('order_index')
            ).subquery()
            query = query.outerjoin(random_subq, Video.id == random_subq.c.video_id).order_by(func.coalesce(random_subq.c.order_index, 1))
            videos = query.offset((page - 1) * page_size).limit(page_size).all()
        else:
            if sort_by == 'filename': query = query.order_by(Video.filename.asc())
            elif sort_by == 'path': query = query.order_by(Video.detail.asc())
            else: query = query.order_by(Video.id.desc())

            if page_size: videos = query.offset((page-1)*page_size).limit(page_size).all()
            elif latest: videos = query.limit(int(latest)).all()
            else: videos = query.all()
            
        pending_map = {}
        if videos:
            video_ids = [v.id for v in videos]
            pending_logs = session.query(RenameLog).filter(
                RenameLog.video_id.in_(video_ids),
                RenameLog.status.in_(['pending', 'retry'])
            ).all()
            for log in pending_logs:
                pending_map[log.video_id] = os.path.basename(log.target_path)
                
    finally:
        session.close()

    if stream:
        def generate():
            yield '['
            for i, v in enumerate(videos):
                if i > 0: yield ','
                fname = pending_map.get(v.id) or os.path.basename(v.detail)
                yield f'{{"id":{v.id},"filename":"{fname}","tags":"{v.tags}","score":{v.score},"detail":"{v.detail}"}}'
            yield ']'
        return Response(stream_with_context(generate()), mimetype='application/json')
        
    return jsonify([{
        "id": v.id, 
        "filename": pending_map.get(v.id) or os.path.basename(v.detail),
        "tags": v.tags, 
        "score": v.score, 
        "detail": v.detail
    } for v in videos])

@dy_bp.route('/videos/update_score', methods=['POST'])
def api_update_score():
    data = request.json
    session = Session()
    video = session.query(Video).filter(Video.id == data.get('id')).first()
    if video:
        video.score = data.get('score')
        video.updatetime = datetime.datetime.now()
        session.commit()
        session.close()
        return jsonify({"success": True})
    session.close()
    return jsonify({"success": False, "msg": "视频不存在"}), 404

@dy_bp.route('/videos/<int:video_id>/stream', methods=['GET'])
def api_stream_video(video_id):
    session = Session()
    video = session.query(Video).filter(Video.id == video_id).first()
    session.close()
    if not video or not os.path.isfile(video.detail): return abort(404, "文件不存在")
    return send_file(video.detail, mimetype='video/mp4', conditional=True)

@dy_bp.route('/videos/count', methods=['GET'])
def api_videos_count():
    session = Session()
    query, _ = apply_video_filters(session.query(Video), request.args, session)
    total = query.count()
    session.close()
    return jsonify({"total": total})

@dy_bp.route('/')
def index():
    if not os.path.exists(INDEX_FILE): return f"找不到首页文件", 404
    return send_file(INDEX_FILE)

FILES_TO_SEND = ["moduels/media/6dyfn_extension.py", "pages/media/9dyfn.html"]

@dy_bp.route('/sys_tags/export_rename_compare_csv', methods=['GET'])
def export_rename_compare_csv():
    session = Session()
    try:
        search = request.args.get('search', '')
        exclude = request.args.get('exclude', '')
        score = int(request.args.get('score', 0))
        limit = int(request.args.get('limit', 0))

        query = session.query(Video)
        
        if search:
            for word in search.split():
                query = query.filter(Video.filename.like(f'%{word}%'))
        if exclude:
            for word in exclude.split():
                query = query.filter(~Video.filename.like(f'%{word}%'))
        if score > 0:
            if score == 1: query = query.filter(Video.score == 1)
            else: query = query.filter(Video.score >= score)
        
        query = query.order_by(Video.id.desc())
        if limit > 0:
            query = query.limit(limit)
            
        videos = query.all()

        def generate():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['视频ID', '原始文件名', '最新物理文件名', '当前标签', '评分', '物理路径'])
            yield output.getvalue()
            output.truncate(0)
            output.seek(0)

            for v in videos:
                first_log = session.query(RenameLog).filter(
                    RenameLog.video_id == v.id
                ).order_by(RenameLog.create_time.asc()).first()
                
                original_name = os.path.basename(first_log.original_path) if first_log else v.filename
                current_physical_name = os.path.basename(v.detail)

                writer.writerow([v.id, original_name, current_physical_name, v.tags, v.score, v.detail])
                yield output.getvalue()
                output.truncate(0)
                output.seek(0)

        return Response(
            stream_with_context(generate()),
            mimetype='text/csv',
            headers={"Content-Disposition": f"attachment; filename=rename_history_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
        )
    finally:
        session.close()

@dy_bp.route('/skip/api/get_latest_code', methods=['GET'])
def get_latest_code():
    files_data = {}
    for rel_path in FILES_TO_SEND:
        target_path = os.path.abspath(rel_path)
        if os.path.exists(target_path):
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    files_data[rel_path] = f.read()
            except Exception as e:
                return jsonify({"error": f"读取文件失败 {rel_path}: {str(e)}"}), 500
        else:
            return jsonify({"error": f"云端缺少下发文件: {rel_path}"}), 404

    return jsonify({"status": "success", "files": files_data})

if __name__ == "__main__":
    app = Flask(__name__)
    app.register_blueprint(dy_bp)
    
    @app.route('/')
    def root_redirect():
        return redirect('/dyfn/')
        
    print(f"[*] 成功挂载蓝图，请访问: http://127.0.0.1:82/")
    app.run(debug=True, host='0.0.0.0', port=82)