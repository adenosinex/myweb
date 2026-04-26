import os
import re
import json
import random 
import datetime
import traceback
from flask import Flask, Blueprint, request, jsonify, Response, stream_with_context, send_file, abort, redirect
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
BLACKLIST_FILE = os.path.join(DB_DIR, 'blacklist.json')
INDEX_FILE = os.path.join(CURRENT_DIR, 'index.html')
# ====================================================

Base = declarative_base()

class Video(Base):
    __tablename__ = 'videos'
    id = Column(Integer, primary_key=True)
    filename = Column(String)
    detail = Column(String)
    tags = Column(String)
    score = Column(Integer)
    file_size = Column(BigInteger, default=0)
    cp = Column(Integer, default=0)
    updatetime = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=True)

class VideoRandomOrder(Base):
    __tablename__ = 'video_random_order'
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True)
    order_index = Column(Integer, index=True)

# 新增：用于延迟改名和记录溯源的日志表
class RenameLog(Base):
    __tablename__ = 'rename_logs'
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True)
    original_path = Column(String)
    target_path = Column(String)
    status = Column(String, default='pending') # 状态: pending, success, failed, restored
    error_msg = Column(String, default='')
    create_time = Column(DateTime, default=datetime.datetime.now)

engine = create_engine(DB_PATH, echo=False)
Session = sessionmaker(bind=engine)

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    Base.metadata.create_all(engine)

def extract_tags(filename):
    main_tag_match = filename.split(' ')[0]
    tags = [main_tag_match]
    tags += re.findall(r'#([\u4e00-\u9fa5\w]+)', filename)
    return tags

def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return ["网址", "http", "www"] # 默认一些低信息量词汇

def save_blacklist(bl_list):
    with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(set(bl_list)), f, ensure_ascii=False)

def clean_filename_by_blacklist(filename, blacklist):
    """根据黑名单清理文件名"""
    new_name = filename
    for kw in blacklist:
        new_name = new_name.replace(kw, '')
    # 清理可能产生的多余空格或连续符号
    new_name = re.sub(r'\s+', ' ', new_name).strip()
    return new_name

def scan_directory_for_videos(path, session, existing_videos):
    from tqdm import tqdm
    valid_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.wmv', '.m4v'}
    added_count = 0
    blacklist = load_blacklist()
    
    print(f"\n[*] 开始扫描目录: {path}")
    
    target_files = []
    with tqdm(desc="阶段 1: 文件扫描中", unit="个") as pbar_scan:
        for root, dirs, files in os.walk(path):
            for file in files:
                if os.path.splitext(file)[1].lower() in valid_exts:
                    target_files.append(os.path.abspath(os.path.join(root, file)))
                    pbar_scan.update(1)
                    
    if not target_files:
        print(f"[*] 该目录下未发现支持的视频文件。")
        return 0
        
    print(f"[*] 阶段 1 完毕。共发现 {len(target_files)} 个视频文件，准备入库。")

    with tqdm(total=len(target_files), desc="阶段 2: 数据库对比/入库", unit="个") as pbar_insert:
        for full_path in target_files:
            if full_path not in existing_videos:
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    size = 0
                
                file_name = os.path.basename(full_path)
                dir_name = os.path.dirname(full_path)
                
                # 自动去除黑名单的关键词
                clean_name = clean_filename_by_blacklist(file_name, blacklist)
                
                tags = extract_tags(file_name)
                
                # 先以源文件名入库，保证当前关联不断
                v = Video(
                    filename=file_name,
                    detail=full_path,
                    tags=','.join(tags),
                    score=0,
                    file_size=size
                )
                session.add(v)
                session.flush() # 获取 id
                
                # 如果名称被黑名单清洗发生了改变，自动推入延迟改名队列
                if clean_name != file_name:
                    new_full_path = os.path.join(dir_name, clean_name)
                    session.add(RenameLog(
                        video_id=v.id, 
                        original_path=full_path, 
                        target_path=new_full_path, 
                        status='pending'
                    ))

                existing_videos.add(full_path)
                added_count += 1
                
            pbar_insert.update(1)
            
    print(f"[*] 目录 {path} 索引完成，本次实际新增入库: {added_count} 个。")
    return added_count


