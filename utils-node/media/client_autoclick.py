import os
import time
import requests
import mss
import pyautogui
import sys
from PIL import Image

# ================= 配置区 (硬编码) =================
CLIENT_ID = "VM_WIN10_01"
SERVER_URL = "http://one4.su7.dpdns.org:5010/api/find_target"
CHECK_INTERVAL = 60

TASKS = [
    {
        "id": "check_a_init",
        "name": "检测是否在下载",
        "image_path": r"C:\Users\xin\Pictures\b_xzz.PNG",
        "action": "check_only",
        "if_success": "GOTO:check_weigui", 
        "if_fail": "GOTO:click_b"      
    },
    {
        "id": "check_weigui",
        "name": " 是否违规 清空 ",
        "image_path": r"C:\Users\xin\Pictures\b_wg.PNG",
        "action": "check_only",
        "click_reference": "right_edge",
        "click_offset": [-306, 0], 
        "if_success": "GOTO:click_b", 
        "if_fail": "GOTO:check_bd0_v1"      
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

# ================= 核心逻辑 =================

def calculate_click_pos(base_x, base_y, task_conf):
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

def find_and_act(server_url, task_conf, is_preview=False):
    screenshot_path = "temp_screen.png"
    task_id = task_conf.get('id')
    
    try:
        with mss.mss() as sct:
            sct.shot(mon=1, output=screenshot_path)
    except Exception as e:
        print(f"    [错误] 截图失败: {e}")
        return False

    # 准备上传文件
    files = {'screenshot': open(screenshot_path, 'rb')}
    data = {'client_id': CLIENT_ID, 'task_id': task_id}
    
    # 动态添加模板文件
    opened_files = [files['screenshot']]
    for key, field in [('image_path', 'template'), ('anchor_image_path', 'anchor_template')]:
        path = task_conf.get(key)
        if path and os.path.exists(path):
            f = open(path, 'rb')
            files[field] = f
            opened_files.append(f)

    try:
        response = requests.post(server_url, files=files, data=data, timeout=20)
        result = response.json()
        
        if result.get('found'):
            bx, by = result['x'], result['y']
            print(f"    [命中] {task_conf.get('name')}: ({bx}, {by})")
            
            final_x, final_y = calculate_click_pos(bx, by, task_conf)
            
            if is_preview:
                print(f"    [预演] 20s 后将点击: ({final_x}, {final_y})")
            elif task_conf.get('action') == 'click':
                print(f"    [执行] 点击 -> ({final_x}, {final_y})")
                pyautogui.moveTo(final_x, final_y, duration=0.2)
                pyautogui.click()
            return True
        return False
    except Exception as e:
        print(f"    [错误] 请求异常: {e}")
        return False
    finally:
        for f in opened_files: f.close()
        if os.path.exists(screenshot_path):
            for _ in range(5):
                try:
                    os.remove(screenshot_path)
                    break
                except:
                    time.sleep(0.1)

def workflow_dispatcher(tasks_list, server_url):
    task_map = {t['id']: t for t in tasks_list}
    current_id = tasks_list[0]['id']
    history_count = 0

    print("\n" + "—"*40)
    while current_id and history_count < 20:
        task = task_map.get(current_id)
        if not task: break
        
        print(f"[{time.strftime('%H:%M:%S')}] 任务: {task.get('name')}")
        is_preview = (task['id'] == "wait_confirm")
        success = find_and_act(server_url, task, is_preview=is_preview)
        
        delay = task.get('delay_after', 0)
        if success and delay > 0:
            print(f"    [等待] {delay}s...")
            time.sleep(delay)
            
        instruction = task.get('if_success') if success else task.get('if_fail')
        if not instruction or instruction == "TERMINATE":
            break
        elif instruction.startswith("GOTO:"):
            current_id = instruction.split("GOTO:")[1]
            time.sleep(0.2)
        history_count += 1
    print("—"*40)

def main():
    pyautogui.PAUSE = 0.1
    pyautogui.FAILSAFE = True
    print(f"[系统] 启动监控，间隔: {CHECK_INTERVAL}s")
    
    while True:
        workflow_dispatcher(TASKS, SERVER_URL)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()