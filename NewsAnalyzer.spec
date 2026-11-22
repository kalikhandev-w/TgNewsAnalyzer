# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for News Analyzer.app (onedir bundle). Build with build_mac.sh.

The spec of 1.0 was lost; this one is recreated from the contents of the shipped 1.0 bundle.
"""
import os
import re

from PyInstaller.utils.hooks import collect_all

ROOT = SPECPATH
APP_NAME = 'News Analyzer'
# diagnostics.APP_VERSION is what crash reports show, so Info.plist takes the version from there
with open(os.path.join(ROOT, 'diagnostics.py'), encoding='utf-8') as f:
    VERSION = re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M).group(1)

datas = [(os.path.join(ROOT, 'assets', 'icon_1024.png'), 'assets')]
binaries = []
hiddenimports = []
# Not visible to import analysis: spacy.load() takes the model by name, and pymorphy3 finds its
# dictionaries through the dist-info entry points
for package in ('ru_core_news_lg', 'pymorphy3_dicts_ru'):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

a = Analysis(
    [os.path.join(ROOT, 'TgNewsAnalyzer.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    argv_emulation=False,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)
app = BUNDLE(
    coll,
    name=f'{APP_NAME}.app',
    icon=os.path.join(ROOT, 'assets', 'icon.icns'),
    bundle_identifier='team.creata.newsanalyzer',
    version=VERSION,
    info_plist={
        # The scipy wheel from requirements-build.txt needs macOS 12.3, pyarrow and scikit-learn 12.0;
        # build_mac.sh fails if any bundled binary needs more than this
        'LSMinimumSystemVersion': '12.3',
        'NSHighResolutionCapable': True,
    },
)
