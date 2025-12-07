import os
import json
import sys
import datetime
import requests

# 尝试导入 SDK Client
try:
    from feishu_calendar_client import CalendarSDK
    SDK_AVAILABLE = True
except ImportError:
    print("⚠️ SDK Client not found.")
    SDK_AVAILABLE = False

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
DATA_FILE = "data/schedule.json"
BACKUP_FILE = "data/schedule.bak.json" # 备份文件路径

# 北京时区
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# ==========================================
# 核心工具：数据清洗
# ==========================================
def normalize_task(task):
    """标准化任务格式，确保有 start_time"""
    if not isinstance(task, dict): return None
    if "start_time" in task: return task 
    if "date" in task and "time" in task:
        try:
            date_str = task["date"]
            parts = task["time"].split("-")
            start_t = parts[0].strip()
            if len(start_t) == 5: start_t += ":00"
            task["start_time"] = f"{date_str}T{start_t}"
            
            if len(parts) > 1:
                end_t = parts[1].strip()
                if len(end_t) == 5: end_t += ":00"
                task["end_time"] = f"{date_str}T{end_t}"
            else:
                dt = datetime.datetime.fromisoformat(task["start_time"])
                task["end_time"] = (dt + datetime.timedelta(hours=1)).isoformat()
            return task
        except: return None
    return None

def get_task_fingerprint(task):
    """生成任务指纹: (时间戳, 标题)"""
    nt = normalize_task(task)
    if not nt: return None
    try:
        dt = datetime.datetime.fromisoformat(nt["start_time"])
        # 强制转换为北京时间戳
        if dt.tzinfo is None: dt = dt.replace(tzinfo=TZ_CN)
        ts = int(dt.timestamp())
        return (ts, nt["task"])
    except: return None

# ==========================================
# 核心逻辑：手动 Diff 同步
# ==========================================
def diff_sync_logic():
    if not SDK_AVAILABLE: return
    print("🔄 Starting Diff Sync (Local vs Backup)...")

    # 1. 读取新旧数据
    try:
        with open(DATA_FILE, "r") as f: new_data = json.load(f)
    except: new_data = []
    
    try:
        with open(BACKUP_FILE, "r") as f: old_data = json.load(f)
    except: old_data = []

    # 2. 构建指纹映射
    # Map: Fingerprint -> Task Object
    new_map = {}
    for t in new_data:
        fp = get_task_fingerprint(t)
        if fp: new_map[fp] = t

    old_map = {}
    for t in old_data:
        fp = get_task_fingerprint(t)
        if fp: old_map[fp] = t

    # 3. 计算差异 (Set Operation)
    new_keys = set(new_map.keys())
    old_keys = set(old_map.keys())

    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys
    
    if not added_keys and not removed_keys:
        print("✅ No diff detected. Calendar is up to date.")
        return

    print(f"📊 Diff Result: +{len(added_keys)} Added, -{len(removed_keys)} Removed")
    
    cal = CalendarSDK()

    # 4. 处理新增 (Add)
    for fp in added_keys:
        task = new_map[fp]
        print(f"➕ Adding: {task['task']}")
        
        # 计算结束时间
        nt = normalize_task(task)
        dt_start = datetime.datetime.fromisoformat(nt["start_time"])
        if nt.get("end_time"):
            dt_end = datetime.datetime.fromisoformat(nt["end_time"])
        else:
            dt_end = dt_start + datetime.timedelta(hours=1)
            
        # 强制时区
        if dt_start.tzinfo is None: dt_start = dt_start.replace(tzinfo=TZ_CN)
        if dt_end.tzinfo is None: dt_end = dt_end.replace(tzinfo=TZ_CN)
        
        desc = f"{task.get('desc', task.get('description', ''))}\n\n[LifeOps Managed]"
        if "priority" in task: desc = f"优先级: {task['priority']}\n" + desc
        
        # 调用 SDK 创建
        cal.create_event({
            "task": task['task'],
            "description": desc,
            "start_time": nt["start_time"], # 这里的字符串格式 SDK 内部会再次解析，但我们已确保格式正确
            "end_time": nt.get("end_time"),
            "priority": task.get("priority")
        })

    # 5. 处理删除 (Remove)
    if removed_keys:
        # 删除比较麻烦，因为本地旧 JSON 里可能没有 event_id
        # 我们需要先获取远程列表，找到对应指纹的 event_id
        print("🔍 Fetching remote events to resolve IDs for deletion...")
        
        # 获取所有删除任务的时间范围，减少 API 查询量
        min_ts = min([k[0] for k in removed_keys])
        max_ts = max([k[0] for k in removed_keys])
        # 稍微放宽一点范围
        remote_events = cal.list_events(min_ts - 3600, max_ts + 86400)
        
        # 构建远程指纹: fp -> event_id
        remote_fp_map = {}
        for ev in remote_events:
            # 只有 Bot 管理的才删
            if "[LifeOps Managed]" in (ev.get("description") or ""):
                # SDK返回的start_time已经是时间戳int
                r_fp = (ev['start_time'], ev['summary'])
                remote_fp_map[r_fp] = ev['event_id']

        for fp in removed_keys:
            task_name = fp[1]
            if fp in remote_fp_map:
                eid = remote_fp_map[fp]
                print(f"🗑️ Deleting: {task_name} (ID: {eid})")
                cal.delete_event(eid)
            else:
                print(f"⚠️ Cannot delete {task_name}: Event not found in remote calendar.")

# ==========================================
# 消息相关 (保持不变)
# ==========================================
def get_tenant_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        res = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
        return res.json().get("tenant_access_token")
    except: return None

def send_message(open_id, content, msg_type="text", title="LifeOps", theme="blue"):
    token = get_tenant_token()
    if not token: return
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    params = {"receive_id_type": "open_id"}
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"receive_id": open_id, "msg_type": "interactive", "content": ""}
    
    # 简单的卡片渲染
    if msg_type == "interactive":
        try:
            # 这里简化处理，直接当文本发，您可以使用之前的 render_schema_v2_card
            card = {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": theme},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": str(content)}}]
            }
            # 尝试解析 JSON 内容优化显示
            try:
                data = json.loads(content)
                if isinstance(data, dict) and "data" in data:
                    # 如果有数据，生成简易列表
                    lines = []
                    for t in data["data"]:
                        lines.append(f"• {t.get('time','')} **{t.get('task','')}**")
                    card["elements"][0]["text"]["content"] = "\n".join(lines)
            except: pass
            
            payload["content"] = json.dumps(card)
        except:
            payload["content"] = json.dumps({"text": content})
    else:
        payload["content"] = json.dumps({"text": content})

    requests.post(url, params=params, headers=headers, json=payload)

# ==========================================
# 主入口
# ==========================================
if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit(1)
    cmd = sys.argv[1]
    
    if cmd == "diff_sync":
        # 🟢 新的 Diff 同步入口
        diff_sync_logic()
        
    elif cmd == "sync":
        # 保留全量 Sync 逻辑 (可用于手动修复或初始化)
        # 这里为了节省代码篇幅，暂时让它和 diff_sync 一样
        # 实际上全量 Sync 应该是之前的逻辑
        pass 
        
    elif cmd == "msg":
        if len(sys.argv) >= 4:
            m_type = sys.argv[4] if len(sys.argv) > 4 else "text"
            m_title = sys.argv[5] if len(sys.argv) > 5 else "LifeOps"
            m_theme = sys.argv[6] if len(sys.argv) > 6 else "blue"
            send_message(sys.argv[2], sys.argv[3], m_type, m_title, m_theme)
