import os

# 配置需要扫描的后缀和排除的文件夹
EXTENSIONS = ('.py', '.html', '.js', '.css')
EXCLUDE_DIRS = {'venv', '.git', '__pycache__', 'node_modules','static'}

def count_lines():
    files_info = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for file in files:
            if file.endswith(EXTENSIONS):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        lines = len(f.readlines())
                        files_info.append((path, lines))
                except:
                    continue
    
    # 按行数降序排列
    files_info.sort(key=lambda x: x[1], reverse=True)
    
    print(f"{'文件路径':<50} | {'行数':<6}")
    print("-" * 60)
    for path, count in files_info:
        # 预警色：超过 600 行提醒，超过 1000 行警告
        status = " [!] 建议拆分" if count > 600 else ""
        if count > 1000: status = " [!!!] 必须拆分"
        print(f"{path[:50]:<50} | {count:<6} {status}")

if __name__ == "__main__":
    count_lines()
