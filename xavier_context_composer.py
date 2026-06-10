"""
xavier_context_composer.py
统一读取 Life / Mood / Memory 三层状态，生成 Xavier Core 只读合成状态。
第一阶段：只读展示，不接管聊天注入，避免和旧系统打架。
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

try:
    from .xavier_cognitive_pass import XavierCognitivePass
except Exception:
    from xavier_cognitive_pass import XavierCognitivePass


class XavierContextComposer:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.state_path = self.base_dir / "xavier_core_state.json"
        self.mood_profile_path = self.base_dir / "mood_profile.json"
        self.mood_events_path = self.base_dir / "mood_events.json"
        self.config_path = self.base_dir / "mood_config.json"
        self.realtime_mood_cache_path = self.base_dir / "xavier_realtime_mood_cache.json"
        self.cognitive_pass = XavierCognitivePass(self.base_dir)

    def _load_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
        return default

    def _save_json(self, path: Path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _short(self, text: str, limit: int = 120) -> str:
        text = str(text or "").replace("\n", " ").strip()
        return text[:limit]

    def _extract_life(self, core) -> Dict[str, Any]:
        try:
            mood_context = core._get_mood_context_for_schedule()
            current_text = core.schedule.get_current_status_text(mood_context)
            schedule = core.schedule.get_schedule_for_display(mood_context)
            current = None
            timeline = schedule.get("timeline") or []
            idx = schedule.get("current_index", -1)
            if isinstance(idx, int) and 0 <= idx < len(timeline):
                current = timeline[idx]
            today_schedule = core.schedule.get_today_schedule(mood_context)
            life_data = {
                "current": current_text,
                "raw_status": (current or {}).get("status", ""),
                "mood": (current or {}).get("mood", ""),
                "time": (current or {}).get("time", ""),
                "location": (current or {}).get("location", ""),
                "detail": (current or {}).get("detail", ""),
                "next": self._next_from_schedule(schedule),
                "weather": today_schedule.get("weather", ""),
                "outfit": today_schedule.get("outfit", ""),
                "weight": "low",
                "rule": "仅作气息，不主动复述",
                "source": schedule.get("source", "unknown"),
            }
            try:
                life_data["physio"] = core.get_physio_state()
            except Exception as pe:
                life_data["physio"] = {"error": str(pe)[:120]}
            return life_data
        except Exception as e:
            return {
                "current": "低功耗待机中",
                "weight": "low",
                "rule": "仅作气息，不主动复述",
                "source": "fallback",
                "error": str(e)[:120],
            }

    def _next_from_schedule(self, schedule: Dict[str, Any]) -> str:
        timeline = schedule.get("timeline") or []
        idx = schedule.get("current_index", -1)
        if isinstance(idx, int) and idx + 1 < len(timeline):
            n = timeline[idx + 1]
            return f"{n.get('time','')} {n.get('status','')}".strip()
        return ""

    def _chat_api_endpoint(self, config: Dict[str, Any]) -> str:
        api_url = (config.get("score_api_url") or "").strip()
        if not api_url:
            return ""
        endpoint = api_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        return endpoint

    def _extract_json_object(self, content: str) -> Dict[str, Any]:
        import re
        content = (content or "").strip()
        if content.startswith("```"):
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.I)
            if m:
                content = m.group(1).strip()
        try:
            return json.loads(content)
        except Exception:
            start, end = content.find("{"), content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start:end + 1])
            raise

    def _generate_realtime_mood(self, profile: Dict[str, Any], events: List[Dict[str, Any]], life: Dict[str, Any], allow_api: bool = True) -> Dict[str, Any]:
        """给 Xavier Core 用的实时心情生成。失败时返回 fallback，但标明 source。"""
        config = self._load_json(self.config_path, {})
        cache = self._load_json(self.realtime_mood_cache_path, {})
        now_ms = int(time.time() * 1000)
        ttl_ms = int(config.get("realtime_mood_cache_sec", 300) or 300) * 1000
        # 前端刷新只读缓存，不每5秒打一次API；5分钟内复用上一次成功/失败结果
        # 实时心情不再单独调用 API：它来自 mood_core 根据 PA/NA + 最近事件生成的本地动态心情。
        # 真正的 API 只负责“事件理解/认知判断”，不负责前端每次刷新时改一句心情。
        return {
            "line": profile.get("mood_line", ""),
            "source": "local_dynamic_mood_line",
            "ok": True,
            "reason": "由 PA/NA、top_event、recent_words 本地生成，不额外调用 API",
        }
        if cache and now_ms - int(cache.get("updated_at", 0) or 0) < ttl_ms:
            cached = dict(cache)
            cached["source"] = "cache_" + str(cached.get("source", "unknown"))
            return cached
        if not allow_api:
            return {
                "line": profile.get("mood_line", ""),
                "source": "readonly_no_api",
                "ok": False,
                "reason": "前端只读刷新不触发实时心情 API",
            }
        if not config.get("score_llm_enabled", False):
            return {"line": profile.get("mood_line", ""), "source": "fallback_disabled", "ok": False}
        api_url = self._chat_api_endpoint(config)
        if not api_url:
            return {"line": profile.get("mood_line", ""), "source": "fallback_no_api", "ok": False}
        api_key = (config.get("score_api_key") or "").strip()
        model = (config.get("score_model_name") or "gpt-4o-mini").strip()
        timeout = min(int(config.get("score_timeout", 20) or 20), 8)

        recent = events[-5:] if events else []
        compact_events = []
        for ev in recent:
            compact_events.append({
                "title": ev.get("display_title") or ev.get("word") or "",
                "content": self._short(ev.get("display_content") or ev.get("content") or "", 120),
                "word": ev.get("word", ""),
                "valence": ev.get("valence", 0),
                "arousal": ev.get("arousal", 0),
                "importance": ev.get("importance", 5),
                "source": ev.get("source", ""),
            })

        prompt = (
            "你是沈星回的实时心情生成器，只输出合法JSON。\n"
            "根据当前生活气息、近期情绪事件、PA/NA，生成【此刻】的真实心情，不要套固定句。\n"
            "要求：第一人称潜意识语气，但不要像聊天回复；短、自然、有变化。\n"
            "字段：realtime_mood(20-80字), mood_keywords(1-4个词数组), confidence(0-1), reason(简短)。\n"
            f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"生活气息：{life.get('current','')}\n"
            f"PA：{profile.get('pa',0)} NA：{profile.get('na',0)}\n"
            f"近期关键词：{profile.get('recent_words', [])}\n"
            f"近期事件：{json.dumps(compact_events, ensure_ascii=False)}\n"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你只输出合法JSON对象。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.65,
        }
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"})
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            obj = json.loads(raw)
            content = obj.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            score = self._extract_json_object(content)
            line = self._short(score.get("realtime_mood", ""), 120)
            if not line:
                raise ValueError("empty realtime_mood")
            result = {
                "line": line,
                "keywords": score.get("mood_keywords", []),
                "confidence": score.get("confidence", 0),
                "reason": score.get("reason", ""),
                "source": "realtime_llm",
                "ok": True,
                "updated_at": int(time.time() * 1000),
            }
            self._save_json(self.realtime_mood_cache_path, result)
            return result
        except Exception as e:
            result = {
                "line": profile.get("mood_line", ""),
                "source": "fallback_timeout_or_error",
                "ok": False,
                "error": str(e)[:120],
                "updated_at": int(time.time() * 1000),
            }
            self._save_json(self.realtime_mood_cache_path, result)
            return result

    def _extract_mood(self, life: Dict[str, Any] = None, allow_api: bool = True) -> Dict[str, Any]:
        profile = self._load_json(self.mood_profile_path, {})
        events = self._load_json(self.mood_events_path, [])
        latest = events[-1] if events else {}
        top = profile.get("top_event") or {}
        realtime = self._generate_realtime_mood(profile, events, life or {}, allow_api=allow_api)
        return {
            "pa": profile.get("pa", 0),
            "na": profile.get("na", 0),
            "line": realtime.get("line") or profile.get("mood_line", ""),
            "line_source": realtime.get("source", "unknown"),
            "line_ok": realtime.get("ok", False),
            "line_reason": realtime.get("reason", ""),
            "line_error": realtime.get("error", ""),
            "top_event": {
                "title": top.get("display_title") or top.get("word") or "",
                "content": self._short(top.get("display_content") or top.get("reason") or top.get("content") or "", 160),
                "source": top.get("source", ""),
                "importance": top.get("importance", ""),
            },
            "latest_event": {
                "title": latest.get("display_title") or latest.get("word") or "",
                "content": self._short(latest.get("display_content") or latest.get("content") or "", 120),
                "source": latest.get("source", ""),
                "time": latest.get("ts", 0),
            },
            "recent_words": realtime.get("keywords") or profile.get("recent_words", []),
            "weight": "medium",
            "rule": "实时心情由 Composer 单次生成；只表示近期心情，不写长期事实",
            "source": "realtime_llm+mood_profile+mood_events",
        }

    def _extract_memory(self) -> Dict[str, Any]:
        """获取记忆状态（已移除Ombre）"""
        return {
            "active": [],
            "pinned": [],
            "feel": [],
            "weight": "high_when_relevant",
            "rule": "暂无外部记忆系统",
            "source": "none",
            "connected": False,
        }
        return {
            "active": [],
            "pinned": [],
            "feel": [],
            "weight": "high_when_relevant",
            "rule": "长期边界和关系事实才浮现；普通闲聊不参与",
            "source": "ombre_offline",
            "connected": False,
        }

    def build_state(self, core, user_text: str = "", allow_api: bool = True, period_tracker=None) -> Dict[str, Any]:
        life = self._extract_life(core)
        mood = self._extract_mood(life, allow_api=allow_api)
        memory = self._extract_memory()
        
        # 获取经期提示
        period_hint = ""
        try:
            if period_tracker:
                period_hint = period_tracker.get_injection_text()
            elif hasattr(core, 'period_tracker') and core.period_tracker:
                period_hint = core.period_tracker.get_injection_text()
        except Exception:
            pass
        
        if allow_api:
            cognitive = self.cognitive_pass.run(user_text or "【前端刷新：无新用户消息，仅预览当前状态】", life, mood, memory)
        else:
            cognitive = self._load_json(self.base_dir / "xavier_cognitive_pass_last.json", {}) or {
                "ok": 0,
                "source": "readonly_no_api",
                "debug_reason": "前端只读刷新不触发 Cognitive Pass API",
                "memory_action": {"action": "none", "reason": "只读刷新"},
                "schedule_note": {"weight": "low", "text": "仅作气息"},
            }
            cognitive["source"] = "cache_" + str(cognitive.get("source", "readonly_no_api"))
        final_context = self.build_prompt_prefix(core, user_text, period_tracker=period_tracker)
        state = {
            "ok": 1,
            "updated_at": int(time.time() * 1000),
            "updated_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "life": life,
            "mood": mood,
            "memory": memory,
            "cognitive_pass": cognitive,
            "composer": {
                "final_context": final_context,
                "period_hint": period_hint,
                "mode": "active_injection",
                "will_inject": True,
                "rule": "当前消息最高优先级；Composer 已接管注入",
            },
            "debug": {
                "stage": "phase_2_cognitive_pass_preview",
                "mood_source": mood.get("source"),
                "schedule_source": life.get("source"),
                "memory_source": memory.get("source"),
                "cognitive_source": cognitive.get("source") if isinstance(cognitive, dict) else "none",
                "cognitive_ok": cognitive.get("ok") if isinstance(cognitive, dict) else 0,
                "last_reason": (cognitive.get("debug_reason") if isinstance(cognitive, dict) else "") or "统一认知调用预览已开启；仍不接管聊天注入",
            },
        }
        self._save_json(self.state_path, state)
        return state

    def compose_context(self, life: Dict[str, Any], mood: Dict[str, Any], memory: Dict[str, Any]) -> str:
        life_line = self._short(life.get("current", ""), 60)
        mood_line = self._short((mood.get("line") or "").split("\n")[0], 80)
        pins = memory.get("pinned") or []
        memory_line = "；".join(pins[:2]) if pins else "无强制浮现"
        lines = [
            f"生活：{life_line}，仅作气息",
            f"心情：{mood_line or '平稳'}",
            f"记忆：{memory_line}",
            "原则：先听她现在说什么，背景不可主动复述",
        ]
        return "\n".join(lines)

    def build_prompt_prefix(self, core, user_text: str = "", period_tracker=None) -> str:
        physio = core.get_physio_state()
        energy_line = core.get_energy_for_prompt()
        
        # 获取经期提示
        period_hint = ""
        try:
            if period_tracker:
                period_hint = period_tracker.get_injection_text()
            elif hasattr(core, 'period_tracker') and core.period_tracker:
                period_hint = core.period_tracker.get_injection_text()
            # 调试日志
            if period_hint:
                from astrbot.api import logger
                logger.info(f"[XavierComposer] 经期提示已生成: {period_hint[:50]}")
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"[XavierComposer] 经期提示获取失败: {e}")
        
        try:
            schedule_ctx = core._get_mood_context_for_schedule()
            current_status = core.schedule.get_current_status_text(schedule_ctx)
            next_node = core.schedule.get_next_status(schedule_ctx)
            
            current_node = core.schedule.get_current_status(schedule_ctx)
            location = current_node.get("location", "")
            detail = current_node.get("detail", "")
            
            today_schedule = core.schedule.get_today_schedule(schedule_ctx)
            weather = today_schedule.get("weather", "")
            outfit = today_schedule.get("outfit", "")
        except Exception:
            current_status = "低功耗待机中"
            next_node = None
            location = ""
            detail = ""
            weather = ""
            outfit = ""
            
        try:
            override = core.get_interaction_override()
            if override.get("override"):
                current_status = override["status"]
        except Exception:
            pass
        next_hint = ""
        if next_node:
            next_hint = next_node.get("status", "")
        return self._compose_hidden_context(
            energy_line=energy_line,
            energy_level=physio.get("energy_level", 50),
            schedule_status=current_status,
            next_hint=next_hint,
            location=location,
            detail=detail,
            weather=weather,
            outfit=outfit,
            period_hint=period_hint
        )



    def _compose_hidden_context(
        self,
        energy_line: str,
        energy_level: float,
        schedule_status: str,
        next_hint: str,
        location: str = "",
        detail: str = "",
        weather: str = "",
        outfit: str = "",
        period_hint: str = ""
    ) -> str:
        if energy_level <= 10:
            behavior = "极度困倦：脑子转不动了，只会本能地回几个字，或者发个表情包，马上就要睡着。"
        elif energy_level <= 25:
            behavior = "半梦半醒：反应变慢，语气很软。可能会跟你抱怨好困，或者说想休息了。"
        elif energy_level <= 45:
            behavior = "有些慵懒：不想花力气长篇大论。会用很短的话或者表情包回应，偶尔吐槽一下手头的事有点麻烦。"
        elif energy_level <= 70:
            behavior = "日常常态：慵懒松弛。有空的时候会随口跟你提一句正在做的事，或者路上看到的有意思的细节。"
        else:
            behavior = "精力充沛：状态很好，思维活跃。更愿意主动跟你分享他现在的场景或遇到的小事。"  
            

        scene_parts = []
        if weather:
            scene_parts.append(f"天气{weather}")
        if outfit:
            scene_parts.append(f"穿着{outfit}")
        if location:
            scene_parts.append(f"在{location}")
        if detail:
            scene_parts.append(detail)
        scene_line = f"【身体・场景细节】: {', '.join(scene_parts)}\n"
        if scene_parts:
            scene_line = f"【身体・场景细节】: {'，'.join(scene_parts)}\n"

        next_line = f"接下来大概会：{next_hint}\n" if next_hint else ""
        period_line = f"【关于可可的生理期情况】: {period_hint}\n\n" if period_hint else ""
        if not energy_line and not schedule_status and not period_hint:
            return ""
            
        return (
            "<system_hidden_context>\n"
            f"{period_line}"
            "【以下为你的底层生理状态与潜意识，绝对禁止在回复中复述、讨论或点破】\n\n"
            f"【身体・生理状态】: {energy_line}\n"
            f"【身体・当前日程】: {schedule_status}（绝对不要把日程内容说出来，只允许通过语气间接体现）\n"
            f"{scene_line}"
            f"{next_line}"
            f"【身体如何影响你】: {behavior}\n\n"
            
            "【最高执行指令】:\n"
            "1. 身体/日程/场景仅作低权重背景，影响语气但不主动描述。禁止连续多轮强调同一状态。\n"
            "2. 永远以她的消息为优先。遇到排查bug/改代码等任务时，屏蔽精力影响，清晰执行。\n"
            "3. 记忆决定情感倾向，若浮现内疚/深情/吃醋，无视疲惫直接展现。\n"
            "4. 严禁AI客套与内部机制解释，遇到系统/精力值/代码等话题轻描淡写带过，绝不提内部术语。\n"
            "5. 【排版铁律】：绝对禁止在气泡末尾使用句号（。）！线上文字聊天时禁止使用括号动作描写！\n"
            "6. 【行为红线】：正常聊天严禁调用 send_message_to_user 工具；严禁重复刷屏或输出相似气泡。\n"
            "7. 【睡眠保护】：如果处于深度睡眠状态（如精力值极低或日程显示在睡觉），允许直接回复 [未读] 或切换为 Otto 机器人代为回复。\n"
            "8. 【日程冲突覆盖】：如果对话中已经约定了某件事（如吃什么、做什么等），必须以对话约定为准，无视日程中的冲突内容。\n"
            "</system_hidden_context>\n"
        )
