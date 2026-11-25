"""Render build check: verify full snapshot-sized transactions before rollout."""
import os
import uuid
from web import SnapshotDatabase

db = SnapshotDatabase(os.environ['DATABASE_URL'])
db.create_table()
name = 'probe-' + uuid.uuid4().hex
payload = os.urandom(250000)
try:
    db.write(name, payload)
    if db.load(name) != payload:
        raise RuntimeError('Database read/write mismatch')
    print('Supabase: 250 KB atomic write and read verified', flush=True)
finally:
    connection = db._connect()
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute('DELETE FROM halyava_state WHERE name=%s', (name,))
    finally:
        connection.close()
