import os, sys
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import text
from db.conn import engine

def run_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    with engine().begin() as conn:
        conn.execute(text(sql))
    print("✅ Migration exécutée")

if __name__ == "__main__":
    run_file("db/migrate.sql")
