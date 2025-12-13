#!/usr/bin/env python3
"""
飞书日历 SDK 封装层 (基于官方 lark-oapi)
"""
import os
import json
import datetime
import lark_oapi as lark
from lark_oapi.api.calendar.v4 import *

class CalendarSDK:
    def __init__(self):
        self.app_id = os.environ.get("FEISHU_APP_ID")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET")
        self.user_open_id = os.environ.get("FEISHU_USER_ID")
        
        # 状态文件：记录机器人创建的日历 ID，避免重复创建
        self.config_file = "data/bot_calendar.json"
        self.calendar_id = None

        # 初始化官方 Client
        self.client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

    def _ensure_bot_calendar(self):
        """确保存在一个专门的 LifeOps 日历"""
        # 1. 读缓存
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    self.calendar_id = data.get('calendar_id')
                    return self.calendar_id
            except: pass

        # 2. 如果没缓存，尝试创建新的
        print("Creating new LifeOps Calendar...")
        request = CreateCalendarRequest.builder() \
            .request_body(Calendar.builder()
                .summary("🤖 LifeOps AI 日历")
                .description("由 Claude 管理的自动化日程")
                .color(-1)
                .permissions("private")
                .build()) \
            .build()

        resp = self.client.calendar.v4.calendar.create(request)
        if resp.success():
            self.calendar_id = resp.data.calendar.calendar_id
            # 保存 ID
            with open(self.config_file, 'w') as f:
                json.dump({'calendar_id': self.calendar_id}, f)
            return self.calendar_id
        else:
            print(f"❌ Create Calendar Failed: {resp.msg}")
            return None

    def create_event(self, task):
        """创建日程并拉人"""
        cal_id = self._ensure_bot_calendar()
        if not cal_id: return None

        # 时间转换
        try:
            dt_start = datetime.datetime.fromisoformat(task["start_time"])
            if "end_time" in task and task["end_time"]:
                dt_end = datetime.datetime.fromisoformat(task["end_time"])
            else:
                dt_end = dt_start + datetime.timedelta(hours=1)
            
            ts_start = str(int(dt_start.timestamp()))
            ts_end = str(int(dt_end.timestamp()))
        except: return None

        desc = f"{task.get('description', '')}\n\n[LifeOps Managed]"
        if "priority" in task: desc = f"优先级: {task['priority']}\n" + desc

        # 构造请求：创建日程
        # 注意：SDK 支持在创建时直接添加 attendee，但为了稳健，我们也可以复用之前的逻辑
        # 这里演示 SDK 的原生写法
        event_body = CalendarEvent.builder() \
            .summary(task["task"]) \
            .description(desc) \
            .start_time(TimeInfo.builder().timestamp(ts_start).timezone("Asia/Shanghai").build()) \
            .end_time(TimeInfo.builder().timestamp(ts_end).timezone("Asia/Shanghai").build()) \
            .need_notification(True) \
            .build()

        req = CreateCalendarEventRequest.builder() \
            .calendar_id(cal_id) \
            .request_body(event_body) \
            .build()

        resp = self.client.calendar.v4.calendar_event.create(req)
        if not resp.success():
            print(f"❌ Create Event Failed: {resp.msg}")
            return None
        
        event_id = resp.data.event.event_id
        print(f"✅ Event Created: {event_id}")

        # 邀请你 (User)
        if self.user_open_id:
            self._add_attendee(cal_id, event_id, self.user_open_id)
            
        return event_id

    def _add_attendee(self, cal_id, event_id, user_id):
        # 构造参与人
        attendee = CalendarEventAttendee.builder() \
            .type("user") \
            .user_id(user_id) \
            .build()

        req = CreateCalendarEventAttendeeRequest.builder() \
            .calendar_id(cal_id) \
            .event_id(event_id) \
            .user_id_type("open_id") \
            .request_body(CreateCalendarEventAttendeeRequestBody.builder()
                .attendees([attendee])
                .build()) \
            .build()
            
        resp = self.client.calendar.v4.calendar_event_attendee.create(req)
        if resp.success():
            print(f"✅ Invited User: {user_id}")
        else:
            print(f"⚠️ Invite Failed: {resp.msg}")

    def delete_event(self, event_id):
        if not self.calendar_id: self._ensure_bot_calendar()
        req = DeleteCalendarEventRequest.builder() \
            .calendar_id(self.calendar_id) \
            .event_id(event_id) \
            .build()
        self.client.calendar.v4.calendar_event.delete(req)
        print(f"🗑️ Deleted: {event_id}")

    def list_events(self, start_ts, end_ts):
        if not self.calendar_id: self._ensure_bot_calendar()
        
        # 使用 iterator 进行自动翻页
        req = ListCalendarEventRequest.builder() \
            .calendar_id(self.calendar_id) \
            .start_time(str(start_ts)) \
            .end_time(str(end_ts)) \
            .page_size(100) \
            .build()
            
        # 修正：使用 lark.iter 遍历所有页
        events = []
        try:
            for item in lark.iter.calendar.v4.calendar_event.list(self.client, req):
                events.append({
                    "event_id": item.event_id,
                    "summary": item.summary,
                    "description": item.description,
                    "start_time": int(item.start_time.timestamp), # 确保转为int
                    "end_time": int(item.end_time.timestamp)
                })
        except Exception as e:
            print(f"⚠️ Error listing events: {e}")
            
        return events
