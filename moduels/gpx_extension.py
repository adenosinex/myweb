import os
import math
import io
import csv
from datetime import datetime
from flask import Blueprint, request, jsonify, make_response
from werkzeug.utils import secure_filename
import gpxpy

gpx_bp = Blueprint('gpx_bp', __name__, url_prefix='/gpx')

GPX_DIR = os.path.join(os.getcwd(), 'db', 'gpx')
os.makedirs(GPX_DIR, exist_ok=True)

def safe_float(val, default=0.0):
    if val is None or math.isnan(val) or math.isinf(val): return default
    return round(float(val), 2)

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
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def extract_raw_data(gpx_obj):
    raw_points = []
    for track in gpx_obj.tracks:
        for segment in track.segments:
            for point in segment.points:
                if point.time:
                    raw_points.append({
                        'time': point.time, 'lat': point.latitude,
                        'lon': point.longitude, 'ele': point.elevation or 0.0
                    })
    
    for i in range(len(raw_points)):
        window = raw_points[max(0, i-2):min(len(raw_points), i+3)]
        raw_points[i]['ele_smooth'] = sum(p['ele'] for p in window) / len(window)
        
    return raw_points

def calculate_kinematics(raw_points, config):
    base_table = []
    if not raw_points: return base_table
    
    last_pt = raw_points[0]
    max_v = config['max_valid_speed_kmh']
    min_dist = config['min_segment_dist']
    min_dt = config['min_segment_dt']
    
    for i in range(1, len(raw_points)):
        curr = raw_points[i]
        dt = (curr['time'] - last_pt['time']).total_seconds()
        if dt < min_dt: continue
            
        dist = haversine(last_pt['lat'], last_pt['lon'], curr['lat'], curr['lon'])
        if dist < min_dist: continue
            
        v_ms = dist / dt
        v_kmh = v_ms * 3.6
        
        # 核心防抖与毒锚点拦截：
        # 如果超出物理极限(漂移)且间隔<60秒，丢弃该点，且绝不更新 last_pt！避免连环失效。
        # 只有在间隔>60秒时，才认定为坐车接驳，允许更新锚点。
        if v_kmh > max_v or v_kmh < 0:
            if dt > 60: last_pt = curr
            continue
            
        ele_diff = curr['ele_smooth'] - last_pt['ele_smooth']
        slope = (ele_diff / dist * 100) if dist > 0 else 0
        v_vert_m_h = max(-3000.0, min(3000.0, (ele_diff / dt) * 3600))
        load_score = v_kmh + (v_vert_m_h / 120.0 if v_vert_m_h > 0 else 0)

        base_table.append({
            'time_obj': curr['time'], 
            'lat1': last_pt['lat'], 'lon1': last_pt['lon'], 'lat2': curr['lat'], 'lon2': curr['lon'],
            'dt': dt, 'dist': dist, 'ele_diff': safe_float(ele_diff), 'ele': safe_float(curr['ele_smooth']),
            'speed_ms': safe_float(v_ms), 'speed_kmh': safe_float(v_kmh),
            'v_vert_m_h': safe_float(v_vert_m_h), 'load_score': safe_float(load_score),
            'slope': safe_float(slope), 'bearing': safe_float(calculate_bearing(last_pt['lat'], last_pt['lon'], curr['lat'], curr['lon']))
        })
        last_pt = curr
        
    return base_table

def trim_and_rezero(base_table, config):
    if len(base_table) < 5: return base_table
    min_s = config.get('trim_min_speed_kmh', 0)
    max_s = config.get('trim_max_speed_kmh', 200)
    
    start_idx, end_idx = 0, len(base_table) - 1
    window = 5
    
    # 核心修剪：第一刀切下去的基准点必须严格合法，再向后看密度
    for i in range(len(base_table) - window + 1):
        if not (min_s <= base_table[i]['speed_kmh'] <= max_s): continue
        if sum(1 for r in base_table[i:i+window] if min_s <= r['speed_kmh'] <= max_s) >= 3:
            start_idx = i; break
            
    for i in range(len(base_table) - 1, window - 2, -1):
        if not (min_s <= base_table[i]['speed_kmh'] <= max_s): continue
        if sum(1 for r in base_table[i-window+1:i+1] if min_s <= r['speed_kmh'] <= max_s) >= 3:
            end_idx = i; break

    trimmed = base_table[start_idx:end_idx + 1] if start_idx < end_idx else base_table
    if not trimmed: return []

    true_start_time = trimmed[0]['time_obj']
    total_dist_m = 0
    for row in trimmed:
        total_dist_m += row['dist']
        row['offset_min'] = safe_float((row['time_obj'] - true_start_time).total_seconds() / 60.0)
        row['cum_dist_km'] = safe_float(total_dist_m / 1000.0)
        row['time_str'] = row['time_obj'].strftime('%H:%M:%S')
        del row['time_obj'] 

    return trimmed

