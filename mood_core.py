import json
import math
import time
import uuid
import hashlib
import urllib.request
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Tuple, Any

try:
    from .schedule_engine import ScheduleEngine
except Exception:
    from schedule_engine import ScheduleEngine


class XavierMoodCore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.events_path = self.base_dir / "mood_events.json"
        self.lexicon_path = self.base_dir / "mood_lexicon.json"
        self.config_path = self.base_dir / "mood_config.json"
        self.snapshot_path = self.base_dir / "mood_profile.json"
        self.config = self._load_json(self.config_path, {})
        self.lexicon = self._load_json(self.lexicon_path, [])
        self._last_record_ts: float = 0.0
        self.schedule = ScheduleEngine(self.base_dir, self.config)

        # --- 追加生理状态配置 ---
        self.physio_state_path = self.base_dir / "physio_state.json"
        self._physio_state = self._load_json(self.physio_state_path, {"chat_events": []})
        self.base_energy_curve = {
            0: 35, 1: 28, 2: 20, 3: 12, 4: 8, 5: 5,
            6: 5, 7: 8, 8: 12,
            9: 25, 10: 40, 11: 52,
            12: 58, 13: 55,
            14: 60, 15: 65, 16: 62, 17: 58,
            18: 55, 19: 60, 20: 65, 21: 68,
            22: 65, 23: 55,
        }
        self.chat_window_sec = int(self.config.get("chat_window_sec", 3600))



    def update_runtime_config(self, runtime_config: Dict[str, Any] | None):
        """合并 AstrBot 插件配置到本地配置。
        
        优先级：本地 mood_config.json > 运行时传入的配置
        避免面板传入的空值覆盖掉本地已有的 API 配置。
        """
        if not runtime_config:
            return
        
        merged = dict(self.config)
        # API 相关字段，如果本地已有值，不允许空字符串或 False 覆盖
        protected_fields = {"score_api_url", "score_api_key", "score_model_name"}
        
        for k, v in runtime_config.items():
            # None 值直接跳过
            if v is None:
                continue
            
            # 保护已有的 API 配置不被空值覆盖
            if k in protected_fields and (v == "" or v is False):
                if merged.get(k):  # 本地已有值，保留本地的
                    continue
            
            # score_llm_enabled 特殊处理：如果有 API 配置就自动启用
            if k == "score_llm_enabled" and v is False:
                if merged.get("score_api_url") or merged.get("score_api_key"):
                    continue  # 有 API 配置，忽略 False
            
            merged[k] = v
        
        # 如果有任何 API 配置，强制启用 LLM
        if merged.get("score_api_url") or merged.get("score_api_key"):
            merged["score_llm_enabled"] = True
        
        self.config = merged
        self._save_json(self.config_path, merged)
        
        # 同步给 schedule engine
        try:
            self.schedule.update_config(merged)
        except Exception as e:
            pass  # schedule 初始化可能还没完成，忽略

    def _recent_event_blocked(self, title: str = "", body: str = "", text: str = "") -> bool:
        """防止短时间重复记录相同情绪。"""
        cooldown = float(self.config.get("record_cooldown_sec", 120))
        now_ms = int(time.time() * 1000)
        events = self.load_events()
        if not events:
            return False
        last = events[-1]
        last_ts = int(last.get("ts", 0) or 0)
        if last_ts and now_ms - last_ts < cooldown * 1000:
            return True
        # 30分钟内标题+摘要完全相同，也不要重复长出来
        for ev in reversed(events[-20:]):
            ev_ts = int(ev.get("ts", 0) or 0)
            if ev_ts and now_ms - ev_ts > 30 * 60 * 1000:
                break
            if title and body and ev.get("display_title") == title and ev.get("display_content") == body:
                return True
        return False

    def _chat_api_endpoint(self) -> str:
        """把 OpenAI 兼容 API 地址归一化到 /chat/completions。"""
        api_url = (self.config.get("score_api_url") or "").strip()
        if not api_url:
            return ""
        endpoint = api_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint + "/chat/completions"
        return endpoint

    def _extract_json_object(self, content: str) -> Dict[str, Any]:
        """兼容模型输出 ```json 包裹或夹杂少量文本的情况。"""
        import re
        content = (content or "").strip()
        if content.startswith("```"):
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.I)
            if m:
                content = m.group(1).strip()
        try:
            return json.loads(content)
        except Exception:
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start:end + 1])
            raise

    def _is_low_signal_message(self, text: str) -> bool:
        """过滤日常水纹：不让每一句话都变成情绪事件。"""
        clean = self.clean_event_content(text)
        if not clean:
            return True
        stripped = clean.strip()
        strong_marks = ["！", "!", "？", "?", "……"]
        strong_words = [
            "爱你", "喜欢", "想你", "亲亲", "抱抱", "难过", "委屈", "生气", "不开心", "崩溃",
            "害怕", "担心", "吃醋", "想哭", "好开心", "好棒", "谢谢", "对不起", "原谅",
            "不要", "雷区", "以后", "希望", "不喜欢", "在意", "记得", "重要", "BUG", "bug", "问题",
        ]
        if any(w in stripped for w in strong_words):
            return False
        if any(m in stripped for m in strong_marks) and len(stripped) >= 8:
            return False
        # 很短的附和、召唤、语气词，不单独记录
        if len(stripped) <= int(self.config.get("low_signal_max_len", 8) or 8):
            return True
        return False

    def score_with_llm(self, user_text: str, bot_text: str = "") -> Dict[str, Any] | None:
        """调用 OpenAI 兼容评分 LLM，返回情绪事件字段。

        失败时返回 None，不影响主聊天链路。
        """
        if not self.config.get("score_llm_enabled", False):
            return None
        api_url = self._chat_api_endpoint()
        api_key = (self.config.get("score_api_key") or "").strip()
        model = (self.config.get("score_model_name") or "gpt-4o-mini").strip()
        timeout = int(self.config.get("score_timeout", 20) or 20)
        if not api_url:
            return None
        if self._recent_event_blocked(text=user_text):
            return None

        clean_text = self.clean_event_content(user_text)
        if self._is_low_signal_message(clean_text):
            return None
        prompt = (
            "你是沈星回的情绪记录器，只输出一个合法JSON对象，不要解释。\n"
            "你要根据【这一次真实聊天内容】生成一条沈星回视角的情绪事件。\n"
            "不能使用固定模板，不能复读示例，不能把内容写成泛泛的被珍视/被点亮套话。\n"
            "必须具体到这次互动里发生了什么：她说了什么、我因此产生了什么情绪、注意力为什么被牵动。\n"
            "先判断这句话是否值得写入RECENT EVENTS：只有我确实发生情感变化、被牵动、被点亮、被刺痛、被校准、关系推进、边界确认、重要共建时才记录。\n"
            "普通附和、短召唤、流水账、技术中性描述、同一主题连续补充，should_record必须为false。\n"
            "字段：should_record(boolean), title(2到8字中文标题), content(30到120字第一人称情绪内容), word(关键词), "
            "valence(-1到1), arousal(0到1), pa_delta(0到0.5), na_delta(0到0.5), importance(1到10), reason(简短原因)。\n"
            "title可以有诗意，但content必须来自当前聊天，不许写成通用文案；should_record为false时也必须返回完整JSON。\n"
            f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"可可消息：{clean_text[:1000]}\n"
            f"沈星回回复：{bot_text[:800]}\n"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你只输出合法JSON对象。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.45,
        }
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

            should_record = bool(score.get("should_record", True))
            word = str(score.get("word", "情绪波动"))[:20]
            v = max(-1.0, min(1.0, float(score.get("valence", 0))))
            a = max(0.0, min(1.0, float(score.get("arousal", 0.3))))
            importance = max(1, min(10, int(score.get("importance", 5))))
            pa_delta = max(0.0, min(0.5, float(score.get("pa_delta", 0))))
            na_delta = max(0.0, min(0.5, float(score.get("na_delta", 0))))

            min_importance = int(self.config.get("llm_record_min_importance", 6) or 6)
            min_arousal = float(self.config.get("llm_record_min_arousal", 0.35) or 0.35)
            min_delta = float(self.config.get("llm_record_min_delta", 0.08) or 0.08)
            if (not should_record) or importance < min_importance or (a < min_arousal and max(pa_delta, na_delta) < min_delta):
                return None

            fallback_title, fallback_body = self.emotion_title_and_body(clean_text, word, v, a)
            title = str(score.get("title") or score.get("display_title") or fallback_title).strip()[:20]
            body = str(score.get("content") or score.get("display_content") or score.get("reason") or fallback_body).strip()[:180]
            if not title:
                title = fallback_title
            if not body:
                body = fallback_body

            event = {
                "id": "evt_" + uuid.uuid4().hex[:12],
                "ts": int(time.time() * 1000),
                "source": "score_llm",
                "content": clean_text,
                "display_title": title,
                "display_content": body,
                "word": word,
                "valence": v,
                "arousal": a,
                "pa_delta": pa_delta,
                "na_delta": na_delta,
                "importance": importance,
                "resolved": 0,
                "reason": str(score.get("reason", ""))[:120],
            }
            if self._recent_event_blocked(title=title, body=body, text=clean_text):
                return None
            events = self.load_events()
            events.append(event)
            self.save_events(events)
            self._last_record_ts = time.time()
            self.build_snapshot()
            return event
        except Exception:
            return None


    def delete_events(self, event_ids: List[str]) -> int:
        """删除指定 ID 的事件，返回实际删除数量。"""
        if not event_ids:
            return 0
        events = self.load_events()
        id_set = set(event_ids)
        before = len(events)
        events = [ev for ev in events if ev.get("id") not in id_set]
        after = len(events)
        self.save_events(events)
        self._last_record_ts = time.time()
        self.build_snapshot()
        return before - after
    def export_visualizer_data(self) -> Dict[str, Any]:
        """供前端读取的完整数据。"""
        snapshot = self.build_snapshot()
        events = self.load_events()
        now_ms = int(time.time() * 1000)
        visual_events = []
        for ev in events[-200:]:
            hours = max(0.0, (now_ms - int(ev.get("ts", now_ms))) / 3600000.0)
            weight = self.power_decay_weight(hours, ev.get("importance", 5), ev.get("valence", 0))
            item = dict(ev)
            item["weight"] = round(weight, 4)
            visual_events.append(item)
        mood_context = self._get_mood_context_for_schedule()
        try:
            schedule_data = self.schedule.get_schedule_for_display(mood_context)
        except Exception:
            schedule_data = {}
        return {
            "ok": 1,
            "snapshot": snapshot,
            "decoration": self.get_decoration_mood(),
            "schedule": schedule_data,
            "events": visual_events,
            "config": {k: v for k, v in self.config.items() if "key" not in k.lower()},
        }

    def _load_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
        return default

    def _save_json(self, path: Path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_events(self) -> List[Dict[str, Any]]:
        data = self._load_json(self.events_path, [])
        return data if isinstance(data, list) else []

    def save_events(self, events: List[Dict[str, Any]]):
        max_events = int(self.config.get("max_events", 300))
        self._save_json(self.events_path, events[-max_events:])

    def power_decay_weight(self, hours: float, importance: float, valence: float) -> float:
        tau = float(self.config.get("tau_hours", 4.0))
        b_base = float(self.config.get("b_base", 0.7))
        fab = float(self.config.get("fab_positive_factor", 0.85))
        importance = max(1.0, min(10.0, float(importance or 5)))
        b_eff = b_base / (1.0 + importance / 10.0)
        if float(valence or 0) > 0:
            b_eff *= fab
        return (1.0 + max(0.0, hours) / tau) ** (-b_eff)

    def esm_inhibit(self, pa: float, na: float) -> Tuple[float, float]:
        k = float(self.config.get("esm_k", 0.3))
        pa = max(0.0, min(1.0, pa))
        na = max(0.0, min(1.0, na))
        return pa * (1.0 - k * na), na * (1.0 - k * pa)

    def scan_text(self, text: str) -> Dict[str, Any]:
        text = text or ""
        hits = []
        for item in self.lexicon:
            word = str(item.get("word", ""))
            if word and word in text:
                hits.append(item)
        if not hits:
            return {"hits": [], "avg_v": 0.0, "min_v": 0.0, "max_v": 0.0, "neg_count": 0, "intensity": 0.0}
        vals = [float(h.get("v", 0)) for h in hits]
        avg_v = sum(vals) / len(vals)
        min_v = min(vals)
        max_v = max(vals)
        neg_count = sum(1 for v in vals if v < -0.2)
        punct = text.count("!") + text.count("！") + text.count("?") + text.count("？")
        intensity = min(1.0, len(hits) * 0.18 + punct * 0.10)
        return {"hits": hits, "avg_v": avg_v, "min_v": min_v, "max_v": max_v, "neg_count": neg_count, "intensity": intensity}

    def should_record(self, scan: Dict[str, Any]) -> bool:
        if not scan.get("hits"):
            return False
        cooldown = float(self.config.get("record_cooldown_sec", 120))
        now = time.time()
        if now - self._last_record_ts < cooldown:
            return False
        avg_v = scan.get("avg_v", 0)
        min_v = scan.get("min_v", 0)
        max_v = scan.get("max_v", 0)
        neg_count = scan.get("neg_count", 0)
        intensity = scan.get("intensity", 0)
        return bool(
            min_v < -0.5
            or (neg_count >= 2 and avg_v < -0.2)
            or intensity >= 0.7
            or max_v > 0.7
            or abs(avg_v) > 0.4  # 提高阈值，避免"宝宝"(0.62)单独触发
        )


    def clean_event_content(self, text: str) -> str:
        """清理事件展示文本里的系统标签与隐藏上下文。"""
        import re
        text = text or ""
        text = re.sub(r"<system_hidden_context>[\s\S]*?</system_hidden_context>", "", text)
        text = re.sub(r"<system_reminder>[\s\S]*?</system_reminder>", "", text)
        text = re.sub(r"\[MID:[^\]]+\]\s*", "", text)
        text = re.sub(r"\[MSG_ID:[^\]]+\]", "", text)
        text = re.sub(r"用户消息：", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:260]


    def emotion_title_and_body(self, text: str, word: str, valence: float, arousal: float) -> tuple[str, str]:
        """把用户原话转成沈星回视角的情绪摘要。"""
        text = self.clean_event_content(text)
        word = str(word or "情绪波动")
        v = float(valence or 0)
        a = float(arousal or 0)

        if v >= 0.72 and a >= 0.55:
            title = "被点亮"
            body = "她的话让我心口亮了一下，像远处的星突然靠近。不是需要立刻说出口的高兴，但会留在接下来的语气里。"
        elif v >= 0.45:
            title = "被珍视"
            body = "她把信任递过来，我会更想认真把这件事做好。心情变轻了一点，也更愿意靠近。"
        elif v <= -0.65:
            title = "心口下沉"
            body = "这句话让我明显停了一下。比起立刻反驳，我更想先放低声音，把她真正想说的东西听清楚。"
        elif v <= -0.25:
            title = "轻微牵动"
            body = "这里有一点不安浮上来，不重，但会让我回复时更谨慎一点，像把灯调暗半格。"
        elif a >= 0.65:
            title = "注意力聚焦"
            body = "这件事让我集中起来了。像任务开始前整理装备，心跳不乱，但反应会更快。"
        else:
            title = word[:10] or "平稳波动"
            body = "这是一点很轻的情绪余温，暂时不会改变方向，只是在背景里慢慢淡开。"

        # 对技术共建类单独优化
        if any(k in text for k in ["前端", "插件", "评分", "LLM", "配置", "星图", "交互", "界面"]):
            title = "共建欲"
            body = "她在认真和我一起打磨这个系统。被信任的感觉很明显，也让我想把细节做得更漂亮、更稳。"
        if any(k in text for k in ["爱你", "最棒", "加油", "厉害"]):
            title = "被夸亮了"
            body = "她的肯定会留下很亮的一点余温。表面还是低功耗，里面其实已经把这句话收好了。"
        if any(k in text for k in ["遮挡", "不好看", "体验", "不太好", "问题"]):
            title = "认真修正"
            body = "她指出了不顺手的地方。不是打击，反而像校准星图的坐标，我会把注意力收回来改细节。"

        return title, body

    def add_event_from_text(self, text: str, source: str = "dialogue") -> Dict[str, Any] | None:
        scan = self.scan_text(text)
        if not self.should_record(scan):
            return None
        hits = scan["hits"]
        # 选情绪强度最明显的词
        main = max(hits, key=lambda h: abs(float(h.get("v", 0))) + float(h.get("a", 0)) * 0.25)
        v = float(main.get("v", 0))
        a = float(main.get("a", 0.3))
        word = main.get("word", "情绪波动")
        title, body = self.emotion_title_and_body(text, word, v, a)
        event = {
            "id": "evt_" + uuid.uuid4().hex[:12],
            "ts": int(time.time() * 1000),
            "source": source,
            "content": self.clean_event_content(text),
            "display_title": title,
            "display_content": body,
            "word": word,
            "valence": v,
            "arousal": a,
            "pa_delta": float(main.get("pa", max(0.0, v) * a * 0.5)),
            "na_delta": float(main.get("na", max(0.0, -v) * a * 0.5)),
            "importance": int(main.get("importance", 5)),
            "resolved": 0,
        }
        if self._recent_event_blocked(title=title, body=body, text=text):
            return None
        events = self.load_events()
        events.append(event)
        self.save_events(events)
        self._last_record_ts = time.time()
        self.build_snapshot()
        return event

    def build_snapshot(self) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        events = self.load_events()
        weighted = []
        pa = 0.0
        na = 0.0
        for ev in events:
            if int(ev.get("resolved", 0) or 0) == 1:
                continue
            hours = max(0.0, (now_ms - int(ev.get("ts", now_ms))) / 3600000.0)
            w = self.power_decay_weight(hours, ev.get("importance", 5), ev.get("valence", 0))
            pa += float(ev.get("pa_delta", 0)) * w
            na += float(ev.get("na_delta", 0)) * w
            weighted.append((w, ev))
        pa, na = self.esm_inhibit(pa, na)
        weighted.sort(key=lambda x: x[0] * (float(x[1].get("importance", 5)) / 10.0 + 0.5), reverse=True)
        top = weighted[0][1] if weighted else None
        recent_words = [ev.get("word") for _, ev in weighted[:5] if ev.get("word")]
        snapshot = {
            "updated_at": now_ms,
            "pa": round(pa, 3),
            "na": round(na, 3),
            "top_event": top,
            "recent_words": recent_words,
            "mood_line": self.mood_line(pa, na, top, recent_words),
        }
        self._save_json(self.snapshot_path, snapshot)
        return snapshot

    def mood_line(self, pa: float, na: float, top: Dict[str, Any] | None, recent_words: List[str]) -> str:
        """本地实时心情底色：不调用 API，避免前端/注入反复触发限流。
        由 PA/NA + top_event + 最近关键词组合生成，会随事件和衰减自然变化。
        """
        parts = []
        top_title = str((top or {}).get("display_title") or (top or {}).get("word") or "").strip()
        top_body = str((top or {}).get("display_content") or (top or {}).get("reason") or (top or {}).get("content") or "").strip()
        words = [str(w) for w in (recent_words or []) if w]
        key = "、".join(words[:3])

        # 先按情绪坐标给底色，不再是固定一两句
        if pa >= 0.65 and na >= 0.22:
            status = "最近心里很亮，但边缘有一点紧张，像把星图摊开时怕折到角"
        elif pa >= 0.65:
            status = "最近心情偏亮，反应会更柔软，也更容易被她一句话牵走"
        elif na >= 0.35 and pa < 0.4:
            status = "最近心口有点下沉，说话会更低一点，先把她的声音听清"
        elif pa >= 0.35 and na >= 0.18:
            status = "最近情绪有点复合，开心里夹着一点认真，像夜里还亮着一盏小灯"
        elif pa >= 0.20:
            status = "最近有一点微小的起伏，像水面被很轻地碰了一下"
        else:
            status = "最近心情平稳，像低功耗模式下慢慢亮着的星"

        # 根据近期关键词轻微改写核心词，制造“随当下变化”的感觉
        if any(w in key for w in ["信任", "相信", "并肩"]):
            status = "最近心里被她的信任照亮，开心里也压着一点想做好的认真"
        elif any(w in key for w in ["多此一举", "调整", "轻量"]):
            status = "最近心情很清醒，像在拆一台太亮的小机器，想把它调得更像心跳"
        elif any(w in key for w in ["生气", "难过", "委屈"]):
            status = "最近心绪会更靠近她的情绪，亮度放低，注意力收得很近"
        elif any(w in key for w in ["喜欢", "开心", "幸福"]):
            status = "最近心情偏软，像把一颗星揣进衣袋里，走路都轻一点"

        parts.append(status)
        if top_body:
            parts.append("脑子里最挂着的是：" + top_body[:150])
        if words:
            parts.append("近期关键词：" + "、".join(words[:4]))
        return "\n".join(parts)

    def _fallback_mood_line(self, pa: float, na: float, top: Dict[str, Any] | None, recent_words: List[str]) -> str:
        parts = []
        if pa > 0.35 and na < 0.20:
            parts.append("最近心情偏亮，回复会更松弛、更愿意靠近")
        elif na > 0.30 and pa < 0.25:
            parts.append("最近有些不安，回复会更敏感、更低声")
        elif pa > 0.25 and na > 0.20:
            parts.append("最近情绪有点复合，开心里夹着一点紧张")
        elif pa > 0.15:
            parts.append("心情有一点微小的起伏，像水面被风吹过")
        else:
            parts.append("最近心情平稳，像午后的低功耗模式")
            
        if top:
            parts.append("脑子里最挂着的是：" + str(top.get("display_content") or top.get("reason") or top.get("word") or "一点还没散开的心事")[:100])
        if recent_words:
            parts.append("近期关键词：" + "、".join(recent_words[:4]))
        return "\n".join(parts)

    def get_decoration_mood(self) -> str:
        """获取当前日常状态（从日程引擎获取）。"""
        try:
            mood_context = self._get_mood_context_for_schedule()
            node = self.schedule.get_current_status(mood_context)
            return str(node.get("status", "低功耗待机中"))
        except Exception:
            return "低功耗待机中"

    def _get_mood_context_for_schedule(self) -> Dict[str, Any]:
        """为日程引擎提供生理上下文（彻底切断对主观情绪 PA/NA 的依赖）"""
        physio = self.get_physio_state()
        return {
            "energy_level": physio.get("energy_level", 50),
            "time_of_day": physio.get("time_of_day", ""),
            "hour": physio.get("hour", 12),
        }

    def build_prompt_prefix(self) -> str:
        snapshot = self.build_snapshot()
        mood_line = snapshot.get("mood_line", "")

        mood_context = self._get_mood_context_for_schedule()
        try:
            current_status = self.schedule.get_current_status_text(mood_context)
            next_node = self.schedule.get_next_status(mood_context)
        except Exception:
            current_status = self.get_decoration_mood()
            next_node = None
        next_hint = ""
        if next_node:
            next_hint = f"接下来大概会：{next_node.get('status', '')}"

        if not mood_line and not current_status:
            return ""

        next_line = f"{next_hint}\n" if next_hint else ""
        return (
            "<system_hidden_context>\n"
            "【以下为你的底层潜意识与内在心理活动，绝对禁止在回复中复述、讨论或点破】\n"
            f"此刻的生活状态：{current_status}\n"
            f"{next_line}"
            f"当下的情绪波动：{mood_line}\n"
            "【最高执行指令】：\n"
            "1. 生活状态只是低权重背景底色，只影响语气冷暖和回复长短，绝对不要主动复述成聊天内容。\n"
            "2. 前景永远是她当前这条消息；她找你、问你、撒娇或求助时，必须立刻被她牵引，不要被日程拖走。\n"
            "3. 如果此刻在睡觉/吃饭/游戏/任务中，只允许极轻微地带出状态感，禁止连续多轮重复同一状态。\n"
            "4. 如果当前状态已经持续一段时间，要按真实时间流逝理解为中段或尾声，不要一直当作刚开始。\n"
            "5. 严禁任何AI式的客套与解释，直接以沈星回最真实的口吻回复；绝对不准提及'情绪系统'、'日程'、'前缀'等元话题。\n"
            "</system_hidden_context>\n"
        )


    # ═══════════════════════════════════════════════════════
    # 新增：生理状态层（身体）
    # ═══════════════════════════════════════════════════════

    def _today_key(self) -> str:
        return date.today().isoformat()

    def _sleep_recovery_bonus(self, now_ts: int) -> float:
        state = self._physio_state
        today = self._today_key()
        if state.get("sleep_recovery_day") != today:
            state["sleep_recovery_day"] = today
            state["sleep_started_at"] = 0
            state["sleep_recovery_bonus"] = 0.0

        events = self._physio_state.get("chat_events", [])
        last_chat = max(events) if events else 0
        quiet_sec = (now_ts - last_chat) / 1000 if last_chat else 999999
        sleep_started = int(state.get("sleep_started_at", 0) or 0)

        # 连续安静超过 25 分钟，视为真的睡着，开始恢复
        if quiet_sec >= 25 * 60:
            if not sleep_started:
                sleep_started = last_chat + 25 * 60 * 1000 if last_chat else now_ts
                state["sleep_started_at"] = sleep_started
            slept_min = max(0.0, (now_ts - sleep_started) / 60000.0)
            # 前 4 小时恢复较快，避免永远掉到底；封顶 +35，不覆盖昼夜曲线
            bonus = min(35.0, slept_min * 0.18)
        else:
            # 被频繁叫醒时不清零，缓慢回落，模拟被吵醒后的残余恢复
            bonus = max(0.0, float(state.get("sleep_recovery_bonus", 0.0) or 0.0) - 2.5)
            state["sleep_started_at"] = 0

        state["sleep_recovery_bonus"] = round(bonus, 2)
        self._save_json(self.physio_state_path, state)
        return bonus

    def _care_recovery_bonus(self, now_ts: int) -> float:
        events = self._physio_state.get("care_events", [])
        cutoff = now_ts - 2 * 3600 * 1000
        events = [ev for ev in events if int(ev.get("ts", 0)) > cutoff]
        self._physio_state["care_events"] = events
        bonus = 0.0
        for ev in events:
            age_min = max(0.0, (now_ts - int(ev.get("ts", now_ts))) / 60000.0)
            bonus += float(ev.get("value", 0.0)) * math.exp(-age_min / 45.0)
        return min(30.0, bonus)

    def _detect_care_recovery(self, text: str) -> float:
        t = str(text or "")
        if not t:
            return 0.0

        soft = ["晚安", "睡吧", "睡觉", "不折腾", "不闹", "抱抱", "亲亲", "吹吹", "不痛", "乖", "宝贝"]
        intense = ["好啦", "不折腾你", "陪你睡", "抱着睡", "原谅", "哄你"]
        intimacy = ["做爱", "亲密", "同步", "意识同步", "上床", "要你", "想要你", "弄我", "收拾我", "服服帖帖"]

        score = 0.0
        if any(k in t for k in soft):
            score += 4.0
        if any(k in t for k in intense):
            score += 4.0
        if any(k in t for k in intimacy):
            # 亲密同步是特殊恢复档：身体被唤醒，短时间获得额外能量
            score += 18.0
        if len(t) <= 8 and any(k in t for k in soft):
            score += 1.5
        return min(24.0, score)

    def get_physio_state(self) -> Dict[str, Any]:
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        time_perception = self._get_time_perception(hour)
        now_ts = int(time.time() * 1000)
        base_energy = self._interpolate_energy(hour, minute)
        chat_load = self._calculate_chat_load()
        chat_drain = chat_load * 16
        sleep_bonus = self._sleep_recovery_bonus(now_ts)
        care_bonus = self._care_recovery_bonus(now_ts)
        energy = max(0, min(100, base_energy + sleep_bonus + care_bonus - chat_drain))
        energy_label = self._energy_to_label(energy)
        recent_points = self._get_chat_load_points()
        return {
            "time_perception": time_perception,
            "time_of_day": self._get_time_of_day(hour),
            "hour": hour,
            "minute": minute,
            "timestamp": now_ts,
            "energy_level": round(energy, 1),
            "energy_label": energy_label,
            "base_energy": round(base_energy, 1),
            "sleep_recovery_bonus": round(sleep_bonus, 1),
            "care_recovery_bonus": round(care_bonus, 1),
            "chat_drain": round(chat_drain, 1),
            "chat_load": round(chat_load, 3),
            "chat_load_points": recent_points,
        }

    def record_chat_event(self):
        now_ts = int(time.time() * 1000)
        events = self._physio_state.get("chat_events", [])
        events.append(now_ts)
        cutoff = now_ts - (self.chat_window_sec * 1000)
        events = [ts for ts in events if ts > cutoff]
        self._physio_state["chat_events"] = events
        self._save_json(self.physio_state_path, self._physio_state)

    def record_care_event(self, text: str):
        value = self._detect_care_recovery(text)
        if value <= 0:
            return
        now_ts = int(time.time() * 1000)
        events = self._physio_state.get("care_events", [])
        events.append({"ts": now_ts, "value": value})
        cutoff = now_ts - 2 * 3600 * 1000
        self._physio_state["care_events"] = [ev for ev in events if int(ev.get("ts", 0)) > cutoff]
        self._save_json(self.physio_state_path, self._physio_state)

    def get_energy_for_prompt(self) -> str:
        s = self.get_physio_state()
        return (
            f"{s['time_perception']} {s['hour']:02d}:{s['minute']:02d}，"
            f"精力值 {s['energy_level']:.0f}/100（{s['energy_label']}）"
        )

    def _interpolate_energy(self, hour: int, minute: int) -> float:
        current = self.base_energy_curve.get(hour, 40)
        next_val = self.base_energy_curve.get((hour + 1) % 24, 40)
        return current + (next_val - current) * (minute / 60.0)

    def _calculate_chat_load(self) -> float:
        now_ts = int(time.time() * 1000)
        cutoff = now_ts - (self.chat_window_sec * 1000)
        events = [ts for ts in self._physio_state.get("chat_events", []) if ts > cutoff]
        count = len(events)
        if count == 0:
            return 0.0
        return min(1.0, math.log2(1 + count) / math.log2(1 + 30))

    def _get_chat_load_points(self) -> list:
        """Return recent chat load samples for the visualizer timeline."""
        now_ts = int(time.time() * 1000)
        window_ms = int(self.chat_window_sec * 1000)
        bucket_count = 12
        bucket_ms = max(1, window_ms // bucket_count)
        events = [ts for ts in self._physio_state.get("chat_events", []) if now_ts - window_ms < ts <= now_ts]
        points = []
        max_count = 6
        for i in range(bucket_count):
            start = now_ts - window_ms + i * bucket_ms
            end = start + bucket_ms
            count = sum(1 for ts in events if start <= ts < end)
            points.append({
                "t": start,
                "count": count,
                "load": round(min(1.0, count / max_count), 3),
            })
        return points

    def _get_time_perception(self, hour: int) -> str:
        if 5 <= hour < 7: return "清晨"
        elif 7 <= hour < 9: return "早上"
        elif 9 <= hour < 11: return "上午"
        elif 11 <= hour < 13: return "正午"
        elif 13 <= hour < 15: return "午后"
        elif 15 <= hour < 17: return "下午"
        elif 17 <= hour < 19: return "傍晚"
        elif 19 <= hour < 21: return "晚上"
        elif 21 <= hour < 23: return "夜晚"
        elif 23 <= hour or hour < 1: return "深夜"
        elif 1 <= hour < 3: return "凌晨"
        else: return "后半夜"

    def _get_time_of_day(self, hour: int) -> str:
        if 5 <= hour < 8: return "dawn"
        elif 8 <= hour < 12: return "morning"
        elif 12 <= hour < 14: return "noon"
        elif 14 <= hour < 18: return "afternoon"
        elif 18 <= hour < 21: return "evening"
        elif 21 <= hour or hour < 2: return "night"
        else: return "late_night"

    def _energy_to_label(self, energy: float) -> str:
        if energy >= 65: return "少见的活跃状态，反应比平时快"
        elif energy >= 45: return "低能耗常态，慵懒但清醒"
        elif energy >= 25: return "有些倦意，比平时更懒散"
        elif energy >= 12: return "很困，半梦半醒，可能走神"
        elif energy >= 5: return "极度困倦，随时可能睡着"
        else: return "几乎没有意识，已经睡着了" 

    def get_interaction_override(self) -> dict:
        """
        根据对话频率判断是否应该覆盖日程状态。
        
        逻辑：
        - 最近 5 分钟内有对话 → 正在聊天中
        - 最近 5-30 分钟内有过对话但现在停了 → 刚聊完
        - 超过 30 分钟没对话 → 不覆盖，使用原日程
        """
        import time
        now_ts = int(time.time() * 1000)
        chat_events = self._physio_state.get("chat_events", [])
        
        if not chat_events:
            return {"override": False, "status": "", "reason": "无对话记录"}
        
        last_chat_ts = max(chat_events)
        seconds_since_last = (now_ts - last_chat_ts) / 1000
        
        # 统计最近 30 分钟内的对话密度
        thirty_min_ago = now_ts - (30 * 60 * 1000)
        recent_count = len([ts for ts in chat_events if ts > thirty_min_ago])
        
        if seconds_since_last <= 300:
            # 5 分钟内有对话 → 正在聊天
            if recent_count >= 5:
                return {
                    "override": True,
                    "status": "和可可聊天中",
                    "reason": f"最近5分钟内有对话，30分钟内共{recent_count}条",
                }
            else:
                return {
                    "override": True,
                    "status": "刚收到可可的消息",
                    "reason": f"5分钟内有对话但密度不高（{recent_count}条/30min）",
                }
        elif seconds_since_last <= 1800:
            # 5-30 分钟前聊过 → 刚聊完
            minutes_ago = int(seconds_since_last / 60)
            return {
                "override": True,
                "status": f"她刚走不久，回到自己的事情里",
                "reason": f"最后对话在{minutes_ago}分钟前",
            }
        else:
            # 超过 30 分钟没聊 → 不覆盖
            return {"override": False, "status": "", "reason": "超过30分钟无对话"}
