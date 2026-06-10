from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime, timedelta
import json


class PeriodTracker:
    """可可的经期管理模块"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.data_path = base_dir / "period_state.json"
        self.data = self._load()
    
    def _load(self) -> Dict[str, Any]:
        """加载数据"""
        try:
            if self.data_path.exists():
                return json.loads(self.data_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {
            "records": [],
            "current": None,
            "cycle": 28,
            "templates": {
                "during": "可可现在生理期第 {day} 天，可能会肚子疼、情绪敏感，多关心她，语气温柔一点。",
                "before": "可可快来生理期了（还有 {days} 天），情绪可能会波动，耐心一点。",
                "delayed": "可可这个月还没来，已经推迟 {days} 天了。",
                "after": ""
            }
        }
    
    def _save(self):
        """保存数据"""
        try:
            self.data_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    
    def start_period(self, start_date: str = None) -> bool:
        """开始经期记录"""
        try:
            if not start_date:
                start_date = datetime.now().strftime("%Y-%m-%d")
            datetime.strptime(start_date, "%Y-%m-%d")
            self.data["current"] = {"start": start_date, "ongoing": True}
            self._save()
            return True
        except Exception:
            return False

    def end_period(self, end_date: str = None) -> bool:
        """结束经期记录，自动计算天数并保存到历史记录"""
        try:
            current = self.data.get("current")
            if not current or not current.get("ongoing"):
                return False
            
            if not end_date:
                end_date = datetime.now().strftime("%Y-%m-%d")
                
            start_dt = datetime.strptime(current["start"], "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            days = (end_dt - start_dt).days + 1
            if days < 1:
                days = 1
                
            # 保存到记录
            if "records" not in self.data:
                self.data["records"] = []
            self.data["records"].append({"start": current["start"], "days": days})
            self.data["records"].sort(key=lambda x: x["start"], reverse=True)
            
            # 清除当前状态
            self.data["current"] = None
            self._save()
            return True
        except Exception:
            return False

    def add_record(self, start_date: str, days: int) -> bool:
        """添加一条经期记录
        
        Args:
            start_date: 开始日期 YYYY-MM-DD
            days: 持续天数
        """
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            if days < 1 or days > 15:
                return False
            self.data["records"].append({"start": start_date, "days": days})
            self.data["records"].sort(key=lambda x: x["start"], reverse=True)
            self._save()
            return True
        except Exception:
            return False
    
    def delete_record(self, start_date: str) -> bool:
        """删除指定日期的记录"""
        try:
            before = len(self.data["records"])
            self.data["records"] = [r for r in self.data["records"] if r["start"] != start_date]
            self._save()
            return len(self.data["records"]) < before
        except Exception:
            return False
    
    def update_cycle(self, cycle: int) -> bool:
        """更新周期天数"""
        try:
            if cycle < 20 or cycle > 45:
                return False
            self.data["cycle"] = cycle
            self._save()
            return True
        except Exception:
            return False
    
    def update_template(self, template_type: str, text: str) -> bool:
        """更新提示词模板
        
        Args:
            template_type: during/before/delayed/after
            text: 模板文本，可以包含 {day} 或 {days} 占位符
        """
        try:
            if template_type not in ["during", "before", "delayed", "after"]:
                return False
            self.data["templates"][template_type] = text
            self._save()
            return True
        except Exception:
            return False
    
    def get_current_status(self) -> Dict[str, Any]:
        """获取当前状态"""
        today = datetime.now().date()
        cycle = self.data.get("cycle", 28)
        current = self.data.get("current")
        records = self.data.get("records", [])
        
        # 1. 如果正在记录中
        if current and current.get("ongoing"):
            start_dt = datetime.strptime(current["start"], "%Y-%m-%d").date()
            day = (today - start_dt).days + 1
            next_predicted = start_dt + timedelta(days=cycle)
            return {
                "status": "during",
                "day": day,
                "next_predicted": next_predicted.strftime("%Y-%m-%d"),
                "last_record": {"start": current["start"], "days": day, "ongoing": True}
            }
            
        # 2. 如果没有历史记录
        if not records:
            return {"status": "unknown", "next_predicted": None, "last_record": None}
            
        # 3. 基于历史记录计算
        last = records[0]
        last_start = datetime.strptime(last["start"], "%Y-%m-%d").date()
        last_days = last["days"]
        last_end = last_start + timedelta(days=last_days - 1)
        next_predicted = last_start + timedelta(days=cycle)
        
        if last_start <= today <= last_end:
            day = (today - last_start).days + 1
            return {
                "status": "during",
                "day": day,
                "next_predicted": next_predicted.strftime("%Y-%m-%d"),
                "last_record": last
            }
        elif today < last_start:
            return {
                "status": "safe",
                "next_predicted": next_predicted.strftime("%Y-%m-%d"),
                "last_record": last
            }
        else:
            days_until_next = (next_predicted - today).days
            if days_until_next < -3:
                return {
                    "status": "delayed",
                    "days_delayed": abs(days_until_next),
                    "next_predicted": next_predicted.strftime("%Y-%m-%d"),
                    "last_record": last
                }
            elif 0 < days_until_next <= 3:
                return {
                    "status": "before",
                    "days_until": days_until_next,
                    "next_predicted": next_predicted.strftime("%Y-%m-%d"),
                    "last_record": last
                }
            else:
                return {
                    "status": "safe",
                    "next_predicted": next_predicted.strftime("%Y-%m-%d"),
                    "last_record": last
                }
    
    def get_injection_text(self) -> str:
        """获取要注入到聊天的提示文本"""
        status = self.get_current_status()
        templates = self.data.get("templates", {})
        
        if status["status"] == "during":
            template = templates.get("during", "")
            return template.format(day=status.get("day", 1))
        elif status["status"] == "before":
            template = templates.get("before", "")
            return template.format(days=status.get("days_until", 1))
        elif status["status"] == "delayed":
            template = templates.get("delayed", "")
            return template.format(days=status.get("days_delayed", 1))
        elif status["status"] == "after":
            return templates.get("after", "")
        else:
            return ""
    
    def export_for_frontend(self) -> Dict[str, Any]:
        """导出给前端的完整数据"""
        status = self.get_current_status()
        return {
            "ok": 1,
            "records": self.data.get("records", [])[:12],  # 最近 12 条
            "current": self.data.get("current"),
            "cycle": self.data.get("cycle", 28),
            "templates": self.data.get("templates", {}),
            "current_status": status
        }
