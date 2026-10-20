"""Save this project's model credential using Windows DPAPI and hidden input."""
import argparse
import ctypes
import getpass
import json
import os
import sys
from pathlib import Path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="你的模型接口地址，例如 https://your-provider.example/v1")
    parser.add_argument("--model", required=True, help="你的模型 ID")
    parser.add_argument("--protocol", choices=["responses", "messages"], default="responses")
    parser.add_argument("--directory", type=Path, default=Path(".local/private"))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-output-tokens", type=int, default=2400)
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
        help="仅当所选 Responses 模型支持时指定；默认不发送该字段",
    )
    args = parser.parse_args(argv)
    if args.reasoning_effort is not None and args.protocol != "responses":
        parser.error("--reasoning-effort 只能与 --protocol responses 一起使用。")
    return args


def main():
    args = parse_args()
    if os.name != "nt" or not sys.stdin.isatty():
        print("Use an interactive Windows terminal for hidden input, or configure process environment variables.")
        return 2
    from ctypes import wintypes
    from agent.config import model_class
    key = getpass.getpass("API key (hidden): ").strip()
    # Validate all fields before storing; never send a network request here.
    extra = {"reasoning_effort": args.reasoning_effort} if args.reasoning_effort is not None else {}
    model_class(args.protocol)(args.base_url, args.model, key, timeout=args.timeout,
                               max_tokens=args.max_output_tokens, **extra)
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
    raw = key.encode()
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    encrypt = crypt.CryptProtectData
    encrypt.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    encrypt.restype = wintypes.BOOL
    if not encrypt(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        print("Could not encrypt the credential.")
        return 1
    try:
        args.directory.mkdir(parents=True, exist_ok=True)
        (args.directory / "credential.bin").write_bytes(ctypes.string_at(output.data, output.size))
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(output.data)
    config = {"base_url": args.base_url, "model": args.model, "auth": "bearer", "protocol": args.protocol}
    config.update(timeout=args.timeout, max_tokens=args.max_output_tokens, **extra)
    (args.directory / "model.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Saved encrypted local config: {args.model} / {args.protocol}. No API call made.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt):
        print("Configuration not completed; no credential was displayed.")
        raise SystemExit(1)
