"""How fast is Postgres at storing doc bodies (the markdown text itself)?

Measures, for realistic markdown at a few sizes:
  - read the current body of a doc (1 query: docs -> doc_revisions)
  - save a new revision with a compare-and-swap on the head pointer (1 transaction)
  - the same under 32 concurrent clients
  - on-disk size per body (TOAST compression: pglz vs lz4)
  - WAL written per save: snapshot-per-save vs. updating a draft row in place

Usage:
  docker run -d --name mykb-bench-pg -e POSTGRES_PASSWORD=bench -p 127.0.0.1:55499:5432 postgres:18.4
  uv run --with 'psycopg[binary]' bench/pg_body.py [postgresql://postgres:bench@127.0.0.1:55499/postgres]
"""

import random
import statistics
import sys
import threading
import time

import psycopg

DSN = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres:bench@127.0.0.1:55499/postgres"
N_DOCS = 10_000
SIZES = {"small 2 KB": 2_000, "typical 20 KB": 20_000, "large 100 KB": 100_000}
rng = random.Random(42)

# ---------- realistic-ish markdown ----------
WORDS = ("the a to and of in is it for on with as that this you be are by or from at an not your "
         "config deploy server data folder upload image editor doc docs team workspace user api key "
         "token cluster node postgres bucket cache request response error retry timeout queue worker "
         "migration schema index query latency throughput replica backup restore secret terraform "
         "container build release version branch merge review search render preview markdown table "
         "install run start stop restart update delete create move rename copy paste link share").split()


def sentence():
    s = " ".join(rng.choice(WORDS) for _ in range(rng.randint(6, 18)))
    return s[0].upper() + s[1:] + "."


def markdown(target: int) -> str:
    out, n = [], 0
    while n < target:
        kind = rng.random()
        if kind < 0.12:
            block = "#" * rng.randint(2, 3) + " " + " ".join(rng.choice(WORDS) for _ in range(3)).title()
        elif kind < 0.25:
            block = "\n".join(f"- {sentence()}" for _ in range(rng.randint(2, 5)))
        elif kind < 0.35:
            lines = [f"{rng.choice(WORDS)}_{rng.randint(1, 99)} = {rng.choice(WORDS)}({rng.randint(0, 999)})"
                     for _ in range(rng.randint(3, 8))]
            block = "```python\n" + "\n".join(lines) + "\n```"
        elif kind < 0.40:
            block = f"![{rng.choice(WORDS)}|50%](/uploads/{rng.choice(WORDS)}-{rng.getrandbits(32):08x}.png)"
        else:
            block = " ".join(sentence() for _ in range(rng.randint(2, 6)))
        out.append(block)
        n += len(block) + 2
    return "\n\n".join(out)[:target]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def fmt_ms(xs):
    return f"p50 {statistics.median(xs) * 1000:6.2f} ms   p95 {pct(xs, .95) * 1000:6.2f} ms   p99 {pct(xs, .99) * 1000:6.2f} ms"


def wal_lsn(cur):
    return cur.execute("select pg_current_wal_lsn()").fetchone()[0]


def wal_bytes(cur, a, b):
    return float(cur.execute("select pg_wal_lsn_diff(%s, %s)", (b, a)).fetchone()[0])


# ---------- setup ----------
conn = psycopg.connect(DSN, autocommit=True)
cur = conn.cursor()
cur.execute("""
  drop table if exists doc_drafts, docs, doc_revisions;
  create table doc_revisions (
    id bigserial primary key, doc_id bigint not null, author_id bigint not null,
    body text not null, created_at timestamptz not null default now());
  create table docs (
    id bigint primary key, workspace_id bigint not null, title text not null,
    head_rev_id bigint not null);
  create table doc_drafts (doc_id bigint primary key, body text not null, updated_at timestamptz not null default now());
""")

print(f"Postgres: {cur.execute('show server_version').fetchone()[0]}  synchronous_commit="
      f"{cur.execute('show synchronous_commit').fetchone()[0]}\n")

# ---------- 1. storage size & compression ----------
print("1) On-disk size of one body (TOAST compresses anything over ~2 KB)")
samples = {name: [markdown(n) for _ in range(200)] for name, n in SIZES.items()}
for method in ("pglz", "lz4"):
    cur.execute(f"alter table doc_revisions alter column body set compression {method}")
    cur.execute("truncate doc_revisions")
    for name, bodies in samples.items():
        with cur.copy("copy doc_revisions (doc_id, author_id, body) from stdin") as cp:
            for b in bodies:
                cp.write_row((0, 0, b))
        raw, stored = cur.execute(
            "select avg(octet_length(body)), avg(pg_column_size(body)) from doc_revisions where length(body) between %s and %s",
            (SIZES[name] - 50, SIZES[name] + 50)).fetchone()
        print(f"   {method:4}  {name:14} raw {raw / 1024:6.1f} KB -> stored {stored / 1024:6.1f} KB  ({raw / stored:.1f}x)")
cur.execute("truncate doc_revisions")
cur.execute("alter table doc_revisions alter column body set compression lz4")
print()

