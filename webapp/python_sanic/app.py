import os
import pathlib

import aiomysql
import jinja2
import jinja2_sanic

from sanic import Sanic
from sanic.response import HTTPResponse, redirect


static_folder = pathlib.Path(__file__).resolve().parent / 'public' / 'css'

app = Sanic(__name__)
app.static('/css', str(static_folder))

jinja2_sanic.setup(app, loader=jinja2.FileSystemLoader('./templates', encoding='utf8'))


app.secret_key = os.environ.get('ISHOCON2_SESSION_SECRET', 'showwin_happy')

_config = {
    'db_host': os.environ.get('ISHOCON2_DB_HOST', 'localhost'),
    'db_port': int(os.environ.get('ISHOCON2_DB_PORT', '3306')),
    'db_username': os.environ.get('ISHOCON2_DB_USER', 'ishocon'),
    'db_password': os.environ.get('ISHOCON2_DB_PASSWORD', 'ishocon'),
    'db_database': os.environ.get('ISHOCON2_DB_NAME', 'ishocon2'),
}


def render_template(template_name, request, **kwargs):
    return jinja2_sanic.render_template(template_name, request, context=kwargs)


def config(key):
    if key in _config:
        return _config[key]
    else:
        raise "config value of %s undefined" % key


@app.listener('before_server_start')
async def mysql_start(_app, loop):
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
    _app.mysql = pool


@app.listener('before_server_stop')
async def mysql_stop(_app, loop):
    _app.mysql.close()
    await _app.mysql.wait_closed()


@app.middleware('response')
async def halt_response(request, response):
    if hasattr(request, 'db'):
        request.db.close()


async def get_election_results():
    async with app.mysql.acquire() as conn:
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
    async with app.mysql.acquire() as conn:
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
    async with app.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT political_party FROM candidates GROUP BY political_party')
            records = await cur.fetchall()
            return [r['political_party'] for r in records]


async def get_candidate_by_id(candidate_id):
    async with app.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM candidates WHERE id = {}'.format(candidate_id))
            return await cur.fetchone()


async def db_initialize():
    async with app.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('DELETE FROM votes')


@app.route('/favicon.ico')
async def get_index(request):
    return HTTPResponse('')


@app.route('/')
async def get_index(request):
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

    return render_template('index.html',
                           request,
                           candidates=candidates,
                           parties=parties,
                           sex_ratio=sex_ratio)


@app.route('/candidates/<candidate_id:int>')
async def get_candidate(request, candidate_id):
    async with app.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM candidates WHERE id = {}'.format(candidate_id))
            candidate = await cur.fetchone()
            if not candidate:
                return redirect('/')

            await cur.execute('SELECT COUNT(*) AS count FROM votes WHERE candidate_id = {}'.format(candidate_id))
            votes = (await cur.fetchone())['count']
            keywords = await get_voice_of_supporter([candidate_id])
            return render_template('candidate.html',
                                   request,
                                   candidate=candidate,
                                   votes=votes,
                                   keywords=keywords)


@app.route('/political_parties/<name:string>')
async def get_political_party(request, name):
    async with app.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            votes = 0
            for r in await get_election_results():
                if r['political_party'] == name:
                    votes += r['count'] or 0

            await cur.execute('SELECT * FROM candidates WHERE political_party = "{}"'.format(name))
            candidates = await cur.fetchall()
            candidate_ids = [c['id'] for c in candidates]
            keywords = await get_voice_of_supporter(candidate_ids)
            return render_template('political_party.html',
                                   request,
                                   political_party=name,
                                   votes=votes,
                                   candidates=candidates,
                                   keywords=keywords)


@app.route('/vote')
async def get_vote(request):
    async with app.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM candidates')
            candidates = await cur.fetchall()
            return render_template('vote.html',
                                   request,
                                   candidates=candidates,
                                   message='')


@app.route('/vote', methods=['POST'])
async def post_vote(request):
    async with app.mysql.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT * FROM users WHERE name = "{}" AND address = "{}" AND mynumber = "{}"'.format(
                request.form.get('name'), request.form.get('address'), request.form.get('mynumber')
            ))
            user = await cur.fetchone()
            await cur.execute('SELECT * FROM candidates WHERE name = "{}"'.format(request.form.get('candidate')))
            candidate = await cur.fetchone()
            voted_count = 0
            if user:
                await cur.execute('SELECT COUNT(*) AS count FROM votes WHERE user_id = {}'.format(user['id']))
                result = await cur.fetchone()
                voted_count = result['count']

            await cur.execute('SELECT * FROM candidates')
            candidates = await cur.fetchall()
            if not user:
                return render_template('vote.html', request, candidates=candidates, message='個人情報に誤りがあります')
            elif user['votes'] < (int(request.form.get('vote_count')) + voted_count):
                return render_template('vote.html', request, candidates=candidates, message='投票数が上限を超えています')
            elif not request.form.get('candidate'):
                return render_template('vote.html', request, candidates=candidates, message='候補者を記入してください')
            elif not candidate:
                return render_template('vote.html', request, candidates=candidates, message='候補者を正しく記入してください')
            elif not request.form.get('keyword'):
                return render_template('vote.html', request, candidates=candidates, message='投票理由を記入してください')

            for _ in range(int(request.form.get('vote_count'))):
                await cur.execute('INSERT INTO votes (user_id, candidate_id, keyword) VALUES ({}, {}, "{}")'.format(
                    user['id'], candidate['id'], request.form.get('keyword')
                ))
            return render_template('vote.html', request, candidates=candidates, message='投票に成功しました')


@app.route('/initialize')
async def get_initialize(request):
    await db_initialize()
    return HTTPResponse('init')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, workers=1, access_log=True)
