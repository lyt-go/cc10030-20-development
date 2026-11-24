"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import merge_review, resource

# 专用前缀路由先注册，通用 /api/{module} 资源路由兜底。
ROUTERS = [merge_review, resource]