dy_bp = Blueprint('dyfn', __name__, url_prefix='/dyfn')

@dy_bp.before_request 
def setup():
    init_db()

def load_paths():
    if os.path.exists(PATHS_FILE):
        with open(PATHS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_paths(paths):
    with open(PATHS_FILE, 'w', encoding='utf-8') as f:
        json.dump(paths, f, ensure_ascii=False)

@dy_bp.route('/video-paths', methods=['GET', 'POST'])
def handle_video_paths():
    if request.method == 'GET':
        return jsonify({'paths': load_paths()})
    
    data = request.get_json()
    path = data.get('path', '').strip()
    if not path or not os.path.isdir(path):
        return jsonify({'error': '路径无效'}), 400
    paths = load_paths()
    if path not in paths:
        paths.append(path)
        save_paths(paths)
    return jsonify({'success': True})

def clean_invalid_videos(session, path=None):
    query = session.query(Video)
    if path:
        query = query.filter(Video.detail.like(f"{os.path.abspath(path)}%"))
    
    invalid_ids = []
    for video in query.all():
        if not os.path.isfile(video.detail):
            invalid_ids.append(video.id)
            
    removed_count = len(invalid_ids)
    
    if invalid_ids:
        chunk_size = 500
        for i in range(0, removed_count, chunk_size):
            chunk_ids = invalid_ids[i:i+chunk_size]
            session.query(VideoRandomOrder).filter(VideoRandomOrder.video_id.in_(chunk_ids)).delete(synchronize_session=False)
            session.query(RenameLog).filter(RenameLog.video_id.in_(chunk_ids)).delete(synchronize_session=False)
            session.query(Video).filter(Video.id.in_(chunk_ids)).delete(synchronize_session=False)
            
    return removed_count

import os
import re

@dy_bp.route('/sys_tags/execute_renames', methods=['POST'])
def execute_renames():
    session = Session()
    # 查询待执行和被手动捞回要求重试的任务
    logs = session.query(RenameLog).filter(RenameLog.status.in_(['pending', 'retry'])).all()
    
    # 提取全局黑名单词汇列表 (表名请根据你实际情况调整)
    blacklist_records = session.query(SysTag).filter(SysTag.type == 'blacklist').all()
    blacklist_words = [r.tag_name for r in blacklist_records]
    
    success_count = 0
    failed_count = 0
    
    for log in logs:
        video = session.query(Video).filter(Video.id == log.video_id).first()
        if not video:
            log.status = 'failed'
            log.error_msg = '视频记录丢失'
            failed_count += 1
            continue
            
        old_path = video.detail
        dir_name = os.path.dirname(old_path)
        ext = os.path.splitext(old_path)[1]
        new_path = os.path.join(dir_name, log.new_filename + ext)
        
        # 1. 如果目标文件已经就绪（可能由于防抖等原因已经修改过了）
        if os.path.exists(new_path):
            video.filename = log.new_filename
            video.detail = new_path
            log.status = 'success'
            success_count += 1
            continue
            
        current_physical_path = old_path
        
        # 2. 核心修复：原文件不存在，使用黑名单规则推导真实的物理路径
        if not os.path.exists(current_physical_path):
            guessed_filename = video.filename
            for bw in blacklist_words:
                if bw.strip():
                    guessed_filename = guessed_filename.replace(bw.strip(), '')
            
            # 模拟前端分词和正则的清理逻辑
            guessed_filename = re.sub(r'\[\s*\]|\(\s*\)|【\s*】', '', guessed_filename)
            guessed_filename = re.sub(r'\.{2,}', '.', guessed_filename)
            guessed_filename = re.sub(r'\s{2,}', ' ', guessed_filename).strip(' .-_')
            
            guessed_path = os.path.join(dir_name, guessed_filename + ext)
            
            if os.path.exists(guessed_path):
                current_physical_path = guessed_path
            else:
                log.status = 'failed'
                log.error_msg = '物理文件彻底丢失无法匹配'
                failed_count += 1
                continue
        
        # 3. 物理更名并同步数据库
        try:
            os.rename(current_physical_path, new_path)
            video.filename = log.new_filename
            video.detail = new_path
            log.status = 'success'
            success_count += 1
        except Exception as e:
            log.status = 'failed'
            log.error_msg = str(e)
            failed_count += 1
            
    session.commit()
    session.close()
    
    msg = f'成功执行 {success_count} 个'
    if failed_count > 0:
        msg += f'，失败 {failed_count} 个'
        
    return jsonify({'success': True, 'msg': msg})


@dy_bp.route('/sys_tags/retry_failed', methods=['POST'])
def retry_failed():
    """捞回失败任务等待再次执行"""
    session = Session()
    failed_logs = session.query(RenameLog).filter(RenameLog.status == 'failed').all()
    count = len(failed_logs)
    
    if count == 0:
        session.close()
        return jsonify({'success': True, 'msg': '当前没有失败的记录'})
        
    for log in failed_logs:
        log.status = 'retry' # 标记为待重试状态
        
    session.commit()
    session.close()
    return jsonify({'success': True, 'msg': f'已重置 {count} 个失败任务，请再次点击批量执行'})

@dy_bp.route('/video-paths/index', methods=['POST'])
def index_video_path():
    data = request.get_json()
    path = data.get('path', '').strip()
    
    if request.args.get('del') == '1':
        paths = load_paths()
        removed_count = 0
        if path in paths:
            paths.remove(path)
            save_paths(paths)
            
            # --- 新增：从数据库中彻底清除该路径下的所有视频记录及其关联表 ---
            session = Session()
            # 匹配路径下的所有文件记录
            query = session.query(Video).filter(Video.detail.like(f"{os.path.abspath(path)}%"))
            delete_ids = [v.id for v in query.all()]
            removed_count = len(delete_ids)
            
            if delete_ids:
                chunk_size = 500
                for i in range(0, removed_count, chunk_size):
                    chunk_ids = delete_ids[i:i+chunk_size]
                    session.query(VideoRandomOrder).filter(VideoRandomOrder.video_id.in_(chunk_ids)).delete(synchronize_session=False)
                    session.query(RenameLog).filter(RenameLog.video_id.in_(chunk_ids)).delete(synchronize_session=False)
                    session.query(Video).filter(Video.id.in_(chunk_ids)).delete(synchronize_session=False)
            session.commit()
            session.close()
            # -----------------------------------------------------------------
            
        return jsonify({'success': True, 'msg': f'已删除目录，并清理了 {removed_count} 条索引记录'})
        
    if not path or not os.path.isdir(path):
        return jsonify({'error': '路径无效'}), 400
        
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
def index_video_path_update():
    paths = load_paths()
    session = Session()
    total_removed = clean_invalid_videos(session)
    existing_videos = {os.path.abspath(v.detail) for v in session.query(Video.detail).all()}
    total_added = 0
    for p in paths:
        if os.path.isdir(p):
            total_added += scan_directory_for_videos(p, session, existing_videos)
            
    session.commit()
    session.close()
    return jsonify({'success': True, 'added': total_added, 'removed': total_removed})
    
@dy_bp.route('/config', methods=['GET'])
def get_config():
    return jsonify({"status": "ok"})

def apply_size_filter(query, size_param):
    if ':' in size_param:
        operator, value = size_param.split(':', 1)
        size_mb = int(value)
        size_bytes = size_mb * 1024 * 1024
        if operator == 'lte':
            query = query.filter(Video.file_size <= size_bytes)
        elif operator == 'gte':
            query = query.filter(Video.file_size >= size_bytes)
        elif operator == 'eq':
            margin = 10 * 1024 * 1024
            query = query.filter(Video.file_size.between(size_bytes - margin, size_bytes + margin))
    else:
        size_bytes = int(size_param) * 1024 * 1024
        query = query.filter(Video.file_size <= size_bytes)
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
        
    if score and int(score) > 0:
        query = query.filter(Video.score == int(score))
    else:
        query = query.filter(Video.score != 1)

    if tags:
        for tag in tags.split(','):
            query = query.filter(Video.tags.like(f"%{tag}%"))
            
    if search:
        for kw in search.strip().split():
            query = query.filter(Video.detail.like(f"%{kw}%"))
            
    if exclude:
        for kw in exclude.strip().split():
            query = query.filter(~Video.detail.like(f"%{kw}%"))

    if size:
        query = apply_size_filter(query, size)
        
    return query, use_random
    
@dy_bp.route('/videos', methods=['GET'])
def get_videos():
    session = Session()
    query = session.query(Video)
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 5))
    latest = int(request.args.get('latest', 0))
    stream = request.args.get('stream', 'false').lower() == 'true'
    sort_by = request.args.get('sort_by', '')
    
    query, use_random = apply_video_filters(query, request.args, session)
    
    if use_random:
        all_video_ids = {vid for (vid,) in session.query(Video.id).all()}
        existing_ids = {vid for (vid,) in session.query(VideoRandomOrder.video_id).all()}
        missing_ids = list(all_video_ids - existing_ids)
        if missing_ids:
            random.shuffle(missing_ids)
            session.query(VideoRandomOrder).update({VideoRandomOrder.order_index: VideoRandomOrder.order_index + len(missing_ids)})
            for i, vid in enumerate(missing_ids):
                session.add(VideoRandomOrder(video_id=vid, order_index=i))
            session.commit()
            
        random_subq = session.query(VideoRandomOrder).order_by(VideoRandomOrder.order_index).with_entities(
            VideoRandomOrder.video_id.label('video_id'),
            VideoRandomOrder.order_index.label('order_index')
        ).subquery()
        
        ordered_query = query.outerjoin(random_subq, Video.id == random_subq.c.video_id).order_by(
            func.coalesce(random_subq.c.order_index, 1)
        )
        videos = ordered_query.offset((page - 1) * page_size).limit(page_size).all()
    else:
        if sort_by == 'filename':
            query = query.order_by(Video.filename.asc())
        elif sort_by == 'path':
            query = query.order_by(Video.detail.asc())
        else:
            query = query.order_by(Video.id.desc())

        if page_size:
            videos = query.offset((page-1)*page_size).limit(page_size).all()
        elif latest:
            videos = query.limit(int(latest)).all()
        else:
            videos = query.all()
            
    session.close()
    
    if stream:
        def generate():
            yield '['
            for i, v in enumerate(videos):
                if i > 0: yield ','
                yield f'{{"id":{v.id},"filename":"{v.filename}","tags":"{v.tags}","score":{v.score},"detail":"{v.detail}"}}'
            yield ']'
        return Response(stream_with_context(generate()), mimetype='application/json')
        
    return jsonify([
        {"id": v.id, "filename": v.filename, "tags": v.tags, "score": v.score, "detail": v.detail} for v in videos
    ])

