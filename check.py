import os
import sys
import requests
import logging
import importlib
import time
from datetime import datetime, timedelta

# ================== [1. 自动化环境配置加载] ==================
def get_config():
    if len(sys.argv) < 2:
        print(f"用法: python3 {os.path.basename(__file__)} <配置文件名>")
        sys.exit(1)

    conf_path = os.path.abspath(sys.argv[1])
    conf_filename = os.path.splitext(os.path.basename(conf_path))[0]
    
    # 状态文件按要求锁定在 /tmp 目录下
    state_file = os.path.join("/tmp", f"{conf_filename}_last_run.txt")
    
    config = {}
    if not os.path.exists(conf_path):
        print(f"错误: 配置文件不存在 -> {conf_path}")
        sys.exit(1)

    with open(conf_path, 'r', encoding='utf-8') as f:
        lines = f.read().split('\n')
    
    current_key, current_val = None, []
    for line in lines:
        if current_key is None:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'): continue
            if '=' in line:
                k, v = line.split('=', 1)
                k, v = k.strip(), v.strip()
                if v.startswith('"""'):
                    current_key = k
                    if v.endswith('"""') and len(v) >= 6:
                        config[k], current_key = v[3:-3], None
                    else:
                        current_val.append(v[3:])
                else:
                    config[k] = v
        else:
            if line.strip().endswith('"""'):
                current_val.append(line.replace('"""', ''))
                config[current_key], current_key, current_val = '\n'.join(current_val), None, []
            else:
                current_val.append(line)

    # --- 变量替换逻辑：处理配置文件中的 {CORE_HOSTS} 等占位符 ---
    for k in config:
        for target_k, target_v in config.items():
            placeholder = f"{{{target_k}}}"
            if placeholder in str(config[k]):
                config[k] = config[k].replace(placeholder, str(target_v))

    # 强制校验必要项
    required_keys = [
        "LOKI_URL", "OPENAI_URL", "OPENAI_API_KEY", "MODEL_NAME", 
        "TEMPERATURE", "TOP_P", "CORE_LOGQL", "ACCESS_LOGQL", 
        "AI_PROMPT_TEMPLATE", "REPORT_TITLE", "REPORT_TAG", "SEND_CHANNEL"
    ]
    missing = [key for key in required_keys if key not in config]
    if missing:
        print(f"错误: 配置文件缺少必要项: {', '.join(missing)}")
        sys.exit(1)
        
    return config, state_file

CONF, STATE_FILE = get_config()

# ================== [2. 核心业务逻辑区] ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("H3C_GlobalAIOps")

def get_send_handler():
    channel_name = CONF["SEND_CHANNEL"]
    module_path = f"send.{channel_name}"
    channel_module = importlib.import_module(module_path)
    if not hasattr(channel_module, "send_to"):
        raise AttributeError(f"模块 {module_path} 中未定义 send_to 函数")
    return getattr(channel_module, "send_to")

def get_last_run_time():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return datetime.fromisoformat(f.read().strip())
        except Exception as e:
            logger.error(f"读取状态文件失败: {e}")
    return None

def save_last_run_time(dt):
    try:
        with open(STATE_FILE, 'w') as f:
            f.write(dt.isoformat())
    except Exception as e:
        logger.error(f"保存状态文件失败: {e}")

def format_loki_timestamp(ns_timestamp):
    return datetime.fromtimestamp(int(ns_timestamp) / 1e9).strftime('%Y-%m-%d %H:%M:%S')

def query_loki(query, start_ns, end_ns):
    if not query: return []
    params = {"query": query, "start": start_ns, "end": end_ns, "limit": 5000, "direction": "forward"}
    try:
        response = requests.get(CONF["LOKI_URL"], params=params, timeout=60)
        response.raise_for_status()
        return response.json().get('data', {}).get('result', [])
    except Exception as e:
        logger.error(f"Loki 查询异常: {e}")
        return None  # 区分异常与空结果

