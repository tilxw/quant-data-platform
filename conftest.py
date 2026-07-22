# 放在项目根目录的空 conftest.py：pytest 见到它会把根目录加入导入路径，
# 于是 tests/ 里可以直接 import qdp，无需安装包。
