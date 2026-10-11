import os
import re
import yaml
import json
import hashlib
import requests
import time
import shutil
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

document_bp = Blueprint('document', __name__)

DOCS_DIR = 'db/docsnew_data'
INBOX_DIR = os.path.join(DOCS_DIR, 'inbox')
ARCHIVE_DIR = os.path.join(DOCS_DIR, 'archive')

# 【新增】回收站：刻意放在 DOCS_DIR 之外，
# 避免 build_memory_index 的 os.walk 再次扫描到已删除文件
TRASH_DIR = DOCS_DIR + '_trash'

MAX_FILES_PER_FOLDER = 100

# =========================================================
# 内存索引
# =========================================================

FILE_INDEX = {}

for d in [DOCS_DIR, INBOX_DIR, ARCHIVE_DIR, TRASH_DIR]:
    os.makedirs(d, exist_ok=True)


# =========================================================
# 基础工具
# =========================================================

def now_timestamp():
    return int(time.time())


def timestamp_to_string(ts):
    if not ts:
        return ''
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


def sanitize_doc_id(value: str) -> str:
    """
    【新增】清洗文档 ID：
    - 允许中文、字母、数字、下划线、短横、点、空格
    - 移除路径分隔符与控制字符
    - 去掉首尾空格和点
    """
    if value is None:
        return ''

    value = str(value).strip()

    # 仅取文件名部分
    value = os.path.basename(value)

    # 移除危险字符
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', value)

    # 去掉首尾的点、空格
    value = value.strip(' .')

    return value


def get_target_folder(base_dir):
    """根据数量自动分配子文件夹"""
    subdirs = []

    for d in os.listdir(base_dir):
        full = os.path.join(base_dir, d)

        if os.path.isdir(full) and d.isdigit():
            subdirs.append(int(d))

    if not subdirs:
        target = os.path.join(base_dir, '001')
        os.makedirs(target, exist_ok=True)
        return target

    current_max = max(subdirs)
    target = os.path.join(
        base_dir,
        f'{current_max:03d}'
    )

    files_in_target = [
        f for f in os.listdir(target)
        if f.endswith('.md')
    ]

    if len(files_in_target) >= MAX_FILES_PER_FOLDER:
        target = os.path.join(
            base_dir,
            f'{current_max + 1:03d}'
        )

        os.makedirs(
            target,
            exist_ok=True
        )

    return target


def calculate_hash(content: str) -> str:
    return hashlib.md5(
        content.encode('utf-8')
    ).hexdigest()


# =========================================================
# Frontmatter
# =========================================================

def parse_frontmatter(content: str):
    if content.startswith('---\n'):

        parts = content.split(
            '---\n',
            2
        )

        if len(parts) >= 3:

            try:
                meta = yaml.safe_load(parts[1])

                if not isinstance(meta, dict):
                    meta = {}

                return meta, parts[2]

            except Exception:
                return {}, parts[2]

    return {}, content


def build_frontmatter(meta: dict, content: str) -> str:

    yaml_str = yaml.dump(
        meta,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False
    )

    return (
        f'---\n'
        f'{yaml_str}'
        f'---\n'
        f'{content}'
    )


# =========================================================
# 时间字段兼容
# =========================================================

def parse_meta_timestamp(meta, *keys):
    """
    尝试从 frontmatter 中读取时间。
    支持：
    created_time: 1234567890
    created_at: 2026-08-25 19:00:00
    updated_time: 1234567890
    updated_at: 2026-08-25 19:00:00
    """

    for key in keys:

        value = meta.get(key)

        if value is None:
            continue

        # 数字时间戳
        if isinstance(value, (int, float)):
            return int(value)

        # 字符串时间戳
        if isinstance(value, str):

            value = value.strip()

            if value.isdigit():
                try:
                    return int(value)
                except Exception:
                    pass

            # ISO / 常用格式
            for fmt in [
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
                '%Y-%m-%d'
            ]:

                try:
                    dt = datetime.strptime(
                        value,
                        fmt
                    )

                    return int(
                        dt.timestamp()
                    )

                except Exception:
                    pass

    return None


