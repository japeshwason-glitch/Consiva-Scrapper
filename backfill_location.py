"""
Backfills reg_pin / reg_state / reg_locations from registered_address.

Stage 01 never populated these -- the parser was written after the Haryana
ingest and nothing called it. Stage 02's location corroboration signal is dead
without them, and regional reporting has nothing to group by.

    python backfill_location.py [--batch 20000]
"""
import argparse, time
import config, psycopg2, psycopg2.extras
import location

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--batch", type=int, default=20000)
args = ap.parse_args()

# Two connections on purpose: committing on the write side would invalidate a
# server-side cursor sharing the same connection, and 1.4M rows must stream
# rather than land in memory.
read_conn = psycopg2.connect(config.DATABASE_URL)
write_conn = psycopg2.connect(config.DATABASE_URL)

read = read_conn.cursor(name="addr_stream")
read.itersize = args.batch
read.execute("""SELECT cin, registered_address FROM companies
                WHERE registered_address IS NOT NULL
                  AND (reg_pin IS NULL OR reg_state IS NULL)""")

write = write_conn.cursor()
done = t0 = 0
t0 = time.time()
batch = []
for cin, addr in read:
    loc = location.parse(addr)
    batch.append((loc["pin"], loc["state"], psycopg2.extras.Json(loc["locations"]), cin))
    if len(batch) >= args.batch:
        psycopg2.extras.execute_batch(write,
            "UPDATE companies SET reg_pin=%s, reg_state=%s, reg_locations=%s WHERE cin=%s",
            batch, page_size=1000)
        write_conn.commit(); done += len(batch); batch = []
        print(f"  {done:,} rows  ({done/(time.time()-t0):,.0f}/s)", flush=True)
if batch:
    psycopg2.extras.execute_batch(write,
        "UPDATE companies SET reg_pin=%s, reg_state=%s, reg_locations=%s WHERE cin=%s",
        batch, page_size=1000)
    write_conn.commit(); done += len(batch)
print(f"backfilled {done:,} rows in {time.time()-t0:.0f}s")
