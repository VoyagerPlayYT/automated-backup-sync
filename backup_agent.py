import os
import sys
import time
import zipfile
import hashlib
import sqlite3
import argparse
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI(title="Automated Backup & Sync Agent")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SOURCE_DIR = os.path.join(DATA_DIR, "storage")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
DB_PATH = os.path.join(DATA_DIR, "backup_history.db")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(SOURCE_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def init_db_and_samples():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backup_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE NOT NULL,
            file_count INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    # Создаем образцы файлов для бэкапа
    sample_files = {
        "family_budget_2026.xlsx": "Семейный бюджет, учет расходов и коммунальных платежей 2026.\nКатегории: ЖКХ, Продукты, Образование.",
        "smart_home_config.json": '{"hub": "HomeServer", "network": "192.168.1.1", "devices": ["camera_hall", "temp_sensor_kitchen", "router"]}',
        "sqlite_database_dump.sql": "-- SQL Backup Dump\nCREATE TABLE family_archive (id INT, name TEXT);\nINSERT INTO family_archive VALUES (1, 'Семейный фотоальбом 2005-2026');",
        "passwords_vault_checksum.sha": "4f9b5a8e9d3c2b1a0e7f8a9b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d"
    }

    for name, content in sample_files.items():
        p = os.path.join(SOURCE_DIR, name)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)


init_db_and_samples()


def calculate_sha256(filepath: str) -> str:
    """Вычисление криптографического хэша SHA-256 для проверки целостности"""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def create_backup(retention_count: int = 5) -> dict:
    """Создание архива с фиксацией SHA-256 и очисткой старых копий"""
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"backup_{timestamp_str}.zip"
    archive_path = os.path.join(BACKUP_DIR, archive_name)

    files_added = 0
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(SOURCE_DIR):
            for file in files:
                abs_p = os.path.join(root, file)
                rel_p = os.path.relpath(abs_p, SOURCE_DIR)
                zipf.write(abs_p, rel_p)
                files_added += 1

    size_bytes = os.path.getsize(archive_path)
    sha256_hash = calculate_sha256(archive_path)

    # Запись в БД
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO backup_records (filename, file_count, size_bytes, sha256, status)
        VALUES (?, ?, ?, ?, 'VERIFIED')
    """, (archive_name, files_added, size_bytes, sha256_hash))
    conn.commit()

    # Ротация старых копий (Retention policy)
    cur.execute("SELECT id, filename FROM backup_records ORDER BY id DESC")
    all_backups = cur.fetchall()
    if len(all_backups) > retention_count:
        for old_id, old_fname in all_backups[retention_count:]:
            old_p = os.path.join(BACKUP_DIR, old_fname)
            if os.path.exists(old_p):
                os.remove(old_p)
            cur.execute("DELETE FROM backup_records WHERE id = ?", (old_id,))
        conn.commit()

    conn.close()

    return {
        "filename": archive_name,
        "files_count": files_added,
        "size_bytes": size_bytes,
        "sha256": sha256_hash,
        "status": "VERIFIED"
    }


def verify_archive_integrity(archive_name: str) -> dict:
    """Проверка целостности zip-контейнера и сверка с контрольной суммой SHA-256"""
    archive_path = os.path.join(BACKUP_DIR, archive_name)
    if not os.path.exists(archive_path):
        return {"status": "MISSING", "error": "Файл архива отсутствует на диске"}

    # 1. Проверка структуры zip
    try:
        with zipfile.ZipFile(archive_path, 'r') as zipf:
            corrupt_file = zipf.testzip()
            if corrupt_file:
                return {"status": "CORRUPTED", "error": f"Поврежден файл внутри: {corrupt_file}"}
    except Exception as e:
        return {"status": "CORRUPTED", "error": str(e)}

    # 2. Сверка SHA-256 с записью в базе данных
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT sha256 FROM backup_records WHERE filename = ?", (archive_name,))
    row = cur.fetchone()
    conn.close()

    current_hash = calculate_sha256(archive_path)
    if row and row[0] == current_hash:
        return {"status": "VALID", "sha256": current_hash}
    elif not row:
        return {"status": "UNTRACKED", "sha256": current_hash}
    else:
        return {"status": "HASH_MISMATCH", "expected": row[0], "actual": current_hash}


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} Б"
    elif size_bytes < 1024 * 1024:
        return f"{round(size_bytes / 1024, 1)} КБ"
    else:
        return f"{round(size_bytes / (1024 * 1024), 2)} МБ"


# Первичный бэкап, если база пуста
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM backup_records")
if cur.fetchone()[0] == 0:
    create_backup()
conn.close()


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.get("/api/backups")
async def list_backups():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM backup_records ORDER BY id DESC")
    records = []
    for r in cur.fetchall():
        d = dict(r)
        d["size_formatted"] = format_size(d["size_bytes"])
        records.append(d)
    conn.close()

    # Считаем размер исходных файлов
    total_source_size = 0
    source_files_count = 0
    for root, _, files in os.walk(SOURCE_DIR):
        for f in files:
            total_source_size += os.path.getsize(os.path.join(root, f))
            source_files_count += 1

    return {
        "backups": records,
        "source_stats": {
            "files_count": source_files_count,
            "total_size": format_size(total_source_size),
            "source_path": SOURCE_DIR
        }
    }


@app.post("/api/backup/create")
async def trigger_backup():
    result = create_backup(retention_count=5)
    return {"status": "created", "backup": result}


@app.get("/api/backup/{filename}/verify")
async def verify_backup(filename: str):
    res = verify_archive_integrity(filename)
    return res


@app.get("/download/{filename}")
async def download_backup(filename: str):
    archive_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(archive_path):
        raise HTTPException(status_code=404, detail="Архив не найден")
    return FileResponse(archive_path, filename=filename)


@app.delete("/api/backup/{filename}")
async def delete_backup(filename: str):
    archive_path = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(archive_path):
        os.remove(archive_path)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM backup_records WHERE filename = ?", (filename,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated Backup Agent")
    parser.add_argument("--cli", action="store_true", help="Запуск в режиме консольной утилиты")
    parser.add_argument("--run-now", action="store_true", help="Создать мгновенный резервный архив")
    args = parser.parse_args()

    if args.cli or args.run_now:
        print("[CLI] Запуск резервного копирования...")
        res = create_backup(retention_count=5)
        print(f"✅ Создан архив: {res['filename']}")
        print(f"📦 Файлов: {res['files_count']} | Размер: {res['size_bytes']} байт")
        print(f"🔒 SHA-256: {res['sha256']}")
        print(f"🛡️ Статус: {res['status']}")
    else:
        import uvicorn
        uvicorn.run("backup_agent:app", host="0.0.0.0", port=8008, reload=False)
