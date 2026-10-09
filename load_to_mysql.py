#!/usr/bin/env python3
"""연습용 DB(GitHub 릴리스의 sqlite)를 내 MySQL 에 적재한다. Workbench 에서 바로 쿼리를 쓰고 실행하기 위한 도구.

준비 (한 번만):
  pip install mysql-connector-python

사용 예:
  python load_to_mysql.py --name olist --user root          # 릴리스에서 받아서 적재. 비밀번호는 실행 중에 물어봄
  python load_to_mysql.py --all --user root                 # 릴리스에 있는 DB 전부
  python load_to_mysql.py --db-file olist.db --name olist --user root    # 이미 받은 sqlite 파일로
옵션:
  --host 127.0.0.1  --port 3306   접속 정보
  --db-name NAME                  MySQL 에 만들 DB 이름 (기본: --name 과 같음)
  --recreate                      같은 이름의 DB 가 이미 있으면 지우고 다시 만듦

비밀번호는 명령어에 쓰지 말고 물어볼 때 입력하세요 (채팅에도 붙여넣지 마세요).
타입은 데이터를 보고 자동으로 정합니다: 날짜 -> DATE/DATETIME, 소수 2자리 이하 -> DECIMAL(16,2), 정수 -> INT/BIGINT, 문자 -> VARCHAR/TEXT.
조인에 쓰일 만한 컬럼(2개 이상 테이블에 같은 이름으로 있는 컬럼)에는 인덱스를 만듭니다.
"""
import argparse, getpass, gzip, os, re, shutil, sqlite3, sys, urllib.request

REPO = os.environ.get("SQLTEST_REPO", "nus1245/sql-practice-data")
TAG = os.environ.get("SQLTEST_TAG", "datasets")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sqlpractice_cache")
UA = {"User-Agent": "sql-practice-loader"}
BATCH = 2000

try:
    import mysql.connector
except ImportError:
    sys.exit("mysql-connector-python 이 없어요. 먼저 실행:  pip install mysql-connector-python")


# ---------- 릴리스에서 받기 ----------
def release_files():
    url = f"https://github.com/{REPO}/releases/expanded_assets/{TAG}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        html = r.read().decode("utf-8", "ignore")
    found = re.findall(rf"/{re.escape(REPO)}/releases/download/{re.escape(TAG)}/([^\"?#\s<>]+)", html)
    return list(dict.fromkeys(found))

def fetch_sqlite(name):
    os.makedirs(CACHE, exist_ok=True)
    db = os.path.join(CACHE, f"{name}.db")
    if os.path.exists(db):
        print(f"  (캐시 사용: {db})")
        return db
    files = release_files()
    if f"{name}.db.gz" not in files:
        sys.exit(f"'{name}' 가 릴리스에 없어요. 있는 DB: {', '.join(f[:-6] for f in files if f.endswith('.db.gz'))}")
    url = f"https://github.com/{REPO}/releases/download/{TAG}/{name}.db.gz"
    print(f"  다운로드 중: {url}")
    gz = db + ".gz"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, open(gz, "wb") as f:
        shutil.copyfileobj(r, f)
    with gzip.open(gz, "rb") as s, open(db, "wb") as d:
        shutil.copyfileobj(s, d)
    os.remove(gz)
    return db


# ---------- 타입 추론 ----------
def infer(scon, table, col):
    """sqlite 컬럼을 훑어서 (MySQL 타입, 값 변환 방식)을 정한다."""
    q = f'"{col}"'
    num = "typeof({0}) IN ('integer','real')".format(q)
    r = scon.execute(f"""
        SELECT COUNT({q}),
               SUM(typeof({q})='integer'), SUM(typeof({q})='real'), SUM(typeof({q})='text'),
               MAX(CASE WHEN typeof({q})='text' THEN length({q}) END),
               MIN(CASE WHEN {num} THEN {q} END), MAX(CASE WHEN {num} THEN {q} END),
               SUM({num} AND {q} = CAST({q} AS INTEGER)),
               SUM({num} AND ABS({q}*100 - ROUND({q}*100)) < 0.000001),
               SUM(typeof({q})='text' AND ((length({q})=10 AND date({q})={q}) OR (length({q})=19 AND datetime({q})={q}))),
               SUM(typeof({q})='text' AND length({q})=10)
        FROM "{table}" """).fetchone()
    n, ni, nr, nt, maxlen, mn, mx, integral, two_dec, dt_ok, d10 = [x or 0 for x in r]
    if n == 0:
        return "VARCHAR(255)", None
    nnum = ni + nr
    if nt > 0 and nnum > 0:                      # 숫자와 문자가 섞임 -> 문자로
        return "VARCHAR(255)", "str"
    if nt > 0:
        if dt_ok == nt:
            return ("DATE" if d10 == nt else "DATETIME"), None
        L = int(maxlen)
        if L > 255:
            return ("TEXT" if L <= 65535 else "LONGTEXT"), None
        for size in (16, 32, 64, 128, 255):
            if L <= size:
                return f"VARCHAR({size})", None
    if integral == nnum:                          # 58.0 처럼 소수로 저장된 정수 포함
        return ("INT" if -2**31 <= mn and mx < 2**31 else "BIGINT"), "int"
    if two_dec == nnum and max(abs(mn), abs(mx)) < 1e13:
        return "DECIMAL(16,2)", "dec2"
    return "DOUBLE", None