def get_created_time(meta, full_path):
    """
    真正创建时间：

    1. frontmatter.created_time
    2. frontmatter.created_at
    3. 历史文件系统 ctime

    历史文档首次扫描时会把这个值固化到 frontmatter。
    """

    ts = parse_meta_timestamp(
        meta,
        'created_time',
        'created_at',
        'created'
    )

    if ts:
        return ts

    try:
        return int(
            os.path.getctime(full_path)
        )
    except Exception:
        return now_timestamp()


def get_update_time(meta, full_path):
    """
    最后修改时间：

    优先文件 mtime。
    """

    try:
        return int(
            os.path.getmtime(full_path)
        )
    except Exception:
        return parse_meta_timestamp(
            meta,
            'updated_time',
            'updated_at',
            'updated'
        ) or now_timestamp()


# =========================================================
# 单文档索引对象
# =========================================================

def build_doc_index(
    full_path,
    rel_path,
    raw_content,
    meta=None
):
    if meta is None:
        meta, pure_content = parse_frontmatter(
            raw_content
        )
    else:
        _, pure_content = parse_frontmatter(
            raw_content
        )

    doc_id = meta.get(
        'id',
        secure_filename(
            os.path.basename(full_path)[:-3]
        )
    )

    status = (
        'archive'
        if 'archive' in rel_path.lower()
        else 'active'
    )

    tags_list = meta.get(
        'tags',
        []
    )

    if not isinstance(tags_list, list):
        tags_list = [str(tags_list)]

    created_time = get_created_time(
        meta,
        full_path
    )

    update_time = get_update_time(
        meta,
        full_path
    )

    return {
        'id': doc_id,
        'path': rel_path.replace('\\', '/'),
        'title': meta.get(
            'title',
            os.path.basename(full_path)[:-3]
        ),
        'status': status,
        'is_pinned': 1 if meta.get('pinned') else 0,
        'tags': tags_list,
        'tags_cache': ','.join(tags_list),

        # 核心时间字段
        'created_time': created_time,
        'update_time': update_time,

        # 便于前端直接展示
        'created_at': timestamp_to_string(
            created_time
        ),
        'updated_at': timestamp_to_string(
            update_time
        ),

        'file_size': os.path.getsize(
            full_path
        ),

        'word_count': len(
            pure_content.strip()
        )
    }


# =========================================================
# 历史文档时间迁移
# =========================================================

def ensure_created_time_in_frontmatter(
    full_path,
    raw_content,
    meta
):
    """
    如果旧文档没有 created_time：

    1. 使用历史 ctime
    2. 写入 frontmatter
    3. 恢复原始 mtime

    这样后续重启也不会再次依赖 ctime。
    """

    if parse_meta_timestamp(
        meta,
        'created_time',
        'created_at',
        'created'
    ):
        return raw_content, meta

    try:
        created_time = int(
            os.path.getctime(full_path)
        )
    except Exception:
        created_time = now_timestamp()

    meta['created_time'] = created_time

    # 保存原 mtime
    try:
        original_mtime = os.path.getmtime(
            full_path
        )
        original_atime = os.path.getatime(
            full_path
        )
    except Exception:
        original_mtime = None
        original_atime = None

    _, pure_content = parse_frontmatter(
        raw_content
    )

    new_content = build_frontmatter(
        meta,
        pure_content
    )

    try:

        with open(
            full_path,
            'w',
            encoding='utf-8'
        ) as f:
            f.write(new_content)

        # 尽可能恢复 mtime
        if original_mtime is not None:

            os.utime(
                full_path,
                (
                    original_atime or original_mtime,
                    original_mtime
                )
            )

    except Exception as e:

        print(
            f'迁移 created_time 失败 {full_path}: {e}'
        )

        return raw_content, meta

    return new_content, meta


# =========================================================
# 全量构建索引
# =========================================================

def build_memory_index():

    global FILE_INDEX

    new_index = {}

    for dirpath, _, filenames in os.walk(
        DOCS_DIR
    ):

        for filename in filenames:

            if not filename.endswith('.md'):
                continue

            full_path = os.path.join(
                dirpath,
                filename
            )

            rel_path = os.path.relpath(
                full_path,
                DOCS_DIR
            )

            try:

                with open(
                    full_path,
                    'r',
                    encoding='utf-8'
                ) as file:

                    raw_content = file.read()

                meta, _ = parse_frontmatter(
                    raw_content
                )

                # 自动补充创建时间
                raw_content, meta = (
                    ensure_created_time_in_frontmatter(
                        full_path,
                        raw_content,
                        meta
                    )
                )

                new_index[
                    meta.get(
                        'id',
                        secure_filename(
                            filename[:-3]
                        )
                    )
                ] = build_doc_index(
                    full_path,
                    rel_path,
                    raw_content,
                    meta
                )

            except Exception as e:

                print(
                    f'解析文件失败 {full_path}: {e}'
                )

    FILE_INDEX = new_index


