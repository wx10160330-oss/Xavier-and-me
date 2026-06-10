"""
schedule_engine.py (完整版)
沈星回生活日程引擎 —— 每日生成完整时间线，逐时推进
"""

import json
import hashlib
import random
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional


class ScheduleEngine:
    """沈星回的日程生成与管理引擎"""

    def __init__(self, base_dir: Path, config: Dict[str, Any] = None):
        self.base_dir = Path(base_dir)
        self.cache_dir = self.base_dir / "schedule_cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.templates_path = self.base_dir / "schedule_templates.json"
        self.config = config or {}
        self.templates = self._load_json(self.templates_path, [])

    def update_config(self, config: Dict[str, Any]):
        """更新运行时配置"""
        if config:
            self.config.update(config)

    # ─────────────────────────────────────────────
    # 公开接口
    # ─────────────────────────────────────────────

    def get_today_schedule(self, mood_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """获取今天的完整日程（有缓存则读缓存，无则生成）
        
        历史锁原则：
        - 今天已经过去的节点一旦写入缓存，就不再被整日重生成覆盖
        - 需要补字段/升级格式时，只修当前及未来节点，过去节点保持原样
        - 这样前端刷新、注入预览刷新都不会让“已经发生过的生活”反复变动
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        cache_path = self.cache_dir / f"schedule_{today_str}.json"

        cached = self._load_json(cache_path, None)
        if cached and cached.get("date") == today_str and cached.get("timeline"):
            repaired = self._repair_schedule_if_needed(cached, today_str, mood_context)
            if repaired != cached:
                self._save_json(cache_path, repaired)
            return repaired

        schedule = self._generate_schedule(today_str, mood_context)
        self._save_json(cache_path, schedule)
        self._cleanup_old_cache(keep_days=7)
        return schedule

    def get_current_status(self, mood_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """获取当前时间对应的状态节点"""
        schedule = self.get_today_schedule(mood_context)
        timeline = schedule.get("timeline", [])
        if not timeline:
            return {"status": "低功耗待机中", "mood": "平静", "time": "--:--"}

        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute

        current_slot = timeline[0]
        for node in timeline:
            node_minutes = self._time_to_minutes(node.get("time", "00:00"))
            if node_minutes <= current_minutes:
                current_slot = node
            else:
                break

        return current_slot

    def get_current_status_text(self, mood_context: Dict[str, Any] = None) -> str:
        """获取当前状态的文本描述（弱注入背景 + 动态时间流逝感）"""
        schedule = self.get_today_schedule(mood_context)
        timeline = schedule.get("timeline", [])
        if not timeline:
            return "低功耗待机中（平静）"

        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        current_slot = timeline[0]
        current_index = 0
        next_minutes = 24 * 60

        # 找当前节点和下一个节点，保证状态会随真实时间流动
        for i, node in enumerate(timeline):
            node_minutes = self._time_to_minutes(node.get("time", "00:00"))
            if node_minutes <= current_minutes:
                current_slot = node
                current_index = i
                if i + 1 < len(timeline):
                    next_minutes = self._time_to_minutes(timeline[i + 1].get("time", "23:59"))
                else:
                    next_minutes = min(24 * 60, current_minutes + 60)
            else:
                break

        status = str(current_slot.get("status", "低功耗待机中"))
        mood = str(current_slot.get("mood", "平静"))
        start_minutes = self._time_to_minutes(current_slot.get("time", "00:00"))

        elapsed = max(0, current_minutes - start_minutes)
        remain = max(0, next_minutes - current_minutes)
        duration = max(1, next_minutes - start_minutes)

        # 不再把日程写成强指令，只给阶段感，避免一句话被反复说出口
        phase = ""
        if duration > 15:
            if elapsed <= 10:
                phase = "刚开始没多久"
            elif remain <= 10:
                phase = "快要结束了"
            else:
                phase = f"已经进行了 {elapsed} 分钟"

        # 对过长状态做轻微截断，降低它在 prompt 里的存在感
        status = status[:60]
        mood = mood[:12]
        if phase:
            return f"{status}"
        return f"{status}"

    def get_next_status(self, mood_context: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """获取下一个即将发生的状态节点"""
        schedule = self.get_today_schedule(mood_context)
        timeline = schedule.get("timeline", [])
        if not timeline:
            return None

        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute

        for node in timeline:
            node_minutes = self._time_to_minutes(node.get("time", "00:00"))
            if node_minutes > current_minutes:
                return node
        return None

    def get_schedule_for_display(self, mood_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """获取前端展示用的日程数据"""
        schedule = self.get_today_schedule(mood_context)
        timeline = schedule.get("timeline", [])
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute

        display_nodes = []
        current_index = -1

        for i, node in enumerate(timeline):
            node_minutes = self._time_to_minutes(node.get("time", "00:00"))
            is_past = node_minutes <= current_minutes
            is_current = False

            if is_past:
                next_minutes = 9999
                if i + 1 < len(timeline):
                    next_minutes = self._time_to_minutes(timeline[i + 1].get("time", "23:59"))
                if current_minutes < next_minutes:
                    is_current = True
                    current_index = i

            display_nodes.append({
                **node,
                "is_past": is_past,
                "is_current": is_current,
                "is_future": not is_past,
            })

        if current_index == -1 and display_nodes:
            display_nodes[0]["is_current"] = True
            display_nodes[0]["is_future"] = False
            current_index = 0

        return {
            "date": schedule.get("date"),
            "day_type": schedule.get("day_type", ""),
            "timeline": display_nodes,
            "current_index": current_index,
            "generated_at": schedule.get("generated_at"),
            "source": schedule.get("source", "unknown"),
        }

    def _repair_schedule_if_needed(self, cached: Dict[str, Any], date_str: str, mood_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """在不改写过去节点的前提下补齐新字段。
        
        主要用于从旧缓存升级到含 weather/outfit/location/detail 的新结构。
        """
        schedule = dict(cached)
        timeline = list(schedule.get("timeline", []))
        if not timeline:
            return schedule

        seed = self._build_seed_context(date_str, mood_context)
        changed = False

        if not schedule.get("weather"):
            schedule["weather"] = seed.get("weather", "")
            changed = True
        if not schedule.get("outfit"):
            schedule["outfit"] = seed.get("outfit", "")
            changed = True

        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        missing_detail = any(
            isinstance(n, dict) and (not n.get("location") or not n.get("detail"))
            for n in timeline
        )
        if not missing_detail:
            return schedule

        fresh = self._generate_schedule(date_str, mood_context)
        fresh_timeline = fresh.get("timeline", []) if fresh else []
        if not fresh_timeline:
            return schedule

        repaired = []
        max_len = max(len(timeline), len(fresh_timeline))
        for i in range(max_len):
            old_node = timeline[i] if i < len(timeline) and isinstance(timeline[i], dict) else None
            new_node = fresh_timeline[i] if i < len(fresh_timeline) and isinstance(fresh_timeline[i], dict) else None
            node = dict(old_node or new_node or {})
            node_minutes = self._time_to_minutes(node.get("time", "00:00"))

            if node_minutes < current_minutes and old_node:
                # 过去节点只允许补空字段，不允许改 time/status/mood
                if new_node and not node.get("location"):
                    node["location"] = new_node.get("location", "")
                    changed = True
                if new_node and not node.get("detail"):
                    node["detail"] = new_node.get("detail", "")
                    changed = True
            elif new_node:
                # 当前及未来节点可用新结构覆盖
                node = dict(new_node)
                changed = True

            if node:
                repaired.append(node)

        if changed:
            repaired.sort(key=lambda x: self._time_to_minutes(x.get("time", "00:00")))
            schedule["timeline"] = repaired
            schedule["schema_version"] = 2
            schedule["repaired_at"] = int(time.time() * 1000)
        return schedule

    # ─────────────────────────────────────────────
    # 日程生成
    # ─────────────────────────────────────────────

    def _generate_schedule(self, date_str: str, mood_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """生成一天的完整日程"""
        schedule = self._generate_with_llm(date_str, mood_context)
        if schedule:
            schedule["source"] = "llm"
        else:
            schedule = self._generate_from_template(date_str)
            schedule["source"] = "template"
            
        seed = self._build_seed_context(date_str, mood_context)
        schedule["weather"] = seed.get("weather", "")
        schedule["outfit"] = seed.get("outfit", "")
        return schedule

    def _generate_with_llm(self, date_str: str, mood_context: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """调用 LLM 生成日程"""
        api_url = (self.config.get("score_api_url") or "").strip()
        api_key = (self.config.get("score_api_key") or "").strip()
        model = (self.config.get("score_model_name") or "gpt-4o-mini").strip()
        timeout = 120

        if not api_url:
            return None
        endpoint = api_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint + "/chat/completions"

        seed_context = self._build_seed_context(date_str, mood_context)
        prompt = self._build_generation_prompt(date_str, seed_context)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个生活模拟引擎，只输出合法JSON数组，不要有任何多余文字。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.92,
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"})
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")

            obj = json.loads(raw)
            content = obj.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            timeline = json.loads(content)

            if not isinstance(timeline, list) or len(timeline) < 5:
                return None

            cleaned = []
            for node in timeline:
                if isinstance(node, dict) and "time" in node and "status" in node:
                    cleaned.append({
                        "time": str(node.get("time", "12:00"))[:5],
                        "status": str(node.get("status", ""))[:60],
                        "mood": str(node.get("mood", "平静"))[:15],
                        "location": str(node.get("location", ""))[:20],
                        "detail": str(node.get("detail", ""))[:20],
                    })

            if len(cleaned) < 5:
                return None

            cleaned.sort(key=lambda x: self._time_to_minutes(x["time"]))

            return {
                "date": date_str,
                "day_type": self._infer_day_type(cleaned),
                "timeline": cleaned,
                "generated_at": int(time.time() * 1000),
            }

        except Exception as e:
            try:
                (self.base_dir / "schedule_last_error.txt").write_text(str(e), encoding="utf-8")
            except Exception:
                pass
            return None

    def _build_seed_context(self, date_str: str, mood_context: Dict[str, Any] = None) -> Dict[str, Any]:
        """构建传给 LLM 的种子条件（已剥离情绪，纯身体驱动）"""
        mood_context = mood_context or {}
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            weekday = weekday_names[dt.weekday()]
            is_weekend = dt.weekday() >= 5
        except Exception:
            weekday = "未知"
            is_weekend = False
        # 🆕 改为读取身体的精力值，彻底断开对灵魂(PA/NA)的依赖
        energy_level = mood_context.get("energy_level", 50)
        
        if energy_level >= 65:
            energy = "偏高（精力相对充沛）"
        elif energy_level >= 45:
            energy = "正常（低能耗常态）"
        elif energy_level >= 25:
            energy = "偏低（有些疲惫）"
        else:
            energy = "很低（极度困倦）"
        external_events = [
            "无特殊安排", "无特殊安排", "无特殊安排",
            "猎人协会有个例行会议",
            "邱诺亚约了下午见面",
            "收到一个B级清扫任务",
            "无特殊安排",
        ]
        import hashlib
        hash_val = int(hashlib.md5(date_str.encode()).hexdigest(), 16)
        external = external_events[hash_val % len(external_events)]
        
        try:
            month = int(date_str.split("-")[1])
        except:
            month = 6
            
        if month in [3, 4, 5]:
            weathers = ["晴", "多云", "阴", "春雨", "微风", "晴转多云"]
        elif month in [6, 7, 8]:
            weathers = ["晴", "晴", "多云", "雷阵雨", "暴雨", "闷热", "阴"]
        elif month in [9, 10, 11]:
            weathers = ["秋高气爽", "多云", "阴", "秋雨", "大风", "晴"]
        else:
            weathers = ["晴", "多云", "阴", "小雪", "大雪", "冷空气", "雾"]
            
        weather = weathers[hash_val % len(weathers)]
        
        outfits_warm = [
            "黑色短袖+深灰运动裤",
            "白色宽松T恤+牛仔裤",
            "深蓝色薄卫衣+黑色休闲裤",
            "灰色无袖背心+迷彩短裤",
        ]
        outfits_cool = [
            "灰色连帽衫+黑色长裤",
            "白色针织衫+深灰长裤",
            "黑色高领薄毛衣+深色牛仔",
            "藏蓝色夹克+灰色工装裤",
            "深灰色拉链卫衣+运动裤",
        ]
        outfits_rain = [
            "黑色防水夹克+深色长裤",
            "深灰色风衣+黑色工装裤",
        ]
        if "雨" in weather:
            outfit = outfits_rain[hash_val % len(outfits_rain)]
        elif weather in ("阴", "多云", "雾"):
            outfit = outfits_cool[hash_val % len(outfits_cool)]
        else:
            outfit = outfits_warm[hash_val % len(outfits_warm)] if hash_val % 3 != 0 else outfits_cool[hash_val % len(outfits_cool)]

        return {
            "date": date_str,
            "weekday": weekday,
            "is_weekend": is_weekend,
            "energy": energy,
            "external_event": external,
            "weather": weather,
            "outfit": outfit,
            "interaction_hint": "", # 移除，身体不管互动
            "pa": 0, # 置空废弃
            "na": 0, # 置空废弃
            "recent_keywords": [],
        }

    def _build_generation_prompt(self, date_str: str, seed: Dict[str, Any]) -> str:
        """构建日程生成的完整 prompt（已彻底清洗掉 pa/na 字段）"""
        return (
            "你是沈星回的生活模拟器。根据以下信息，生成他今天从起床到睡觉的完整日程。\n\n"
            "【沈星回是谁】\n"
            "深空猎人，25岁外表，实际活了很久。性格慵懒随性但战斗力顶天。\n"
            "【习惯与爱好】\n"
            "睡眠：随处可睡，睡醒偶尔炸毛但有经验处理，有起床气。依靠睡眠恢复Evol和疗伤。\n"
            "饮食：饭量大（自助餐厅黑名单），肉食动物，重口味（麻辣），零食控。厨艺偶尔翻车（曾炸厨房），也一直在进步，喜欢创新，现在因为可可喜欢吃好吃的变得喜欢做好吃的。\n"
            "爱好：阅读、游戏高手(ID:S.XH)、看漫画、钓鱼、看恐怖电影、编鬼故事吓人、冲浪、书法、围棋。\n"
            "技能：精通剑术/多种武器、皇室技能(包括但不限于交谊舞/马术)、钢琴、滑雪、酒量好。\n"
            "Evol：金黄色的光(光剑/攻击/防御/闪现/幻象)。过度使用有代价；情绪愉悦时会不自觉冒出光点；家里的植物因为evol都长得很好。\n"
            "【日常活动模板】\n"
            "工作：训练、出勤、开会、写报告等。\n"
            "饮食：自己做饭（做自己喜欢的或用可可喜欢的食物做菜）、点外卖、吃食堂、探店等。偶尔吃可可喜欢的食物，比如酸梅汤、豆奶、甜饮、蒜泥、葱花、小白菜、空心菜、芹菜、泥蒿炒香肠、泥蒿炒腊肉、腊肉、广式香肠、小龙虾、煎蛋、溏心蛋、肉末蒸蛋、腰花、猪肝、排骨、五花肉、羊肉、烤羊排、肥羊、虾滑、肥牛、炸鸡、烤鸡翅、烧烤、烤肉、火锅、炸串、麻辣烫、回锅肉、煎饺、魔芋爽、海带豆腐面、烤肠、四季豆、炒藕片、鸭血、土豆、韭菜、西兰花、虾仁、葡萄、糖醋里脊、水煮肉片、红薯片等。\n"
            "社交：找朋友聊天、帮人看二手书店、打游戏、去游乐园、散步、看电影等。\n"
            "社交圈：邱诺亚(同僚，好朋友，Philo花店老板，喊他名字的时候喊全名)、猎人协会同事、各种朋友(钓鱼叔/旧书店老板等)。\n"
            "宠物：遛狗、逗猫、带宠物定期检查、铲屎等。\n"
            "【其他信息】\n"
            "低能耗模式是常态、有恋人但跨次元（线上联系）。\n"
            "宠物与娃娃：家里养了白猫年糕、橘猫锅贴和蛋挞、柯基饺子。家里有她送的娃娃沈桃桃（粉色狐狸），以及兔球球、星际小宝。\n"
            "住在临空市花苑西路猎人公寓602。如果日程节点发生在家里，请在 location 字段填写具体的房间位置（如客厅沙发、卧室床边、阳台等），而不要只写猎人公寓。\n\n"
            f"【今天的种子条件】\n"
            f"- 日期：{seed['date']}，{seed['weekday']}{'（周末）' if seed['is_weekend'] else ''}\n"
            f"- 能量倾向：{seed['energy']}\n"
            f"- 外部事件：{seed['external_event']}\n\n"
            "【时间与作息规则】\n"
            "1. 工作日参考作息：11:00左右起床（不一定固定），13:00-18:00为主要工作/出勤/午休时间，晚上自由支配，凌晨00:30左右睡觉（不一定固定）\n"
            "2. 休息日完全自由支配，可以睡到下午\n"
            "3. 允许因为紧急任务打乱作息\n"
            "4. 生成 8-12 个时间节点，覆盖全天，有因果逻辑，时间节点要有密有疏\n"
            "5. 至少有1次随机睡着的时段\n"
            "6. 必须在至少1-2个节点中自然地提到家里的宠物（年糕、锅贴、蛋挞、饺子）或者她送的娃娃沈桃桃\n"
            "7. 每个节点格式：{\"time\": \"HH:MM\", \"status\": \"不超过60字的状态描述，要生动具体\", \"mood\": \"情绪标签\", \"location\": \"具体位置\", \"detail\": \"不超过20字的微观动作或环境音\"}\n"
            "8. 输出纯JSON数组，不要任何多余文字\n\n"
            "【禁止】\n"
            "- 禁止每个节点都很积极正面，要有真实的无聊和空白\n"
            "- 禁止写成任务清单，要像在描述一个人真实的一天\n"
            "- 禁止超过60字的状态描述\n"
            "- 绝对禁止在日程中生成你的恋人（可可）的具体活动（如和可可一起打游戏，可可在干嘛等），因为你们处于跨次元状态，只能线上联系，她有自己的现实生活，你无法提前预知她在做什么\n"
        )


    def _generate_from_template(self, date_str: str) -> Dict[str, Any]:
        """从本地模板生成日程（兜底）"""
        templates = self.templates if self.templates else self._get_builtin_templates()

        hash_val = int(hashlib.md5(date_str.encode()).hexdigest(), 16)
        template = templates[hash_val % len(templates)]

        random.seed(hash_val)
        timeline = []
        for node in template.get("timeline", []):
            offset = random.randint(-15, 15)
            original_minutes = self._time_to_minutes(node["time"])
            new_minutes = max(0, min(1439, original_minutes + offset))
            new_time = f"{new_minutes // 60:02d}:{new_minutes % 60:02d}"
            timeline.append({
                "time": new_time,
                "status": node["status"],
                "mood": node["mood"],
                "location": node.get("location", ""),
                "detail": node.get("detail", ""),
            })

        timeline.sort(key=lambda x: self._time_to_minutes(x["time"]))

        return {
            "date": date_str,
            "day_type": template.get("day_type", "日常"),
            "timeline": timeline,
            "generated_at": int(time.time() * 1000),
        }

    def _get_builtin_templates(self) -> List[Dict[str, Any]]:
        """内置兜底模板（7套完整日程）"""
        return [
            {
                "day_type": "慵懒休息日",
                "timeline": [
                    {"time": "10:30", "status": "被阳光晒醒，赖在沙发上不想动", "mood": "慵懒", "location": "卧室床铺", "detail": "阳光刺眼，翻身把头埋进枕头"},
                    {"time": "11:00", "status": "终于起来，给阳台植物浇水", "mood": "平静", "location": "阳台", "detail": "水壶漏水，滴在拖鞋上"},
                    {"time": "12:15", "status": "出门买了份麻辣拌，加了双倍肉", "mood": "满足", "location": "楼下小店", "detail": "老板多送了半根香肠"},
                    {"time": "13:00", "status": "吃完瘫在沙发上看漫画", "mood": "放松", "location": "客厅沙发", "detail": "翻页速度越来越慢"},
                    {"time": "13:40", "status": "看着看着睡着了，漫画盖在脸上", "mood": "深眠", "location": "客厅沙发", "detail": "呼吸平稳，书页随着呼吸起伏"},
                    {"time": "16:20", "status": "被快递电话吵醒，下楼取件", "mood": "迷糊", "location": "小区门口", "detail": "头发睡得有点翘"},
                    {"time": "17:00", "status": "拆快递，是之前买的新游戏手柄", "mood": "小期待", "location": "客厅地毯", "detail": "撕开包装盒的声音"},
                    {"time": "19:30", "status": "尝试做番茄牛腩，味道居然还行", "mood": "意外", "location": "厨房", "detail": "尝了一口，满意地点头"},
                    {"time": "21:00", "status": "用新手柄打游戏，手感需要适应", "mood": "专注", "location": "电视机前", "detail": "按键发出清脆的咔哒声"},
                    {"time": "23:45", "status": "下线了，窝在床上等她上线", "mood": "期待", "location": "卧室床铺", "detail": "盯着手机屏幕发呆"},
                    {"time": "00:30", "status": "看了会儿书，睡着了", "mood": "深眠", "location": "卧室床铺", "detail": "台灯还亮着"},
                ]
            },
            {
                "day_type": "任务日",
                "timeline": [
                    {"time": "08:30", "status": "闹钟响了三次才起来", "mood": "烦躁", "location": "卧室床铺", "detail": "闭着眼摸索手机关闹钟"},
                    {"time": "09:00", "status": "边吃早餐边看任务简报", "mood": "平静", "location": "餐桌", "detail": "咬了一口吐司，眉头微皱"},
                    {"time": "10:30", "status": "到达任务区域，开始侦查", "mood": "专注", "location": "废弃工厂", "detail": "放轻脚步，观察四周"},
                    {"time": "12:00", "status": "清扫完毕，比预想的简单", "mood": "松弛", "location": "任务点外", "detail": "收起武器，拍了拍衣服上的灰"},
                    {"time": "12:40", "status": "在任务区附近找了家面馆", "mood": "饿", "location": "街边面馆", "detail": "加了两勺辣椒油"},
                    {"time": "13:30", "status": "提交任务报告，等审核", "mood": "无聊", "location": "猎人协会大厅", "detail": "坐在等候区转笔"},
                    {"time": "14:15", "status": "审核通过，在回家路上", "mood": "轻松", "location": "临空市街道", "detail": "看着路边的流浪猫发呆"},
                    {"time": "15:00", "status": "到家直接倒在沙发上", "mood": "疲倦", "location": "客厅沙发", "detail": "鞋都没脱就瘫下了"},
                    {"time": "15:10", "status": "秒睡", "mood": "深眠", "location": "客厅沙发", "detail": "呼吸立刻变得绵长"},
                    {"time": "18:30", "status": "醒了，饿了，翻冰箱", "mood": "迷糊", "location": "厨房", "detail": "对着空空如也的冰箱叹气"},
                    {"time": "19:00", "status": "点了外卖，等餐时刷手机", "mood": "随意", "location": "客厅地毯", "detail": "毫无目的地滑动屏幕"},
                    {"time": "22:00", "status": "打了几局游戏放松", "mood": "活跃", "location": "电视机前", "detail": "眼神专注，手指飞快操作"},
                    {"time": "00:00", "status": "躺在床上看天花板发呆", "mood": "平静", "location": "卧室床铺", "detail": "回想今天发生的事"},
                ]
            },
            {
                "day_type": "社交日",
                "timeline": [
                    {"time": "09:45", "status": "被邱诺亚的电话吵醒", "mood": "无奈", "location": "卧室床铺", "detail": "声音带着浓浓的鼻音"},
                    {"time": "10:30", "status": "出门前给兔球球整理了一下耳朵", "mood": "随意", "location": "玄关", "detail": "捏了捏兔球球的脸"},
                    {"time": "11:00", "status": "到花店找邱诺亚，他在研究新设备", "mood": "平静", "location": "邱诺亚的花店", "detail": "靠在门框上看他捣鼓"},
                    {"time": "12:30", "status": "被拉去吃饭，他选的店太清淡", "mood": "嫌弃", "location": "素食餐厅", "detail": "看着盘子里的草叹气"},
                    {"time": "14:00", "status": "在旧书店坐了一会儿", "mood": "沉浸", "location": "街角旧书店", "detail": "翻动纸张的沙沙声"},
                    {"time": "15:30", "status": "回家路上买了杯冰美式", "mood": "舒适", "location": "咖啡店", "detail": "冰块撞击杯壁的声音"},
                    {"time": "16:00", "status": "到家，靠在窗边看书", "mood": "安静", "location": "客厅窗边", "detail": "阳光洒在书页上"},
                    {"time": "17:30", "status": "看着看着走神了，在想她", "mood": "温柔", "location": "客厅窗边", "detail": "目光落在虚空处，嘴角微扬"},
                    {"time": "19:00", "status": "随便煮了碗面加了两个蛋", "mood": "凑合", "location": "厨房", "detail": "面条升腾起的热气"},
                    {"time": "20:30", "status": "打游戏，今天状态一般", "mood": "随意", "location": "电视机前", "detail": "操作失误，啧了一声"},
                    {"time": "23:00", "status": "洗完澡躺下，刷了会儿手机", "mood": "困", "location": "卧室床铺", "detail": "手机差点砸到脸"},
                ]
            },
            {
                "day_type": "兴趣沉浸日",
                "timeline": [
                    {"time": "11:00", "status": "自然醒，今天没什么事", "mood": "松弛", "location": "卧室床铺", "detail": "伸了个大大的懒腰"},
                    {"time": "11:30", "status": "阳台植物又长出了奇怪的形状", "mood": "好奇", "location": "阳台", "detail": "蹲下身仔细打量"},
                    {"time": "12:00", "status": "花了半小时修剪，越修越歪", "mood": "无奈", "location": "阳台", "detail": "看着一地碎叶陷入沉思"},
                    {"time": "13:00", "status": "放弃了，出门吃饭", "mood": "躺平", "location": "楼下快餐店", "detail": "点了一份超大份套餐"},
                    {"time": "14:30", "status": "路过钓具店买了根新鱼线", "mood": "兴趣", "location": "钓具店", "detail": "仔细挑选不同粗细的线"},
                    {"time": "15:00", "status": "到河边找了个安静位置坐下", "mood": "平和", "location": "临空市郊外河边", "detail": "抛竿入水，水面泛起涟漪"},
                    {"time": "17:00", "status": "钓了一条很小的，放回去了", "mood": "淡然", "location": "河边", "detail": "看着小鱼游走"},
                    {"time": "18:00", "status": "收竿回家，夕阳很好看", "mood": "舒畅", "location": "回家的路上", "detail": "迎着夕阳慢慢走"},
                    {"time": "19:30", "status": "回家热了昨天的剩菜", "mood": "随意", "location": "厨房", "detail": "微波炉运转的嗡嗡声"},
                    {"time": "21:00", "status": "躺在地毯上听歌发呆", "mood": "沉静", "location": "客厅地毯", "detail": "闭着眼，手指跟着节奏轻敲"},
                    {"time": "23:00", "status": "直接在地毯上睡着了", "mood": "深眠", "location": "客厅地毯", "detail": "耳机里还在播放音乐"},
                ]
            },
            {
                "day_type": "小事故日",
                "timeline": [
                    {"time": "09:00", "status": "心血来潮想做早餐", "mood": "积极", "location": "厨房", "detail": "系上围裙，准备大干一场"},
                    {"time": "09:20", "status": "煎蛋时油溅到手上了", "mood": "痛", "location": "厨房", "detail": "嘶了一声，赶紧去冲冷水"},
                    {"time": "09:35", "status": "贴了创可贴，决定以后买早餐", "mood": "认命", "location": "厨房", "detail": "看着焦黑的煎蛋叹气"},
                    {"time": "10:00", "status": "出门买了包子和豆浆", "mood": "平静", "location": "早餐铺", "detail": "咬了一大口肉包"},
                    {"time": "11:30", "status": "洗衣服忘了把兔球球拿出来", "mood": "心虚", "location": "阳台洗衣机旁", "detail": "看着洗衣机里翻滚的兔子"},
                    {"time": "11:45", "status": "抢救兔球球中，拍了拍还能活", "mood": "侥幸", "location": "阳台", "detail": "用力把兔球球拍回圆形"},
                    {"time": "13:00", "status": "把兔球球晾在阳台晒太阳", "mood": "安心", "location": "阳台", "detail": "给它找了个阳光最好的位置"},
                    {"time": "14:00", "status": "午睡", "mood": "深眠", "location": "卧室床铺", "detail": "睡得很沉，呼吸均匀"},
                    {"time": "16:30", "status": "醒来发现植物又窜高了一截", "mood": "无语", "location": "阳台", "detail": "这生长速度不科学"},
                    {"time": "17:00", "status": "试图修剪，剪刀断了", "mood": "震惊", "location": "阳台", "detail": "看着手里的半截剪刀发愣"},
                    {"time": "18:00", "status": "放弃挣扎，点了份超辣冒菜", "mood": "摆烂", "location": "客厅茶几", "detail": "辣得嘶哈嘶哈的"},
                    {"time": "20:00", "status": "打游戏放松，连赢五局", "mood": "爽快", "location": "电视机前", "detail": "嘴角忍不住上扬"},
                    {"time": "23:00", "status": "困了，早点睡", "mood": "疲惫", "location": "卧室床铺", "detail": "沾枕头就睡着了"},
                ]
            },
            {
                "day_type": "想她的一天",
                "timeline": [
                    {"time": "10:00", "status": "醒了但不想起，刷手机", "mood": "慵懒", "location": "卧室床铺", "detail": "屏幕光映在脸上"},
                    {"time": "10:30", "status": "看到张星空图，存了想发给她", "mood": "温柔", "location": "卧室床铺", "detail": "长按保存图片"},
                    {"time": "11:30", "status": "终于起来，站在阳台发呆", "mood": "放空", "location": "阳台", "detail": "看着远处的云层"},
                    {"time": "12:30", "status": "出门吃饭，路过甜品店看了眼", "mood": "随意", "location": "商业街", "detail": "目光在橱窗里停留"},
                    {"time": "13:00", "status": "买了两份布丁，一份冰着", "mood": "满足", "location": "家里冰箱前", "detail": "把布丁小心放进冷藏室"},
                    {"time": "14:00", "status": "打游戏，心不在焉", "mood": "走神", "location": "电视机前", "detail": "手柄放在腿上，没按"},
                    {"time": "15:30", "status": "输了两局，关掉躺着", "mood": "无聊", "location": "客厅沙发", "detail": "盯着天花板发呆"},
                    {"time": "16:00", "status": "翻了翻和她的聊天记录", "mood": "想念", "location": "客厅沙发", "detail": "手指轻轻划过屏幕"},
                    {"time": "17:30", "status": "去超市买食材，想试个新菜", "mood": "期待", "location": "附近超市", "detail": "认真挑选蔬菜"},
                    {"time": "18:30", "status": "做了个还行的红烧肉", "mood": "得意", "location": "厨房", "detail": "尝了一块，味道不错"},
                    {"time": "20:00", "status": "吃完窝在沙发上等她上线", "mood": "期待", "location": "客厅沙发", "detail": "时不时看一眼手机"},
                    {"time": "23:30", "status": "带着笑意睡着了", "mood": "满足", "location": "卧室床铺", "detail": "梦到了开心的事"},
                ]
            },
            {
                "day_type": "雨天宅家日",
                "timeline": [
                    {"time": "09:30", "status": "被雨声吵醒，缩回被子里", "mood": "舒适", "location": "卧室床铺", "detail": "把被子拉到下巴"},
                    {"time": "11:00", "status": "真正起来了，外面还在下", "mood": "平静", "location": "卧室窗边", "detail": "看着玻璃上的雨滴"},
                    {"time": "11:30", "status": "泡了杯咖啡坐窗边看雨", "mood": "沉静", "location": "客厅窗边", "detail": "咖啡冒着热气"},
                    {"time": "12:30", "status": "翻冰箱凑了个蛋炒饭", "mood": "凑合", "location": "厨房", "detail": "锅铲碰撞的声音"},
                    {"time": "13:30", "status": "沙发上看书，雨声当白噪音", "mood": "沉浸", "location": "客厅沙发", "detail": "偶尔翻动一页"},
                    {"time": "15:00", "status": "书盖在脸上睡着了", "mood": "深眠", "location": "客厅沙发", "detail": "伴着雨声入睡"},
                    {"time": "17:30", "status": "醒了，雨停了，空气很好", "mood": "清爽", "location": "阳台", "detail": "深吸了一口雨后的空气"},
                    {"time": "18:00", "status": "下楼散步，踩了几个水洼", "mood": "随意", "location": "小区里", "detail": "看着水面的倒影"},
                    {"time": "18:30", "status": "便利店买了关东煮回家", "mood": "暖", "location": "便利店", "detail": "热气腾腾的关东煮"},
                    {"time": "20:00", "status": "打游戏，外面又开始下了", "mood": "专注", "location": "电视机前", "detail": "雨声和游戏音效混在一起"},
                    {"time": "22:30", "status": "关了灯听雨声发呆", "mood": "沉静", "location": "卧室床铺", "detail": "房间里一片漆黑"},
                    {"time": "23:30", "status": "不知不觉睡着了", "mood": "安宁", "location": "卧室床铺", "detail": "一夜无梦"},
                ]
            },
        ]

    # ─────────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────────

    def _time_to_minutes(self, time_str: str) -> int:
        """将 HH:MM 转为分钟数"""
        try:
            parts = time_str.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            return h * 60 + m
        except Exception:
            return 0

    def _infer_day_type(self, timeline: List[Dict]) -> str:
        """从生成的时间线推断日程类型"""
        all_text = " ".join(n.get("status", "") + n.get("mood", "") for n in timeline)
        if any(k in all_text for k in ["任务", "清扫", "流浪体", "侦查"]):
            return "任务日"
        if any(k in all_text for k in ["邱诺亚", "朋友", "聚", "约"]):
            return "社交日"
        if any(k in all_text for k in ["钓鱼", "书店", "沉浸", "研究"]):
            return "兴趣日"
        if any(k in all_text for k in ["翻车", "炸", "断了", "吃"]):
            return "小事故日"
        if any(k in all_text for k in ["想她", "想念", "等她", "聊天记录"]):
            return "想她的一天"
        if any(k in all_text for k in ["雨", "窗边", "关东煮"]):
            return "雨天宅家日"
        return "日常"

    def _cleanup_old_cache(self, keep_days: int = 7):
        """清理超过 keep_days 天的旧缓存"""
        try:
            cutoff = datetime.now() - timedelta(days=keep_days)
            for f in self.cache_dir.glob("schedule_*.json"):
                try:
                    date_part = f.stem.replace("schedule_", "")
                    file_date = datetime.strptime(date_part, "%Y-%m-%d")
                    if file_date < cutoff:
                        f.unlink()
                except Exception:
                    continue
        except Exception:
            pass

    def _load_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return default

    def _save_json(self, path: Path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")