# regenerate_csv.py
import os
import sqlite3
# 假设你的蓝图文件名为 hike_app.py，从中引入数据库路径和解析函数
from  moduels.tech.1hike_extension  import DB_FILE, parse_gpx_to_csv

def batch_regenerate():
    if not os.path.exists(DB_FILE):
        print(f"错误: 找不到数据库文件 {DB_FILE}")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT id, original_name, gpx_path, csv_path FROM gpx_records")
    records = cursor.fetchall()
    
    if not records:
        print("数据库中没有 GPX 记录。")
        return

    success_count = 0
    fail_count = 0

    for row in records:
        record_id = row['id']
        name = row['original_name']
        gpx_path = row['gpx_path']
        csv_path = row['csv_path']

        print(f"正在处理 [{record_id}] {name} ...", end=" ")

        if not os.path.exists(gpx_path):
            print("失败: 原始 GPX 文件丢失。")
            fail_count += 1
            continue

        # 执行解析并覆盖原 CSV
        if parse_gpx_to_csv(gpx_path, csv_path):
            print("成功。")
            success_count += 1
        else:
            print("失败: 解析异常。")
            fail_count += 1

    conn.close()
    print("-" * 30)
    print(f"批量处理完成！成功覆盖: {success_count} 个，失败: {fail_count} 个。")

if __name__ == "__main__":
    batch_regenerate()