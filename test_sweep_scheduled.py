import sys
sys.path.insert(0, "D:/Architecture")
from gq.auto_collector import GQCollector

c = GQCollector()
print("调用真实 _sweep_scheduled() ...")
c._sweep_scheduled()
print("DONE")