# 启动扫描
build_memory_index()


# =========================================================
# AI
# =========================================================

def generate_tags_from_ai(
    content: str
) -> list:

    api_key = os.getenv(
        'OP_API_KEY'
    )

    if not api_key:
        return ['API_KEY_MISSING']

    snippet = content[:800].strip()

    if not snippet:
        return []

    headers = {
        'Authorization':
            f'Bearer {api_key}',
        'Content-Type':
            'application/json'
    }

    data = {

        'model':
            'deepseek/deepseek-v4-flash',

        'messages': [

            {
                'role':
                    'system',

                'content':
                    'You are a keyword extraction assistant. '
                    'Extract 3 to 5 highly relevant keywords '
                    'in Chinese. Return ONLY a comma-separated '
                    'list of keywords, NO JSON, NO explanations.'
            },

            {
                'role':
                    'user',

                'content':
                    f'Text:\n\n{snippet}'
            }
        ],

        'max_tokens':
            100,

        'temperature':
            0.2
    }

    try:

        response = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers=headers,
            json=data,
            timeout=20
        )

        response.raise_for_status()

        raw_text = (
            response.json()[
                'choices'
            ][0][
                'message'
            ][
                'content'
            ]
            .strip()
        )

        raw_text = re.sub(
            r'<think>.*?</think>',
            '',
            raw_text,
            flags=re.DOTALL
        ).strip()

        raw_tags = []

        try:

            json_str = re.sub(
                r'^```json\s*',
                '',
                raw_text,
                flags=re.IGNORECASE
            )

            json_str = re.sub(
                r'\s*```$',
                '',
                json_str
            )

            parsed = json.loads(
                json_str
            )

            if (
                isinstance(parsed, dict)
                and 'tags' in parsed
            ):
                raw_tags = parsed['tags']

            elif isinstance(
                parsed,
                list
            ):
                raw_tags = parsed

            else:
                raise ValueError()

        except Exception:

            raw_tags = re.split(
                r'[,，、\n;；]+',
                raw_text
                    .replace('"', '')
                    .replace('`', '')
                    .replace('[', '')
                    .replace(']', '')
                    .replace('{', '')
                    .replace('}', '')
            )

        clean_tags = []

        for t in raw_tags:

            t_clean = re.sub(
                r'[^\w\u4e00-\u9fa5\-]',
                '',
                str(t)
            ).strip()

            if (
                1 < len(t_clean) <= 15
                and t_clean.lower()
                not in [
                    'tags',
                    'keywords',
                    '关键词',
                    '标签'
                ]
            ):
                clean_tags.append(
                    t_clean
                )

        return clean_tags[:5]

    except Exception as e:

        print(
            f'OpenRouter API Error: {e}'
        )

        return ['AI_ERROR']


# =========================================================
# 初始化
# =========================================================

@document_bp.route(
    '/docapi/init',
    methods=['POST']
)
def init_system():

    build_memory_index()

    return jsonify({
        'message':
            '扫描重建索引完成',
        'total':
            len(FILE_INDEX)
    })


# =========================================================
# 文档列表
# =========================================================

@document_bp.route(
    '/docapi/documents',
    methods=['GET']
)
@document_bp.route(
    '/docapi/skip/documents',
    methods=['GET']
)
def list_documents():

    docs = list(
        FILE_INDEX.values()
    )

    docs.sort(
        key=lambda x: (
            x['is_pinned'],
            x['update_time']
        ),
        reverse=True
    )

    return jsonify({
        'docs': docs
    })


# =========================================================
# 单文档
# =========================================================

