"""How fast is R2 at storing doc bodies? The counterpart to pg_body.py.

Measures PUT and GET latency for 2 / 20 / 100 KB markdown objects, one at a time and with
32 concurrent clients. Run it from where the API would run (e.g. a Cloud Run job in your
region), not your laptop, or the numbers mostly measure your home internet.

Needs an R2 bucket and an API token with Object Read & Write on it:
  export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=mykb-bench
  uv run --no-project --with boto3 bench/r2_body.py

Everything it writes goes under bench/<random>/ and is deleted at the end.
"""

import os
import random
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name="auto",
    config=Config(max_pool_connections=64, retries={"max_attempts": 0}),
)
BUCKET = os.environ["R2_BUCKET"]
PREFIX = f"bench/{uuid.uuid4().hex[:8]}/"
SIZES = {"small 2 KB": 2_000, "typical 20 KB": 20_000, "large 100 KB": 100_000}
rng = random.Random(42)
WORDS = "the a to and of in is for deploy server data folder upload editor team api cluster postgres bucket cache".split()


def body(n):
    return " ".join(rng.choice(WORDS) for _ in range(n // 5))[:n].encode()


def pct(xs, p):
    return sorted(xs)[min(len(xs) - 1, int(len(xs) * p))]


def fmt(xs):
    return f"p50 {statistics.median(xs) * 1000:7.1f} ms   p95 {pct(xs, .95) * 1000:7.1f} ms   p99 {pct(xs, .99) * 1000:7.1f} ms"


def timed(fn):
    t = time.perf_counter(); fn(); return time.perf_counter() - t


written = []
try:
    print(f"R2 bucket {BUCKET}, writing under {PREFIX}\n")
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "warmup", Body=b"x"); written.append(PREFIX + "warmup")

    print("One client, sequential")
    for name, n in SIZES.items():
        data = body(n)
        keys = [f"{PREFIX}{name.split()[0]}/{i}.md" for i in range(100)]
        puts = [timed(lambda k=k: s3.put_object(Bucket=BUCKET, Key=k, Body=data, ContentType="text/markdown")) for k in keys]
        written += keys
        gets = [timed(lambda k=k: s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()) for k in keys]
        print(f"   PUT {name:14} {fmt(puts)}")
        print(f"   GET {name:14} {fmt(gets)}")

    print("\n32 concurrent clients, 20 KB")
    data = body(20_000)
    keys = [f"{PREFIX}conc/{i}.md" for i in range(640)]
    written += keys
    with ThreadPoolExecutor(32) as ex:
        t0 = time.perf_counter(); puts = list(ex.map(lambda k: timed(lambda: s3.put_object(Bucket=BUCKET, Key=k, Body=data)), keys)); tp = time.perf_counter() - t0
        t0 = time.perf_counter(); gets = list(ex.map(lambda k: timed(lambda: s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()), keys)); tg = time.perf_counter() - t0
    print(f"   PUT {len(keys) / tp:6.0f}/s   {fmt(puts)}")
    print(f"   GET {len(keys) / tg:6.0f}/s   {fmt(gets)}")
finally:
    for i in range(0, len(written), 1000):
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in written[i:i + 1000]]})
    print(f"\nCleaned up {len(written)} objects.")