def fetch_and_format_logs(start_dt, end_dt):
    start_ns, end_ns = int(start_dt.timestamp() * 1e9), int(end_dt.timestamp() * 1e9)
    time_range_str = f"{start_dt.strftime('%m-%d %H:%M')} 至 {end_dt.strftime('%m-%d %H:%M')}"
    
    host_data = {}
    # 分别查询并合并，只要有一个查询返回 None（报错），整体判定为异常
    res_core = query_loki(CONF["CORE_LOGQL"], start_ns, end_ns)
    res_access = query_loki(CONF["ACCESS_LOGQL"], start_ns, end_ns)
    
    if res_core is None or res_access is None:
        return None, time_range_str

    for item in res_core + res_access:
        host = item['stream'].get('host', 'Unknown')
        lines = [f"[{format_loki_timestamp(val[0])}] {val[1].strip()}" for val in item['values']]
        host_data.setdefault(host, []).extend(lines)
        
    return host_data, time_range_str

def analyze_global_with_llm(host_data):
    combined_context = ""
    for host, logs in host_data.items():
        # 日志采样防止 token 溢出
        sampled = logs[:30] + (["... [省略 {} 条] ...".format(len(logs)-60)] + logs[-30:] if len(logs) > 60 else [])
        combined_context += f"\n--- [设备: {host}] ---\n" + "\n".join(sampled) + "\n"
        logger.info(f"--- 载入设备上下文 ({host}) 共 {len(logs)} 条日志 ---")

    prompt = CONF["AI_PROMPT_TEMPLATE"].replace("{combined_context}", combined_context.strip())
    payload = {
        "model": CONF["MODEL_NAME"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(CONF["TEMPERATURE"]),
        "top_p": float(CONF["TOP_P"])
    }
    headers = {"Authorization": f"Bearer {CONF['OPENAI_API_KEY']}", "Content-Type": "application/json"}
    
    try:
        res = requests.post(CONF["OPENAI_URL"], json=payload, headers=headers, timeout=600)
        res.raise_for_status()
        res_json = res.json()
        if 'choices' in res_json and len(res_json['choices']) > 0:
            return True, res_json['choices'][0]['message']['content'].strip()
        return False, f"LLM 返回结构异常: {res_json}"
    except Exception as e:
        return False, f"LLM 故障: {str(e)}"

# ================== [3. 执行入口] ==================
def main():
    try:
        send_to = get_send_handler()
    except Exception as e:
        logger.error(f"发送通道初始化失败: {e}")
        return

    now = datetime.now()
    last_run_dt = get_last_run_time()
    
    # 时间窗口逻辑
    if not last_run_dt:
        start_dt = now - timedelta(hours=1)
        logger.info("未发现运行记录，默认拉取前 1 小时日志。")
    else:
        start_dt = last_run_dt
        if (now - start_dt) < timedelta(hours=1):
            start_dt = now - timedelta(hours=1)
            logger.info("运行间隔过短，强制拉取前 1 小时。")

    host_data, time_range = fetch_and_format_logs(start_dt, now)
    
    # 场景 1: Loki 查询失败
    if host_data is None:
        error_msg = f"⚠️ 巡检异常: Loki 接口响应失败，请检查网路或 Loki 服务状态。\n统计时段: {time_range}"
        logger.error(error_msg)
        send_to(f"❌ {CONF['REPORT_TITLE']}查询失败", error_msg, CONF["REPORT_TAG"])
        return

    # 场景 2: 无告警日志
    if not host_data:
        logger.info(f"时段 {time_range} 内无异常日志。")
        save_last_run_time(now)
        send_to(CONF["REPORT_TITLE"], f"✅ 时段 {time_range} 内网络运行平稳，无告警日志。", CONF["REPORT_TAG"])
        return

    # 场景 3: 发现告警，执行 AI 分析
    summary_lines = [f" - {host}: {len(logs)}条" for host, logs in host_data.items()]
    total_logs = sum(len(logs) for logs in host_data.values())
    
    logger.info("正在发送全局数据至 LLM...")
    success, ai_result = analyze_global_with_llm(host_data)
    display_content = ai_result if success else f"⚠️ AI 诊断不可用: {ai_result}"

    report_content = (
        f"统计时段: {time_range}\n告警总计: {total_logs} 条\n"
        f"设备明细:\n{chr(10).join(summary_lines)}\n"
        f"------------------\n{display_content}"
    )
    
    try:
        send_to(CONF["REPORT_TITLE"], report_content, CONF["REPORT_TAG"])
        save_last_run_time(now)
        logger.info("巡检完成，状态已更新。")
    except Exception as e:
        logger.error(f"推送过程发生异常: {e}")

if __name__ == "__main__":
    main()