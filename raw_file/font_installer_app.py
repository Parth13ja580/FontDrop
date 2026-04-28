#!/usr/bin/env python3
"""
FontDrop — ZIP Font Installer (Desktop App)
Packaged as a native Windows app via pywebview + PyInstaller.
No browser needed — opens in its own window.
Runs fullscreen and always requests Administrator elevation via UAC.

Build steps:
    pip install pywebview pyinstaller

    1. Create the admin manifest (already provided as fontdrop.manifest):
       Place fontdrop.manifest in the same folder as this script.

    2. Build the exe:
       pyinstaller --noconfirm --onefile --windowed --name FontDrop ^
           --manifest fontdrop.manifest ^
           font_installer_app.py

Flags explained:
  --onefile      → single .exe
  --windowed     → no console/terminal window
  --name         → sets the .exe name
  --manifest     → embeds the admin UAC manifest into the exe
"""

import os
import sys
import json
import threading
import zipfile
import io
import base64
import time
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse, parse_qs

# ── Font extensions we care about ──────────────────────────────────────────────
FONT_EXTENSIONS = {'.ttf', '.otf', '.woff', '.woff2', '.fon', '.fnt', '.eot', '.pfb', '.pfm'}

# ── Windows font install dir ────────────────────────────────────────────────────
WINDOWS_FONTS_DIR = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"

# ── In-memory state ─────────────────────────────────────────────────────────────
_zip_data_list = []   # List of (filename, zip_bytes) tuples
_found_fonts   = []   # List of font dicts from last scan
_history_file  = Path.home() / "AppData" / "Local" / "FontDrop" / "install_history.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# History helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def log_install(font_names: list, source_zip: str):
    _history_file.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if _history_file.exists():
        try:
            history = json.loads(_history_file.read_text(encoding="utf-8"))
        except Exception:
            history = []
    history.append({
        "timestamp": datetime.now().isoformat(),
        "source":    source_zip,
        "fonts":     font_names,
        "count":     len(font_names),
    })
    history = history[-100:]
    _history_file.write_text(json.dumps(history, indent=2), encoding="utf-8")


def get_install_history():
    if not _history_file.exists():
        return []
    try:
        return json.loads(_history_file.read_text(encoding="utf-8"))
    except Exception:
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Installed-fonts manager (Windows registry)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def list_installed_fonts():
    if sys.platform != "win32":
        return []
    import winreg
    fonts = []
    for hkey, reg_path, location, font_dir in [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
         "system",
         WINDOWS_FONTS_DIR),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
         "user",
         Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"),
    ]:
        try:
            key = winreg.OpenKey(hkey, reg_path, 0, winreg.KEY_READ)
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                    ext = Path(value).suffix.lower()
                    if ext in {".ttf", ".otf", ".fon", ".fnt"}:
                        fonts.append({
                            "name":     name,
                            "filename": os.path.basename(value),
                            "location": location,
                            "path":     str(font_dir / os.path.basename(value))
                                        if not os.path.isabs(value) else value,
                        })
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except Exception:
            pass
    return sorted(fonts, key=lambda f: f["name"].lower())


