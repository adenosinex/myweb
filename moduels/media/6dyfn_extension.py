import os
import re
import json
import random 
import datetime
import csv
import io
import traceback
from flask import Flask, Blueprint, request, jsonify, Response, stream_with_context, send_file, abort, redirect, make_response
from sqlalchemy import create_engine, Column, Integer, String, BigInteger, DateTime, func
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

engine = create_engine(DB_PATH, echo=False)
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
        return re.findall(r'#([\u4e00-\u9fa5a-zA-Z0-9_]+)', filename)

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
            
            # 从 log 中获取目标基础名并再次应用黑名单清洗（双重保险）
            target_base = os.path.basename(log.target_path)
            if target_base.lower().endswith(ext.lower()): 
                target_base = target_base[:-len(ext)]
            
            for bw in blacklist_words:
                target_base = re.sub(re.escape(bw), '', target_base, flags=re.IGNORECASE)
            
            target_base = re.sub(r'\[\s*\]|\(\s*\)|【\s*】', '', target_base)
            target_base = re.sub(r'\.{2,}', '.', target_base)
            target_base = re.sub(r'\s{2,}', ' ', target_base).strip(' .-_')
            
            if not target_base: 
                target_base = f"video_{video.id}"
                
            new_filename = target_base + ext
            new_full_path = os.path.join(dir_name, new_filename)
            
            # --- 关键逻辑：同步物理文件与数据库 ---
            try:
                # 如果旧文件存在，且新旧路径不同，则改名
                if os.path.exists(old_path) and old_path != new_full_path:
                    os.rename(old_path, new_full_path)
                elif not os.path.exists(old_path) and not os.path.exists(new_full_path):
                    # 如果原文件和目标文件都不在，说明彻底丢了
                    log.status, log.error_msg = 'failed', '找不到物理文件'
                    failed_count += 1; continue
                
                # 🌟 修复点：物理改名成功（或新路径已存在）后，强制同步数据库字段
                video.filename = new_filename
                video.detail = new_full_path
                video.tags = ','.join(RenameService.extract_tags(new_filename))
                
                log.status = 'success'
                log.target_path = new_full_path # 更新日志里的最终路径
                success_count += 1
            except Exception as e:
                log.status, log.error_msg = 'failed', str(e)
                failed_count += 1

        # 2. 全库洗牌模式 (mode='full')
        if mode == 'full' and blacklist_words:
            # 逻辑同上，确保在全库清洗时也执行 video.detail = new_path
            for video in session.query(Video).all():
                old_path = video.detail
                if not os.path.exists(old_path): continue
                
                ext = os.path.splitext(old_path)[1]
                base_name = video.filename[:-len(ext)] if video.filename.lower().endswith(ext.lower()) else video.filename
                
                clean_base = base_name
                needs_clean = False
                for bw in blacklist_words:
                    if re.search(re.escape(bw), clean_base, re.IGNORECASE):
                        clean_base = re.sub(re.escape(bw), '', clean_base, flags=re.IGNORECASE)
                        needs_clean = True
                
                if needs_clean:
                    clean_base = re.sub(r'\[\s*\]|\(\s*\)|【\s*】', '', clean_base)
                    clean_base = re.sub(r'\s{2,}', ' ', clean_base).strip(' .-_')
                    new_filename = clean_base + ext
                    new_path = os.path.join(os.path.dirname(old_path), new_filename)
                    
                    if new_path != old_path:
                        try:
                            os.rename(old_path, new_path)
                            video.filename = new_filename
                            video.detail = new_path # 🌟 同步数据库路径
                            video.tags = ','.join(RenameService.extract_tags(new_filename))
                            success_count += 1
                        except: failed_count += 1

        session.commit() # 🌟 统一提交，确保所有修改落盘
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
    use_random = False
    
    if search and 'random' in search.lower():
        use_random = True
        search = search.replace('random', '').strip()
        
    # 🌟 修复 1：明确剔除默认的 '0' 值
    if score and str(score) != '0': 
        query = query.filter(Video.score == int(score))
    else: 
        # 兼容部分通过第三方工具导入导致 score 为 NULL 的脏数据
        from sqlalchemy import or_
        query = query.filter(or_(Video.score != 1, Video.score.is_(None)))

    if tags:
        for tag in tags.split(','): 
            if tag.strip():
                query = query.filter(Video.tags.like(f"%{tag}%"))
                
    if search:
        for kw in search.strip().split(): 
            query = query.filter(Video.detail.like(f"%{kw}%"))
            
    if exclude:
        for kw in exclude.strip().split(): 
            query = query.filter(~Video.detail.like(f"%{kw}%"))

    # 🌟 修复 2：如果前端传来了 size='0'，直接忽略，不执行小于 0 的过滤
    if size and str(size) != '0': 
        query = apply_size_filter(query, str(size))
        
    return query, use_random
# ================= 视图控制与路由分配 (Views) =================
dy_bp = Blueprint('dyfn', __name__, url_prefix='/dyfn')

@dy_bp.before_request 
def setup(): init_db()