@dy_bp.route('/videos/update_score', methods=['POST'])
def update_score():
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
def stream_video_file(video_id):
    session = Session()
    video = session.query(Video).filter(Video.id == video_id).first()
    session.close()
    if not video or not os.path.isfile(video.detail):
        return abort(404, "视频文件不存在")
    return send_file(video.detail, mimetype='video/mp4', conditional=True)

@dy_bp.route('/videos/count', methods=['GET'])
def get_videos_count():
    session = Session()
    query = session.query(Video)
    query, _ = apply_video_filters(query, request.args, session)
    total = query.count()
    session.close()
    return jsonify({"total": total})

# ================= 新增功能：系统性文件名修改、黑名单及重命名队列 =================

@dy_bp.route('/sys_tags/blacklist', methods=['GET', 'POST'])
def handle_blacklist():
    """管理自动清洗黑名单"""
    if request.method == 'GET':
        return jsonify({'blacklist': load_blacklist()})
    
    data = request.get_json()
    new_bl = data.get('blacklist', [])
    if isinstance(new_bl, list):
        save_blacklist(new_bl)
        return jsonify({'success': True, 'msg': '黑名单更新成功'})
    return jsonify({'success': False, 'msg': '参数格式错误'}), 400

@dy_bp.route('/sys_tags/tokenize', methods=['POST'])
def tokenize_filename():
    """分词功能，返回核心词汇供用户选择组合加tag"""
    if not jieba:
        return jsonify({'success': False, 'msg': '未安装 jieba 库，无法进行分词。请在环境内执行 pip install jieba'}), 500
        
    data = request.get_json()
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'success': False, 'msg': '缺少文件名'})
    
    # 移除后缀名进行分词
    name_without_ext, _ = os.path.splitext(filename)
    words = list(jieba.cut(name_without_ext))
    # 过滤单字或无意义符号
    core_words = [w for w in words if len(w.strip()) > 1 and not bool(re.match(r'^[^\w\u4e00-\u9fa5]+$', w))]
    
    return jsonify({'success': True, 'words': core_words})