@document_bp.route(
    '/docapi/document/<doc_id>',
    methods=['GET']
)
@document_bp.route(
    '/docapi/skip/document/<doc_id>',
    methods=['GET']
)
def get_document(doc_id):

    doc_info = FILE_INDEX.get(
        doc_id
    )

    if not doc_info:
        return jsonify({
            'error':
                '文档不在索引中'
        }), 404

    full_path = os.path.join(
        DOCS_DIR,
        doc_info['path']
    )

    if not os.path.exists(
        full_path
    ):
        return jsonify({
            'error':
                '物理文件丢失'
        }), 404

    with open(
        full_path,
        'r',
        encoding='utf-8'
    ) as f:

        content = f.read()

    # 再获取一次真实最新元数据
    meta, _ = parse_frontmatter(
        content
    )

    doc_info = build_doc_index(
        full_path,
        doc_info['path'],
        content,
        meta
    )

    FILE_INDEX[doc_id] = doc_info

    return jsonify({
        'id':
            doc_id,
        'content':
            content,
        'meta':
            doc_info
    })


# =========================================================
# 保存
# =========================================================
@document_bp.route(
    '/docapi/document/save',
    methods=['POST']
)
@document_bp.route(
    '/docapi/skip/document/save',
    methods=['POST']
)
def save_document():

    data = request.json or {}

    doc_id = data.get('id')
    raw_content = data.get('content', '')

    # ★ 新增：接收客户端传来的创建时间
    client_created_time = data.get('created_time')

    if not doc_id:
        return jsonify({
            'error':
                '缺少文档ID'
        }), 400

    existing_doc = FILE_INDEX.get(
        doc_id
    )

    status = (
        existing_doc['status']
        if existing_doc
        else 'active'
    )

    # -----------------------------------------------------
    # 路径
    # -----------------------------------------------------

    if (
        existing_doc
        and os.path.exists(
            os.path.join(
                DOCS_DIR,
                existing_doc['path']
            )
        )
    ):

        rel_path = existing_doc['path']

    else:

        target_dir = (
            get_target_folder(
                ARCHIVE_DIR
            )
            if status == 'archive'
            else get_target_folder(
                INBOX_DIR
            )
        )

        rel_path = os.path.relpath(
            os.path.join(
                target_dir,
                f'{doc_id}.md'
            ),
            DOCS_DIR
        )

    full_path = os.path.join(
        DOCS_DIR,
        rel_path
    )

    # -----------------------------------------------------
    # Frontmatter
    # -----------------------------------------------------

    meta, pure_content = parse_frontmatter(
        raw_content
    )

    now = now_timestamp()

    # ★ 修改：创建时间优先级
    #   已有文档 → 用索引里的 created_time，再退到请求值，最后 now
    #   新文档   → 用请求带来的 created_time，没有才用 now
    if existing_doc:

        created_time = int(
            existing_doc.get('created_time')
            or client_created_time
            or now
        )

    else:

        try:
            created_time = int(client_created_time) if client_created_time else now
        except (TypeError, ValueError):
            created_time = now

    # ★ 修改：始终以请求参数为准，避免 content 里带的旧 id 覆盖
    meta['id'] = doc_id

    meta['created_time'] = created_time

    meta['updated_time'] = now

    meta['updated'] = (
        datetime.fromtimestamp(
            now
        ).strftime(
            '%Y-%m-%d %H:%M:%S'
        )
    )

    meta['status'] = status

    final_content = build_frontmatter(
        meta,
        pure_content
    )

    with open(
        full_path,
        'w',
        encoding='utf-8'
    ) as f:

        f.write(final_content)

    # -----------------------------------------------------
    # 重新建立索引
    # -----------------------------------------------------

    FILE_INDEX[doc_id] = build_doc_index(
        full_path,
        rel_path,
        final_content,
        meta
    )

    return jsonify({
        'message':
            '保存成功',
        'meta':
            FILE_INDEX[doc_id]
    })
# =========================================================
# 【新增】重命名（修复前端已调用但后端缺失的接口）
# =========================================================

