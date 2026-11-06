"""凭证管理接口：读写 secret_vars.xlsx「全局变量」表，值全程掩码，绝不回显明文。"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.shared import authz, secrets_writer
from backend.shared.secrets_writer import SecretError
from core.context import load_config

ROOT = Path(__file__).resolve().parents[3]  # 项目根（backend/shared/routers/ 上溯三级）

router = APIRouter(prefix="/api/secrets", tags=["凭证管理"])

_ERROR_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409, "locked": 409}


class SecretCreate(BaseModel):
    name: str
    value: str
    remark: str | None = None


class SecretUpdate(BaseModel):
    value: str | None = None      # 留空/缺省表示保持原值不变
    remark: str | None = None


def _path() -> Path:
    rel = load_config().get("secret_vars_file") or "data/secret_vars.xlsx"
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def _guard(fn, *args):
    try:
        fn(*args)
    except SecretError as e:
        raise HTTPException(status_code=_ERROR_STATUS.get(e.kind, 400), detail=str(e))


@router.get("", summary="凭证变量列表（值掩码，不回显明文）")
def list_secrets():
    try:
        return secrets_writer.read_all(_path())
    except SecretError as e:
        raise HTTPException(status_code=_ERROR_STATUS.get(e.kind, 400), detail=str(e))


@router.post("", status_code=201, summary="新增凭证变量（文件不存在时自动创建）")
def create_secret(body: SecretCreate, _: dict = Depends(authz.require_perm("secret:manage"))):
    _guard(secrets_writer.create_secret, _path(), body.name, body.value, body.remark)
    return list_secrets()


@router.put("/{name}", summary="更新凭证变量（value 留空表示不改值，仅改备注）")
def update_secret(name: str, body: SecretUpdate, _: dict = Depends(authz.require_perm("secret:manage"))):
    _guard(secrets_writer.update_secret, _path(), name, body.value, body.remark)
    return list_secrets()


@router.delete("/{name}", summary="删除凭证变量（整行删除）")
def delete_secret(name: str, _: dict = Depends(authz.require_perm("secret:manage"))):
    _guard(secrets_writer.delete_secret, _path(), name)
    return list_secrets()