@dy_bp.route('/sys_tags/migrate_legacy', methods=['POST'])
def migrate_legacy_data():
    """接收前端触发的旧数据迁移操作"""
    session = Session()
    try:
        data = request.get_json() or {}
        # 1. 迁移本地 JSON 黑名单至 SQLite
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                legacy_bl = json.load(f)
                if legacy_bl: BlacklistService.sync_list(session, legacy_bl)
        # 2. 迁移前端传来的 localStorage Tag 数据
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

import os
from datetime import datetime
from flask import request, jsonify

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
        
        # 🌟 核心机制：查询是否已有待处理的任务 (UPSERT 逻辑)
        existing_log = session.query(RenameLog).filter(
            RenameLog.video_id == video_id,
            RenameLog.status.in_(['pending', 'retry', 'failed'])
        ).first()
        
        if existing_log:
            # 如果处于排队中，直接覆盖目标路径，防止生成多条冲突的日志
            existing_log.target_path = target_path
            existing_log.status = 'pending'
            existing_log.error_msg = None
            existing_log.create_time = datetime.now() # 刷新操作时间
        else:
            # 没有排队任务，才新建
            new_log = RenameLog(
                video_id=video_id,
                original_path=video.detail,
                target_path=target_path,
                status='pending',
                create_time=datetime.now()
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

import io
import csv
import re
import os
from flask import request, send_file
from sqlalchemy import not_

# --- 新增：专门用于后端的智能文件名过滤器 ---
def is_meaningful_filename(filename):
    """
    判断文件名是否有分析价值：剔除无意义前缀、日期、长哈希、纯数字和符号。
    """
    name = os.path.splitext(filename)[0].lower()
    
    # 1. 剔除常见无意义前缀
    name = re.sub(r'^(video|v|mp4|hd)_+', '', name)
    name = re.sub(r'video|mp4', '', name)
    
    # 2. 剔除日期 (如 2024-12-03) 和时间片段
    name = re.sub(r'\d{4}-\d{2}-\d{2}', '', name)
    
    # 3. 剔除长串类似 UUID 或 Hex 散列值的乱码 (8位以上)
    name = re.sub(r'[a-f0-9]{8,}', '', name)
    
    # 4. 彻底剔除所有数字、标点符号、空格，只看剩下的纯“字”
    name = re.sub(r'[\d\W_]+', '', name)
    
    # 5. 判定：如果剩下的字符少于 2 个，并且连一个汉字都没有，视为“废品”
    if len(name) < 2 and not re.search(r'[\u4e00-\u9fa5]', name):
        return False
        
    return True


@dy_bp.route('/sys_tags/export_csv', methods=['GET'])
def export_csv():
    session = Session()  # 视你的数据库会话获取方式而定
    try:
        query = session.query(Video)
        
        # 1. 继承网页端基础筛选条件
        query, _ = apply_video_filters(query, request.args, session)
        
        # 2. 过滤已打标的（带有 #）
        query = query.filter(not_(Video.filename.like('%#%')))
        
        # 3. 过滤处于改名队列中还没落实的
        pending_subquery = session.query(RenameLog.video_id).filter(
            RenameLog.status.in_(['pending', 'retry'])
        ).subquery()
        query = query.filter(not_(Video.id.in_(pending_subquery)))
        
        # 4. 排序
        sort_by = request.args.get('sort_by')
        if sort_by == 'filename':
            query = query.order_by(Video.filename.asc())
        elif sort_by == 'path':
            query = query.order_by(Video.detail.asc())
        else:
            query = query.order_by(Video.id.desc())
            
        limit = request.args.get('limit', type=int, default=0)
        
        # 🌟 核心修改：不在 SQL 层做 Limit，改为在 Python 层流式过滤
        valid_videos = []
        for v in query.all():
            # 经过清洗依然有意义的，才加入导出列表
            if is_meaningful_filename(v.filename):
                valid_videos.append(v)
                
                # 当收集到足够的合法数据时，立刻停止循环
                if limit > 0 and len(valid_videos) >= limit:
                    break
                    
        # 5. 生成 CSV
        output = io.StringIO()
        output.write('\ufeff')  # BOM 头防乱码
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

import os
from flask import request, jsonify, Response, stream_with_context
from sqlalchemy import func
import random

import os
from flask import request, jsonify, Response, stream_with_context
from sqlalchemy import func
import random
from datetime import datetime

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
                # 🌟 强制治愈脱节：有排队用排队名，没排队强制截取 detail 真实的物理文件名！
                fname = pending_map.get(v.id) or os.path.basename(v.detail)
                yield f'{{"id":{v.id},"filename":"{fname}","tags":"{v.tags}","score":{v.score},"detail":"{v.detail}"}}'
            yield ']'
        return Response(stream_with_context(generate()), mimetype='application/json')
        
    return jsonify([{
        "id": v.id, 
        "filename": pending_map.get(v.id) or os.path.basename(v.detail), # 🌟 强制治愈脱节
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

import csv
import io
from flask import Response, stream_with_context
from sqlalchemy import asc

# 🌟 新增路由：专门导出原始名与物理名对比表，不影响原打标 CSV
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
                # 查找最早的一条改名日志来判定原始名
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
            headers={"Content-Disposition": f"attachment; filename=rename_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
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
        return redirect('/dy/')
        
    print(f"[*] 成功挂载蓝图，请访问: http://127.0.0.1:82/")
    app.run(debug=True, host='0.0.0.0', port=82)