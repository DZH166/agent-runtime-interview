"""Explicit environment or Windows DPAPI configuration; never auto-discover keys."""
import ctypes
import json
import os
from pathlib import Path

from .models import ModelError
from .provider import MessagesModel
from .responses_provider import ResponsesModel


def model_class(protocol):
    if protocol == "responses":
        return ResponsesModel
    if protocol == "messages":
        return MessagesModel
    raise ModelError("model_config", "AGENT_PROTOCOL 必须是 responses 或 messages。")


def from_environment():
    return model_class(os.environ.get("AGENT_PROTOCOL", "responses")).from_env()


def load_saved_model(directory: Path):
    """Read only the supplied directory, and leave all plaintext in memory."""
    if os.name != "nt":
        raise ValueError("本机加密配置只支持当前 Windows 用户；其他环境请使用环境变量。")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
    config = json.loads((directory / "model.json").read_text("utf-8"))
    if not isinstance(config, dict) or not all(isinstance(config.get(k), str) and config[k]
                                              for k in ("base_url", "model")):
        raise ModelError("model_config", "加密配置缺少地址或模型。")
    encrypted = (directory / "credential.bin").read_bytes()
    buffer = ctypes.create_string_buffer(encrypted)
    source = Blob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    decrypt = crypt.CryptUnprotectData
    decrypt.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    decrypt.restype = wintypes.BOOL
    if not decrypt(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError("无法解密当前 Windows 用户的配置。")
    try:
        key = ctypes.string_at(output.data, output.size).decode("utf-8")
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(output.data)
    cls = model_class(config.get("protocol", "messages"))
    extra = {"reasoning_effort": config.get("reasoning_effort")} if cls is ResponsesModel else {}
    return cls(config["base_url"], config["model"], key, auth=config.get("auth", "bearer"),
               max_tokens=config.get("max_tokens", 1200), timeout=config.get("timeout", 40), **extra)