@dy_bp.route('/sys_tags/queue_rename', methods=['POST'])
def queue_rename():
    """将改名需求加入延时队列，由后台/单次调用执行，防占用报错"""
    data = request.get_json()
    video_id = data.get('video_id')
    new_filename = data.get('new_filename') # 用户加tag或分词组合后的新名字
    
    if not video_id or not new_filename:
        return jsonify({'success': False, 'msg': '参数不完整'}), 400
        
    session = Session()
    video = session.query(Video).filter(Video.id == video_id).first()
    if not video:
        session.close()
        return jsonify({'success': False, 'msg': '视频记录不存在'}), 404
        
    dir_name = os.path.dirname(video.detail)
    ext = os.path.splitext(video.detail)[1]
    
    # 保证后缀
    if not new_filename.lower().endswith(ext.lower()):
        new_filename += ext
        
    target_path = os.path.join(dir_name, new_filename)
    
    # 加入队列
    log = RenameLog(
        video_id=video.id,
        original_path=video.detail,
        target_path=target_path,
        status='pending'
    )
    session.add(log)
    session.commit()
    session.close()
    
    return jsonify({'success': True, 'msg': '已加入延时改名队列'})

 
@dy_bp.route('/sys_tags/restore_rename', methods=['POST'])
def restore_rename():
    """根据日志溯源，将改错的文件恢复回原文件名"""
    data = request.get_json()
    log_id = data.get('log_id')
    
    session = Session()
    log = session.query(RenameLog).filter(RenameLog.id == log_id).first()
    if not log or log.status != 'success':
        session.close()
        return jsonify({'success': False, 'msg': '未找到成功的改名记录'}), 404
        
    try:
        if os.path.exists(log.target_path) and not os.path.exists(log.original_path):
            os.rename(log.target_path, log.original_path)
            
            video = session.query(Video).filter(Video.id == log.video_id).first()
            if video:
                video.filename = os.path.basename(log.original_path)
                video.detail = log.original_path
                video.tags = ','.join(extract_tags(video.filename))
                
            log.status = 'restored'
            session.commit()
            session.close()
            return jsonify({'success': True, 'msg': '文件名恢复成功'})
        else:
            session.close()
            return jsonify({'success': False, 'msg': '物理环境已不支持恢复（原文件可能已被占位）'}), 400
    except OSError as e:
        session.close()
        return jsonify({'success': False, 'msg': f'恢复失败，文件可能被占用：{str(e)}'}), 500

# ================= 网页与接口路由 =================
@dy_bp.route('/')
def index():
    if not os.path.exists(INDEX_FILE):
        return f"找不到首页文件：{INDEX_FILE}。请确认已成功下载 HTML 文件。", 404
    return send_file(INDEX_FILE)

FILES_TO_SEND = ["moduels/dyfn_extension.py", "pages/media/9dyfn.html"]
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