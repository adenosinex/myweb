import math
from flask import Blueprint, request, jsonify
import gpxpy

gpx_bp = Blueprint('gpx_bp', __name__)

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    initial_bearing = math.degrees(math.atan2(x, y))
    return (initial_bearing + 360) % 360

def extract_raw_data(gpx_obj):
    raw_points = []
    for track in gpx_obj.tracks:
        for segment in track.segments:
            for point in segment.points:
                if point.time:
                    raw_points.append({
                        'time': point.time,
                        'lat': point.latitude,
                        'lon': point.longitude,
                        'ele': point.elevation or 0.0
                    })
    return raw_points

def calculate_kinematics(raw_points):
    base_table = []
    start_time = raw_points[0]['time'] if raw_points else None
    
    for i in range(1, len(raw_points)):
        p1, p2 = raw_points[i-1], raw_points[i]
        dt = (p2['time'] - p1['time']).total_seconds()
        
        if dt < 0.5: continue
            
        dist = haversine(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
        v_ms = dist / dt
        v_kmh = v_ms * 3.6
        
        if v_kmh > 200: continue
            
        ele_diff = p2['ele'] - p1['ele']
        slope = (ele_diff / dist * 100) if dist > 0 else 0
        bearing = calculate_bearing(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
        
        offset_min = round((p2['time'] - start_time).total_seconds() / 60.0, 1)

        base_table.append({
            'offset_min': offset_min,
            'time_str': p2['time'].strftime('%H:%M:%S'),
            'lat1': p1['lat'], 'lon1': p1['lon'],
            'lat2': p2['lat'], 'lon2': p2['lon'],
            'dt': dt,
            'dist': dist,
            'ele_diff': ele_diff,
            'speed_ms': v_ms,
            'speed_kmh': round(v_kmh, 1),
            'slope': round(slope, 1),
            'bearing': bearing,
            'ele': round(p2['ele'], 1)
        })
    return base_table

def calculate_dynamics(base_table):
    total_jerk = 0.0
    for i in range(len(base_table)):
        if i == 0:
            base_table[i]['accel'] = 0.0
            base_table[i]['jerk'] = 0.0
            continue
            
        prev, curr = base_table[i-1], base_table[i]
        accel = (curr['speed_ms'] - prev['speed_ms']) / curr['dt']
        curr['accel'] = round(accel, 2)
        
        jerk = (curr['accel'] - prev['accel']) / curr['dt']
        curr['jerk'] = round(jerk, 2)
        total_jerk += abs(jerk)
        
    avg_jerk = total_jerk / (len(base_table) - 1) if len(base_table) > 1 else 0
    return base_table, avg_jerk

def analyze_events(base_table, hard_brake_threshold=-3.5, stop_threshold_kmh=3.0):
    events = {
        'hard_brakes': [],
        'sharp_curves': [],
        'stop_points': [],
        'abnormal_speeds': [],
        'steep_curves': []  # 【新增】危险陡坡弯道
    }
    current_stop_duration = 0
    stop_start_offset = 0

    for i, row in enumerate(base_table):
        # 1. 停车识别
        if row['speed_kmh'] < stop_threshold_kmh:
            if current_stop_duration == 0:
                stop_start_offset = row['offset_min']
            current_stop_duration += row['dt']
        else:
            if current_stop_duration >= 60:
                events['stop_points'].append({
                    'offset_min': stop_start_offset,
                    'lat': row['lat2'], 'lon': row['lon2'],
                    'desc': f"停留时长: {round(current_stop_duration/60, 1)} 分钟"
                })
            current_stop_duration = 0
            
        # 2. 重刹识别
        if row['accel'] < hard_brake_threshold:
            if not events['hard_brakes'] or (row['offset_min'] - events['hard_brakes'][-1]['offset_min']) > 0.5:
                events['hard_brakes'].append({
                    'offset_min': row['offset_min'],
                    'lat': row['lat2'], 'lon': row['lon2'],
                    'desc': f"减速度: {row['accel']} m/s² (时速降至 {row['speed_kmh']}km/h)"
                })
            
        # 3. 弯道及陡坡弯道识别
        turn_angle = 0
        if i > 0:
            turn_angle = abs(row['bearing'] - base_table[i-1]['bearing'])
            turn_angle = 360 - turn_angle if turn_angle > 180 else turn_angle
            
            if turn_angle > 30 and row['dist'] < 100 and row['speed_kmh'] > 15:
                # 常规急弯
                if not events['sharp_curves'] or (row['offset_min'] - events['sharp_curves'][-1]['offset_min']) > 0.2:
                    events['sharp_curves'].append({
                        'offset_min': row['offset_min'],
                        'lat': row['lat2'], 'lon': row['lon2'],
                        'desc': f"入弯时速: {row['speed_kmh']} km/h, 偏航角变化: {round(turn_angle)}°"
                    })
                # 【新增】危险陡坡急弯 (绝对坡度 >= 8% 且伴随急弯，极度考验刹车和控车)
                if abs(row['slope']) >= 8:
                    if not events['steep_curves'] or (row['offset_min'] - events['steep_curves'][-1]['offset_min']) > 0.2:
                        direction = "上坡" if row['slope'] > 0 else "下坡"
                        events['steep_curves'].append({
                            'offset_min': row['offset_min'],
                            'lat': row['lat2'], 'lon': row['lon2'],
                            'desc': f"危险{direction}弯: 坡度 {row['slope']}%, 入弯时速 {row['speed_kmh']} km/h"
                        })

        # 4. 异常速度突变
        if abs(row['accel']) > 2.0 and abs(row['slope']) < 3 and turn_angle < 10:
            if not events['abnormal_speeds'] or (row['offset_min'] - events['abnormal_speeds'][-1]['offset_min']) > 0.5:
                action = "突加速" if row['accel'] > 0 else "突减速"
                events['abnormal_speeds'].append({
                    'offset_min': row['offset_min'],
                    'lat': row['lat2'], 'lon': row['lon2'],
                    'desc': f"直道{action}: 瞬时 a={row['accel']} m/s², 当前速度 {row['speed_kmh']} km/h"
                })

    return events

def analyze_terrain(base_table):
    if not base_table:
        return {'total_distance': 0, 'total_climb': 0, 'max_ele': 0, 'max_slope': 0, 'avg_speed': 0}

    total_dist_m = sum(row['dist'] for row in base_table)
    total_climb = sum(row['ele_diff'] for row in base_table if row['ele_diff'] > 0)
    
    moving_segments = [s for s in base_table if s['speed_kmh'] > 3.0]
    moving_time = sum(s['dt'] for s in moving_segments)
    avg_speed = (sum(s['dist'] for s in moving_segments) / moving_time * 3.6) if moving_time > 0 else 0

    # 【新增】多级坡度统计 (基于真实物理环境，8%以上开始吃力，15%以上为极限恶劣路况)
    up_steep = sum(1 for r in base_table if 8 <= r['slope'] < 15)
    up_extreme = sum(1 for r in base_table if r['slope'] >= 15)
    down_steep = sum(1 for r in base_table if -15 < r['slope'] <= -8)
    down_extreme = sum(1 for r in base_table if r['slope'] <= -15)

    max_uphill = max((r['slope'] for r in base_table), default=0)
    max_downhill = min((r['slope'] for r in base_table), default=0)

    return {
        'total_distance': round(total_dist_m / 1000, 1),  # 【修复】统一命名为 total_distance 解决空值问题
        'total_climb': round(total_climb),
        'max_ele': round(max(r['ele'] for r in base_table)),
        'max_uphill': max_uphill,
        'max_downhill': max_downhill,
        'slope_stats': {
            'up_steep': up_steep,
            'up_extreme': up_extreme,
            'down_steep': down_steep,
            'down_extreme': down_extreme
        },
        'avg_speed': round(avg_speed, 1)
    }

@gpx_bp.route('/analyze_gpx', methods=['POST'])
def analyze_gpx():
    if 'file' not in request.files: return jsonify({'error': '未上传文件'}), 400
    file = request.files['file']
    if file.filename == '' or not file.filename.lower().endswith('.gpx'):
        return jsonify({'error': '请上传有效的 .gpx 文件'}), 400

    try:
        gpx_obj = gpxpy.parse(file)
    except Exception as e:
        return jsonify({'error': f'GPX解析失败: {str(e)}'}), 400

    raw_points = extract_raw_data(gpx_obj)
    if len(raw_points) < 2: return jsonify({'error': '轨迹点不足'}), 400

    kinematics_table = calculate_kinematics(raw_points)
    final_table, avg_jerk = calculate_dynamics(kinematics_table)
    
    events_data = analyze_events(final_table)
    terrain_data = analyze_terrain(final_table)
    
    brakes_count = len(events_data['hard_brakes'])
    dist_km = terrain_data['total_distance']
    score = max(0, min(100, round(100 - (brakes_count / dist_km * 100 * 1.5) - (avg_jerk * 30), 1))) if dist_km > 0 else 100
    
    if score >= 85: smoothness_text = "极佳 (发力线性，寻迹精准)"
    elif score >= 70: smoothness_text = "良好 (粗中有细的巡航)"
    else: smoothness_text = "激烈 (加减速频繁)"

    return jsonify({
        'overall': {
            'score': score,
            'smoothness_text': smoothness_text,
            'avg_speed_kmh': terrain_data['avg_speed'],
            'terrain': terrain_data,
            'counts': {
                'hard_brakes': brakes_count,
                'stop_points': len(events_data['stop_points']),
                'sharp_curves': len(events_data['sharp_curves']),
                'abnormal_speeds': len(events_data['abnormal_speeds']),
                'steep_curves': len(events_data['steep_curves']) # 暴露至前端面板
            }
        },
        'events': events_data,
        'segments': final_table
    })