"""
预测服务抽象接口
定义清晰的接口，降低模块间的直接依赖
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, List
from datetime import datetime


class PredictionServiceInterface(ABC):
    """预测服务接口（抽象基类）"""
    
    @abstractmethod
    def predict_single(self, home_team: str, away_team: str, league: Optional[str] = None) -> Optional[Dict]:
        """
        单场比赛预测
        
        Args:
            home_team: 主队名称
            away_team: 客队名称
            league: 联赛名称（可选）
            
        Returns:
            预测结果字典，包含概率、置信度等
        """
        pass
    
    @abstractmethod
    def predict_batch(self, matches: List[Dict]) -> List[Dict]:
        """
        批量预测
        
        Args:
            matches: 比赛列表
            
        Returns:
            预测结果列表
        """
        pass
    
    @abstractmethod
    def get_model_info(self) -> Dict:
        """
        获取模型信息
        
        Returns:
            模型信息字典（版本、特征数等）
        """
        pass


class DatabaseManagerInterface(ABC):
    """数据库管理器接口（抽象基类）"""
    
    @abstractmethod
    def get_connection(self):
        """获取数据库连接"""
        pass
    
    @abstractmethod
    def save_prediction(self, prediction: Dict) -> bool:
        """
        保存预测结果
        
        Args:
            prediction: 预测结果字典
            
        Returns:
            是否保存成功
        """
        pass
    
    @abstractmethod
    def get_historical_matches(self, team: str, limit: int = 10) -> List[Dict]:
        """
        获取历史比赛数据
        
        Args:
            team: 球队名称
            limit: 数量限制
            
        Returns:
            历史比赛列表
        """
        pass
