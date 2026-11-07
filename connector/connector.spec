# PyInstaller recipe for a single connector.exe (run on Windows):
#   pip install -r requirements.txt pyodbc pyinstaller
#   pyinstaller connector.spec
# The program is written to dist\connector.exe.
# -*- mode: python ; coding: utf-8 -*-

hidden = [
    "sqlalchemy.dialects.mssql.pymssql", "sqlalchemy.dialects.mysql.pymysql",
    "sqlalchemy.dialects.postgresql.psycopg2", "sqlalchemy.dialects.oracle.oracledb",
    "sqlalchemy.dialects.sqlite", "pymssql", "pymysql", "psycopg2", "oracledb", "pyodbc", "zk", "tzdata",
]

a = Analysis(
    ["connector.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="connector",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
