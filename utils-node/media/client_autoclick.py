import os
import time
import requests
import mss
import pyautogui
import yaml
import shutil
import sys
from PIL import Image

# ================= 配置区 =================
CONFIG_FILE = "config.yaml"
MANAGED_DIR = "templates"
CLIENT_ID = "VM_WIN10_01"

def calculate_click_pos(base_x, base_y, task_conf):
    """
    统一坐标计算规则：基准 + 偏移
    Top-Left: (base_x + offset_x, base_y + offset_y)
    Right-Edge: (base_x + width + offset_x, base_y + offset_y)
    """
    tx, ty = base_x, base_y
    ref = task_conf.get('click_reference', 'top_left')
    img_path = task_conf.get('image_path')
    
    if ref == 'right_edge' and img_path and os.path.exists(img_path):
        try:
            with Image.open(img_path) as img:
                w, _ = img.size
                tx = base_x + w
        except Exception as e:
            print(f"    [警告] 读取图片宽度失败: {e}")
            
    offset = task_conf.get('click_offset', [0, 0])
    return int(tx + offset[0]), int(ty + offset[1])

import time # 确保顶部导入了 time

def find_and_act(server_url, task_conf, is_preview=False):
    screenshot_path = "temp_screen.png"
    task_id = task_conf.get('id')
    
    # 1. 确保截图完成并释放句柄
    try:
        with mss.mss() as sct:
            sct.shot(mon=1, output=screenshot_path)
    except Exception as e:
        print(f"    [错误] 截图失败: {e}")
        return False

    # 2. 准备文件上传
    files = {'screenshot': open(screenshot_path, 'rb')}
    data = {'client_id': CLIENT_ID, 'task_id': task_id}
    
    for key, field in [('image_path', 'template'), ('anchor_image_path', 'anchor_template')]:
        path = task_conf.get(key)
        if path and os.path.exists(path):
            files[field] = open(path, 'rb')

    try:
        response = requests.post(server_url, files=files, data=data, timeout=20)
        result = response.json()
        
        if result.get('found'):
            bx, by = result['x'], result['y']
            print(f"    [命中] 找到目标: 原始左上角({bx}, {by})")
            
            final_x, final_y = calculate_click_pos(bx, by, task_conf)
            
            if is_preview:
                print(f"    [预演] 检测成功。若20s后目标仍在，将点击坐标: ({final_x}, {final_y})")
            elif task_conf.get('action') == 'click':
                print(f"    [执行] 点击动作 -> 目标坐标: ({final_x}, {final_y})")
                pyautogui.moveTo(final_x, final_y, duration=0.2)
                pyautogui.click()
            else:
                print(f"    [状态] 纯检测模式，计算参考点: ({final_x}, {final_y})")
            return True
        return False
    except Exception as e:
        print(f"    [错误] 网络请求失败: {e}")
        return False
    finally:
        # 3. 安全关闭文件句柄
        for f in files.values():
            try:
                f.close()
            except:
                pass
        
        # 4. 安全删除临时文件 (带重试机制)
        # Windows 下经常因为文件句柄未释放导致删除失败，重试通常能解决
        if os.path.exists(screenshot_path):
            for attempt in range(5): # 尝试删除 5 次
                try:
                    os.remove(screenshot_path)
                    break # 删除成功则跳出循环
                except PermissionError:
                    time.sleep(0.1) # 等待 0.1 秒后重试

                    