def uninstall_font(font_name: str, location: str):
    if sys.platform != "win32":
        return {"success": False, "error": "Windows only"}
    import ctypes, winreg

    hkey     = winreg.HKEY_LOCAL_MACHINE if location == "system" else winreg.HKEY_CURRENT_USER
    reg_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    font_dir = WINDOWS_FONTS_DIR if location == "system" \
               else Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"
    try:
        rk = winreg.OpenKey(hkey, reg_path, 0, winreg.KEY_READ)
        filename = winreg.QueryValueEx(rk, font_name)[0]
        winreg.CloseKey(rk)

        rk = winreg.OpenKey(hkey, reg_path, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(rk, font_name)
        winreg.CloseKey(rk)

        font_path = Path(filename) if os.path.isabs(filename) else font_dir / filename
        if font_path.exists():
            ctypes.windll.gdi32.RemoveFontResourceW(str(font_path))
            font_path.unlink()

        ctypes.windll.user32.SendMessageW(0xFFFF, 0x001D, 0, 0)
        return {"success": True}
    except PermissionError:
        return {"success": False, "error": "Permission denied — run as Administrator"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ZIP scanning helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def find_fonts_in_zip(zip_bytes: bytes, source_name: str = ""):
    fonts = []

    def scan_zip(zf, prefix=""):
        for info in zf.infolist():
            name = info.filename
            ext  = Path(name).suffix.lower()
            if ext in FONT_EXTENSIONS:
                fonts.append({
                    "name":       Path(name).name,
                    "ext":        ext.lstrip(".").upper(),
                    "zip_path":   prefix + name,
                    "size_kb":    round(info.file_size / 1024, 1),
                    "source_zip": source_name,
                })
            elif ext == ".zip":
                try:
                    nested = zf.read(name)
                    with zipfile.ZipFile(io.BytesIO(nested)) as nzf:
                        scan_zip(nzf, prefix=prefix + name + "//")
                except Exception:
                    pass

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        scan_zip(zf)
    return fonts


def extract_font_bytes(zip_bytes: bytes, zip_path: str) -> bytes:
    """Extract a font file from a (possibly nested) ZIP."""
    if "//" in zip_path:
        outer, inner = zip_path.split("//", 1)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            nested = zf.read(outer)
        return extract_font_bytes(nested, inner)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.read(zip_path)


def zip_bytes_for(source_name: str):
    for name, zb in _zip_data_list:
        if name == source_name:
            return zb
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Font installation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _do_install(fonts_to_install: list, dest_dir: Path, use_registry_hkey):
    """
    Core install loop. Returns results dict.
    use_registry_hkey: winreg constant or None (user-only path skips HKLM).
    """
    if sys.platform != "win32":
        return {"success": [], "failed": [{"name": "–", "reason": "Windows only"}], "skipped": []}

    import ctypes, winreg

    dest_dir.mkdir(parents=True, exist_ok=True)
    reg_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    results  = {"success": [], "failed": [], "skipped": []}

    for font in fonts_to_install:
        zb = zip_bytes_for(font["source_zip"])
        if zb is None:
            results["failed"].append({"name": font["name"], "reason": "Source ZIP not in memory"})
            continue

        font_name = Path(font["zip_path"].replace("//", "/")).name
        dest      = dest_dir / font_name
        try:
            if dest.exists():
                results["skipped"].append(font_name)
                continue
            data = extract_font_bytes(zb, font["zip_path"])
            dest.write_bytes(data)

            try:
                rk = winreg.OpenKey(use_registry_hkey, reg_path, 0, winreg.KEY_SET_VALUE)
                reg_value = font_name if use_registry_hkey == winreg.HKEY_LOCAL_MACHINE else str(dest)
                winreg.SetValueEx(rk, font_name, 0, winreg.REG_SZ, reg_value)
                winreg.CloseKey(rk)
            except Exception:
                pass

            ctypes.windll.gdi32.AddFontResourceW(str(dest))
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x001D, 0, 0)
            results["success"].append(font_name)
        except PermissionError:
            results["failed"].append({"name": font_name, "reason": "Permission denied — run as Administrator"})
        except Exception as e:
            results["failed"].append({"name": font_name, "reason": str(e)})

    if results["success"]:
        sources = list({f["source_zip"] for f in fonts_to_install})
        log_install(results["success"], ", ".join(sources))

    return results


def install_fonts_windows(fonts_to_install: list):
    import winreg
    return _do_install(fonts_to_install, WINDOWS_FONTS_DIR, winreg.HKEY_LOCAL_MACHINE)


def install_fonts_fallback(fonts_to_install: list):
    import winreg
    user_dir = Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"
    return _do_install(fonts_to_install, user_dir, winreg.HKEY_CURRENT_USER)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Multipart parser helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_multipart_zips(content_type: str, body: bytes):
    """
    Returns list of (filename, zip_bytes) for every .zip part found.
    """
    import re  # Imported here for safety

    boundary = content_type.split("boundary=")[-1].strip().encode()
    parts    = body.split(b"--" + boundary)
    results  = []
    
    for part in parts:
        if b'filename=' not in part or b'.zip' not in part:
            continue
            
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
            
        headers_raw = part[:header_end].decode(errors="replace")
        data        = part[header_end + 4:].rstrip(b"\r\n--")
        
        # 100% Bulletproof Regex extraction
        # This looks specifically for: filename="EXACT_NAME.zip"
        match = re.search(r'filename="([^"]+)"', headers_raw)
        
        if match:
            fname = match.group(1)
        else:
            # Fallback just in case the browser doesn't send quotes
            match = re.search(r'filename=([^\s;]+)', headers_raw)
            fname = match.group(1) if match else "unknown.zip"
            
        results.append((fname, data))
        
    return results
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTML UI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>FontDrop</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@300;400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0a0a0f;
    --surface: #111118;
    --surface2: #18181f;
    --border: #2a2a35;
    --accent: #7c6dfa;
    --accent2: #fa6d9a;
    --accent3: #6dfabd;
    --text: #e8e8f0;
    --muted: #666680;
    --danger: #fa4d6d;
    --success: #4dfaa0;
    --warn: #facc4d;
  }

  [data-theme="light"] {
    --bg: #f5f5f7;
    --surface: #ffffff;
    --surface2: #f0f0f5;
    --border: #d1d1d6;
    --accent: #5e4ec2;
    --accent2: #c2356e;
    --accent3: #2ec28e;
    --text: #1d1d1f;
    --muted: #86868b;
    --danger: #d93025;
    --success: #34a853;
    --warn: #ea8600;
  }

  body {
    font-family: 'DM Mono', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
    transition: background 0.3s, color 0.3s;
  }

  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(124,109,250,0.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(124,109,250,0.04) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
    z-index: 0;
  }

  [data-theme="light"] body::before {
    background-image:
      linear-gradient(rgba(94,78,194,0.05) 1px, transparent 1px),
      linear-gradient(90deg, rgba(94,78,194,0.05) 1px, transparent 1px);
  }

  .container {
    max-width: 1100px;
    margin: 0 auto;
    padding: 30px 24px 80px;
    position: relative;
    z-index: 1;
  }

  /* ── Header ── */
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 32px;
    flex-wrap: wrap;
    gap: 16px;
  }
  .logo {
    font-family: 'Syne', sans-serif;
    font-size: 2.2rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  .tagline { color: var(--muted); font-size: 0.72rem; letter-spacing: 0.1em; margin-top: 2px; }

  .theme-toggle {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 4px;
    display: flex;
    gap: 4px;
  }
  .theme-btn {
    padding: 5px 12px;
    border-radius: 16px;
    font-size: 0.72rem;
    border: none;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    transition: all 0.2s;
    font-family: 'DM Mono', monospace;
  }
  .theme-btn.active { background: var(--accent); color: #fff; }

  /* ── Tabs ── */
  .tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 24px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0;
  }
  .tab {
    font-family: 'Syne', sans-serif;
    padding: 10px 20px;
    background: transparent;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 600;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: all 0.18s;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-badge {
    display: inline-block;
    background: var(--accent);
    color: #fff;
    font-size: 0.6rem;
    padding: 1px 6px;
    border-radius: 10px;
    margin-left: 6px;
    font-family: 'DM Mono', monospace;
  }

  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* ── Drop zone (Ethereal Aurora) ── */
  .drop-zone {
    border: 1px dashed var(--border);
    border-radius: 16px;
    padding: 50px 40px;
    text-align: center;
    background: var(--surface);
    position: relative;
    cursor: pointer;
    overflow: hidden;
    z-index: 1;
    transition: all 0.4s ease;
    margin-bottom: 0;
  }
  .drop-zone::before {
    content: ''; 
    position: absolute; 
    inset: -50%;
    /* Complex conic gradient that we will blur heavily */
    background: conic-gradient(from 180deg at 50% 50%, rgba(124,109,250,0.15) 0deg, rgba(250,109,154,0.15) 180deg, rgba(124,109,250,0.15) 360deg);
    filter: blur(60px);
    opacity: 0;
    transition: opacity 0.5s ease;
    animation: rotateSlow 10s linear infinite;
    z-index: 0; 
    pointer-events: none;
  }
  @keyframes rotateSlow { 100% { transform: rotate(360deg); } }

  .drop-zone:hover {
    border-style: solid;
    border-color: rgba(250, 109, 154, 0.4);
    background: transparent;
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
  }
  .drop-zone:hover::before { opacity: 1; }

  .drop-zone.drag-over {
    border-style: solid;
    border-color: var(--accent);
    background: transparent;
    transform: scale(1.012);
    box-shadow: 0 0 0 4px rgba(124,109,250,0.18);
  }
  .drop-zone.drag-over::before { opacity: 1; }
  .drop-zone.drag-over .drop-content { transform: translateY(-6px) scale(1.02); }

  /* Content wrapper for z-indexing above Aurora */
  .drop-content {
    position: relative; 
    z-index: 2; 
    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .drop-zone:hover .drop-content { transform: translateY(-4px); }

  .drop-icon {
    font-size: 2.8rem;
    margin-bottom: 14px;
    display: block;
    transition: transform 0.22s;
  }
  .drop-zone:hover .drop-icon { transform: scale(1.12); }
  
  .drop-title {
    font-family: 'Syne', sans-serif;
    font-size: 1.1rem;
    font-weight: 700;
    margin-bottom: 8px;
    transition: color 0.2s;
  }
  .drop-zone:hover .drop-title { color: var(--accent); }
  
  .drop-sub { color: var(--muted); font-size: 0.78rem; position: relative; z-index: 2; }
  .drop-sub strong { color: var(--accent); cursor: pointer; text-decoration: underline; }

  /* ✨ PREMIUM BADGE STYLING ✨ */
  .badge-multi {
    background: linear-gradient(135deg, rgba(255,255,255,0.03), rgba(255,255,255,0.01));
    border: 1px solid rgba(255, 255, 255, 0.06);
    color: var(--muted);
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 8px;
    font-size: 0.72rem;
    letter-spacing: 0.02em;
    margin-left: 6px;
    display: inline-block;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.05);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
  }
  
  .drop-zone:hover .badge-multi {
    background: linear-gradient(135deg, rgba(124, 109, 250, 0.9), rgba(250, 109, 154, 0.9));
    border-color: rgba(255, 255, 255, 0.25);
    color: #ffffff;
    box-shadow: 0 4px 16px rgba(124, 109, 250, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    transform: translateY(-2px) scale(1.02);
  }

  #zip-input { display: none; }

  .zip-chips {
    margin-top: 16px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: center;
  }
  .zip-chip {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 5px 12px;
    font-size: 0.72rem;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: border-color 0.15s, background 0.15s;
    position: relative;
  }
  .zip-chip:hover {
    border-color: var(--danger);
    background: rgba(250,77,109,0.06);
  }
  .zip-chip-remove {
    cursor: pointer;
    color: var(--danger);
    font-weight: bold;
    opacity: 0;
    transition: opacity 0.15s, transform 0.15s;
    font-size: 0.85rem;
    line-height: 1;
    padding: 0 2px;
    border-radius: 50%;
    transform: scale(0.7);
  }
  .zip-chip:hover .zip-chip-remove {
    opacity: 1;
    transform: scale(1);
  }

  /* ── Progress bar ── */
  .install-progress-wrap {
    display: none;
    margin-top: 18px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
  }
  .install-progress-wrap.visible { display: block; }
  .install-progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
    font-size: 0.76rem;
  }
  .install-progress-label { font-weight: 600; color: var(--text); }
  .install-progress-count { color: var(--accent); font-family: 'DM Mono', monospace; }
  .install-progress-track {
    height: 8px;
    background: var(--surface2);
    border-radius: 99px;
    overflow: hidden;
    border: 1px solid var(--border);
  }
  .install-progress-bar {
    height: 100%;
    width: 0%;
    border-radius: 99px;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    transition: width 0.25s ease;
    position: relative;
  }
  .install-progress-bar::after {
    content: '';
    position: absolute;
    top: 0; right: 0; bottom: 0;
    width: 40px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.25));
    animation: shimmer 1.2s linear infinite;
  }
  @keyframes shimmer {
    from { opacity: 0; transform: translateX(-20px); }
    50%  { opacity: 1; }
    to   { opacity: 0; transform: translateX(20px); }
  }
  .install-progress-status {
    margin-top: 8px;
    font-size: 0.68rem;
    color: var(--muted);
    min-height: 16px;
    transition: color 0.2s;
  }
  .install-progress-status.done { color: var(--success); }
  .zip-chips-bar {
    margin-top: 16px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
  }
  .zip-chips-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: center;
  }
  .btn-clear-zips {
    font-family: 'DM Mono', monospace;
    font-size: 0.68rem;
    padding: 4px 12px;
    border-radius: 8px;
    border: 1px solid var(--danger);
    background: transparent;
    color: var(--danger);
    cursor: pointer;
    transition: all 0.15s;
    display: none;
  }
  .btn-clear-zips.visible { display: inline-block; }
  .btn-clear-zips:hover { background: var(--danger); color: #fff; }

  /* ── Inline sticky preview panel ── */
  .inline-preview-panel {
    background: var(--surface2);
    border: 1px solid var(--accent);
    border-radius: 10px;
    padding: 14px 18px;
    margin: 4px 0;
    animation: fadeIn 0.15s ease;
  }
  .inline-preview-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
    gap: 8px;
    flex-wrap: wrap;
  }
  .inline-preview-name { font-size: 0.78rem; font-weight: 600; color: var(--accent); }
  .inline-preview-close {
    background: none; border: none; color: var(--muted);
    cursor: pointer; font-size: 1rem; padding: 0 4px;
  }
  .inline-preview-close:hover { color: var(--danger); }
  .inline-preview-text-input {
    font-family: 'DM Mono', monospace;
    font-size: 0.74rem;
    padding: 6px 10px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    outline: none;
    flex: 1;
    min-width: 160px;
  }
  .inline-preview-text-input:focus { border-color: var(--accent); }
  .inline-preview-display {
    font-size: 2rem;
    line-height: 1.45;
    padding: 16px;
    background: var(--surface);
    border-radius: 8px;
    word-break: break-word;
    min-height: 64px;
    border: 1px solid var(--border);
    margin-top: 10px;
  }
  .inline-preview-sizes {
    display: flex;
    gap: 5px;
    margin-top: 8px;
    flex-wrap: wrap;
    align-items: center;
  }
  .inline-size-btn {
    font-size: 0.65rem;
    padding: 2px 7px;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    cursor: pointer;
    transition: all 0.1s;
  }
  .inline-size-btn.active { border-color: var(--accent); color: var(--accent); }

  /* ── Hover preview tooltip on font row ── */
  .font-item { position: relative; }
  .hover-preview-tip {
    display: none;
    position: absolute;
    left: 60px; right: 80px;
    top: calc(100% + 2px);
    z-index: 50;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    pointer-events: none;
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    font-size: 1.4rem;
    line-height: 1.4;
    word-break: break-word;
  }
  .font-item:hover .hover-preview-tip { display: block; }

  /* ── Scanning ── */
  .scan-overlay { display: none; text-align: center; padding: 48px; }
  .scan-overlay.active { display: block; }
  .spinner {
    width: 38px; height: 38px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    margin: 0 auto 16px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Results panel ── */
  .results-panel { display: none; }
  .results-panel.visible { display: block; }

  .results-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
    flex-wrap: wrap;
    gap: 10px;
  }
  .results-info { font-family: 'Syne', sans-serif; font-size: 0.92rem; font-weight: 600; }
  .results-info span { color: var(--accent); }

  .toolbar {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }

  .btn {
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    padding: 6px 11px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--text);
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn-accent {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    font-weight: 500;
    padding: 8px 18px;
    font-size: 0.76rem;
  }
  .btn-accent:hover { background: #6a5ce0; border-color: #6a5ce0; color: #fff; }
  .btn-accent:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-ghost { border-color: transparent; background: transparent; color: var(--muted); }
  .btn-ghost:hover { color: var(--danger); border-color: var(--danger); }

  input[type=text], .search-box, .sort-select {
    font-family: 'DM Mono', monospace;
    font-size: 0.76rem;
    padding: 6px 11px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    outline: none;
    transition: border-color 0.15s;
  }
  input[type=text]:focus, .search-box:focus { border-color: var(--accent); }
  input[type=text]::placeholder, .search-box::placeholder { color: var(--muted); }
  .search-box { width: 155px; }
  .preview-input { width: 260px; }
  .sort-select { cursor: pointer; }

  /* ── Font list ── */
  .font-list-wrap {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 18px;
    max-height: 440px;
    overflow-y: auto;
  }
  .font-list-wrap::-webkit-scrollbar { width: 5px; }
  .font-list-wrap::-webkit-scrollbar-track { background: var(--surface); }
  .font-list-wrap::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .group-header {
    padding: 7px 16px;
    background: var(--surface2);
    color: var(--muted);
    font-size: 0.68rem;
    letter-spacing: 0.07em;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 2;
  }

  .font-item {
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 9px 16px;
    border-bottom: 1px solid rgba(42,42,53,0.3);
    cursor: pointer;
    transition: background 0.1s;
    user-select: none;
  }
  .font-item:last-child { border-bottom: none; }
  .font-item:hover { background: var(--surface2); }
  .font-item.checked { background: rgba(124,109,250,0.07); }

  .cb-box {
    width: 17px; height: 17px;
    border: 1.5px solid var(--border);
    border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    font-size: 10px;
    color: #fff;
    flex-shrink: 0;
    transition: all 0.1s;
  }
  .font-item.checked .cb-box { background: var(--accent); border-color: var(--accent); }

  .font-meta { flex: 1; min-width: 0; }
  .font-filename {
    font-size: 0.79rem;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .font-path {
    font-size: 0.66rem;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 1px;
  }

  .ext-badge {
    font-size: 0.62rem;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }
  .ext-TTF   { background: rgba(124,109,250,0.18); color: #a89bff; }
  .ext-OTF   { background: rgba(250,109,154,0.18); color: #ff9bbe; }
  .ext-WOFF  { background: rgba(109,250,189,0.18); color: #7affcc; }
  .ext-WOFF2 { background: rgba(250,204,77,0.18);  color: #ffd966; }
  .ext-other { background: rgba(102,102,128,0.18); color: var(--muted); }

  .font-size { font-size: 0.68rem; color: var(--muted); flex-shrink: 0; width: 50px; text-align: right; }

  .preview-btn {
    font-size: 0.68rem;
    padding: 3px 7px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    cursor: pointer;
    color: var(--muted);
    transition: all 0.15s;
    flex-shrink: 0;
  }
  .preview-btn:hover { border-color: var(--accent); color: var(--accent); }
  .preview-btn.active-eye { border-color: var(--accent); color: var(--accent); background: rgba(124,109,250,0.12); }

  /* ── Preview modal ── */
  .preview-modal {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    z-index: 1000;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .preview-modal.active { display: flex; }
  .preview-content {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    max-width: 700px;
    width: 100%;
    padding: 28px;
    position: relative;
    animation: fadeIn 0.15s ease;
  }
  @keyframes fadeIn { from { opacity:0; transform:scale(0.97); } to { opacity:1; transform:scale(1); } }
  .preview-close {
    position: absolute; top: 14px; right: 16px;
    font-size: 1.4rem; cursor: pointer; color: var(--muted);
    background: none; border: none;
  }
  .preview-close:hover { color: var(--text); }
  .preview-title {
    font-family: 'Syne', sans-serif;
    font-size: 1rem;
    font-weight: 700;
    margin-bottom: 6px;
  }
  .preview-sub { font-size: 0.68rem; color: var(--muted); margin-bottom: 16px; }
  .preview-text-input {
    width: 100%;
    margin-bottom: 12px;
    padding: 8px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 0.76rem;
    outline: none;
  }
  .preview-text-input:focus { border-color: var(--accent); }
  .preview-display {
    font-size: 2rem;
    line-height: 1.45;
    padding: 22px;
    background: var(--surface2);
    border-radius: 10px;
    word-break: break-word;
    min-height: 80px;
    border: 1px solid var(--border);
  }
  .preview-sizes {
    display: flex;
    gap: 6px;
    margin-top: 10px;
    flex-wrap: wrap;
  }
  .size-btn {
    font-size: 0.68rem;
    padding: 3px 9px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--muted);
    cursor: pointer;
  }
  .size-btn.active { border-color: var(--accent); color: var(--accent); }

  /* ── Bottom bar ── */
  .bottom-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  #selected-count { color: var(--muted); font-size: 0.74rem; }
  #selected-count strong { color: var(--accent); }

  /* ── Install result ── */
  .install-result { display: none; margin-top: 22px; }
  .install-result.visible { display: block; }
  .result-section {
    border-radius: 10px;
    padding: 13px 17px;
    margin-bottom: 9px;
    border: 1px solid transparent;
  }
  .result-section h3 {
    font-family: 'Syne', sans-serif;
    font-size: 0.82rem;
    font-weight: 700;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .success-section { background: rgba(77,250,160,0.05); border-color: rgba(77,250,160,0.2); }
  .success-section h3 { color: var(--success); }
  .skipped-section { background: rgba(250,204,77,0.05); border-color: rgba(250,204,77,0.2); }
  .skipped-section h3 { color: var(--warn); }
  .failed-section  { background: rgba(250,77,109,0.05); border-color: rgba(250,77,109,0.2); }
  .failed-section  h3 { color: var(--danger); }
  .result-list { list-style: none; font-size: 0.73rem; }
  .result-list li { padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .result-list li:last-child { border: none; }
  .result-list span { color: var(--danger); font-size: 0.68rem; }
  .note-box {
    margin-top: 10px;
    padding: 10px 15px;
    background: rgba(250,204,77,0.05);
    border: 1px solid rgba(250,204,77,0.2);
    border-radius: 8px;
    color: var(--warn);
    font-size: 0.71rem;
  }

  /* ── Installed tab ── */
  .installed-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }
  .installed-item:last-child { border-bottom: none; }
  .installed-item:hover { background: var(--surface2); }
  .installed-meta { flex: 1; min-width: 0; }
  .installed-name { font-size: 0.8rem; font-weight: 500; }
  .installed-location { font-size: 0.66rem; color: var(--muted); margin-top: 2px; }
  .location-system { color: var(--accent3); }
  .location-user   { color: var(--warn); }
  .uninstall-btn {
    font-size: 0.68rem;
    padding: 4px 10px;
    background: transparent;
    border: 1px solid var(--danger);
    color: var(--danger);
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s;
    flex-shrink: 0;
  }
  .uninstall-btn:hover { background: var(--danger); color: #fff; }

  /* ── History tab ── */
  .history-item {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 10px;
  }
  .history-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 7px;
  }
  .history-source { font-size: 0.78rem; font-weight: 600; color: var(--accent); }
  .history-date   { font-size: 0.68rem; color: var(--muted); }
  .history-fonts  { font-size: 0.71rem; color: var(--text); opacity: 0.75; line-height: 1.5; }

  .empty-state {
    padding: 48px;
    text-align: center;
    color: var(--muted);
    font-size: 0.8rem;
  }
</style>
</head>
<body data-theme="dark">
<div class="container">

  <div class="header">
    <div>
      <div class="logo">FontDrop</div>
      <div class="tagline">ZIP FONT INSTALLER · DESKTOP</div>
    </div>
    <div class="theme-toggle">
      <button class="theme-btn active" id="btn-dark"  onclick="setTheme('dark')">🌙 Dark</button>
      <button class="theme-btn"        id="btn-light" onclick="setTheme('light')">☀️ Light</button>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('install',this)">📦 Install</button>
    <button class="tab"        onclick="switchTab('installed',this)">
      🗂️ Installed <span class="tab-badge" id="installed-badge">…</span>
    </button>
    <button class="tab" onclick="switchTab('history',this)">📜 History</button>
  </div>

  <div class="tab-content active" id="install-tab">

    <div id="drop-zone" class="drop-zone">
      <div class="drop-content">
        <span class="drop-icon">🗜️</span>
        <div class="drop-title">Drop ZIP files here</div>
        <div class="drop-sub">or <strong id="browse-link">browse to select</strong> <span class="badge-multi">multiple ZIPs supported</span></div>
        <input type="file" id="zip-input" accept=".zip" multiple />
        <div class="zip-chips-bar" id="zip-chips-bar">
          <div class="zip-chips-row" id="zip-chips"></div>
          <button class="btn-clear-zips" id="btn-clear-zips" onclick="clearAllZips();event.stopPropagation();">🗑️ Clear All ZIPs</button>
        </div>
      </div>
    </div>

    <div id="scanning" class="scan-overlay">
      <div class="spinner"></div>
      <div>Scanning ZIP files for fonts…</div>
    </div>

    <div id="results" class="results-panel">
      <div class="results-header">
        <div class="results-info">Found <span id="font-count">0</span> fonts</div>
        <div class="toolbar">
          <input class="search-box" id="search-box" placeholder="Search…" oninput="filterFonts()" />
          <select class="sort-select" id="sort-select" onchange="renderList()">
            <option value="folder">Sort: Folder</option>
            <option value="name">Sort: Name</option>
            <option value="size">Sort: Size ↓</option>
            <option value="type">Sort: Type</option>
          </select>
          <button class="btn" onclick="selectAll()">All</button>
          <button class="btn" onclick="selectNone()">None</button>
          <button class="btn" onclick="selectByExt('TTF')">TTF</button>
          <button class="btn" onclick="selectByExt('OTF')">OTF</button>
        </div>
      </div>

      <div style="margin-bottom:12px; display:flex; gap:8px; align-items:center;">
        <span style="font-size:0.68rem; color:var(--muted); white-space:nowrap;">Preview text:</span>
        <input type="text" class="search-box preview-input" id="global-preview-text"
               style="flex:1; width:auto;"
               value="The quick brown fox jumps over the lazy dog"
               placeholder="Preview text…" />
      </div>

      <div class="font-list-wrap">
        <div id="font-list"></div>
      </div>

      <div class="bottom-bar">
        <div>
          <div id="selected-count">No fonts selected</div>
          <div style="margin-top:8px; display:flex; gap:6px; flex-wrap:wrap;">
            <button class="btn" onclick="exportList()">💾 Export List</button>
            <button class="btn btn-ghost" onclick="loadMoreZips()">+ Add More ZIPs</button>
          </div>
        </div>
        <button class="btn btn-accent" id="install-btn" onclick="installFonts()" disabled>⚡ Install Selected</button>
      </div>

      <div id="install-result" class="install-result"></div>

      <div class="install-progress-wrap" id="install-progress-wrap">
        <div class="install-progress-header">
          <span class="install-progress-label">⚡ Installing fonts…</span>
          <span class="install-progress-count" id="progress-count">0 / 0</span>
        </div>
        <div class="install-progress-track">
          <div class="install-progress-bar" id="progress-bar"></div>
        </div>
        <div class="install-progress-status" id="progress-status">Preparing…</div>
      </div>
    </div>
  </div><div class="tab-content" id="installed-tab">
    <div class="results-header">
      <div class="results-info">Installed: <span id="installed-count">…</span></div>
      <div class="toolbar">
        <input class="search-box" id="installed-search" placeholder="Search…" oninput="filterInstalled()" />
        <button class="btn" onclick="loadInstalledFonts()">🔄 Refresh</button>
      </div>
    </div>
    <div class="font-list-wrap" style="max-height:520px;">
      <div id="installed-list"><div class="empty-state">Loading…</div></div>
    </div>
  </div>

  <div class="tab-content" id="history-tab">
    <div class="results-header">
      <div class="results-info">Installation History</div>
      <button class="btn" onclick="clearHistory()">🗑️ Clear History</button>
    </div>
    <div id="history-list"><div class="empty-state">Loading…</div></div>
  </div>

</div><div class="preview-modal" id="preview-modal" onclick="closePreview(event)">
  <div class="preview-content" onclick="event.stopPropagation()">
    <button class="preview-close" onclick="closePreview()">×</button>
    <div class="preview-title" id="preview-font-name">—</div>
    <div class="preview-sub" id="preview-font-sub"></div>
    <input type="text" class="preview-text-input" id="preview-text-input"
           value="The quick brown fox jumps over the lazy dog"
           oninput="updatePreviewText()" />
    <div class="preview-display" id="preview-display">Loading…</div>
    <div class="preview-sizes">
      <span style="font-size:0.68rem; color:var(--muted); margin-right:4px;">Size:</span>
      <button class="size-btn" onclick="setPreviewSize(24)">24</button>
      <button class="size-btn active" onclick="setPreviewSize(32)">32</button>
      <button class="size-btn" onclick="setPreviewSize(48)">48</button>
      <button class="size-btn" onclick="setPreviewSize(64)">64</button>
      <button class="size-btn" onclick="setPreviewSize(96)">96</button>
    </div>
  </div>
</div>

<script>
// ════════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════════
let allFonts       = [];
let selectedFonts  = new Set();   // indices into allFonts
let searchQuery    = '';
let installedFonts = [];
let installedSearch= '';
let loadedZipNames = [];
let previewFontIdx = null;
let previewSize    = 32;
let previewFontLoaded = false;
let pinnedPreviewIdx  = null;   // index of font with open inline panel
let pinnedPanelHTML   = '';     // saved HTML to restore after re-render
let pinnedPanelSize   = 32;     // size chosen in open panel

// ════════════════════════════════════════════════════════════
// Theme
// ════════════════════════════════════════════════════════════
function setTheme(t) {
  document.body.setAttribute('data-theme', t);
  document.getElementById('btn-dark').classList.toggle('active',  t==='dark');
  document.getElementById('btn-light').classList.toggle('active', t==='light');
  localStorage.setItem('fd-theme', t);
}
(function(){ const t = localStorage.getItem('fd-theme'); if(t) setTheme(t); })();

// ════════════════════════════════════════════════════════════
// Tabs
// ════════════════════════════════════════════════════════════
function switchTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(name+'-tab').classList.add('active');
  if (name==='installed') loadInstalledFonts();
  if (name==='history')   loadHistory();
}

// ════════════════════════════════════════════════════════════
// Drag & drop / file pick
// ════════════════════════════════════════════════════════════
const dz = document.getElementById('drop-zone');
dz.addEventListener('dragover',  e=>{ e.preventDefault(); dz.classList.add('drag-over'); });
dz.addEventListener('dragleave', ()=> dz.classList.remove('drag-over'));
dz.addEventListener('drop', e=>{
  e.preventDefault(); dz.classList.remove('drag-over');
  const files = Array.from(e.dataTransfer.files).filter(f=>f.name.endsWith('.zip'));
  if (files.length) handleFiles(files);
});

// Clicking anywhere on the drop zone opens file picker,
// EXCEPT when clicking a chip remove button, the chip itself, or the clear-all button
dz.addEventListener('click', e => {
  if (e.target.closest('.zip-chip-remove') || e.target.closest('.zip-chip')) return;
  if (e.target.closest('#btn-clear-zips'))  return;
  document.getElementById('zip-input').value = '';
  document.getElementById('zip-input').click();
});

document.getElementById('zip-input').addEventListener('change', e=>{
  if (e.target.files.length) handleFiles(Array.from(e.target.files));
});

function loadMoreZips() {
  document.getElementById('zip-input').value='';
  document.getElementById('zip-input').click();
}

// ════════════════════════════════════════════════════════════
// Upload & scan (multi-ZIP)
// ════════════════════════════════════════════════════════════
async function handleFiles(files) {
  document.getElementById('scanning').classList.add('active');
  document.getElementById('results').classList.remove('visible');

  // add names (deduplicate)
  files.forEach(f=>{ if(!loadedZipNames.includes(f.name)) loadedZipNames.push(f.name); });
  renderZipChips();

  const form = new FormData();
  files.forEach(f => form.append('files', f));

  try {
    const res  = await fetch('/scan', { method:'POST', body:form });
    const data = await res.json();
    if (data.error) { alert('Scan error: '+data.error); resetInstall(); return; }

    // merge with existing fonts from other ZIPs
    allFonts = [...allFonts, ...data.fonts];
    // auto-select new fonts
    data.fonts.forEach((_,i) => selectedFonts.add(allFonts.length - data.fonts.length + i));

    document.getElementById('font-count').textContent = allFonts.length;
    renderList();
    updateCount();
    document.getElementById('scanning').classList.remove('active');
    document.getElementById('results').classList.add('visible');
  } catch(e) {
    alert('Upload failed: '+e.message);
    resetInstall();
  }
}

function renderZipChips() {
  const container = document.getElementById('zip-chips');
  container.innerHTML = loadedZipNames.map(n => `
    <div class="zip-chip">
      📦 <span>${escHtml(n)}</span>
      <span class="zip-chip-remove" 
            onclick="event.stopPropagation(); removeZip('${escAttr(n)}');" 
            title="Remove this ZIP">✕</span>
    </div>
  `).join('');
  
  const btn = document.getElementById('btn-clear-zips');
  btn.classList.toggle('visible', loadedZipNames.length > 0);
}

function removeZip(name) {
  // 1. Remove from the name tracking list
  loadedZipNames = loadedZipNames.filter(n => n !== name);

  // 2. Notify backend to clear memory
  fetch('/remove-zip', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name})
  });

  // 3. Filter fonts and RE-MAP the selection indices
  const newFonts = [];
  const oldToNewMap = {};

  allFonts.forEach((font, oldIdx) => {
    if (font.source_zip !== name) {
      oldToNewMap[oldIdx] = newFonts.length;
      newFonts.push(font);
    }
  });

  const newSelected = new Set();
  selectedFonts.forEach(oldIdx => {
    if (oldToNewMap[oldIdx] !== undefined) {
      newSelected.add(oldToNewMap[oldIdx]);
    }
  });

  // 4. Update state
  allFonts = newFonts;
  selectedFonts = newSelected;

  // 5. Reset pinned preview if it belonged to the removed ZIP
  if (pinnedPreviewIdx !== null && allFonts[pinnedPreviewIdx]?.source_zip === name) {
    pinnedPreviewIdx = null;
    pinnedPanelHTML = '';
  }

  // 6. Refresh UI
  document.getElementById('font-count').textContent = allFonts.length;
  renderZipChips();
  renderList();
  updateCount();

  if (allFonts.length === 0) {
    document.getElementById('results').classList.remove('visible');
  }
}

function clearAllZips() {
  if (!confirm('Remove all loaded ZIPs and reset?')) return;
  resetInstall();
}

// ════════════════════════════════════════════════════════════
// Render font list
// ════════════════════════════════════════════════════════════
function getVisible() {
  const q = searchQuery.toLowerCase();
  return allFonts.map((f,i)=>({...f, _idx:i}))
    .filter(f => !q || f.name.toLowerCase().includes(q) || f.zip_path.toLowerCase().includes(q));
}

function renderList() {
  const visible  = getVisible();
  const sortBy   = document.getElementById('sort-select').value;

  let sorted = [...visible];
  if (sortBy==='name') sorted.sort((a,b)=>a.name.localeCompare(b.name));
  if (sortBy==='size') sorted.sort((a,b)=>b.size_kb-a.size_kb);
  if (sortBy==='type') sorted.sort((a,b)=>a.ext.localeCompare(b.ext));

  // Group
  const groups = {};
  sorted.forEach(f=>{
    let key;
    if (sortBy==='folder') {
      const folder = f.zip_path.includes('/') ? f.zip_path.split('/').slice(0,-1).join('/') : '(root)';
      key = '📦 '+f.source_zip + (folder!=='(root)' ? ' › '+folder : '');
    } else if (sortBy==='type') {
      key = f.ext;
    } else if (sortBy==='size') {
      key = f.size_kb>=100 ? 'Large (≥100 KB)' : 'Small (<100 KB)';
    } else {
      key = '📦 '+f.source_zip;
    }
    (groups[key]=groups[key]||[]).push(f);
  });

  let html = '';
  for (const [group, fonts] of Object.entries(groups)) {
    html += `<div class="group-header">${group} · ${fonts.length}</div>`;
    fonts.forEach(f=>{
      const sel = selectedFonts.has(f._idx);
      const extCls = ['TTF','OTF','WOFF','WOFF2'].includes(f.ext) ? f.ext : 'other';
      const isOpen = pinnedPreviewIdx === f._idx;
      html += `
        <div class="font-item ${sel?'checked':''}" onclick="toggleFont(${f._idx})" data-idx="${f._idx}">
          <div class="cb-box">${sel?'✓':''}</div>
          <div class="font-meta">
            <div class="font-filename">${escHtml(f.name)}</div>
            <div class="font-path">${escHtml(f.zip_path)} · ${escHtml(f.source_zip)}</div>
          </div>
          <span class="ext-badge ext-${extCls}">${f.ext}</span>
          <div class="font-size">${f.size_kb} KB</div>
          <button class="preview-btn ${isOpen?'active-eye':''}" onclick="togglePinnedPreview(${f._idx});event.stopPropagation();" title="Pin preview">👁️</button>
          <div class="hover-preview-tip" id="hpt-${f._idx}">Loading…</div>
        </div>
        <div id="pinned-panel-${f._idx}" style="display:${isOpen?'block':'none'}; padding:0 16px;"></div>`;
    });
  }
  if (!html) html = '<div class="empty-state">No fonts match your search</div>';
  document.getElementById('font-list').innerHTML = html;

  // Restore open pinned panel after re-render
  if (pinnedPreviewIdx !== null) {
    const panel = document.getElementById(`pinned-panel-${pinnedPreviewIdx}`);
    if (panel && pinnedPanelHTML) {
      panel.style.display = 'block';
      panel.innerHTML = pinnedPanelHTML;
      restoreInlinePanelEvents(pinnedPreviewIdx);
    }
  }

  // Wire hover tips
  document.querySelectorAll('.font-item').forEach(item => {
    const idx = parseInt(item.dataset.idx);
    let hoverLoaded = false;
    item.addEventListener('mouseenter', async () => {
      const tip = document.getElementById(`hpt-${idx}`);
      if (!tip) return;
      const previewText = document.getElementById('global-preview-text').value || 'Aa Bb Cc';
      if (!hoverLoaded) {
        tip.textContent = '⏳';
        const f = allFonts[idx];
        try {
          const family = `FDHover_${idx}`;
          if (![...document.fonts].some(ff=>ff.family===family)) {
            const res  = await fetch(`/preview?source=${encodeURIComponent(f.source_zip)}&path=${encodeURIComponent(f.zip_path)}`);
            const data = await res.json();
            if (!data.error) {
              const ff = new FontFace(family, `url(data:font/ttf;base64,${data.base64})`);
              await ff.load();
              document.fonts.add(ff);
            }
          }
          tip.style.fontFamily = `FDHover_${idx}, sans-serif`;
          hoverLoaded = true;
        } catch(_) {}
      }
      tip.textContent = previewText;
    });
  });
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s) { 
  return s.replace(/'/g,"\\'").replace(/"/g,'&quot;'); 
}

function toggleFont(idx) {
  selectedFonts.has(idx) ? selectedFonts.delete(idx) : selectedFonts.add(idx);
  renderList(); updateCount();
}
function selectAll()  { getVisible().forEach(f=>selectedFonts.add(f._idx)); renderList(); updateCount(); }
function selectNone() { getVisible().forEach(f=>selectedFonts.delete(f._idx)); renderList(); updateCount(); }
function selectByExt(ext) { getVisible().filter(f=>f.ext===ext).forEach(f=>selectedFonts.add(f._idx)); renderList(); updateCount(); }
function filterFonts() { searchQuery = document.getElementById('search-box').value; renderList(); }

function updateCount() {
  const n = selectedFonts.size;
  document.getElementById('selected-count').innerHTML =
    n===0 ? 'No fonts selected' : `<strong>${n}</strong> font${n>1?'s':''} selected`;
  document.getElementById('install-btn').disabled = n===0;
}

// ════════════════════════════════════════════════════════════
// Inline sticky preview panel (eye button)
// ════════════════════════════════════════════════════════════
async function togglePinnedPreview(idx) {
  if (pinnedPreviewIdx === idx) {
    // Close it
    pinnedPreviewIdx = null;
    pinnedPanelHTML  = '';
    renderList();
    return;
  }
  pinnedPreviewIdx = idx;
  pinnedPanelSize  = 32;

  renderList(); // re-render so panel slot appears

  const panel = document.getElementById(`pinned-panel-${idx}`);
  if (!panel) return;
  const font = allFonts[idx];

  panel.innerHTML = `
    <div class="inline-preview-panel" id="ipp-${idx}">
      <div class="inline-preview-header">
        <span class="inline-preview-name">👁️ ${escHtml(font.name)}</span>
        <div style="display:flex;gap:8px;flex:1;margin-left:12px;">
          <input class="inline-preview-text-input" id="ipt-${idx}"
                 value="${escHtml(document.getElementById('global-preview-text').value || 'The quick brown fox')}"
                 placeholder="Type preview text…" oninput="updateInlinePreview(${idx})" />
        </div>
        <button class="inline-preview-close" onclick="closePinnedPreview()" title="Close">✕</button>
      </div>
      <div class="inline-preview-display" id="ipd-${idx}">⏳ Loading…</div>
      <div class="inline-preview-sizes">
        <span style="font-size:0.65rem;color:var(--muted);margin-right:4px;">Size:</span>
        ${[16,24,32,48,64,96].map(s=>`<button class="inline-size-btn${s===32?' active':''}" onclick="setInlineSize(${idx},${s})">${s}</button>`).join('')}
      </div>
    </div>`;

  pinnedPanelHTML = panel.innerHTML;
  restoreInlinePanelEvents(idx);

  // load font
  const display = document.getElementById(`ipd-${idx}`);
  try {
    const family = `FDHover_${idx}`;
    if (![...document.fonts].some(ff=>ff.family===family)) {
      const res  = await fetch(`/preview?source=${encodeURIComponent(font.source_zip)}&path=${encodeURIComponent(font.zip_path)}`);
      const data = await res.json();
      if (data.error) { display.textContent = '⚠ Preview unavailable'; return; }
      const ff = new FontFace(family, `url(data:font/ttf;base64,${data.base64})`);
      await ff.load();
      document.fonts.add(ff);
    }
    display.style.fontFamily = `FDHover_${idx}, sans-serif`;
    display.style.fontSize   = pinnedPanelSize + 'px';
    display.textContent = document.getElementById(`ipt-${idx}`)?.value || 'The quick brown fox';
  } catch(e) {
    display.textContent = '⚠ ' + e.message;
  }
}

function restoreInlinePanelEvents(idx) {
  // After innerHTML is set, wire up live events that were lost
  const inp = document.getElementById(`ipt-${idx}`);
  if (inp) {
    inp.addEventListener('input', () => updateInlinePreview(idx));
    inp.onclick = e => e.stopPropagation();
  }
  const panel = document.getElementById(`ipp-${idx}`);
  if (panel) panel.onclick = e => e.stopPropagation();
}

function updateInlinePreview(idx) {
  const display = document.getElementById(`ipd-${idx}`);
  const input   = document.getElementById(`ipt-${idx}`);
  if (!display || !input) return;
  display.textContent = input.value || 'Type something…';
  // keep pinnedPanelHTML fresh so re-render restores the latest text
  const panel = document.getElementById(`pinned-panel-${idx}`);
  if (panel) pinnedPanelHTML = panel.innerHTML;
}

function setInlineSize(idx, s) {
  pinnedPanelSize = s;
  const display = document.getElementById(`ipd-${idx}`);
  if (display) display.style.fontSize = s + 'px';
  document.querySelectorAll(`#ipp-${idx} .inline-size-btn`).forEach(b=>{
    b.classList.toggle('active', parseInt(b.textContent)===s);
  });
  const panel = document.getElementById(`pinned-panel-${idx}`);
  if (panel) pinnedPanelHTML = panel.innerHTML;
}

function closePinnedPreview() {
  pinnedPreviewIdx = null;
  pinnedPanelHTML  = '';
  renderList();
}

// ════════════════════════════════════════════════════════════
// Font preview modal (kept for legacy / fallback)
// ════════════════════════════════════════════════════════════
async function openPreview(idx) {
  previewFontIdx = idx;
  const font = allFonts[idx];
  document.getElementById('preview-font-name').textContent = font.name;
  document.getElementById('preview-font-sub').textContent  = `${font.ext} · ${font.size_kb} KB · ${font.source_zip}`;
  document.getElementById('preview-text-input').value = document.getElementById('global-preview-text').value;

  const display = document.getElementById('preview-display');
  display.textContent = 'Loading preview…';
  display.style.fontFamily = 'inherit';
  previewFontLoaded = false;

  document.getElementById('preview-modal').classList.add('active');

  try {
    const res  = await fetch(`/preview?source=${encodeURIComponent(font.source_zip)}&path=${encodeURIComponent(font.zip_path)}`);
    const data = await res.json();
    if (data.error) { display.textContent = '⚠ Preview not available: '+data.error; return; }

    // Remove any previously loaded preview font
    document.fonts.forEach(f=>{ if(f.family==='FDPreview') { try{document.fonts.delete(f);}catch(_){} } });

    const ff = new FontFace('FDPreview', `url(data:font/ttf;base64,${data.base64})`);
    await ff.load();
    document.fonts.add(ff);

    previewFontLoaded = true;
    display.style.fontFamily = 'FDPreview, sans-serif';
    display.style.fontSize   = previewSize+'px';
    display.textContent = document.getElementById('preview-text-input').value;
  } catch(e) {
    display.textContent = '⚠ Preview error: '+e.message;
  }
}

function closePreview(e) {
  if (!e || e.target.id==='preview-modal') {
    document.getElementById('preview-modal').classList.remove('active');
  }
}

function updatePreviewText() {
  if (!previewFontLoaded) return;
  document.getElementById('preview-display').textContent =
    document.getElementById('preview-text-input').value || 'Type something above…';
}

function setPreviewSize(s) {
  previewSize = s;
  document.querySelectorAll('.size-btn').forEach(b=>{
    b.classList.toggle('active', parseInt(b.textContent)===s);
  });
  document.getElementById('preview-display').style.fontSize = s+'px';
}

// ════════════════════════════════════════════════════════════
// Export list
// ════════════════════════════════════════════════════════════
function exportList() {
  const sel = Array.from(selectedFonts).map(i=>allFonts[i]);
  let txt = 'FontDrop Export — '+new Date().toLocaleString()+'\n\n';
  sel.forEach(f=>{ txt += `${f.name}\t${f.ext}\t${f.size_kb} KB\t${f.source_zip}/${f.zip_path}\n`; });
  const blob = new Blob([txt], {type:'text/plain'});
  const url  = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement('a'), {href:url, download:'fontdrop_export.txt'});
  a.click();
  URL.revokeObjectURL(url);
}

// ════════════════════════════════════════════════════════════
// Install — with progress bar
// ════════════════════════════════════════════════════════════
async function installFonts() {
  if (!selectedFonts.size) return;
  const btn      = document.getElementById('install-btn');
  const wrap     = document.getElementById('install-progress-wrap');
  const bar      = document.getElementById('progress-bar');
  const countEl  = document.getElementById('progress-count');
  const statusEl = document.getElementById('progress-status');

  const total    = selectedFonts.size;
  const fontsArr = Array.from(selectedFonts).map(i => allFonts[i]);

  // Show progress UI
  btn.disabled = true;
  btn.textContent = '⏳ Installing…';
  document.getElementById('install-result').classList.remove('visible');
  wrap.classList.add('visible');
  document.getElementById('install-progress-label') && (document.querySelector('.install-progress-label').textContent = `⚡ Installing ${total} font${total>1?'s':''}…`);
  bar.style.width = '0%';
  countEl.textContent = `0 / ${total}`;
  statusEl.className = 'install-progress-status';
  statusEl.textContent = 'Sending to installer…';

  // Animate bar to 15% immediately to show activity
  setTimeout(() => { bar.style.width = '15%'; }, 50);

  try {
    // Simulate per-font progress via chunked installs (batches of 5)
    const batchSize = 5;
    let installed = 0;
    let allResults = { success: [], skipped: [], failed: [] };

    for (let i = 0; i < fontsArr.length; i += batchSize) {
      const batch = fontsArr.slice(i, i + batchSize);
      statusEl.textContent = `Installing: ${batch[0].name}${batch.length > 1 ? ` + ${batch.length-1} more` : ''}…`;

      const res  = await fetch('/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fonts: batch })
      });
      const data = await res.json();

      if (data.success)  allResults.success.push(...data.success);
      if (data.skipped)  allResults.skipped.push(...data.skipped);
      if (data.failed)   allResults.failed.push(...data.failed);
      if (data.note && !allResults.note) allResults.note = data.note;
      if (data.error)    allResults.error = data.error;

      installed = Math.min(i + batchSize, fontsArr.length);
      const pct = Math.round((installed / total) * 100);
      bar.style.width = Math.max(pct, 18) + '%';
      countEl.textContent = `${Math.min(installed, total)} / ${total}`;
    }

    // Finish
    bar.style.width = '100%';
    countEl.textContent = `${total} / ${total}`;
    const ok = allResults.success.length, sk = allResults.skipped.length, fl = allResults.failed.length;
    statusEl.className = 'install-progress-status done';
    statusEl.textContent = `✅ Done — ${ok} installed${sk ? `, ${sk} skipped` : ''}${fl ? `, ${fl} failed` : ''}`;

    setTimeout(() => {
      wrap.classList.remove('visible');
      bar.style.width = '0%';
    }, 3200);

    showInstallResult(allResults);
    loadInstalledFonts();

  } catch(e) {
    wrap.classList.remove('visible');
    alert('Install error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '⚡ Install Selected';
  }
}

function showInstallResult(data) {
  let html = '';
  if (data.success?.length) html += `
    <div class="result-section success-section">
      <h3>✅ Installed (${data.success.length})</h3>
      <ul class="result-list">${data.success.map(n=>`<li>${escHtml(n)}</li>`).join('')}</ul>
    </div>`;
  if (data.skipped?.length) html += `
    <div class="result-section skipped-section">
      <h3>⏭️ Already Installed (${data.skipped.length})</h3>
      <ul class="result-list">${data.skipped.map(n=>`<li>${escHtml(n)}</li>`).join('')}</ul>
    </div>`;
  if (data.failed?.length) html += `
    <div class="result-section failed-section">
      <h3>❌ Failed (${data.failed.length})</h3>
      <ul class="result-list">${data.failed.map(f=>`<li>${escHtml(f.name)} <span>— ${escHtml(f.reason)}</span></li>`).join('')}</ul>
    </div>`;
  if (data.error) html += `<div class="result-section failed-section"><h3>⚠️ Error</h3><p>${escHtml(data.error)}</p></div>`;
  if (data.note)  html += `<div class="note-box">ℹ️ ${escHtml(data.note)}</div>`;
  const el = document.getElementById('install-result');
  el.innerHTML = html; el.classList.add('visible');
  el.scrollIntoView({behavior:'smooth', block:'start'});
}

// ════════════════════════════════════════════════════════════
// Installed tab
// ════════════════════════════════════════════════════════════
async function loadInstalledFonts() {
  try {
    const res  = await fetch('/installed');
    const data = await res.json();
    installedFonts = data.fonts || [];
    const n = installedFonts.length;
    document.getElementById('installed-count').textContent = n;
    document.getElementById('installed-badge').textContent = n;
    renderInstalledList();
  } catch(e) {
    document.getElementById('installed-list').innerHTML =
      '<div class="empty-state">Failed to load installed fonts</div>';
  }
}

function renderInstalledList() {
  const q  = installedSearch.toLowerCase();
  const fl = installedFonts.filter(f=>!q||f.name.toLowerCase().includes(q)||f.filename.toLowerCase().includes(q));
  const html = fl.map(f=>`
    <div class="installed-item">
      <div class="installed-meta">
        <div class="installed-name">${escHtml(f.name)}</div>
        <div class="installed-location">
          <span class="location-${f.location}">${f.location==='system'?'🔒 System':'👤 User'}</span>
          · ${escHtml(f.filename)}
        </div>
      </div>
      <button class="uninstall-btn" onclick="uninstallFont('${escAttr(f.name)}','${f.location}')">Uninstall</button>
    </div>`).join('');
  document.getElementById('installed-list').innerHTML =
    html || '<div class="empty-state">No fonts found</div>';
}

function filterInstalled() {
  installedSearch = document.getElementById('installed-search').value;
  renderInstalledList();
}


async function uninstallFont(name, location) {
  if (!confirm(`Uninstall "${name}"?`)) return;
  try {
    const res  = await fetch('/uninstall', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, location})
    });
    const data = await res.json();
    if (data.success) {
      loadInstalledFonts();
    } else {
      alert('Uninstall failed: '+(data.error||'unknown error'));
    }
  } catch(e) { alert('Error: '+e.message); }
}

