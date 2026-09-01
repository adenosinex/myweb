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

MAX_FILES_PER_FOLDER = 100

# =========================================================
# 内存索引
# =========================================================

FILE_INDEX = {}

for d in [DOCS_DIR, INBOX_DIR, ARCHIVE_DIR]:
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
def save_document():

    data = request.json or {}

    doc_id = data.get('id')
    raw_content = data.get(
        'content',
        ''
    )

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

    if existing_doc:

        # 已存在文档：
        # 必须保持原 created_time
        created_time = int(
            existing_doc.get(
                'created_time'
            )
            or now
        )

    else:

        # 新文档第一次保存
        created_time = now

    meta['id'] = meta.get(
        'id',
        doc_id
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

            tag_target =q_lower.replace(
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