def generate_default_yaml():
    default_config = {
        "system": {
            "server_url": "http://one4.su7.dpdns.org:5010/api/find_target",
            "download_dir": r"\\One\d\downloadD",
            "check_interval": 5,
            "debug_mode": True
        },
        "tasks": [
            {
                "id": "check_a_init",
                "name": "检测是否在下载",
                "image_path": r"C:\Users\xin\Pictures\b_xzz.PNG",
                "action": "check_only",
                "if_success": "GOTO:check_bd0_v1", 
                "if_fail": "GOTO:click_b"      
            },
            {
                "id": "check_bd0_v1",
                "name": "速度归零首检",
                "image_path": r"C:\Users\xin\Pictures\bd0.PNG",
                "action": "check_only",
                "if_success": "GOTO:wait_confirm",
                "if_fail": "TERMINATE"
            },
            {
                "id": "wait_confirm",
                "name": "归零等待确认(20s)",
                "image_path": r"C:\Users\xin\Pictures\bd0.PNG",
                "action": "check_only",
                "click_reference": "right_edge",
                "click_offset": [-306, 0], 
                "delay_after": 20,
                "if_success": "GOTO:check_bd0_v2",
                "if_fail": "TERMINATE"
            },
            {
                "id": "check_bd0_v2",
                "name": "确认清理(终检点击)",
                "image_path": r"C:\Users\xin\Pictures\bd0.PNG",
                "action": "click",
                "click_reference": "right_edge",
                "click_offset": [-306, 0], 
                "if_success": "TERMINATE",
                "if_fail": "TERMINATE"
            },
            {
                "id": "click_b",
                "name": "点击开始下载",
                "image_path": r"C:\Users\xin\Pictures\b_dw.PNG",
                "action": "click",
                "delay_after": 3,
                "if_success": "GOTO:check_a_again",
                "if_fail": "GOTO:click_c"
            },
            {
                "id": "check_a_again",
                "name": "再次检测状态",
                "image_path": r"C:\Users\xin\Pictures\b_xzz.PNG",
                "action": "check_only",
                "if_success": "GOTO:click_c",  
                "if_fail": "GOTO:click_e_1"    
            },
            {
                "id": "click_e_1",
                "name": "异常清理(E1)",
                "anchor_image_path": r"C:\Users\xin\Pictures\b_anchor.PNG",
                "image_path": r"C:\Users\xin\Pictures\b_sc.PNG",
                "action": "click",
                "if_success": "TERMINATE",
                "if_fail": "TERMINATE"
            },
            {
                "id": "click_c",
                "name": "点击添加链接",
                "image_path": r"C:\Users\xin\Pictures\b_get.PNG",
                "action": "click",
                "delay_after": 5,
                "if_success": "GOTO:check_d",
                "if_fail": "TERMINATE"
            },
            {
                "id": "check_d",
                "name": "检测下载弹窗",
                "image_path": r"C:\Users\xin\Pictures\b_dw.PNG",
                "action": "check_only",
                "if_success": "TERMINATE",    
                "if_fail": "GOTO:click_e_2"    
            },
            {
                "id": "click_e_2",
                "name": "异常清理(E2)",
                "anchor_image_path": r"C:\Users\xin\Pictures\b_anchor.PNG",
                "image_path": r"C:\Users\xin\Pictures\b_sc.PNG",
                "action": "click",
                "if_success": "TERMINATE",
                "if_fail": "TERMINATE"
            }
        ]
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(default_config, f, allow_unicode=True, sort_keys=False)
    print(f"[配置] 默认文件 {CONFIG_FILE} 已更新。")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        generate_default_yaml()
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def workflow_dispatcher(tasks_list, server_url):
    task_map = {t['id']: t for t in tasks_list}
    current_id = tasks_list[0]['id']
    history = []

    print("\n" + "—"*40)
    while current_id:
        task = task_map.get(current_id)
        if not task or len(history) > 20: break
        
        now = time.strftime("%H:%M:%S")
        print(f"[{now}] 正在处理: {task.get('name')}")
        history.append(current_id)
        
        is_preview = (task['id'] == "wait_confirm")
        success = find_and_act(server_url, task, is_preview=is_preview)
        
        delay = task.get('delay_after', 0)
        if success and delay > 0:
            print(f"    [等待] 挂起 {delay}s...")
            time.sleep(delay)
            
        instruction = task.get('if_success') if success else task.get('if_fail')
        if not instruction or instruction == "TERMINATE":
            print(f"[{now}] 流程正常结束。")
            break
        elif instruction.startswith("GOTO:"):
            current_id = instruction.split("GOTO:")[1]
            print(f"    [流转] 下一站 -> {current_id}")
            time.sleep(0.2)
    print("—"*40)

def main_loop():
    config = load_config()
    sys_conf = config['system']
    
    # 强制重新加载任务以应用最新的 image_path 逻辑
    tasks = config.get('tasks', [])
    
    while True:
        workflow_dispatcher(tasks, sys_conf['server_url'])
        time.sleep(sys_conf['check_interval'])

if __name__ == "__main__":
    pyautogui.PAUSE = 0.1
    pyautogui.FAILSAFE = True
    main_loop()