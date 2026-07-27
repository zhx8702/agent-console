"""WeChat 4.0 image decryption — extract image key and decrypt V2 .dat files.

Image key is independent from the database key, extracted from WeChat process memory.
All images share the same key within a login session.
"""

import ctypes
import ctypes.wintypes as wt
import json
import re
import struct
from pathlib import Path

try:
    from wxbot_client.secure_files import atomic_write_private_text
except ImportError:  # pragma: no cover - direct client launch
    from secure_files import atomic_write_private_text

# ============================================================
# Constants
# ============================================================

V2_SIG = b"\x07\x08V2\x08\x07"
V2_HDR_SZ = 15
XOR_BYTE = 0x88

JPEG_HEAD = b"\xff\xd8\xff"
PNG_HEAD = b"\x89PNG"

MEM_COMMIT = 0x1000
PAGE_READABLE = {0x02, 0x04, 0x06, 0x08, 0x20, 0x40, 0x80}

IMG_KEY_RE = re.compile(rb"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


kernel32 = ctypes.windll.kernel32


# ============================================================
# PKCS7
# ============================================================


def _pkcs7_unpad(data):
    if not data or len(data) % 16 != 0:
        return None
    n = data[-1]
    if n == 0 or n > 16:
        return None
    if data[-n:] != bytes([n]) * n:
        return None
    return data[:-n]


# ============================================================
# V2 .dat decryption
# ============================================================


def decrypt_dat(data, key16):
    """Decrypt V2 format .dat data. key16 = 16-byte AES key (ASCII bytes).
    Returns image bytes on success, None on failure."""
    if len(data) < V2_HDR_SZ or data[:6] != V2_SIG:
        return None

    aes_size = struct.unpack_from("<I", data, 6)[0]
    xor_size = struct.unpack_from("<I", data, 10)[0]

    if aes_size % 16 == 0:
        aligned = aes_size + 16
    else:
        aligned = ((aes_size + 15) // 16) * 16

    from Crypto.Cipher import AES

    aes_ct = data[V2_HDR_SZ : V2_HDR_SZ + aligned]
    if len(aes_ct) != aligned:
        return None

    dec_aes = _pkcs7_unpad(AES.new(key16, AES.MODE_ECB).decrypt(aes_ct))
    if dec_aes is None:
        return None

    mid_start = V2_HDR_SZ + aligned
    mid_end = len(data) - xor_size
    mid = data[mid_start:mid_end] if mid_end > mid_start else b""

    tail = bytes(b ^ XOR_BYTE for b in data[-xor_size:]) if xor_size > 0 else b""

    return dec_aes + mid + tail


def is_valid_image(data):
    return data[:3] == JPEG_HEAD or data[:4] == PNG_HEAD


def detect_ext(data):
    if data[:3] == JPEG_HEAD:
        return ".jpg"
    if data[:4] == PNG_HEAD:
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:2] == b"BM":
        return ".bmp"
    return ".bin"


# ============================================================
# Find test .dat files for key validation
# ============================================================


def find_test_dats(data_dir, max_files=5):
    cache = Path(data_dir) / "cache"
    if not cache.exists():
        return []
    results = []
    for dat in cache.rglob("*_b.dat"):
        try:
            with open(dat, "rb") as f:
                head = f.read(6)
            if head == V2_SIG:
                results.append((dat.stat().st_size, dat))
        except Exception:
            pass
    results.sort()
    return [p for _, p in results[:max_files]]


# ============================================================
# Scan process memory for image key candidates
# ============================================================


def _scan_process(pid):
    import pymem

    pm = pymem.Pymem()
    pm.open_process_from_id(pid)

    candidates = set()
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0

    while addr < 0x7FFFFFFFFFFF:
        ret = kernel32.VirtualQueryEx(
            pm.process_handle,
            ctypes.c_void_p(addr),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi),
        )
        if ret == 0:
            break

        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0

        if mbi.State == MEM_COMMIT and 0 < size < 200_000_000 and mbi.Protect in PAGE_READABLE:
            try:
                chunk = pm.read_bytes(base, size)
                for m in IMG_KEY_RE.finditer(chunk):
                    candidates.add(m.group(0).decode("ascii"))
            except Exception:
                pass

        addr = base + max(size, 4096)

    pm.close_process()
    return candidates


# ============================================================
# Extract image key (memory scan + validation)
# ============================================================


def extract_image_key(data_dir):
    """Extract image key from Weixin.exe process memory.
    Returns 16-char hex string, or None on failure."""
    from setup_decrypt import find_weixin_processes

    test_files = find_test_dats(data_dir)
    if not test_files:
        print("  no V2 .dat test files found")
        return None

    test_path = test_files[0]
    test_data = test_path.read_bytes()
    print(f"  test file: {test_path.name} ({len(test_data)} B)")

    procs = find_weixin_processes()
    if not procs:
        print("  Weixin.exe process not found")
        return None

    all_cands = set()
    for pid, _name, mem in procs:
        print(f"  scanning PID {pid} ({mem // 1024 // 1024} MB)...")
        try:
            cands = _scan_process(pid)
            all_cands |= cands
            print(f"    {len(cands)} candidates")
        except Exception as e:
            print(f"    scan failed: {e}")

    print(f"  total candidates: {len(all_cands)}")
    if not all_cands:
        return None

    from Crypto.Cipher import AES

    aes_size = struct.unpack_from("<I", test_data, 6)[0]
    aligned = (aes_size + 16) if aes_size % 16 == 0 else ((aes_size + 15) // 16) * 16
    aes_ct = test_data[V2_HDR_SZ : V2_HDR_SZ + aligned]

    for cand in all_cands:
        key_bytes = cand.encode("ascii")
        try:
            dec = AES.new(key_bytes, AES.MODE_ECB).decrypt(aes_ct)
            result = _pkcs7_unpad(dec)
            if result is not None and is_valid_image(result):
                print(f"  found image key: {cand}")
                return cand
        except Exception:
            pass

    for cand in all_cands:
        key_bytes = cand.encode("ascii")
        try:
            dec = AES.new(key_bytes, AES.MODE_ECB).decrypt(aes_ct)
            if _pkcs7_unpad(dec) is not None:
                print(f"  found image key (PKCS7 valid): {cand}")
                return cand
        except Exception:
            pass

    print(f"  no valid key found ({len(all_cands)} candidates tested)")
    return None


# ============================================================
# Key persistence — keys.json
# ============================================================


def save_image_key(key_hex, decrypted_dir):
    keys_file = Path(decrypted_dir) / "keys.json"
    keys = {}
    if keys_file.exists():
        try:
            keys = json.loads(keys_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    keys["image_key"] = key_hex
    atomic_write_private_text(
        keys_file,
        json.dumps(keys, indent=2, ensure_ascii=False) + "\n",
    )
    print(f"  saved to {keys_file}")


def load_image_key(decrypted_dir):
    keys_file = Path(decrypted_dir) / "keys.json"
    if not keys_file.exists():
        return None
    try:
        return json.loads(keys_file.read_text(encoding="utf-8")).get("image_key")
    except Exception:
        return None


# ============================================================
# File-level decryption
# ============================================================


def decrypt_file(dat_path, key_hex, out_path=None):
    """Decrypt a single .dat to image. Returns output path, or None on failure."""
    data = Path(dat_path).read_bytes()
    key_bytes = key_hex.encode("ascii")

    result = decrypt_dat(data, key_bytes)
    if result is None:
        return None

    if out_path is None:
        ext = detect_ext(result)
        out_path = str(Path(dat_path).with_suffix(ext))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(result)
    return out_path
