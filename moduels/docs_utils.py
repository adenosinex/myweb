import json
import re
import time
from datetime import datetime
import requests

class WeatherService:
    def __init__(self):
        # 伪装请求头，防止被反爬拦截
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://weather.com.cn/"
        }

    def send_weather(self, url):
        """发送HTTP请求获取原始文本"""
        resp = requests.get(url, headers=self.headers, timeout=5)
        resp.encoding = 'utf-8' # 气象局接口多为 utf-8，确保中文不乱码
        return resp.text

    def get_weather(self, lat=31.093228072338526, lon=109.91479684164297):
        """获取并解析原始天气信息"""
        try:
            timestamp = int(datetime.now().timestamp() * 1000)
            
            # 1. 获取分钟级降水预报 (短临预报)
            url_less = f"https://mpf.weather.com.cn/mpf_v3/webgis/minute?lat={lat}&lon={lon}&callback=fc5m&_={timestamp}"
            content_less = self.send_weather(url_less)
            match_less = re.search(r'fc5m\((.*?)\)', content_less, re.DOTALL)
            
            # 2. 获取实时天气实况 (SK)
            url_more = f"https://forecast.weather.com.cn/town/api/v1/sk?lat={lat}&lng={lon}&callback=getDataSK&_={timestamp}"
            content = self.send_weather(url_more)
            match_more = re.search(r'getDataSK\((.*?)\)', content, re.DOTALL)
            
            if match_more and match_less:
                weather_data = json.loads(match_more.group(1))
                weather_data_less = json.loads(match_less.group(1))
                
                return {
                    'status': 'success',
                    'data': {
                        'temperature': weather_data.get('temp', 'N/A'),
                        'humidity': weather_data.get('humidity', 'N/A'),
                        'wind_direction': weather_data.get('WD', 'N/A'), # 新增：风向提取
                        'wind_speed': weather_data.get('WS', 'N/A'),     # 风力/风速
                        'weather_desc': weather_data.get('weather', '未知'),
                        'forecast': weather_data_less.get('msg', '暂无短临预报'), # 提取短临预报的文本
                        'timestamp': datetime.now().isoformat()
                    },
                    'message': 'success'
                }
            else:
                return {'status': 'error', 'message': 'JSONP 解析匹配失败', 'data': {}}
                
        except Exception as e:
            return {'status': 'error', 'message': f'获取天气信息失败: {str(e)}', 'data': {}}
    
    def get_weather_simple(self, lat=31.093228072338526, lon=109.91479684164297, location="巫山"):
        """获取简化版天气，专供前端或 Markdown 渲染调用"""
        weather_data = self.get_weather(lat, lon)
        
        if weather_data['status'] == 'success':
            data = weather_data['data']
            return {
                'status': 'success',
                'data': {
                    'location': location, 
                    'temperature': data['temperature'],
                    'weather_desc': data['weather_desc'],
                    'humidity': f"{data['humidity']}%",
                    'wind_direction': data['wind_direction'],
                    'wind_speed': data['wind_speed'],
                    'forecast': data['forecast'],
                    'now': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                }
            }
        return weather_data

    def get_weather_text(self, lat=31.093228072338526, lon=109.91479684164297, location="巫山"):
        """一句话天气，最适合嵌入到 Markdown 中"""
        res = self.get_weather_simple(lat, lon, location)
        if res['status'] == 'success':
            d = res['data']
            return f"{d['location']}当前 {d['weather_desc']} {d['temperature']}°C，{d['wind_direction']}{d['wind_speed']}，湿度 {d['humidity']}。{d['forecast']}"
        return "实时天气获取失败"