def convert(kind):
    if kind == "int":
        return lambda v: None if v is None else int(v)
    if kind == "dec2":
        return lambda v: None if v is None else round(float(v), 2)
    if kind == "str":
        return lambda v: None if v is None else str(v)
    return None


# ---------- 적재 ----------
def load_one(cnx, scon, dbname, recreate):
    cur = cnx.cursor()
    cur.execute("SHOW DATABASES LIKE %s", (dbname,))
    if cur.fetchone():
        if not recreate:
            sys.exit(f"MySQL 에 '{dbname}' DB 가 이미 있어요. 지우고 다시 만들려면 --recreate, 다른 이름으로 만들려면 --db-name 을 쓰세요.")
        cur.execute(f"DROP DATABASE `{dbname}`")
    cur.execute(f"CREATE DATABASE `{dbname}` DEFAULT CHARACTER SET utf8mb4")
    cur.execute(f"USE `{dbname}`")
    cur.execute("SET foreign_key_checks=0")
    cur.execute("SET unique_checks=0")

    tables = [r[0] for r in scon.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1")]
    plan, name_count = {}, {}
    for t in tables:
        cols = [r[1] for r in scon.execute(f'PRAGMA table_info("{t}")')]
        plan[t] = [(c, *infer(scon, t, c)) for c in cols]
        for c in cols:
            name_count.setdefault(c, []).append(t)

    for t in tables:
        ddl = ", ".join(f"`{c}` {typ} NULL" for c, typ, _ in plan[t])
        cur.execute(f"CREATE TABLE `{t}` ({ddl}) DEFAULT CHARSET=utf8mb4")
        cols = [c for c, _, _ in plan[t]]
        convs = [convert(k) for _, _, k in plan[t]]
        sql = f"INSERT INTO `{t}` ({', '.join('`'+c+'`' for c in cols)}) VALUES ({', '.join(['%s']*len(cols))})"
        total = scon.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        done = 0
        src = scon.execute(f'SELECT * FROM "{t}"')
        while True:
            rows = src.fetchmany(BATCH)
            if not rows:
                break
            batch = [tuple(f(v) if f else v for f, v in zip(convs, row)) for row in rows]
            cur.executemany(sql, batch)
            done += len(batch)
            print(f"\r  {t}: {done:,}/{total:,}", end="", flush=True)
        cnx.commit()
        print(f"\r  {t}: {total:,}행 적재 완료" + " " * 10)

    # 조인 후보 컬럼 인덱스
    for c, ts in name_count.items():
        if len(ts) < 2:
            continue
        for t in ts:
            typ = next(ty for cc, ty, _ in plan[t] if cc == c)
            if "TEXT" in typ:
                continue
            idx = f"idx_{t}_{c}"[:64]
            cur.execute(f"CREATE INDEX `{idx}` ON `{t}`(`{c}`)")
    cnx.commit()

    print("\n  검증 (sqlite 행 수 == MySQL 행 수):")
    ok = True
    for t in tables:
        a = scon.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        b = cur.fetchone()[0]
        flag = "OK" if a == b else "불일치!"
        ok &= (a == b)
        print(f"    {t:35s} {a:>10,} / {b:>10,}  {flag}")
    print("\n  컬럼 타입 요약:")
    for t in tables:
        print(f"    {t}: " + ", ".join(f"{c} {typ}" for c, typ, _ in plan[t]))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="DB 이름 (릴리스의 <name>.db.gz)")
    ap.add_argument("--all", action="store_true", help="릴리스의 모든 DB")
    ap.add_argument("--db-file", help="이미 받은 sqlite 파일 경로")
    ap.add_argument("--db-name", help="MySQL 에 만들 DB 이름 (기본: --name)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", help="(권장하지 않음) 생략하면 실행 중에 물어봅니다")
    ap.add_argument("--recreate", action="store_true")
    a = ap.parse_args()
    if not (a.name or a.all):
        ap.error("--name <DB이름> 또는 --all 이 필요해요")

    pw = a.password if a.password is not None else os.environ.get("MYSQL_PWD") or getpass.getpass(f"MySQL '{a.user}' 비밀번호 (입력이 화면에 안 보여요): ")
    try:
        cnx = mysql.connector.connect(host=a.host, port=a.port, user=a.user, password=pw, charset="utf8mb4")
    except mysql.connector.Error as e:
        sys.exit(f"MySQL 접속 실패: {e}\n  - MySQL 서버가 켜져 있나요? (Workbench 에서 접속되는지 확인)\n  - host/port/user/비밀번호가 맞나요?")

    if a.all:
        names = [f[:-6] for f in release_files() if f.endswith(".db.gz")]
    else:
        names = [a.name]
    allok = True
    for name in names:
        print(f"\n=== {name} ===")
        path = a.db_file if (a.db_file and not a.all) else fetch_sqlite(name)
        scon = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        allok &= load_one(cnx, scon, (a.db_name if (a.db_name and not a.all) else name), a.recreate)
        scon.close()
    cnx.close()
    print("\n" + ("전부 완료!" if allok else "⚠ 행 수가 다른 테이블이 있어요. 위 검증 표를 확인하세요."))
    print("Workbench 에서 왼쪽 SCHEMAS 패널을 새로고침(Refresh)하면 DB 가 보여요. 쿼리 창에서 `USE olist;` 로 시작하세요.")

main()
