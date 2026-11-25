"""One-time import of existing encrypted snapshots; never overwrite PostgreSQL."""
import base64
import json
import os
import urllib.request
from cryptography.fernet import Fernet
import psycopg2


def migrate(database):
    def github(path):
        request = urllib.request.Request(
            'https://api.github.com/repos/' + os.environ['STATE_REPO'] + path,
            headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                     'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    for filename, name in [('state.enc', 'bot'), ('scanner.enc', 'scanner')]:
        if database.load(name) is not None:
            continue
        item = github('/contents/' + filename)
        if item.get('encoding') != 'base64':
            item = github('/git/blobs/' + item['sha'])
        payload = base64.b64decode(item['content'])
        raw = Fernet(os.environ['STATE_ENCRYPTION_KEY'].encode()).decrypt(payload)
        if not raw.startswith(b'SQLite format 3'):
            raise RuntimeError('Invalid migration snapshot')
        connection = database._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute('INSERT INTO halyava_state(name,payload) VALUES(%s,%s) ON CONFLICT(name) DO NOTHING',
                               (name, psycopg2.Binary(payload)))
        finally:
            connection.close()
        print('Migrated encrypted state: ' + name, flush=True)
