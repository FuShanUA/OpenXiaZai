#!/usr/bin/env python3
"""OpenXiaZai 打包脚本 — 支持 macOS 和 Windows。
macOS:  pip install pyinstaller && python build.py mac
Win:    pip install pyinstaller && python build.py win
"""

import os, sys, shutil, subprocess, platform

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
NAME = "OpenXiaZai"
ICON_MAC = os.path.join(ROOT, "app_icon.icns")
ICON_WIN = os.path.join(ROOT, "assets", "icon_256.png")  # PyInstaller 支持 png 转 ico


def ensure_pyinstaller():
    try:
        import PyInstaller
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def clean():
    for d in [BUILD, DIST]:
        if os.path.exists(d):
            shutil.rmtree(d)


def find_binary(name):
    """查找二进制文件路径，优先同目录，其次系统 PATH。"""
    local = os.path.join(ROOT, name)
    if os.path.exists(local):
        return local
    p = shutil.which(name)
    if p:
        return p
    # macOS Homebrew 常见路径
    for prefix in ["/opt/homebrew/bin", "/usr/local/bin"]:
        cand = os.path.join(prefix, name)
        if os.path.exists(cand):
            return cand
    return None


def build_mac():
    print("=" * 50)
    print("  构建 macOS .app + .dmg")
    print("=" * 50)

    aria2 = find_binary("aria2c")
    ffmpeg = find_binary("ffmpeg")
    if not aria2:
        print("错误: 未找到 aria2c，请先 brew install aria2")
        sys.exit(1)
    if not ffmpeg:
        print("警告: 未找到 ffmpeg，m3u8 下载将不可用")

    clean()

    # 生成 spec 文件
    spec = f"""# -*- mode: python ; coding: utf-8 -*-
import os, sys
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['{os.path.join(ROOT, "launcher.py")}'],
    pathex=['{ROOT}'],
    binaries=[{f"('{aria2}', '.')"},{f"('{ffmpeg}', '.')" if ffmpeg else ""}],
    datas=[
        ('{os.path.join(ROOT, "templates")}', 'templates'),
        ('{os.path.join(ROOT, "static")}', 'static'),
    ],
    hiddenimports=['flask', 'webview', 'requests', 'werkzeug', 'jinja2', 'markupsafe'],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PIL', 'scipy'],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='{NAME}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='{ICON_MAC}',
)

app = BUNDLE(
    exe,
    name='{NAME}.app',
    icon='{ICON_MAC}',
    bundle_identifier='com.codex.openxiazai',
    info_plist={{
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.13',
        'CFBundleShortVersionString': '1.0',
        'CFBundleVersion': '1.0',
    }},
)
"""
    spec_path = os.path.join(ROOT, "mac.spec")
    with open(spec_path, "w") as f:
        f.write(spec)

    subprocess.check_call([sys.executable, "-m", "PyInstaller", spec_path, "--noconfirm"], cwd=ROOT)

    # 验证产物
    app_path = os.path.join(DIST, f"{NAME}.app")
    exe_path = os.path.join(app_path, "Contents", "MacOS", NAME)
    if not os.path.exists(exe_path):
        print("错误: 构建失败")
        sys.exit(1)

    # 创建 dmg
    print("创建 DMG…")
    dmg_path = os.path.join(DIST, f"{NAME}.dmg")
    subprocess.check_call([
        "hdiutil", "create", "-volname", NAME,
        "-srcfolder", app_path, "-ov", "-format", "UDZO", dmg_path,
    ])

    size_mb = os.path.getsize(dmg_path) / 1024 / 1024
    print(f"✅ 构建完成: {dmg_path} ({size_mb:.1f} MB)")
    # 清理 spec
    os.remove(spec_path)


def build_win():
    print("=" * 50)
    print("  构建 Windows .exe")
    print("=" * 50)

    clean()

    # 生成 spec（Windows 不需要 BUNDLE，直接出 exe 即可）
    spec = f"""# -*- mode: python ; coding: utf-8 -*-
import os, sys

a = Analysis(
    ['{os.path.join(ROOT, "launcher.py")}'],
    pathex=['{ROOT}'],
    binaries=[],
    datas=[
        ('{os.path.join(ROOT, "templates")}', 'templates'),
        ('{os.path.join(ROOT, "static")}', 'static'),
    ],
    hiddenimports=['flask', 'webview', 'requests', 'werkzeug', 'jinja2', 'markupsafe'],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PIL', 'scipy'],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='{NAME}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='{ICON_WIN}',
)
"""
    spec_path = os.path.join(ROOT, "win.spec")
    with open(spec_path, "w") as f:
        f.write(spec)

    subprocess.check_call([sys.executable, "-m", "PyInstaller", spec_path, "--noconfirm"], cwd=ROOT)

    exe_path = os.path.join(DIST, f"{NAME}.exe")
    if not os.path.exists(exe_path):
        print("错误: 构建失败")
        sys.exit(1)

    size_mb = os.path.getsize(exe_path) / 1024 / 1024
    print(f"✅ 构建完成: {exe_path} ({size_mb:.1f} MB)")
    print("提示: Windows 用户需自行安装 aria2c 和 ffmpeg 并加入 PATH")
    os.remove(spec_path)


if __name__ == "__main__":
    ensure_pyinstaller()
    target = sys.argv[1] if len(sys.argv) > 1 else ("mac" if platform.system() == "Darwin" else "win")
    if target == "mac":
        build_mac()
    elif target == "win":
        build_win()
    else:
        print(f"用法: python build.py [mac|win]")
        sys.exit(1)