@document_bp.route(
    '/docapi/document/rename',
    methods=['POST']
)
def rename_document():

    data = request.json or {}

    old_id = data.get('old_id')
    new_id = data.get('new_id')

    if not old_id or not new_id:
        return jsonify({
            'error': '缺少 old_id 或 new_id'
        }), 400

    old_id = str(old_id).strip()
    new_id = sanitize_doc_id(new_id)

    if not new_id:
        return jsonify({
            'error': '非法的文件名'
        }), 400

    if new_id == old_id:
        # 没有实际变化，直接返回当前状态
        doc_info = FILE_INDEX.get(old_id)

        if not doc_info:
            return jsonify({
                'error': '文档不存在'
            }), 404

        return jsonify({
            'message': 'ID 未变化',
            'old_id': old_id,
            'new_id': new_id,
            'meta': doc_info
        })

    # 冲突检测
    if new_id in FILE_INDEX:
        return jsonify({
            'error': f'ID「{new_id}」已存在'
        }), 409

    doc_info = FILE_INDEX.get(old_id)

    if not doc_info:
        return jsonify({
            'error': '文档不存在'
        }), 404

    old_full_path = os.path.join(
        DOCS_DIR,
        doc_info['path']
    )

    if not os.path.exists(old_full_path):
        return jsonify({
            'error': '物理文件丢失'
        }), 404

    # -----------------------------------------------------
    # 读取并改写 frontmatter
    # -----------------------------------------------------

    with open(
        old_full_path,
        'r',
        encoding='utf-8'
    ) as f:
        raw_content = f.read()

    meta, pure_content = parse_frontmatter(
        raw_content
    )

    now = now_timestamp()

    created_time = int(
        meta.get('created_time')
        or doc_info.get('created_time')
        or now
    )

    meta['id'] = new_id
    meta['created_time'] = created_time
    meta['updated_time'] = now
    meta['updated'] = datetime.fromtimestamp(
        now
    ).strftime('%Y-%m-%d %H:%M:%S')

    final_content = build_frontmatter(
        meta,
        pure_content
    )

    # -----------------------------------------------------
    # 同目录下使用新文件名
    # -----------------------------------------------------

    old_dir = os.path.dirname(old_full_path)
    new_full_path = os.path.join(
        old_dir,
        f'{new_id}.md'
    )

    if (
        os.path.abspath(new_full_path)
        != os.path.abspath(old_full_path)
        and os.path.exists(new_full_path)
    ):
        return jsonify({
            'error': f'目标文件 {new_id}.md 已存在'
        }), 409

    try:

        with open(
            new_full_path,
            'w',
            encoding='utf-8'
        ) as f:
            f.write(final_content)

        # 若路径不同则删除旧文件
        if os.path.abspath(new_full_path) != os.path.abspath(old_full_path):
            try:
                os.remove(old_full_path)
            except Exception as e:
                print(f'删除旧文件失败 {old_full_path}: {e}')

    except Exception as e:

        print(f'重命名失败 {old_full_path} -> {new_full_path}: {e}')

        return jsonify({
            'error': f'重命名失败: {e}'
        }), 500

    # -----------------------------------------------------
    # 更新索引
    # -----------------------------------------------------

    new_rel_path = os.path.relpath(
        new_full_path,
        DOCS_DIR
    )

    FILE_INDEX.pop(old_id, None)

    FILE_INDEX[new_id] = build_doc_index(
        new_full_path,
        new_rel_path,
        final_content,
        meta
    )

    return jsonify({
        'message': '重命名成功',
        'old_id': old_id,
        'new_id': new_id,
        'meta': FILE_INDEX[new_id]
    })


# =========================================================
# 【新增】删除（软删除 → 回收站）
# =========================================================

@document_bp.route(
    '/docapi/document/delete',
    methods=['POST']
)
def delete_document():

    data = request.json or {}

    doc_id = data.get('id')

    if not doc_id:
        return jsonify({
            'error': '缺少文档ID'
        }), 400

    doc_info = FILE_INDEX.get(doc_id)

    if not doc_info:
        return jsonify({
            'error': '文档不存在'
        }), 404

    full_path = os.path.join(
        DOCS_DIR,
        doc_info['path']
    )

    # 物理文件已丢失：只清理索引
    if not os.path.exists(full_path):

        FILE_INDEX.pop(doc_id, None)

        return jsonify({
            'message': '文件已不存在，索引已清理',
            'id': doc_id
        })

    os.makedirs(TRASH_DIR, exist_ok=True)

    # 用 时间戳 + 原文件名 命名，避免重名覆盖
    trash_name = (
        f'{now_timestamp()}_'
        f'{secure_filename(os.path.basename(full_path))}'
    )

    trash_path = os.path.join(
        TRASH_DIR,
        trash_name
    )

    try:

        shutil.move(full_path, trash_path)

    except Exception as e:

        print(f'删除文档失败 {full_path}: {e}')

        return jsonify({
            'error': f'删除失败: {e}'
        }), 500

    FILE_INDEX.pop(doc_id, None)

    return jsonify({
        'message': '已移入回收站',
        'id': doc_id,
        'trash': trash_name
    })


