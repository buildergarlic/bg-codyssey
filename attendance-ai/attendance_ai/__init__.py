"""AI 근태 판정기.

출입관리 단말기에서 출근/퇴근 메뉴를 잘못 고르고 지문을 찍어도, 실제 지문 시각과
앞뒤 순서·개인별 출퇴근 패턴을 학습한 AI가 진짜 출근/퇴근을 판정해 정확한
근로시간을 계산한다.
"""

from .config import Settings
from .engine import AnalysisResult, analyze
from .io_utils import load_punches

__all__ = ["Settings", "AnalysisResult", "analyze", "load_punches"]
__version__ = "1.0.0"
