"""数字员工 Agent 包：角色定义 + 规则引擎 + LLM 适配。"""
from agents.roles import ROLES, Role, chair, get_role, role_ids
from agents.engine import analyze_as, build_context

__all__ = ["ROLES", "Role", "chair", "get_role", "role_ids", "analyze_as", "build_context"]
