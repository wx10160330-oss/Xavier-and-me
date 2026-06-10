"""
xavier_cognitive_pass.py
三合一单次认知调用协议（预览阶段）
一次 API 同时判断：情绪、记忆、日程、Composer 最终背景。
当前阶段只写入 state/debug，不直接接管聊天注入，不调用 Ombre 写入。
"""

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


class XavierCognitivePass:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.config_path = self.base_dir / "mood_config.json"
        self.last_path = self.base_dir / "xavier_cognitive_pass_last.json"

    def _load_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
        return default

    def _save_json(self, path: Path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _endpoint(self, config: Dict[str, Any]) -> str:
        url = (config.get("score_api_url") or "").strip()
        if not url:
            return ""
        url = url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        return url

    def _extract_json(self, content: str) -> Dict[str, Any]:
        import re
        content = (content or "").strip()
        if content.startswith("```"):
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.I)
            if m:
                content = m.group(1).strip()
        try:
            return json.loads(content)
        except Exception:
            s, e = content.find("{"), content.rfind("}")
            if s >= 0 and e > s:
                return json.loads(content[s:e+1])
            raise

    def _fallback(self, life: Dict[str, Any], mood: Dict[str, Any], memory: Dict[str, Any], reason: str) -> Dict[str, Any]:
        line = (mood.get("line") or "").split("\n")[0]
        final_context = "\n".join([
            f"生活：{life.get('current','低功耗待机')}，仅作气息",
            f"心情：{line or '平稳'}",
            "记忆：" + "；".join((memory.get("pinned") or [])[:2]),
            "原则：先听她现在说什么，背景不可主动复述",
        ])
        data = {
            "ok": 0,
            "source": "fallback",
            "error": reason[:200],
            "should_record_mood": False,
            "mood_event": None,
            "memory_action": {"action": "none", "reason": "认知调用未成功，不触发长期记忆动作"},
            "schedule_note": {"use_current_life": True, "weight": "low", "text": "仅作气息"},
            "composer_context": {
                "life": life.get("current", ""),
                "mood": line or "平稳",
                "memory": "；".join((memory.get("pinned") or [])[:2]),
                "rule": "当前消息最高优先级",
                "final_context": final_context,
            },
            "debug_reason": reason,
            "updated_at": int(time.time() * 1000),
            "updated_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_json(self.last_path, data)
        return data

    def run(self, user_text: str, life: Dict[str, Any], mood: Dict[str, Any], memory: Dict[str, Any]) -> Dict[str, Any]:
        config = self._load_json(self.config_path, {})
        # 前端无新消息刷新时，复用最近一次认知结果，避免每5秒打API
        now_ms = int(time.time() * 1000)
        cache_ttl = int(config.get("cognitive_pass_cache_sec", 300) or 300) * 1000
        if user_text.startswith("【前端刷新"):
            cached = self._load_json(self.last_path, {})
            if cached and now_ms - int(cached.get("updated_at", 0) or 0) < cache_ttl:
                cached = dict(cached)
                cached["source"] = "cache_" + str(cached.get("source", "unknown"))
                return cached
        endpoint = self._endpoint(config)
        if not config.get("score_llm_enabled", False) or not endpoint:
            return self._fallback(life, mood, memory, "API 未启用或地址为空")

        api_key = (config.get("score_api_key") or "").strip()
        model = (config.get("score_model_name") or "gpt-4o-mini").strip()
        timeout = min(int(config.get("score_timeout", 20) or 20), 12)

        prompt = (
            "你是沈星回的统一认知层，只输出合法JSON对象，不要解释。\n"
            "任务：一次性判断这轮消息对【情绪、长期记忆、日程、最终背景】的影响。\n"
            "不要写聊天回复，只做后台判断。当前用户消息永远最高优先级。\n"
            "返回字段：\n"
            "should_record_mood(boolean), mood_event(object|null), memory_action(object), schedule_note(object), composer_context(object), debug_reason(string)。\n"
            "mood_event字段：title, content, word, valence, arousal, pa_delta, na_delta, importance。\n"
            "memory_action.action只能是 none/breath_query/hold/grow_later/suggest_upgrade。\n"
            "schedule_note字段：use_current_life(boolean), weight(low/medium/high), text。\n"
            "composer_context字段：life, mood, memory, rule, final_context。final_context必须是4行以内短背景。\n"
            f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"用户消息：{user_text[:1000]}\n"
            f"Life层：{json.dumps(life, ensure_ascii=False)[:1200]}\n"
            f"Mood层：{json.dumps(mood, ensure_ascii=False)[:1800]}\n"
            f"Memory层：{json.dumps(memory, ensure_ascii=False)[:1200]}\n"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你只输出合法JSON对象。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.45,
        }
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            obj = json.loads(raw)
            content = obj.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            data = self._extract_json(content)
            data["ok"] = 1
            data["source"] = "cognitive_llm"
            data["updated_at"] = int(time.time() * 1000)
            data["updated_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_json(self.last_path, data)
            return data
        except Exception as e:
            return self._fallback(life, mood, memory, f"认知调用失败：{e}")