# ---------- seed ----------
mix = [SIZES["small 2 KB"]] * 3 + [SIZES["typical 20 KB"]] * 6 + [SIZES["large 100 KB"]]
pool = [markdown(rng.choice(mix)) for _ in range(500)]
t0 = time.perf_counter()
with cur.copy("copy doc_revisions (id, doc_id, author_id, body) from stdin") as cp:
    for i in range(1, N_DOCS + 1):
        cp.write_row((i, i, 1, rng.choice(pool)))
cur.execute("select setval('doc_revisions_id_seq', %s)", (N_DOCS,))
with cur.copy("copy docs (id, workspace_id, title, head_rev_id) from stdin") as cp:
    for i in range(1, N_DOCS + 1):
        cp.write_row((i, i % 200, f"Doc {i}", i))
with cur.copy("copy doc_drafts (doc_id, body) from stdin") as cp:
    for i in range(1, N_DOCS + 1):
        cp.write_row((i, rng.choice(pool)))
cur.execute("create index on doc_revisions (doc_id, id desc)")
cur.execute("vacuum analyze")
print(f"Seeded {N_DOCS:,} docs (mix: 30% 2 KB, 60% 20 KB, 10% 100 KB) in {time.perf_counter() - t0:.1f}s\n")

READ = "select r.body from docs d join doc_revisions r on r.id = d.head_rev_id where d.id = %s"


class Conflict(Exception):
    pass


conflicts = 0


def save(c, doc_id, body):
    """New revision + compare-and-swap on the head pointer, one transaction."""
    with c.transaction():
        base = c.execute("select head_rev_id from docs where id = %s", (doc_id,)).fetchone()[0]
        new = c.execute("insert into doc_revisions (doc_id, author_id, body) values (%s, 1, %s) returning id",
                        (doc_id, body)).fetchone()[0]
        n = c.execute("update docs set head_rev_id = %s where id = %s and head_rev_id = %s", (new, doc_id, base)).rowcount
        if n == 0:  # someone else saved this doc first: the CAS caught it, so roll back and report a conflict
            raise Conflict


# ---------- 2. single-client latency ----------
print("2) One client, sequential (includes the network hop into Docker)")
c = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
lat = []
for _ in range(5000):
    t = time.perf_counter(); c.execute(READ, (rng.randint(1, N_DOCS),)).fetchone(); lat.append(time.perf_counter() - t)
print(f"   read current body      {fmt_ms(lat)}")
for name, n in SIZES.items():
    body = markdown(n)
    lat = []
    for _ in range(600):
        t = time.perf_counter(); save(c, rng.randint(1, N_DOCS), body); lat.append(time.perf_counter() - t)
    print(f"   save revision {name:14} {fmt_ms(lat)}")
print()


# ---------- 3. concurrency ----------
def run_concurrent(fn, threads=32, seconds=10):
    lats, lock, stop = [], threading.Lock(), time.perf_counter() + seconds

    def worker():
        cc = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
        mine = []
        while time.perf_counter() < stop:
            t = time.perf_counter(); fn(cc); mine.append(time.perf_counter() - t)
        with lock:
            lats.extend(mine)
        cc.close()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return len(lats) / seconds, lats


print("3) 32 concurrent clients, 10 s each")
ops, lat = run_concurrent(lambda cc: cc.execute(READ, (rng.randint(1, N_DOCS),)).fetchone())
print(f"   reads                  {ops:8,.0f}/s   {fmt_ms(lat)}")
typical = markdown(SIZES["typical 20 KB"])
a = wal_lsn(cur)
def save_counting(cc):
    global conflicts
    try:
        save(cc, rng.randint(1, N_DOCS), typical)
    except Conflict:
        conflicts += 1


ops, lat = run_concurrent(save_counting)
b = wal_lsn(cur)
saves = ops * 10
print(f"   saves (20 KB revision) {ops:8,.0f}/s   {fmt_ms(lat)}   ({conflicts} caught as conflicts by the CAS)")
snap_wal = wal_bytes(cur, a, b) / saves
print()

# ---------- 4. WAL per save: snapshot vs draft-in-place ----------
print("4) WAL written per 20 KB save (drives replica lag, backup size, disk I/O)")


def draft_update(cc):
    cc.execute("update doc_drafts set body = %s, updated_at = now() where doc_id = %s", (typical, rng.randint(1, N_DOCS)))


a = wal_lsn(cur)
ops_d, lat_d = run_concurrent(draft_update, seconds=5)
b = wal_lsn(cur)
draft_wal = wal_bytes(cur, a, b) / (ops_d * 5)
print(f"   new revision per save  {snap_wal / 1024:6.1f} KB/save")
print(f"   update draft in place  {draft_wal / 1024:6.1f} KB/save   ({ops_d:,.0f} saves/s, {fmt_ms(lat_d)})")
print()

# ---------- 5. table sizes ----------
revs = cur.execute("select count(*) from doc_revisions").fetchone()[0]
total = cur.execute("select pg_total_relation_size('doc_revisions')").fetchone()[0]
print(f"5) doc_revisions: {revs:,} rows, {total / 1024**2:,.0f} MB on disk incl. TOAST + indexes "
      f"= {total / revs / 1024:.1f} KB per revision")
