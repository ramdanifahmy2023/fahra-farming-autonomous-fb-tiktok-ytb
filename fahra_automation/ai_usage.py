import time

from .util import iso


async def post(client, *, payload, store=None, operation, subject=None):
    start = time.monotonic()
    response = None
    try:
        response = await client.post('chat/completions', json=payload)
        return response
    finally:
        if store is not None:
            body = {}
            if response is not None:
                try:
                    body = response.json()
                except (ValueError, TypeError):
                    pass
            usage = body.get('usage') or {}
            def count(key):
                value = usage.get(key)
                return value if type(value) is int and value >= 0 else None
            with store.tx() as c:
                c.execute("""INSERT INTO ai_usage(at,operation,subject_id,model,prompt_tokens,
                    completion_tokens,total_tokens,duration_ms) VALUES(?,?,?,?,?,?,?,?)""",
                    (iso(), operation, subject, body.get('model') or payload['model'],
                     count('prompt_tokens'), count('completion_tokens'), count('total_tokens'),
                     (time.monotonic()-start)*1000))
