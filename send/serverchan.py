import os
from serverchan_sdk import sc_send

def get_env_config():
    # 锁定当前脚本的绝对路径
    base_path = os.path.splitext(os.path.abspath(__file__))[0]
    env_path = base_path + ".env"
    
    config = {}
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip()
    return config

# 预加载配置
ENV = get_env_config()

def send_to(report_title, report_content, report_tag):
    # 从配置中提取 SENDKEY
    sendkey = ENV.get("SENDKEY")
    
    if not sendkey:
        print("错误: env 文件中未找到 SENDKEY")
        return None

    # 调用 Server酱 SDK
    options = {
        "tags": f"网络巡检|{report_tag}"
    }
    
    try:
        response = sc_send(
            sendkey, 
            report_title, 
            report_content, 
            options
        )
        return response
    except Exception as e:
        print(f"Server酱发送失败: {e}")
        return None