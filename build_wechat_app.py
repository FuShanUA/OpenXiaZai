#!/usr/bin/env python3
"""Build the standalone macOS WeChatDownloader app."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "wechat-app"
ICONSET = BUILD / "WeChatDownloader.iconset"
ICON_PNG = ROOT / "assets" / "wechat_downloader_icon.png"
ICON_ICNS = ROOT / "assets" / "wechat_downloader_icon.icns"
SPEC_PATH = ROOT / "wechat_downloader.spec"


def make_icon() -> None:
    ICON_PNG.parent.mkdir(parents=True, exist_ok=True)
    render_size = 2048
    final_size = 1024
    # macOS composites edge-touching alpha icons with a visible halo. Keep the
    # artwork inside the platform's icon safe area instead of filling the canvas.
    body_size = 1664
    body_offset = (render_size - body_size) // 2
    image = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))

    radius = 448
    mask = Image.new("L", (render_size, render_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, render_size, render_size),
        radius=radius,
        fill=255,
    )

    gradient_source = Image.linear_gradient("L").rotate(45, expand=True)
    crop_left = (gradient_source.width - render_size) // 2
    crop_top = (gradient_source.height - render_size) // 2
    gradient_source = gradient_source.crop(
        (crop_left, crop_top, crop_left + render_size, crop_top + render_size)
    ).resize((render_size, render_size), Image.Resampling.BICUBIC)
    gradient = ImageOps.colorize(
        gradient_source,
        black=(4, 142, 70),
        white=(54, 224, 112),
    ).convert("RGBA")
    image.paste(gradient, (0, 0), mask)

    highlight = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    highlight_draw = ImageDraw.Draw(highlight)
    highlight_draw.ellipse(
        (-320, -520, 1560, 920),
        fill=(255, 255, 255, 58),
    )
    highlight = highlight.filter(ImageFilter.GaussianBlur(130))
    highlight_alpha = ImageChops.multiply(highlight.getchannel("A"), mask)
    highlight.putalpha(highlight_alpha)
    image.alpha_composite(highlight)

    # A WeChat-adjacent mark without copying the trademark: two chat bubbles,
    # a Channels-style play triangle, and a download badge. Shapes are drawn at
    # 2x so the 16px icon still keeps a readable silhouette.
    bubble_layer = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    bubble_draw = ImageDraw.Draw(bubble_layer)
    white = (255, 255, 255, 246)

    bubble_draw.polygon(
        ((430, 1030), (620, 1310), (760, 1050)),
        fill=white,
    )
    bubble_draw.rounded_rectangle(
        (300, 340, 1420, 1080),
        radius=220,
        fill=white,
    )
    bubble_draw.polygon(
        ((1390, 450), (1580, 700), (1250, 530)),
        fill=(255, 255, 255, 232),
    )
    bubble_draw.rounded_rectangle(
        (1100, 190, 1600, 500),
        radius=155,
        fill=(255, 255, 255, 232),
    )

    bubble_shadow = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    bubble_shadow.alpha_composite(bubble_layer, (0, 18))
    shadow_alpha = bubble_shadow.getchannel("A").point(lambda alpha: int(alpha * 0.20))
    bubble_shadow.putalpha(shadow_alpha)
    bubble_shadow = bubble_shadow.filter(ImageFilter.GaussianBlur(22))
    bubble_shadow_alpha = ImageChops.multiply(bubble_shadow.getchannel("A"), mask)
    bubble_shadow.putalpha(bubble_shadow_alpha)
    image.alpha_composite(bubble_shadow)
    image.alpha_composite(bubble_layer)

    draw = ImageDraw.Draw(image)
    draw.polygon(
        (
            (670, 560),
            (670, 900),
            (1000, 730),
        ),
        fill=(4, 154, 82, 248),
    )

    badge = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    badge_draw = ImageDraw.Draw(badge)
    badge_draw.ellipse((1150, 1160, 1660, 1670), fill=(255, 255, 255, 252))
    badge_shadow = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    badge_shadow.alpha_composite(badge, (0, 16))
    shadow_alpha = badge_shadow.getchannel("A").point(lambda alpha: int(alpha * 0.24))
    badge_shadow.putalpha(shadow_alpha)
    badge_shadow = badge_shadow.filter(ImageFilter.GaussianBlur(20))
    badge_shadow_alpha = ImageChops.multiply(badge_shadow.getchannel("A"), mask)
    badge_shadow.putalpha(badge_shadow_alpha)
    image.alpha_composite(badge_shadow)
    image.alpha_composite(badge)

    draw = ImageDraw.Draw(image)
    draw.polygon(
        (
            (1260, 1260),
            (1550, 1260),
            (1405, 1470),
        ),
        fill=(4, 154, 82, 250),
    )
    draw.rounded_rectangle(
        (1230, 1520, 1580, 1590),
        radius=35,
        fill=(4, 154, 82, 250),
    )
    draw.rounded_rectangle(
        (1220, 1520, 1590, 1538),
        radius=18,
        fill=(4, 154, 82, 250),
    )
    draw.rounded_rectangle(
        (1220, 1572, 1590, 1590),
        radius=18,
        fill=(4, 154, 82, 250),
    )

    icon_canvas = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    icon_canvas.alpha_composite(
        image.resize((body_size, body_size), Image.Resampling.LANCZOS),
        (body_offset, body_offset),
    )
    image = icon_canvas.resize((final_size, final_size), Image.Resampling.LANCZOS)

    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)
    image.save(ICON_PNG)
    sizes = [16, 32, 128, 256, 512]
    for icon_size in sizes:
        resized = image.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
        resized.save(ICONSET / f"icon_{icon_size}x{icon_size}.png")
        doubled = image.resize((icon_size * 2, icon_size * 2), Image.Resampling.LANCZOS)
        doubled.save(ICONSET / f"icon_{icon_size}x{icon_size}@2x.png")

    if ICON_ICNS.exists():
        ICON_ICNS.unlink()
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICON_ICNS)], check=True)


def build_app() -> None:
    make_icon()
    DIST.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)

    spec = f"""# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['{ROOT / "wechat_launcher.py"}'],
    pathex=['{ROOT}'],
    binaries=[],
    datas=[
        ('{ROOT / "wechat_capture"}', 'wechat_capture'),
        *collect_data_files('webview'),
    ],
    hiddenimports=['webview'],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WeChatDownloader',
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
    icon='{ICON_ICNS}',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WeChatDownloader',
)

app = BUNDLE(
    coll,
    name='WeChatDownloader.app',
    icon='{ICON_ICNS}',
    bundle_identifier='com.shanfu.wechatdownloader',
    info_plist={{
        'CFBundleDisplayName': 'WeChatDownloader',
        'CFBundleName': 'WeChatDownloader',
        'CFBundleShortVersionString': '1.0.11',
        'CFBundleVersion': '1.0.11',
        'LSMinimumSystemVersion': '10.13',
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSHighResolutionCapable': True,
    }},
)
"""
    SPEC_PATH.write_text(spec, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(SPEC_PATH),
            "--noconfirm",
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD),
        ],
        cwd=ROOT,
        check=True,
    )
    SPEC_PATH.unlink(missing_ok=True)

    app_path = DIST / "WeChatDownloader.app"
    executable = app_path / "Contents" / "MacOS" / "WeChatDownloader"
    if not executable.is_file():
        raise RuntimeError("Build failed: WeChatDownloader executable is missing")
    print(f"Built: {app_path}")


if __name__ == "__main__":
    build_app()