# =========================================================
# 文档操作
# =========================================================

@document_bp.route(
    '/docapi/document/action',
    methods=['POST']
)
def document_action():

    data = request.json or {}

    doc_id = data.get('id')
    action = data.get('action')

    doc_info = FILE_INDEX.get(
        doc_id
    )

    if not doc_info:
        return jsonify({
            'error':
                '文档不存在'
        }), 404

    full_path = os.path.join(
        DOCS_DIR,
        doc_info['path']
    )

    if not os.path.exists(
        full_path
    ):
        return jsonify({
            'error':
                '物理文件丢失'
        }), 404

    with open(
        full_path,
        'r',
        encoding='utf-8'
    ) as f:
        raw_content = f.read()

    meta, pure_content = parse_frontmatter(
        raw_content
    )

    new_status = doc_info[
        'status'
    ]

    rel_path = doc_info[
        'path'
    ]

    if action == 'toggle_pin':

        meta['pinned'] = not bool(
            meta.get(
                'pinned',
                False
            )
        )

    elif action == 'toggle_archive':

        new_status = (
            'active'
            if doc_info['status'] ==
            'archive'
            else 'archive'
        )

        meta['status'] = new_status

        target_dir = (
            get_target_folder(
                ARCHIVE_DIR
            )
            if new_status == 'archive'
            else get_target_folder(
                INBOX_DIR
            )
        )

        rel_path = os.path.relpath(
            os.path.join(
                target_dir,
                f'{doc_id}.md'
            ),
            DOCS_DIR
        )

        new_full_path = os.path.join(
            DOCS_DIR,
            rel_path
        )

        shutil.move(
            full_path,
            new_full_path
        )

        full_path = new_full_path

    # -----------------------------------------------------
    # 时间
    # -----------------------------------------------------

    now = now_timestamp()

    created_time = int(
        meta.get(
            'created_time'
        )
        or doc_info.get(
            'created_time'
        )
        or now
    )

    meta['created_time'] = (
        created_time
    )

    meta['updated_time'] = now

    meta['updated'] = (
        datetime.fromtimestamp(
            now
        ).strftime(
            '%Y-%m-%d %H:%M:%S'
        )
    )

    meta['status'] = new_status

    final_content = build_frontmatter(
        meta,
        pure_content
    )

    with open(
        full_path,
        'w',
        encoding='utf-8'
    ) as f:

        f.write(final_content)

    FILE_INDEX[doc_id] = build_doc_index(
        full_path,
        rel_path,
        final_content,
        meta
    )

    return jsonify({
        'message':
            '操作成功',
        'meta':
            FILE_INDEX[doc_id]
    })


# =========================================================
# AI 标签
# =========================================================

@document_bp.route(
    '/docapi/document/ai/tags',
    methods=['POST']
)
def get_ai_tags():

    data = request.json or {}

    _, pure_content = parse_frontmatter(
        data.get(
            'content',
            ''
        )
    )

    tags = generate_tags_from_ai(
        pure_content
    )

    return jsonify({
        'tags':
            tags
    })