// ════════════════════════════════════════════════════════════
// History tab
// ════════════════════════════════════════════════════════════
async function loadHistory() {
  try {
    const res  = await fetch('/history');
    const data = await res.json();
    const history = (data.history||[]).slice().reverse();
    const html = history.map(e=>`
      <div class="history-item">
        <div class="history-header">
          <div class="history-source">📦 ${escHtml(e.source)}</div>
          <div class="history-date">${new Date(e.timestamp).toLocaleString()}</div>
        </div>
        <div class="history-fonts">
          ${e.count} font${e.count>1?'s':''}: ${e.fonts.slice(0,6).map(escHtml).join(', ')}${e.fonts.length>6?' …':''}
        </div>
      </div>`).join('');
    document.getElementById('history-list').innerHTML =
      html || '<div class="empty-state">No installation history yet</div>';
  } catch(e) {
    document.getElementById('history-list').innerHTML =
      '<div class="empty-state">Failed to load history</div>';
  }
}

async function clearHistory() {
  if (!confirm('Clear all installation history?')) return;
  await fetch('/history/clear', {method:'POST'});
  loadHistory();
}

// ════════════════════════════════════════════════════════════
// Reset install tab
// ════════════════════════════════════════════════════════════
function resetInstall() {
  loadedZipNames = []; allFonts = []; selectedFonts.clear(); searchQuery='';
  pinnedPreviewIdx = null; pinnedPanelHTML = '';
  document.getElementById('zip-chips').innerHTML  = '';
  const btn = document.getElementById('btn-clear-zips');
  if (btn) btn.classList.remove('visible');
  document.getElementById('scanning').classList.remove('active');
  document.getElementById('results').classList.remove('visible');
  document.getElementById('install-result').classList.remove('visible');
  document.getElementById('zip-input').value = '';
}

