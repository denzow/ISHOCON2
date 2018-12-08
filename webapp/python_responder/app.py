import os
from urllib.parse import unquote_plus

import aiomysql
import responder

api = responder.API(
    secret_key=os.environ.get('ISHOCON2_SESSION_SECRET', 'showwin_hapiy'),
    templates_dir='templates',
    static_dir='public/css',
    static_route='/css',
)


_config = {
    'db_host': os.environ.get('ISHOCON2_DB_HOST', 'localhost'),
    'db_port': int(os.environ.get('ISHOCON2_DB_PORT', '3306')),
    'db_username': os.environ.get('ISHOCON2_DB_USER', 'ishocon'),
    'db_password': os.environ.get('ISHOCON2_DB_PASSWORD', 'ishocon'),
    'db_database': os.environ.get('ISHOCON2_DB_NAME', 'ishocon2'),
}


def config(key):
    if key in _config:
        return _config[key]
    else:
        raise "config value of %s undefined" % key


@api.on_event('startup')
async def open_database_connection_pool():
    pool = await aiomysql.create_pool(**{
            'host': config('db_host'),
            'port': config('db_port'),
            'user': config('db_username'),
            'password': config('db_password'),
            'db': config('db_database'),
            'charset': 'utf8mb4',
            'cursorclass': aiomysql.DictCursor,
            'autocommit': True,
        })
    api.mysql = pool


@api.on_event('shutdown')
async def open_database_connection_pool():
    api.mysql.close()
    await api.mysql.wait_closed()


async def get_election_results():
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
        SELECT c.id, c.name, c.political_party, c.sex, v.count
        FROM candidates AS c
        LEFT OUTER JOIN
          (SELECT candidate_id, COUNT(*) AS count
          FROM votes
          GROUP BY candidate_id) AS v
        ON c.id = v.candidate_id
        ORDER BY v.count DESC
        """)
            return await cur.fetchall()


async def get_voice_of_supporter(candidate_ids):
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            candidate_ids_str = ','.join([str(cid) for cid in candidate_ids])
            await cur.execute("""
        SELECT keyword
        FROM votes
        WHERE candidate_id IN ({})
        GROUP BY keyword
        ORDER BY COUNT(*) DESC
        LIMIT 10
        """.format(candidate_ids_str))
            records = await cur.fetchall()
            return [r['keyword'] for r in records]


async def get_all_party_name():
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT political_party FROM candidates GROUP BY political_party')
            records = await cur.fetchall()
            return [r['political_party'] for r in records]


async def get_candidate_by_id(candidate_id):
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM candidates WHERE id = {}'.format(candidate_id))
            return await cur.fetchone()


async def db_initialize():
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('DELETE FROM votes')


@api.route('/')
async def get_index(req, resp):
    candidates = []
    election_results = await get_election_results()
    # 上位10人と最下位のみ表示
    candidates += election_results[:10]
    candidates.append(election_results[-1])

    parties_name = await get_all_party_name()
    parties = {}
    for name in parties_name:
        parties[name] = 0
    for r in election_results:
        parties[r['political_party']] += r['count'] or 0
    parties = sorted(parties.items(), key=lambda x: x[1], reverse=True)

    sex_ratio = {'men': 0, 'women': 0}
    for r in election_results:
        if r['sex'] == '男':
            sex_ratio['men'] += r['count'] or 0
        elif r['sex'] == '女':
            sex_ratio['women'] += r['count'] or 0

    resp.content = api.template(
        'index.html',
        candidates=candidates,
        parties=parties,
        sex_ratio=sex_ratio
    )


@api.route('/candidates/{candidate_id}')
async def get_candidate(req, resp, candidate_id):
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM candidates WHERE id = {}'.format(candidate_id))
            candidate = await cur.fetchone()
            if not candidate:
                api.redirect(
                    resp=resp,
                    location='/'
                )
            else:
                await cur.execute('SELECT COUNT(*) AS count FROM votes WHERE candidate_id = {}'.format(candidate_id))
                votes = (await cur.fetchone())['count']
                keywords = await get_voice_of_supporter([candidate_id])
                resp.content = api.template(
                    'candidate.html',
                    candidate=candidate,
                    votes=votes,
                    keywords=keywords
                )


@api.route('/political_parties/{raw_name}')
async def get_political_party(req, resp, raw_name):
    name = unquote_plus(raw_name)
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            votes = 0
            for r in await get_election_results():
                if r['political_party'] == name:
                    votes += r['count'] or 0
            await cur.execute('SELECT * FROM candidates WHERE political_party = "{}"'.format(unquote_plus(name)))
            candidates = await cur.fetchall()
            candidate_ids = [c['id'] for c in candidates]
            keywords = await get_voice_of_supporter(candidate_ids)
            resp.content = api.template(
                'political_party.html',
                political_party=name,
                votes=votes,
                candidates=candidates,
                keywords=keywords
            )


@api.route('/vote')
async def vote(req, resp):
    if req.method == 'post':
        await post_vote(req, resp)
    else:
        await get_vote(req, resp)


async def get_vote(req, resp):
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM candidates')
            candidates = await cur.fetchall()
            resp.content = api.template(
                'vote.html',
                candidates=candidates,
                message=''
            )


async def post_vote(req, resp):
    async with api.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            form = await req.media()
            await cur.execute('SELECT * FROM users WHERE name = "{}" AND address = "{}" AND mynumber = "{}"'.format(
                form.get('name'), form.get('address'), form.get('mynumber')
            ))
            user = await cur.fetchone()
            await cur.execute('SELECT * FROM candidates WHERE name = "{}"'.format(form.get('candidate')))
            candidate = await cur.fetchone()
            voted_count = 0
            if user:
                await cur.execute('SELECT COUNT(*) AS count FROM votes WHERE user_id = {}'.format(user['id']))
                result = await cur.fetchone()
                voted_count = result['count']

            await cur.execute('SELECT * FROM candidates')
            candidates = await cur.fetchall()
            if not user:
                resp.content = api.template(
                    'vote.html',
                    candidates=candidates,
                    message='個人情報に誤りがあります'
                )
            elif user['votes'] < (int(form.get('vote_count')) + voted_count):
                resp.content = api.template(
                    'vote.html',
                    candidates=candidates,
                    message='投票数が上限を超えています'
                )
            elif not form.get('candidate'):
                resp.content = api.template(
                    'vote.html',
                    candidates=candidates,
                    message='候補者を記入してください'
                )
            elif not candidate:
                resp.content = api.template(
                    'vote.html',
                    candidates=candidates,
                    message='候補者を正しく記入してください'
                )
            elif not form.get('keyword'):
                resp.content = api.template(
                    'vote.html',
                    candidates=candidates,
                    message='投票理由を記入してください'
                )
            else:
                for _ in range(int(form.get('vote_count'))):
                    await cur.execute('INSERT INTO votes (user_id, candidate_id, keyword) VALUES ({}, {}, "{}")'.format(
                        user['id'], candidate['id'], form.get('keyword')
                    ))
                resp.content = api.template(
                    'vote.html',
                    candidates=candidates,
                    message='投票に成功しました'
                )


@api.route('/initialize')
async def get_initialize(req, resp):
    await db_initialize()


if __name__ == '__main__':
    api.run(address='0.0.0.0', port=8080)
