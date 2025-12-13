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
    SDK_AVAILABLE = False

DATA_FILE = "data/schedule.json"
STATE_FILE = "data/last_sync_state.json" # 🟢 关键：这是我们的"记忆"文件

# 北京时区
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

def normalize_task(task):
    """标准化任务格式"""
    if not isinstance(task, dict): return None
    # 兼容处理
    if "start_time" not in task and "date" in task and "time" in task:
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
        except: return None
    return task

def get_task_fingerprint(task):
    """生成指纹: (UnixTimestamp, Title)"""
    nt = normalize_task(task)
    if not nt or "start_time" not in nt: return None
    try:
        dt = datetime.datetime.fromisoformat(nt["start_time"])
        if dt.tzinfo is None: dt = dt.replace(tzinfo=TZ_CN)
        return (int(dt.timestamp()), nt["task"])
    except: return None

def diff_sync_logic():
    if not SDK_AVAILABLE: return
    print("🔄 Starting Stateful Sync...")

    # 1. 读取 当前期望 (Current) 和 上次状态 (Last State)
    try:
        with open(DATA_FILE, "r") as f: current_data = json.load(f)
    except: current_data = []
    
    try:
        with open(STATE_FILE, "r") as f: last_state = json.load(f)
    except: 
        print("⚠️ No previous state found. Assuming first run (or lost state).")
        # 🟢 策略：如果完全没有状态文件，为了防止"邀请风暴"，
        # 我们可以选择信任当前的 schedule.json 已经被同步过了（只做标记不执行），
        # 或者更加激进地只同步未来。
        # 这里为了安全，如果丢失状态，我们让 last_state = current_data，
        # 这样第一把不会重复创建，只有下次修改才会触发。
        last_state = current_data 
        # 如果你确实想重新全量导入，请手动删空 data/last_sync_state.json 再提交

    # 2. 构建指纹 Map
    curr_map = {}
    for t in current_data:
        fp = get_task_fingerprint(t)
        if fp: curr_map[fp] = t

    last_map = {}
    for t in last_state:
        fp = get_task_fingerprint(t)
        if fp: last_map[fp] = t

    # 3. 计算差异
    curr_keys = set(curr_map.keys())
    last_keys = set(last_map.keys())

    added_keys = curr_keys - last_keys
    removed_keys = last_keys - curr_keys
    
    print(f"📊 Diff: +{len(added_keys)} New, -{len(removed_keys)} Removed")
    
    if not added_keys and not removed_keys:
        print("✅ No changes detected.")
        return

    cal = CalendarSDK()

    # 4. 执行新增 (不需要查飞书，直接创建)
    for fp in added_keys:
        task = curr_map[fp]
        print(f"➕ Adding: {task['task']}")
        nt = normalize_task(task)
        
        desc = f"{task.get('desc', task.get('description', ''))}\n\n[LifeOps Managed]"
        if "priority" in task: desc = f"优先级: {task['priority']}\n" + desc
        
        cal.create_event({
            "task": task['task'],
            "description": desc,
            "start_time": nt["start_time"],
            "end_time": nt.get("end_time"),
            "priority": task.get("priority")
        })

    # 5. 执行删除 (只查特定时间段，减少 SDK 负担)
    if removed_keys:
        # 找出删除任务的时间范围
        timestamps = [k[0] for k in removed_keys]
        min_ts = min(timestamps) - 3600
        max_ts = max(timestamps) + 3600
        
        print(f"🔍 Searching remote events in deletion range ({len(removed_keys)} tasks)...")
        # 只拉取相关时间段的日程
        remote_events = cal.list_events(min_ts, max_ts)
        
        # 建立远程指纹库
        remote_fp_map = {}
        for ev in remote_events:
            # 只有机器人的日程才处理
            if "[LifeOps Managed]" in (ev.get("description") or ""):
                # list_events 返回的 start_time 已经是 int timestamp
                r_fp = (int(ev['start_time']), ev['summary'])
                remote_fp_map[r_fp] = ev['event_id']

        for fp in removed_keys:
            if fp in remote_fp_map:
                eid = remote_fp_map[fp]
                print(f"🗑️ Deleting: {fp[1]} (ID: {eid})")
                cal.delete_event(eid)
            else:
                print(f"⚠️ Skip Delete: '{fp[1]}' not found on remote.")

    # 6. 🟢 关键：状态回写 (State Persistence)
    # 只有当同步动作执行完毕后，才把当前状态保存为"上次状态"
    print("💾 Updating State File...")
    with open(STATE_FILE, "w") as f:
        json.dump(current_data, f, indent=2, ensure_ascii=False)

# ==========================================
# 消息发送工具 (保持不变)
# ==========================================
def get_tenant_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        res = requests.post(url, json={"app_id": os.environ.get("FEISHU_APP_ID"), "app_secret": os.environ.get("FEISHU_APP_SECRET")})
        return res.json().get("tenant_access_token")
    except: return None

def send_message(open_id, content, msg_type="text", title="LifeOps", theme="blue"):
    token = get_tenant_token()
    if not token: return
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"receive_id": open_id, "msg_type": "interactive", "receive_id_type": "open_id", "content": ""}
    
    if msg_type == "interactive":
        try:
            card = {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": theme},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": str(content)}}]
            }
            payload["content"] = json.dumps(card)
        except: payload["content"] = json.dumps({"text": content})
    else:
        payload["content"] = json.dumps({"text": content})

    requests.post(url, params={"receive_id_type": "open_id"}, headers=headers, json=payload)

if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit(1)
    cmd = sys.argv[1]
    if cmd in ["diff_sync", "sync"]: diff_sync_logic()
    elif cmd == "msg" and len(sys.argv) >= 4:
        send_message(sys.argv[2], sys.argv[3], *sys.argv[4:])