// ════════════════════════════════════════════════════════════
// Init
// ════════════════════════════════════════════════════════════
loadInstalledFonts();
</script>
</body>
</html>
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTTP handler — ALL routes live here
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # silence server logs

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path in ('/', '/index.html'):
            self._html(HTML_PAGE.encode())
            return

        if path == '/installed':
            fonts = list_installed_fonts()
            self._json({"fonts": fonts})
            return

        if path == '/history':
            self._json({"history": get_install_history()})
            return

        if path == '/preview':
            source = params.get('source', [''])[0]
            fpath  = params.get('path',   [''])[0]
            zb = zip_bytes_for(source)
            if not zb:
                self._json({"error": "ZIP not loaded"}, 404)
                return
            try:
                data = extract_font_bytes(zb, fpath)
                b64  = base64.b64encode(data).decode()
                self._json({"base64": b64})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        self._json({"error": "Not found"}, 404)

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        global _zip_data_list, _found_fonts
        parsed = urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        # ── /scan ─────────────────────────────────────────────────────────────
        if path == '/scan':
            ct    = self.headers.get('Content-Type', '')
            pairs = parse_multipart_zips(ct, body)
            if not pairs:
                self._json({"error": "No ZIP files found in upload"}, 400)
                return
            all_fonts = []
            for fname, zb in pairs:
                # Store / update in global list
                _zip_data_list = [(n, d) for n, d in _zip_data_list if n != fname]
                _zip_data_list.append((fname, zb))
                try:
                    fonts = find_fonts_in_zip(zb, fname)
                    all_fonts.extend(fonts)
                except Exception as e:
                    self._json({"error": f"Error scanning {fname}: {e}"}, 500)
                    return
            _found_fonts = all_fonts
            self._json({"fonts": all_fonts, "count": len(all_fonts)})
            return

        # ── /install ──────────────────────────────────────────────────────────
        if path == '/install':
            try:
                payload = json.loads(body)
            except Exception:
                self._json({"error": "Invalid JSON"}, 400)
                return
            fonts = payload.get('fonts', [])
            if not fonts:
                self._json({"error": "No fonts specified"}, 400)
                return
            if sys.platform != 'win32':
                self._json({"error": "Font installation is Windows-only."})
                return
            try:
                results = install_fonts_windows(fonts)
            except PermissionError:
                results = install_fonts_fallback(fonts)
                results['note'] = ('Installed to user fonts folder (no admin rights). '
                                   'Fonts will be active after you log out and back in.')
            except Exception as e:
                if 'Access' in str(e) or 'Permission' in str(e):
                    results = install_fonts_fallback(fonts)
                    results['note'] = ('Installed to user fonts folder (no admin rights). '
                                       'Fonts will be active after you log out and back in.')
                else:
                    self._json({"error": str(e)}, 500)
                    return
            self._json(results)
            return

        # ── /uninstall ────────────────────────────────────────────────────────
        if path == '/uninstall':
            try:
                payload = json.loads(body)
            except Exception:
                self._json({"error": "Invalid JSON"}, 400)
                return
            result = uninstall_font(payload.get('name', ''), payload.get('location', 'user'))
            self._json(result)
            return

        # ── /history/clear ────────────────────────────────────────────────────
        if path == '/history/clear':
            if _history_file.exists():
                _history_file.write_text('[]', encoding='utf-8')
            self._json({"ok": True})
            return

        # ── /remove-zip ───────────────────────────────────────────────────────
        if path == '/remove-zip':
            try:
                payload = json.loads(body)
            except Exception:
                self._json({"error": "Invalid JSON"}, 400)
                return
            name = payload.get('name', '')
            _zip_data_list[:] = [(n, d) for n, d in _zip_data_list if n != name]
            self._json({"ok": True})
            return

        self._json({"error": "Not found"}, 404)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def start_server(port: int) -> HTTPServer:
    server = HTTPServer(('127.0.0.1', port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def main():
    try:
        import webview
    except ImportError:
        print("pywebview not installed. Run:  pip install pywebview")
        sys.exit(1)

    PORT = 7432
    start_server(PORT)
    time.sleep(0.5)   # let server bind before the window opens

    webview.create_window(
        title      = "FontDrop",
        url        = f"http://127.0.0.1:{PORT}",
        width      = 1020,
        height     = 740,
        min_size   = (680, 500),
        resizable  = True,
        fullscreen = True,   # launches in true fullscreen; user can press F11 to toggle
    )
    webview.start()


if __name__ == '__main__':
    main()