def calculate_dynamics(base_table):
    total_jerk = 0.0
    for i in range(len(base_table)):
        if i == 0:
            base_table[i]['accel'], base_table[i]['jerk'] = 0.0, 0.0
            continue
        prev, curr = base_table[i-1], base_table[i]
        curr['accel'] = safe_float((curr['speed_ms'] - prev['speed_ms']) / curr['dt'])
        curr['jerk'] = safe_float((curr['accel'] - prev['accel']) / curr['dt'])
        total_jerk += abs(curr['jerk'])
    return base_table, safe_float(total_jerk / (len(base_table) - 1) if len(base_table) > 1 else 0)

def analyze_events(base_table, config):
    events = { 'hard_brakes': [], 'sharp_curves': [], 'stop_points': [], 'abnormal_speeds': [], 'steep_curves': [], 'up_steep': [], 'up_extreme': [], 'down_steep': [], 'down_extreme': [] }
    current_stop_duration, stop_start_offset = 0, 0
    last_slope_km = {'up_steep': -1, 'up_extreme': -1, 'down_steep': -1, 'down_extreme': -1}

    for i, row in enumerate(base_table):
        dist_km = row['cum_dist_km']
        if row['speed_kmh'] < config['stop_speed_threshold']:
            if current_stop_duration == 0: stop_start_offset = row['offset_min']
            current_stop_duration += row['dt']
        else:
            if current_stop_duration >= config['stop_duration_sec']:
                events['stop_points'].append({ 'offset_min': stop_start_offset, 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"停留时长: {round(current_stop_duration/60, 1)} 分钟" })
            current_stop_duration = 0
            
        if config['enable_dynamics']:
            if row['accel'] < config['hard_brake_threshold']:
                if not events['hard_brakes'] or (row['offset_min'] - events['hard_brakes'][-1]['offset_min']) > 0.5:
                    events['hard_brakes'].append({ 'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"减速度: {row['accel']} m/s²" })
            if abs(row['accel']) > config['abnormal_accel_threshold'] and abs(row['slope']) < 3:
                turn_angle = abs(row['bearing'] - base_table[i-1]['bearing']) if i > 0 else 0
                turn_angle = 360 - turn_angle if turn_angle > 180 else turn_angle
                if turn_angle < 10 and (not events['abnormal_speeds'] or (row['offset_min'] - events['abnormal_speeds'][-1]['offset_min']) > 0.5):
                    action = "突加速" if row['accel'] > 0 else "突减速"
                    events['abnormal_speeds'].append({'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"直道{action}: a={row['accel']} m/s²"})

            turn_angle = 0
            if i > 0:
                turn_angle = abs(row['bearing'] - base_table[i-1]['bearing'])
                turn_angle = 360 - turn_angle if turn_angle > 180 else turn_angle
                if turn_angle > config['sharp_curve_angle'] and row['dist'] < 100 and row['speed_kmh'] > config['sharp_curve_speed']:
                    if not events['sharp_curves'] or (row['offset_min'] - events['sharp_curves'][-1]['offset_min']) > 0.2:
                        events['sharp_curves'].append({'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"入弯: {row['speed_kmh']} km/h, 偏航: {round(turn_angle)}°"})
                    if abs(row['slope']) >= config['steep_slope_threshold'] and (not events['steep_curves'] or (row['offset_min'] - events['steep_curves'][-1]['offset_min']) > 0.2):
                        events['steep_curves'].append({'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"险弯: 坡度 {row['slope']}%, 入弯 {row['speed_kmh']} km/h"})

        slope = row['slope']
        steep, extreme = config['steep_slope_threshold'], config['extreme_slope_threshold']
        if steep <= slope < extreme:
            if dist_km - last_slope_km['up_steep'] > 0.2: events['up_steep'].append({'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"坡度: +{slope}%"}); last_slope_km['up_steep'] = dist_km
        elif slope >= extreme:
            if dist_km - last_slope_km['up_extreme'] > 0.2: events['up_extreme'].append({'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"坡度: +{slope}%"}); last_slope_km['up_extreme'] = dist_km
        elif -extreme < slope <= -steep:
            if dist_km - last_slope_km['down_steep'] > 0.2: events['down_steep'].append({'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"坡度: {slope}%"}); last_slope_km['down_steep'] = dist_km
        elif slope <= -extreme:
            if dist_km - last_slope_km['down_extreme'] > 0.2: events['down_extreme'].append({'offset_min': row['offset_min'], 'cum_dist_km': dist_km, 'lat': row['lat2'], 'lon': row['lon2'], 'desc': f"坡度: {slope}%"}); last_slope_km['down_extreme'] = dist_km

    return events

def analyze_terrain(base_table, events_data, moving_speed_threshold):
    if not base_table: return {'total_distance': 0, 'total_climb': 0, 'avg_speed': 0}
    total_dist_m = sum(row['dist'] for row in base_table)
    total_climb = sum(row['ele_diff'] for row in base_table if row['ele_diff'] > 0)
    moving_segs = [s for s in base_table if s['speed_kmh'] > moving_speed_threshold]
    moving_time = sum(s['dt'] for s in moving_segs)
    avg_speed = (sum(s['dist'] for s in moving_segs) / moving_time * 3.6) if moving_time > 0 else 0
    return {
        'total_distance': safe_float(total_dist_m / 1000),
        'total_climb': safe_float(total_climb),
        'max_uphill': safe_float(max((r['slope'] for r in base_table), default=0)),
        'max_downhill': safe_float(min((r['slope'] for r in base_table), default=0)),
        'slope_stats': {'up_steep': len(events_data['up_steep']), 'up_extreme': len(events_data['up_extreme']), 'down_steep': len(events_data['down_steep']), 'down_extreme': len(events_data['down_extreme'])},
        'avg_speed': safe_float(avg_speed),
        'total_time_min': safe_float(sum(s['dt'] for s in base_table) / 60)
    }

def compare_tracks(track_results):
    if len(track_results) < 2: return None
    max_dist = max(tr['overall']['terrain']['total_distance'] for tr in track_results)
    bin_size = 0.1
    num_bins = int(max_dist / bin_size) + 1
    
    comparison_data = {
        'bins_km': [safe_float(i * bin_size) for i in range(num_bins)],
        'track_names': [tr['filename'] for tr in track_results],
        'cum_time_matrix': {tr['filename']: [0] * num_bins for tr in track_results},
        'time_diff_vs_baseline': {}
    }
    
    for tr in track_results:
        fname = tr['filename']
        current_bin = 0
        current_time = 0
        for seg in tr['segments']:
            while seg['cum_dist_km'] > (current_bin + 1) * bin_size and current_bin < num_bins - 1:
                comparison_data['cum_time_matrix'][fname][current_bin] = current_time
                current_bin += 1
            current_time = seg['offset_min']
        while current_bin < num_bins:
            comparison_data['cum_time_matrix'][fname][current_bin] = current_time
            current_bin += 1

    baseline_name = comparison_data['track_names'][0]
    baseline_times = comparison_data['cum_time_matrix'][baseline_name]
    for fname in comparison_data['track_names'][1:]:
        times = comparison_data['cum_time_matrix'][fname]
        comparison_data['time_diff_vs_baseline'][fname] = [safe_float((times[i] - baseline_times[i]) * 60) for i in range(num_bins)]
    return comparison_data

# 新增精准的上下限控制
SPORT_PROFILES = {
    'motor': { 'min_segment_dist': 10, 'min_segment_dt': 1.5, 'trim_min_speed_kmh': 5.0, 'trim_max_speed_kmh': 200.0, 'max_valid_speed_kmh': 200.0, 'moving_speed_threshold': 3.0, 'stop_speed_threshold': 3.0, 'stop_duration_sec': 60, 'enable_dynamics': True, 'hard_brake_threshold': -3.5, 'abnormal_accel_threshold': 2.5, 'sharp_curve_angle': 30, 'sharp_curve_speed': 25, 'steep_slope_threshold': 8, 'extreme_slope_threshold': 15 },
    'cycle': { 'min_segment_dist': 5,  'min_segment_dt': 1.5, 'trim_min_speed_kmh': 3.0, 'trim_max_speed_kmh': 80.0,  'max_valid_speed_kmh': 80.0,  'moving_speed_threshold': 2.0, 'stop_speed_threshold': 1.5, 'stop_duration_sec': 30, 'enable_dynamics': True, 'hard_brake_threshold': -2.0, 'abnormal_accel_threshold': 1.5, 'sharp_curve_angle': 45, 'sharp_curve_speed': 15, 'steep_slope_threshold': 6, 'extreme_slope_threshold': 12 },
    'hike':  { 'min_segment_dist': 3,  'min_segment_dt': 2.0, 'trim_min_speed_kmh': 0.5, 'trim_max_speed_kmh': 10.0,  'max_valid_speed_kmh': 12.0,  'moving_speed_threshold': 0.5, 'stop_speed_threshold': 0.5, 'stop_duration_sec': 120, 'enable_dynamics': False, 'hard_brake_threshold': -99, 'abnormal_accel_threshold': 99, 'sharp_curve_angle': 180, 'sharp_curve_speed': 99, 'steep_slope_threshold': 12, 'extreme_slope_threshold': 25 }
}
SPORT_PROFILES['motor']['score_evaluator'] = lambda dist, b, j: max(0, min(100, round(100 - (b/dist*100*1.5) - (j*30), 1))) if dist > 0 else 100
SPORT_PROFILES['cycle']['score_evaluator'] = lambda dist, b, j: max(0, min(100, round(100 - (b/dist*100*2.0) - (j*20), 1))) if dist > 0 else 100
SPORT_PROFILES['hike']['score_evaluator']  = lambda dist, c, _: min(100, round(60 + (c/dist*10) if dist > 0 else 60, 1))

@gpx_bp.route('/list_files', methods=['GET'])
def list_files():
    files = [f for f in os.listdir(GPX_DIR) if f.lower().endswith('.gpx')]
    files.sort(key=lambda x: os.path.getmtime(os.path.join(GPX_DIR, x)), reverse=True)
    return jsonify({'files': files})

@gpx_bp.route('/analyze/<sport_type>', methods=['POST'])
def analyze_gpx(sport_type):
    if sport_type not in SPORT_PROFILES: return jsonify({'error': '不支持的运动类型'}), 400
    config = SPORT_PROFILES[sport_type]
    
    filenames = request.form.getlist('filenames[]')
    files_to_process = []
    if filenames:
        for fname in filenames:
            fpath = os.path.join(GPX_DIR, secure_filename(fname))
            if os.path.exists(fpath): files_to_process.append((fname, fpath))
    else:
        for f in request.files.getlist('files[]'):
            if f.filename:
                safe_name = secure_filename(f.filename)
                fpath = os.path.join(GPX_DIR, safe_name)
                f.save(fpath)
                files_to_process.append((safe_name, fpath))

    if not files_to_process: return jsonify({'error': '未提供合法文件'}), 400

    results = []
    for fname, fpath in files_to_process:
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                gpx_obj = gpxpy.parse(f)
            raw_points = extract_raw_data(gpx_obj)
            if len(raw_points) < 2: continue
            
            k_raw = calculate_kinematics(raw_points, config)
            k_trim = trim_and_rezero(k_raw, config)
            final_table, avg_jerk = calculate_dynamics(k_trim)
            
            if not final_table:
                print(f"Skipping {fname}: 数据修剪后无有效段落")
                continue
                
            events = analyze_events(final_table, config)
            terrain = analyze_terrain(final_table, events, config['moving_speed_threshold'])
            
            score = config['score_evaluator'](terrain['total_distance'], len(events['hard_brakes']) if sport_type != 'hike' else terrain['total_climb'], avg_jerk)
            results.append({
                'filename': fname,
                'overall': { 'score': score, 'terrain': terrain, 'counts': { k: len(v) for k, v in events.items() if isinstance(v, list) } },
                'events': events,
                'segments': final_table
            })
        except Exception as e:
            print(f"Skipping {fname}: {str(e)}")

    if not results: return jsonify({'error': 'GPS 轨迹完全失效，或已全部被极端阈值拦截'}), 400
    return jsonify({ 'tracks': results, 'comparison': compare_tracks(results) if len(results) > 1 else None })