"""把飞书 App Secret、Claude 长期 token 存进系统的密钥库，不落明文文件。

Windows：凭据管理器，「控制面板 → 凭据管理器 → Windows 凭据」里能看到 feishu-bridge:* 两项，可以手动删。
Mac：钥匙串（通过 keyring 库），「钥匙串访问」里搜 feishu-bridge 能看到，服务名 feishu-bridge、账户名是项目名。
"""
from __future__ import annotations

import json
from pathlib import Path

from .osutil import WINDOWS

PREFIX = "feishu-bridge:"
APP_SECRET = "app_secret"
CLAUDE_TOKEN = "claude_token"
STORE_NAME = "Windows 凭据管理器" if WINDOWS else "macOS 钥匙串"

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _adv.CredReadW.restype = wintypes.BOOL
    _adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _adv.CredWriteW.restype = wintypes.BOOL
    _adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _adv.CredDeleteW.restype = wintypes.BOOL
    _adv.CredFree.argtypes = [ctypes.c_void_p]
    _adv.CredFree.restype = None


def _service(prefix: str) -> str:
    """钥匙串里的服务名：feishu-bridge:app_secret → 服务 feishu-bridge、账户 app_secret。"""
    return prefix.rstrip(":")


def read(name: str, prefix: str = PREFIX) -> str | None:
    """读一项；没有返回 None。"""
    if not WINDOWS:
        import keyring
        return keyring.get_password(_service(prefix), name)
    p = ctypes.POINTER(_CREDENTIAL)()
    if not _adv.CredReadW(prefix + name, _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise OSError(err, f"读取凭据失败（{err}）")
    try:
        c = p.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
    finally:
        _adv.CredFree(p)


def write(name: str, value: str, prefix: str = PREFIX) -> None:
    data = value.encode("utf-8")
    if not data:
        raise ValueError("不能存空值")
    if not WINDOWS:
        import keyring
        keyring.set_password(_service(prefix), name, value)
        return
    if len(data) > 2560:
        raise ValueError("太长了（凭据管理器单项上限 2560 字节）")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    c = _CREDENTIAL()
    c.Type = _CRED_TYPE_GENERIC
    c.TargetName = prefix + name
    c.CredentialBlobSize = len(data)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = _CRED_PERSIST_LOCAL_MACHINE
    c.UserName = "feishu-bridge"
    if not _adv.CredWriteW(ctypes.byref(c), 0):
        err = ctypes.get_last_error()
        raise OSError(err, f"写入凭据失败（{err}）")


def secret_from_mcp(claude_json: Path, server: str, app_id: str) -> str:
    """从 ~/.claude.json 里某个飞书 MCP 的启动参数取 App Secret（-a <app_id> -s <secret>，或写在 env 里）。
    只认 App ID 对得上的那一组，免得把别的应用的密钥拿错。"""
    spec = (json.loads(Path(claude_json).read_text(encoding="utf-8")).get("mcpServers") or {}).get(server)
    if not spec:
        raise KeyError(f"~/.claude.json 里没有叫 {server} 的 MCP 服务器")
    args = [str(a) for a in spec.get("args") or []]

    def after(*flags: str) -> str:
        return next((args[i + 1] for i, a in enumerate(args[:-1]) if a in flags), "")

    got_id = after("-a", "--app-id", "--app_id")
    secret = after("-s", "--app-secret", "--app_secret")
    env = spec.get("env") or {}
    got_id = got_id or next((str(v) for k, v in env.items() if k.upper().endswith("APP_ID")), "")
    secret = secret or next((str(v) for k, v in env.items() if k.upper().endswith("APP_SECRET")), "")
    if got_id != app_id:
        raise ValueError(f"MCP「{server}」用的应用是 {got_id or '?'}，不是 config.toml 里的 {app_id}")
    if not secret:
        raise ValueError(f"MCP「{server}」的配置里没找到 App Secret")
    return secret


def delete(name: str, prefix: str = PREFIX) -> bool:
    if not WINDOWS:
        import keyring
        import keyring.errors
        try:
            keyring.delete_password(_service(prefix), name)
            return True
        except keyring.errors.PasswordDeleteError:
            return False
    if _adv.CredDeleteW(prefix + name, _CRED_TYPE_GENERIC, 0):
        return True
    if ctypes.get_last_error() == _ERROR_NOT_FOUND:
        return False
    raise OSError(ctypes.get_last_error(), "删除凭据失败")