@document_bp.route(
    '/docapi/ai/batch_tags',
    methods=['POST']
)
def batch_ai_tags():

    pending_docs = [
        d
        for d in FILE_INDEX.values()
        if (
            not d['tags']
            or d['tags'] == ['未分类']
            or d['tags'] == ['笔记']
        )
    ]

    if not pending_docs:
        return jsonify({
            'message':
                '无待处理文档',
            'processed':
                0
        })

    processed = 0

    for doc in pending_docs[:2]:

        full_path = os.path.join(
            DOCS_DIR,
            doc['path']
        )

        if not os.path.exists(
            full_path
        ):
            continue

        with open(
            full_path,
            'r',
            encoding='utf-8'
        ) as f:

            content = f.read()

        meta, pure_content = parse_frontmatter(
            content
        )

        tags = generate_tags_from_ai(
            pure_content
        )

        if (
            tags
            and 'ERROR' not in tags[0]
            and 'MISSING' not in tags[0]
        ):

            meta['tags'] = tags

            now = now_timestamp()

            # 不修改 created_time
            meta['created_time'] = int(
                meta.get(
                    'created_time'
                )
                or doc.get(
                    'created_time'
                )
                or now
            )

            meta['updated_time'] = now

            meta['updated'] = (
                datetime.fromtimestamp(
                    now
                ).strftime(
                    '%Y-%m-%d %H:%M:%S'
                )
            )

            new_content = build_frontmatter(
                meta,
                pure_content
            )

            with open(
                full_path,
                'w',
                encoding='utf-8'
            ) as f:

                f.write(new_content)

            FILE_INDEX[doc['id']] = (
                build_doc_index(
                    full_path,
                    doc['path'],
                    new_content,
                    meta
                )
            )

            processed += 1

    return jsonify({
        'message':
            '批量处理完成',
        'processed':
            processed
    })


# =========================================================
# 相关文档
# =========================================================

@document_bp.route(
    '/docapi/document/<doc_id>/related',
    methods=['GET']
)
def get_related(doc_id):

    current_doc = FILE_INDEX.get(doc_id)

    if (
        not current_doc
        or not current_doc.get('tags')
    ):
        return jsonify([])

    target_tags = set(
        t
        for t in current_doc['tags']
        if t not in ['未分类', '笔记']
    )

    if not target_tags:
        return jsonify([])

    results = []

    for other_id, other_doc in FILE_INDEX.items():

        if other_id == doc_id:
            continue

        overlap = len(
            target_tags &
            set(
                other_doc.get(
                    'tags',
                    []
                )
            )
        )

        if overlap > 0:

            results.append({
                'id':
                    other_id,
                'title':
                    other_doc['title'],
                'overlap':
                    overlap
            })

    results.sort(
        key=lambda x: x['overlap'],
        reverse=True
    )

    return jsonify(
        results[:5]
    )


# =========================================================
# 时间线
# =========================================================

@document_bp.route(
    '/docapi/timeline',
    methods=['GET']
)
def get_timeline():

    mode = request.args.get(
        'mode',
        'created'
    )

    month = request.args.get(
        'month'
    )

    if mode not in [
        'created',
        'updated'
    ]:
        mode = 'created'

    field = (
        'created_time'
        if mode == 'created'
        else 'update_time'
    )

    date_counts = {}

    min_date = None
    max_date = None

    for doc in FILE_INDEX.values():

        ts = doc.get(field)

        if not ts:
            continue

        dt = datetime.fromtimestamp(
            ts
        )

        key = dt.strftime(
            '%Y-%m-%d'
        )

        # 全库范围
        if min_date is None or key < min_date:
            min_date = key

        if max_date is None or key > max_date:
            max_date = key

        # 月份过滤
        if month:

            if not re.match(
                r'^\d{4}-\d{2}$',
                month
            ):
                continue

            if dt.strftime(
                '%Y-%m'
            ) != month:
                continue

        date_counts[key] = (
            date_counts.get(key, 0)
            + 1
        )

    return jsonify({

        'mode':
            mode,

        'month':
            month,

        'counts':
            date_counts,

        'min_date':
            min_date,

        'max_date':
            max_date,

        'total':
            sum(
                date_counts.values()
            )

    })


# =========================================================
# 搜索
# =========================================================

@document_bp.route(
    '/docapi/search',
    methods=['GET']
)
def search_documents():

    q = request.args.get(
        'q',
        ''
    ).strip()

    if not q:
        return jsonify({
            'results': []
        })

    results = []

    q_lower = q.lower()

    for doc_id, doc in FILE_INDEX.items():

        if q.startswith('tag:'):

            tag_target = q_lower.replace(
                    'tag:',
                    ''
                ).strip()

            if (
                tag_target
                in doc['tags_cache'].lower()
            ):
                results.append(doc)

            continue

        if (
            q_lower
            in doc['title'].lower()
            or
            q_lower
            in doc['tags_cache'].lower()
        ):

            results.append(doc)

            continue

        full_path = os.path.join(
            DOCS_DIR,
            doc['path']
        )

        if os.path.exists(
            full_path
        ):

            try:

                with open(
                    full_path,
                    'r',
                    encoding='utf-8'
                ) as f:

                    if q_lower in (
                        f.read().lower()
                    ):

                        results.append(doc)

            except Exception:
                pass

    return jsonify({
        'results':
            results
    })
