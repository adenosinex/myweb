import os
import re
import json
import random 
import datetime
from flask import Flask, Blueprint, request, jsonify, Response, stream_with_context, send_file, abort, redirect
from sqlalchemy import create_engine, Column, Integer, String, BigInteger, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker

# ================= 本地扁平化路径配置 =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(CURRENT_DIR, 'db')
DB_PATH = f"sqlite:///{os.path.join(DB_DIR, 'videos.db')}"
PATHS_FILE = os.path.join(DB_DIR, 'video_paths.json')
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

def scan_directory_for_videos(path, session, existing_videos):
    from tqdm import tqdm
    valid_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.wmv', '.m4v'}
    added_count = 0
    
    print(f"\n[*] 开始扫描目录: {path}")
    
    # 第一阶段：动态计数进度条（由于无法预知总数，只显示当前找到的数量和速度）
    target_files = []
    with tqdm(desc="阶段 1: 文件扫描中", unit="个") as pbar_scan:
        for root, dirs, files in os.walk(path):
            for file in files:
                if os.path.splitext(file)[1].lower() in valid_exts:
                    target_files.append(os.path.abspath(os.path.join(root, file)))
                    pbar_scan.update(1)  # 每找到一个符合条件的文件，计数加1
                    
    if not target_files:
        print(f"[*] 该目录下未发现支持的视频文件。")
        return 0
        
    print(f"[*] 阶段 1 完毕。共发现 {len(target_files)} 个视频文件，准备入库。")

    # 第二阶段：百分比进度条（此时已经知道了总数，可以显示完整进度和预估时间）
    with tqdm(total=len(target_files), desc="阶段 2: 数据库对比/入库", unit="个") as pbar_insert:
        for full_path in target_files:
            if full_path not in existing_videos:
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    size = 0
                
                file_name = os.path.basename(full_path)
                tags = extract_tags(file_name)
                
                session.add(Video(
                    filename=file_name,
                    detail=full_path,
                    tags=','.join(tags),
                    score=0,
                    file_size=size
                ))
                existing_videos.add(full_path)
                added_count += 1
                
            pbar_insert.update(1)
            
    print(f"[*] 目录 {path} 索引完成，本次实际新增入库: {added_count} 个。")
    return added_count


dy_bp = Blueprint('dy', __name__, url_prefix='/dy')

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
    """清理数据库中物理文件已丢失的记录"""
    query = session.query(Video)
    if path:
        # 如果指定了路径，仅匹配该路径下的文件
        query = query.filter(Video.detail.like(f"{os.path.abspath(path)}%"))
    
    # 1. 纯读取阶段：找出所有失效的视频 ID
    invalid_ids = []
    for video in query.all():
        if not os.path.isfile(video.detail):
            invalid_ids.append(video.id)
            
    removed_count = len(invalid_ids)
    
    # 2. 写入阶段：统一执行批量删除，避免读写锁冲突
    if invalid_ids:
        # 分批处理，防止单次清理数量超出 SQLite 的变量限制 (通常为 999)
        chunk_size = 500
        for i in range(0, removed_count, chunk_size):
            chunk_ids = invalid_ids[i:i+chunk_size]
            session.query(VideoRandomOrder).filter(VideoRandomOrder.video_id.in_(chunk_ids)).delete(synchronize_session=False)
            session.query(Video).filter(Video.id.in_(chunk_ids)).delete(synchronize_session=False)
            
    return removed_count

@dy_bp.route('/video-paths/index', methods=['POST'])
def index_video_path():
    data = request.get_json()
    path = data.get('path', '').strip()
    
    if request.args.get('del') == '1':
        paths = load_paths()
        if path in paths:
            paths.remove(path)
            save_paths(paths)
        return jsonify({'success': True, 'msg': '已删除目录'})
        
    if not path or not os.path.isdir(path):
        return jsonify({'error': '路径无效'}), 400
        
    if request.args.get('all') == '1':
        session = Session()
        # 扫描前：先清理该目录下已在物理磁盘消失的文件记录
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
    
    # 扫描更新前：全局清理数据库中所有已失效的视频记录
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
    exclude = request_args.get('exclude')  # 新增：接收排除关键词
    tags = request_args.get('tags')
    size = request_args.get('size')
    use_random = False
    
    if search and 'random' in search.lower():
        use_random = True
        search = search.replace('random', '').strip()
        
    if score and int(score) > 0:
        query = query.filter(Video.score == int(score))
    else:
        # 默认剔除 1星(不喜欢) 的视频，除非明确指定查询 1星
        query = query.filter(Video.score != 1)

    if tags:
        for tag in tags.split(','):
            query = query.filter(Video.tags.like(f"%{tag}%"))
            
    # 处理包含关键词
    if search:
        for kw in search.strip().split():
            query = query.filter(Video.detail.like(f"%{kw}%"))
            
    # ====== 处理排除关键词 ======
    if exclude:
        for kw in exclude.strip().split():
            # 使用 ~ 符号实现 NOT LIKE 过滤，剔除包含该关键词的路径或文件名
            query = query.filter(~Video.detail.like(f"%{kw}%"))
    # ============================

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
        # 【修改点】: 引入自定义排序逻辑
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

# ================= 网页与接口路由 =================
@dy_bp.route('/')
def index():
    if not os.path.exists(INDEX_FILE):
        return f"找不到首页文件：{INDEX_FILE}。请确认已成功下载 HTML 文件。", 404
    return send_file(INDEX_FILE)

FILES_TO_SEND = ["moduels/dy_extension.py", "pages/media/9dy.html"]
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
        
    print(f"[*] 成功挂载蓝图，请访问: http://127.0.0.1:81/")
    app.run(debug=True, host='0.0.0.0', port=81)