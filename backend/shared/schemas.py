"""平台共享请求/响应模型（自动生成 OpenAPI 文档的类型契约）：项目/执行/环境/场景/通知/调度/依赖/用户权限。"""
from typing import Any

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    """新建用例项目（一个业务系统一个用例文件：接口=xlsx，UI=json，App=apjson）。"""

    name: str = Field(description="项目名称（唯一，不含 / \\ ? #）")
    desc: str = Field(default="", description="项目描述（可选）")
    kind: str = Field(default="api", description="项目类型：api=接口用例(Excel) | ui=UI用例(JSON) | app=App用例(.apjson)；创建后不可改")


class ProjectUpdate(BaseModel):
    """更新项目元信息（仅名称/描述，文件路径不可变）。"""

    name: str | None = None
    desc: str | None = None


class ProjectOut(BaseModel):
    """项目对外视图。"""

    id: str
    name: str
    desc: str = ""
    file: str = Field(description="相对项目根的用例文件路径（接口 xlsx / UI json / App apjson，前端据此贯穿 file 参数）")
    kind: str = Field(default="api", description="项目类型：api | ui | app")
    builtin: bool = Field(default=False, description="内置默认项目不可删除")
    created_at: str | None = None
    updated_at: str | None = None
    case_count: int = 0


class RunCreate(BaseModel):
    """发起一次执行批次。"""

    env: str | None = Field(default=None, description="环境名，留空用 config.yaml 的 default_env")
    module: str | None = Field(default=None, description="按模块筛选（包含匹配）")
    priority: str | None = Field(default=None, description="按优先级筛选，逗号分隔，如 P0,P1")
    keyword: str | None = Field(default=None, description="关键字筛选（编号/接口名/描述/URL）")
    cases: list[str] | None = Field(default=None, description="按用例编号精确选择（优先于 module/priority/keyword）")
    vars: dict[str, Any] | None = Field(default=None, description="运行前变量覆盖（键值映射，覆盖同名 secret/Excel/YAML 变量）")
    file: str = Field(default="data/api_cases.xlsx", description="用例文件或目录（相对项目根）")
    workers: int | None = Field(default=None, ge=1, le=4,
                                description="UI 批次并行 worker 数（仅 .json 用例文件生效，其余批次忽略；含依赖用例自动降级串行）")


class RunOut(BaseModel):
    run_id: str
    status: str = Field(description="pending / running / passed / failed / error")
    source: str = Field(default="api", description="api=后端发起，cli=历史本地运行")
    args: dict | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    report_dir: str | None = Field(default=None, description="报告目录（相对项目根）")
    log_tail: str | None = None


class HealthOut(BaseModel):
    status: str
    default_env: str | None = None
    cases_file_exists: bool


class EnvItem(BaseModel):
    """单个环境的可编辑配置（写回 config.yaml 的 env 段）。"""

    name: str = Field(description="环境名（唯一，YAML key）")
    base_url: str = Field(description="基础 URL，必须以 http(s):// 开头")
    timeout: float | None = Field(default=None, description="秒，正数；留空用框架默认")
    verify_ssl: bool = Field(default=True, description="是否校验 TLS 证书")
    default_headers: dict | None = Field(default=None, description="环境默认请求头（用例头优先）")


class EnvsUpdate(BaseModel):
    """整体覆盖式保存环境配置（镜像当前 env 段，含默认环境）。"""

    default_env: str = Field(description="默认环境名，必须是 envs 之一")
    envs: list[EnvItem] = Field(description="完整环境列表（至少一个）")


class PlanCreate(BaseModel):
    """新建测试场景。"""

    name: str = Field(description="场景名称")
    desc: str = Field(default="", description="场景描述")
    case_ids: list[str] = Field(description="用例编号列表（至少一条）")
    env: str = Field(default="", description="偏好环境名（空则用默认环境）")


class PlanUpdate(BaseModel):
    """更新场景（只传需要修改的字段）。"""

    name: str | None = None
    desc: str | None = None
    case_ids: list[str] | None = None
    env: str | None = None


class PlanOut(BaseModel):
    """场景对外视图。"""

    id: str
    name: str
    desc: str
    case_ids: list[str]
    env: str
    created_at: str
    updated_at: str
    last_run_id: str | None = None
    last_run_at: str | None = None


# ==================== Webhook 通知 ====================


class WebhookCreate(BaseModel):
    """新建 Webhook 通知配置。"""

    name: str = Field(description="配置名称（便于识别）")
    url: str = Field(description="Webhook 地址")
    type: str = Field(default="custom", description="wechat_work / feishu / custom")
    events: list[str] = Field(default=["run_fail", "run_error"],
                              description="触发事件: run_pass / run_fail / run_error")
    enabled: bool = True


class WebhookUpdate(BaseModel):
    """更新 Webhook（只传需修改字段）。"""

    name: str | None = None
    url: str | None = None
    type: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


class WebhookOut(BaseModel):
    """Webhook 对外视图。"""

    id: str
    name: str
    url: str
    type: str
    events: list[str]
    enabled: bool
    created_at: str
    updated_at: str


# ==================== 定时任务 ====================


class ScheduleCreate(BaseModel):
    """新建定时任务。"""

    name: str = Field(description="任务名称")
    cron: str = Field(description="Cron 表达式（标准 5 位：分 时 日 月 周）")
    plan_id: str = Field(default="", description="关联场景 ID（与 run_config 二选一）")
    run_config: dict | None = Field(default=None, description="直接指定执行参数（env/module/priority 等）")
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    """更新定时任务（只传需修改字段）。"""

    name: str | None = None
    cron: str | None = None
    plan_id: str | None = None
    run_config: dict | None = None
    enabled: bool | None = None


class ScheduleOut(BaseModel):
    """定时任务对外视图。"""

    id: str
    name: str
    cron: str
    plan_id: str
    run_config: dict
    enabled: bool
    last_run_id: str | None = None
    last_run_at: str | None = None
    next_run_at: str | None = None
    created_at: str
    updated_at: str


# ==================== 用例依赖 ====================


class DepsSetRequest(BaseModel):
    """设置用例依赖列表。"""

    dependencies: list[str] = Field(description="前置依赖用例 ID 列表")


# ==================== 用户与权限（RBAC） ====================


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str
    password: str


class UserOut(BaseModel):
    """用户输出：绝不包含密码/哈希字段。"""

    username: str
    display_name: str
    role: str
    role_name: str
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None
    permissions: list[str] = []


class LoginResponse(BaseModel):
    """登录响应：token + 用户信息（含权限码，前端 v-perm 据此显隐）。"""

    token: str
    user: UserOut


class UserCreate(BaseModel):
    """新建用户。"""

    username: str
    password: str
    display_name: str = ""
    role: str = Field(default="qa", description="admin/tl/qa/dev/viewer")


class UserUpdate(BaseModel):
    """更新用户（只传改动字段）。"""

    display_name: str | None = None
    role: str | None = None
    enabled: bool | None = None
    password: str | None = None


class PermMatrixUpdate(BaseModel):
    """整体提交「权限码 → 角色」矩阵（设计文档 8.2）。

    权限码目录由后端 perm_store.CATALOG 固定，不接受新增码，防止前端 v-perm 失效。
    """

    matrix: dict[str, list[str]] = Field(
        description='权限码 -> 角色列表，如 {"case:write": ["admin","tl","qa"], "user:manage": ["admin"]}',
    )