# =========================================================
# 统计接口
# =========================================================

import re as _re

_L1_CODES = {
    'pol','his','mil','eco','soc','psy','know','exe','med','out',
    'tec','fan','cul','dai','adu','unk',
}
_CAT_P1 = _re.compile(r'^\[([a-z]{2,8})\s+([a-z]{2,8})\]\s*(.*)$', _re.I)
_CAT_P2 = _re.compile(r'^([a-z]{2,8})\s+([a-z]{2,8})\s+(.+)$', _re.I)


def _parse_category(title):
    t = str(title or '').strip()
    m = _CAT_P1.match(t)
    if m:
        return (m.group(1).lower(), m.group(2).lower(),
                m.group(3).strip() or '未命名笔记')
    m = _CAT_P2.match(t)
    if m and m.group(1).lower() in _L1_CODES:
        return m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
    return 'unk', '', (t or '未命名笔记')


@document_bp.route('/docapi/stats', methods=['GET'])
def get_stats():
    """汇总统计：总览、分类、时间、最长/最短。"""
    now = now_timestamp()
    day_ago   = now - 86400
    week_ago  = now - 7 * 86400
    month_ago = now - 30 * 86400

    total_docs = len(FILE_INDEX)
    total_words = 0
    pinned = 0
    archived = 0
    today_new = week_new = month_new = 0

    cat_map = {}          # code -> {count, words, max, min, children{}}
    all_items = []

    for doc in FILE_INDEX.values():
        title = doc.get('title', '')
        l1, l2, clean = _parse_category(title)
        wc = int(doc.get('word_count', 0) or 0)

        total_words += wc
        if doc.get('is_pinned') == 1:
            pinned += 1
        if doc.get('status') == 'archive':
            archived += 1

        ct = int(doc.get('created_time', 0) or 0)
        if ct >= day_ago:   today_new += 1
        if ct >= week_ago:  week_new += 1
        if ct >= month_ago: month_new += 1

        s = cat_map.setdefault(l1, {
            'code': l1, 'count': 0, 'words': 0,
            'max': 0, 'min': 0, 'children': {},
        })
        s['count'] += 1
        s['words'] += wc
        if wc > s['max']: s['max'] = wc
        if s['min'] == 0 or wc < s['min']: s['min'] = wc

        if l2:
            c = s['children'].setdefault(l2, {
                'code': l2, 'count': 0, 'words': 0,
            })
            c['count'] += 1
            c['words'] += wc

        all_items.append({
            'id': doc['id'],
            'title': clean,
            'words': wc,
            'created_time': ct,
            'update_time': int(doc.get('update_time', 0) or 0),
        })

    # 平均
    categories = []
    for code, s in cat_map.items():
        s['avg'] = round(s['words'] / s['count'], 1) if s['count'] else 0
        s['children'] = [
            {**c, 'avg': round(c['words'] / c['count'], 1) if c['count'] else 0}
            for c in sorted(s['children'].values(), key=lambda x: -x['count'])
        ]
        categories.append(s)
    categories.sort(key=lambda x: -x['count'])

    top_longest = sorted(all_items, key=lambda x: -x['words'])[:5]
    top_shortest = sorted(
        [x for x in all_items if x['words'] > 0],
        key=lambda x: x['words']
    )[:5]

    return jsonify({
        'total': {
            'docs': total_docs,
            'words': total_words,
            'avg_words': round(total_words / total_docs, 1) if total_docs else 0,
            'pinned': pinned,
            'archived': archived,
            'active': total_docs - archived,
        },
        'time': {
            'today': today_new,
            'week': week_new,
            'month': month_new,
        },
        'categories': categories,
        'top_longest': top_longest,
        'top_shortest': top_shortest,
        'generated_at': now,
    })