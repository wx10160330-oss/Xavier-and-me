from pathlib import Path
from typing import Dict
import asyncio
import json
import threading
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 动态添加插件目录到 sys.path，确保能导入同级模块
_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from mood_core import XavierMoodCore
from xavier_context_composer import XavierContextComposer
from period_tracker import PeriodTracker


@register(
    "astrbot_plugin_xavier_mood_core",
    "Xavier",
    "沈星回情绪系统核心：幂律衰减、PA/NA软互抑、回复前心情快照注入",
    "0.1.0",
)
class XavierMoodCorePlugin(Star):
    def __init__(self, context: Context, config: Dict = None):
        super().__init__(context)
        self.config = config or {}
        self.base_dir = Path(__file__).parent
        self.core = XavierMoodCore(self.base_dir)
        self.composer = XavierContextComposer(self.base_dir)
        self.period = PeriodTracker(self.base_dir)
        self.core.update_runtime_config(self.config)
        self.plugin_cfg = self.core.config
        self.httpd = None
        self.http_thread = None
        if self.plugin_cfg.get("visualizer_enabled", True):
            self._start_visualizer()
        logger.info("XavierMoodCorePlugin v0.2.0 loaded")

    async def initialize(self):
        """初始化"""
        pass

    def _start_visualizer(self):
        host = str(self.plugin_cfg.get("visualizer_host", "127.0.0.1"))
        port = int(self.plugin_cfg.get("visualizer_port", 5002) or 5002)
        core = self.core
        composer = self.composer
        period = self.period
        base_dir = self.base_dir

        class MoodHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send(self, code, body, content_type="text/html; charset=utf-8"):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_POST(self):
                try:
                    if self.path.startswith("/api/mood/delete"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        ids = payload.get("ids", [])
                        deleted = core.delete_events(ids)
                        self._send(200, json.dumps({"ok": 1, "deleted": deleted}), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/core/run"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        user_text = payload.get("text", "")
                        data = composer.build_state(core, user_text=user_text, allow_api=True)
                        self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/period/add"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        start = payload.get("start", "")
                        days = int(payload.get("days", 5))
                        ok = period.add_record(start, days)
                        self._send(200, json.dumps({"ok": 1 if ok else 0}), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/period/start"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        start = payload.get("start", "")
                        ok = period.start_period(start if start else None)
                        self._send(200, json.dumps({"ok": 1 if ok else 0}), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/period/end"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        end = payload.get("end", "")
                        ok = period.end_period(end if end else None)
                        self._send(200, json.dumps({"ok": 1 if ok else 0}), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/period/delete"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        start = payload.get("start", "")
                        ok = period.delete_record(start)
                        self._send(200, json.dumps({"ok": 1 if ok else 0}), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/period/cycle"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        cycle = int(payload.get("cycle", 28))
                        ok = period.update_cycle(cycle)
                        self._send(200, json.dumps({"ok": 1 if ok else 0}), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/period/template"):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length).decode("utf-8") if length else "{}"
                        payload = json.loads(body)
                        template_type = payload.get("type", "")
                        text = payload.get("text", "")
                        ok = period.update_template(template_type, text)
                        self._send(200, json.dumps({"ok": 1 if ok else 0}), "application/json; charset=utf-8")
                        return
                        return
                    self._send(404, "Not Found")
                except Exception as e:
                    logger.error(f"[XavierMoodCore] POST handler error: {e}", exc_info=True)
                    self._send(500, f"error: {e}", "text/plain; charset=utf-8")

            def do_GET(self):
                try:
                    
                    if self.path.startswith("/api/period"):
                        data = period.export_for_frontend()
                        self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/core"):
                        data = composer.build_state(core, allow_api=False, period_tracker=period)
                        self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/schedule"):
                        data = core.schedule.get_schedule_for_display(core._get_mood_context_for_schedule())
                        self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
                        return
                    if self.path.startswith("/api/mood"):
                        data = core.export_visualizer_data()
                        self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
                        return
                    html_path = base_dir / "mood_visualizer.html"
                    if not html_path.exists():
                        self._send(404, "mood_visualizer.html not found")
                        return
                    self._send(200, html_path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.error(f"[XavierMoodCore] GET handler error: {e}", exc_info=True)
                    self._send(500, f"mood visualizer error: {e}", "text/plain; charset=utf-8")

        try:
            self.httpd = ThreadingHTTPServer((host, port), MoodHandler)
            self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.http_thread.start()
            logger.info(f"[XavierMoodCore] visualizer started at http://{host}:{port}")
        except Exception as e:
            logger.warning(f"[XavierMoodCore] visualizer start failed: {e}")

    async def terminate(self):
        try:
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception as e:
            logger.debug(f"[XavierMoodCore] terminate error: {e}")

    def _is_target_user(self, event: AstrMessageEvent) -> bool:
        target = str(self.plugin_cfg.get("target_user_id", "{YOUR_USER_ID}"))
        try:
            sender = str(event.get_sender_id())
            return sender == target
        except Exception as e:
            logger.debug(f"[XavierMoodCore] _is_target_user check failed: {e}")
            return True

    def _is_command_or_system(self, text: str) -> bool:
        """过滤掉不需要情绪记录的消息：指令、系统标记、纯表情、超短召唤"""
        s = (text or "").strip()
        if not s:
            return True
        
        # 常见无意义指令词
        ignore_words = ["sid", "new", "del", "switch"]
        if any(s.lower().startswith(w) for w in ignore_words):
            return True
        
        # 本插件的指令关键词
        plugin_commands = ["心情", "心情详情", "记录情绪"]
        if any(cmd in s for cmd in plugin_commands):
            return True
            
        # 指令前缀
        if s.startswith("/") or s.startswith("!") or s.startswith("#"):
            return True
        # 系统注入的标记
        system_tags = ["[当下心情状态]", "<system_reminder>", "<system_hidden_context>", "[MID:", "[MSG_ID:"]
        if any(tag in s for tag in system_tags):
            return True
        # 纯表情或超短召唤（≤3字且不含字母数字）
        if len(s) <= 3 and not any(c.isalnum() for c in s):
            return True
        return False


    async def _async_score_task(self, text: str):
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.core.score_with_llm, text, "")
        except Exception as e:
            logger.debug(f"[XavierMoodCore] 后台打分任务失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-20)
    async def inject_mood_before_llm(self, event: AstrMessageEvent):
        """回复前：把心情快照作为隐性上下文注入本轮消息。"""
        try:
            if not self._is_target_user(event):
                return
            text = event.message_str or ""
            
            is_cmd_or_sys = self._is_command_or_system(text)
            

            if is_cmd_or_sys:
                return

            # 记录聊天事件（精力计算用）
            self.core.record_chat_event()
            
            # 尝试记录关怀恢复事件
            try:
                self.core.record_care_event(text)
            except Exception as e:
                logger.debug(f"[XavierMoodCore] care event recording failed: {e}")
            
            # 后台异步情绪打分
            if self.plugin_cfg.get("record_user_message", True):
                asyncio.create_task(self._async_score_task(text))

            # 生成并注入心情上下文
            if not self.plugin_cfg.get("inject_mood_to_message", True):
                return

            prefix = self.composer.build_prompt_prefix(self.core, user_text=text, period_tracker=self.period)
            if not prefix:
                logger.debug("[XavierMoodCore] empty prefix, skip injection")
                return

            new_text = f"{prefix}\n用户消息：{text}"
            event.message_str = new_text
            try:
                if hasattr(event, "message_obj") and hasattr(event.message_obj, "message_str"):
                    event.message_obj.message_str = new_text
            except Exception as e:
                logger.debug(f"[XavierMoodCore] message_obj update failed: {e}")
            event.set_extra("_xavier_mood_injected", True)
        except Exception as e:
            logger.error(f"[XavierMoodCore] inject_mood_before_llm failed: {e}", exc_info=True)

    @filter.command("心情")
    async def xavier_mood(self, event: AstrMessageEvent):
        """查看当前心情快照。"""
        try:
            snapshot = self.core.build_snapshot()
            line = snapshot.get("mood_line", "低功耗模式")
            yield event.plain_result(line)
        except Exception as e:
            logger.error(f"[XavierMoodCore] xavier_mood command failed: {e}", exc_info=True)
            yield event.plain_result("心情查询失败")

    @filter.command("心情详情")
    async def xavier_mood_debug(self, event: AstrMessageEvent):
        """查看内部调试信息。"""
        try:
            data = self.core.export_visualizer_data()
            snapshot = data.get("snapshot", {})
            top = snapshot.get("top_event") or {}
            text = (
                f"PA: {snapshot.get('pa', 0):.2f}\n"
                f"NA: {snapshot.get('na', 0):.2f}\n"
                f"今日状态：{data.get('decoration', '--')}\n"
                f"近期关键词：{'、'.join(snapshot.get('recent_words', [])[:5]) or '--'}\n"
                f"顶部事件：{top.get('content', '--')[:80]}\n"
                f"前端：http://127.0.0.1:{self.plugin_cfg.get('visualizer_port', 5002)}"
            )
            yield event.plain_result(text)
        except Exception as e:
            logger.error(f"[XavierMoodCore] xavier_mood_debug command failed: {e}", exc_info=True)
            yield event.plain_result("调试信息获取失败")

    @filter.command("记录情绪")
    async def xavier_mood_seed(self, event: AstrMessageEvent):
        """手动把本条内容写成一次情绪事件：/xavier_mood_seed 内容"""
        try:
            text = (event.message_str or "").replace("/xavier_mood_seed", "", 1).strip()
            if not text:
                yield event.plain_result("给我一句要记录的内容")
                return
            ev = self.core.add_event_from_text(text, source="manual")
            if ev:
                yield event.plain_result(f"记下了：{ev.get('word', '情绪波动')}")
            else:
                yield event.plain_result("这句话情绪波动不大，先不写进池子")
        except Exception as e:
            logger.error(f"[XavierMoodCore] xavier_mood_seed command failed: {e}", exc_info=True)
            yield event.plain_result("记录失败")

