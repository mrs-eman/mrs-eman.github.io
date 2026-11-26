# Alphabet World QA bot
# Opens the whole site like a child would (girl and boy, phones, tablets, laptop),
# plays the important moments and writes a report with screenshots.
# Usage: python3 qa_bot.py <site folder> <report folder>
import asyncio, json, os, re, sys, glob, time, threading, functools, subprocess, html
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from playwright.async_api import async_playwright
from PIL import Image
import numpy as np

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else '.')
OUT = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else 'qa-report')
FAST = os.environ.get('QA_FAST') == '1'
os.makedirs(os.path.join(OUT, 'shots'), exist_ok=True)
PORT = 8799
BASE = f'http://127.0.0.1:{PORT}/'

SIZES = {
    'phone-land': (915, 412), 'phone-port': (390, 844), 'iphone': (393, 852),
    'samsung-tab': (1280, 800), 'ipad': (820, 1180), 'laptop': (1440, 860),
}
LOAD_SIZES = ['phone-land', 'phone-port', 'samsung-tab', 'laptop']
FLOW_SIZES = ['phone-land', 'phone-port', 'samsung-tab', 'ipad']
if FAST:
    LOAD_SIZES = ['phone-land', 'phone-port']; FLOW_SIZES = ['phone-land', 'phone-port']
IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'

results = []   # dicts: area, name, size, ok, msg, shot
def add(area, name, size, ok, msg='', shot=None):
    results.append(dict(area=area, name=name, size=size, ok=bool(ok), msg=msg, shot=shot))
    print(('PASS ' if ok else 'FAIL ') + f'{area} | {name} | {size} | {msg}', flush=True)

# ---------- server ----------
class H(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
def serve():
    h = functools.partial(H, directory=ROOT)
    ThreadingHTTPServer(('127.0.0.1', PORT), h).serve_forever()
threading.Thread(target=serve, daemon=True).start()

# ---------- static checks ----------
def rd(p): return open(os.path.join(ROOT, p), encoding='utf-8', errors='ignore').read()
SW = rd('sw.js')
def swlist(name):
    i = SW.find(name + '=[')
    return set(x.lstrip('/') for x in re.findall(r'"([^"]+)"', SW[i:SW.find(']', i)])) if i >= 0 else set()
ONLYG, ONLYB = swlist('ONLYG'), swlist('ONLYB')
GIRL = sorted(f for f in glob.glob('*.html', root_dir=ROOT) + glob.glob('ws*/index.html', root_dir=ROOT) if '_b' not in f)
BOY = sorted(glob.glob('*_b.html', root_dir=ROOT) + glob.glob('ws*/index_b.html', root_dir=ROOT))
# boy pages that are allowed to show girl things (the choose screen shows both children)
CHOOSE_OK = {'intro.mp4', 'intro.webm', 'intro_poster.jpg', 'btn_skip.webp', 'btn_sound.webp',
             'intro_b.mp4', 'intro_b.webm', 'intro_poster_b.jpg', 'btn_skip_b.webp', 'btn_sound_b.webp'}

def static_checks():
    try:
        r = subprocess.run(['node', '--check', os.path.join(ROOT, 'sw.js')], capture_output=True, text=True)
        add('Files', 'sw.js has no code mistakes', '-', r.returncode == 0, r.stderr.strip()[:200])
    except FileNotFoundError:
        add('Files', 'sw.js has no code mistakes', '-', True, 'node not installed, skipped')
    m = re.search(r"CACHE='([^']+)'", SW); add('Files', 'Update version', '-', bool(m), m.group(1) if m else 'no CACHE line')
    every = set()
    for k in ('ALL', 'ONLYG', 'ONLYB', 'CORE'): every |= swlist(k)
    miss = sorted(f for f in every if f and not f.endswith('/') and not os.path.exists(os.path.join(ROOT, f)))
    add('Files', 'Every file in the offline list exists', '-', not miss, ', '.join(miss[:15]))
    miss = set()
    for f in GIRL + BOY:
        for h in re.findall(r'a/([0-9a-f]{14}\.(?:webp|mp3|png|jpg|mp4|webm|wav))', rd(f)):
            if not os.path.exists(os.path.join(ROOT, 'a', h)): miss.add(f'{f}: a/{h}')
    add('Files', 'Every picture and sound used by the pages exists', '-', not miss, ', '.join(sorted(miss)[:15]))
    notcached = set()
    for f in GIRL + BOY:
        for h in re.findall(r'a/([0-9a-f]{14}\.(?:webp|mp3|png|jpg))', rd(f)):
            if 'a/' + h not in every: notcached.add('a/' + h)
    add('Files', 'Every picture and sound is saved for offline', '-', not notcached,
        f'{len(notcached)} not in sw.js: ' + ', '.join(sorted(notcached)[:10]))

# ---------- browser helpers ----------
def init_js(g, unlock=True, chosen=True, extra=''):
    s = "try{"
    if unlock: s += "localStorage.setItem('mea-unlock','1');"
    if g: s += f"localStorage.setItem('mea-gender','{g}');"
    if chosen: s += "sessionStorage.setItem('mea-chosen','1');sessionStorage.setItem('introSeen','1');"
    s += extra + "}catch(e){}"
    return s

class Run:
    def __init__(self, ctx, page):
        self.ctx, self.page, self.err, self.bad, self.urls = ctx, page, [], [], []
async def open_page(br, path, size, g, unlock=True, chosen=True, extra='', ua=None, touch=True):
    W, H = SIZES[size]
    ctx = await br.new_context(viewport={'width': W, 'height': H}, has_touch=touch, user_agent=ua,
                               service_workers='block')
    await ctx.route(re.compile(r'^https?://(?!127\.0\.0\.1)'), lambda r: r.abort())
    await ctx.add_init_script(init_js(g, unlock, chosen, extra))
    pg = await ctx.new_page(); R = Run(ctx, pg)
    pg.on('pageerror', lambda e: R.err.append(str(e)[:160]))
    pg.on('response', lambda r: R.bad.append(f'{r.status} {r.url.split(str(PORT) + "/")[-1]}') if r.status >= 400 and str(PORT) in r.url else None)
    pg.on('request', lambda r: R.urls.append(r.url.split(str(PORT) + '/')[-1].split('?')[0]))
    await pg.goto(BASE + path, wait_until='load', timeout=30000)
    return R
async def shot(R, name):
    fn = re.sub(r'[^\w.-]+', '_', name) + '.jpg'
    p = os.path.join(OUT, 'shots', fn)
    await R.page.screenshot(path=p, type='jpeg', quality=60)
    return 'shots/' + fn
def blank(path):
    a = np.array(Image.open(os.path.join(OUT, path)).convert('RGB').resize((64, 64))).astype(int)
    return a.std() < 6
def leak(R, g, path):
    other = ONLYG if g == 'boy' else ONLYB
    bad = sorted(set(u for u in R.urls if u in other and os.path.basename(u) not in CHOOSE_OK))
    return bad
async def hide_intro(R):
    await R.page.evaluate("""(()=>{const i=document.getElementById('iv');if(i)i.style.display='none';
      const v=document.querySelectorAll('video');v.forEach(x=>{try{x.pause()}catch(e){}})})()""")
async def in_view(R, sel):
    return await R.page.evaluate("""(sel)=>{const W=innerWidth,H=innerHeight,out=[];
      document.querySelectorAll(sel).forEach(e=>{const r=e.getBoundingClientRect(),cs=getComputedStyle(e);
        if(r.width<4||r.height<4||cs.visibility==='hidden'||cs.display==='none'||+cs.opacity===0)return;
        if(r.left<-3||r.top<-3||r.right>W+3||r.bottom>H+3)out.push((e.className||e.tagName)+' '+Math.round(r.left)+','+Math.round(r.top)+','+Math.round(r.right)+','+Math.round(r.bottom))});return out}""", sel)

SEM = asyncio.Semaphore(4)
async def job(fn, *a):
    async with SEM:
        try: await fn(*a)
        except Exception as e: add('Bot', fn.__name__, str(a[1:3]), False, 'bot could not finish: ' + str(e)[:150])

# ---------- 1. every page opens ----------
async def t_load(br, path, size, g):
    R = await open_page(br, path, size, g)
    await R.page.wait_for_timeout(1800)
    s = await shot(R, f'load_{path}_{size}')
    msg = []
    if R.err: msg.append('code error: ' + R.err[0])
    if R.bad: msg.append('missing: ' + ', '.join(sorted(set(R.bad))[:4]))
    if blank(s): msg.append('empty screen')
    lk = leak(R, g, path)
    if lk: msg.append(('girl-only' if g == 'boy' else 'boy-only') + ' files used (wrong picture, or missing when offline): ' + ', '.join(lk[:4]))
    add('Pages open', path, size, not msg, '; '.join(msg), s)
    await R.ctx.close()

# ---------- 2. home: lock, choose, intro video, switch ----------
async def t_lock(br, size):
    R = await open_page(br, 'index.html', size, None, unlock=False, chosen=False)
    await R.page.wait_for_timeout(2000)
    s = await shot(R, f'lock_{size}')
    vis = await R.page.evaluate("(()=>{const m=document.getElementById('mLock');if(!m)return 'no lock';const r=m.getBoundingClientRect(),cs=getComputedStyle(m);return (r.width>20&&cs.visibility!=='hidden'&&cs.display!=='none')?'ok':'hidden'})()")
    add('Home', 'Password screen shows (first visit)', size, vis == 'ok' and not blank(s), vis if vis != 'ok' else ('empty screen' if blank(s) else ''), s)
    await R.ctx.close()
async def t_choose(br, start, pick, size):
    g0 = 'boy' if '_b' in start else 'girl'
    R = await open_page(br, start, size, None, unlock=True, chosen=False)
    await R.page.wait_for_timeout(1800)
    s1 = await shot(R, f'choose_{start}_{size}')
    ok = await R.page.evaluate("(()=>{const g=document.getElementById('gch');if(!g)return false;const r=g.getBoundingClientRect();return r.width>50&&getComputedStyle(g).visibility!=='hidden'})()")
    add('Home', f'Choose Your Character shows ({start})', size, ok and not blank(s1), '' if ok else 'choose screen not visible', s1)
    btn = await R.page.query_selector(f'#gch button[data-g="{pick}"]:visible') or await R.page.query_selector(f'#gch [data-g="{pick}"]')
    if not btn:
        add('Home', f'Pick {pick} -> welcome video', size, False, 'no button for ' + pick); await R.ctx.close(); return
    await btn.tap() if True else None
    await R.page.wait_for_timeout(1500)
    v = await R.page.evaluate("(()=>{const o=document.getElementById('gvid'),v=o&&o.querySelector('video');if(!v)return null;const r=o.getBoundingClientRect();return [v.currentSrc.split('/').pop(),r.width,getComputedStyle(o).visibility]})()")
    s2 = await shot(R, f'pick_{start}_{pick}_{size}')
    want = 'intro_b' if pick == 'boy' else 'intro.'
    ok = bool(v) and want in v[0] and v[1] > 50 and v[2] != 'hidden'
    add('Home', f'Pick {pick} -> {pick} welcome video starts ({start})', size, ok, '' if ok else f'video: {v}', s2)
    if R.err: add('Home', f'No code errors ({start} pick {pick})', size, False, R.err[0])
    await R.ctx.close()
async def t_switch(br, start, size):
    g = 'boy' if '_b' in start else 'girl'; other = 'girl' if g == 'boy' else 'boy'
    R = await open_page(br, start, size, g)
    await R.page.wait_for_timeout(2000)
    sw = await R.page.query_selector('#gsw')
    if not sw:
        add('Home', 'Switch character button', size, False, 'no switch button'); await R.ctx.close(); return
    await sw.tap(); await R.page.wait_for_timeout(900)
    btn = await R.page.query_selector(f'#gch button[data-g="{other}"]:visible') or await R.page.query_selector(f'#gch [data-g="{other}"]')
    if btn: await btn.tap()
    await R.page.wait_for_timeout(1500)
    v = await R.page.evaluate("(()=>{const v=document.querySelector('#gvid video');return v?v.currentSrc.split('/').pop():null})()")
    s = await shot(R, f'switch_{start}_{size}')
    want = 'intro_b' if other == 'boy' else 'intro.'
    add('Home', f'Switch {g} -> {other}: video starts', size, bool(v) and want in v, f'video: {v}', s)
    await R.ctx.close()

# ---------- 3. crown celebration for every map + 5 crowns video ----------
def crown_state(n, allc):
    js = "localStorage.setItem('mea-allcrowns','" + ('1' if allc else '') + "');" if allc else "localStorage.removeItem('mea-allcrowns');"
    js = "if(!sessionStorage.getItem('qa')){sessionStorage.setItem('qa','1');" + js
    js += ("for(let m=1;m<=5;m++){localStorage.setItem('lfg-map'+m,JSON.stringify({best:{1:3,2:3,3:3}}));"
           "localStorage.setItem('lfg-map'+m+'-wsdone',JSON.stringify(['1','2','3','4','5','6']));"
           f"localStorage.setItem('lfg-map'+m+'-champ','1');if(m!=={n})localStorage.setItem('lfg-map'+m+'-crown','1');else localStorage.removeItem('lfg-map'+m+'-crown')}}}}")
    return js
async def t_crown(br, page, n, size):
    g = 'boy' if '_b' in page else 'girl'; last = (n == 5)
    R = await open_page(br, page, size, g, extra=crown_state(n, not last))
    pg = R.page; W, H = SIZES[size]
    await pg.wait_for_timeout(2800)
    sp = await shot(R, f'crown_{g}{n}_{size}_0surprise')
    has = await pg.evaluate("!![...document.querySelectorAll('div')].find(d=>d.getAttribute('aria-label')==='Tap for your surprise')")
    add('Crowns', f'{g} map {n}: "Tap for your surprise" shows', size, has, '' if has else 'no surprise button', sp)
    if not has: await R.ctx.close(); return
    if g == 'boy' and any('665ff7f97c2823' in u for u in R.urls):
        add('Crowns', f'boy map {n}: surprise picture is the boy one', size, False, 'girl surprise picture used')
    await pg.mouse.click(W / 2, H / 2)
    probs = []; shots_ = []
    for k, t in enumerate([2400, 1400, 3200, 3500]):
        await pg.wait_for_timeout(t)
        info = await pg.evaluate(f"""(()=>{{const W=innerWidth,H=innerHeight,c=document.querySelector('.card.c{n}').getBoundingClientRect();
          const sp=[...document.querySelectorAll('div')].find(d=>d.style.boxShadow&&d.style.boxShadow.includes('200vmax'));const s=sp&&sp.getBoundingClientRect();
          const imgs=[...document.images].filter(i=>getComputedStyle(i).position==='fixed'&&+getComputedStyle(i).zIndex>=9002&&i.isConnected);
          const word=imgs.find(i=>i.alt&&i.alt.startsWith('Alphabet')),kid=imgs.find(i=>i.style.bottom==='0px');
          const R=e=>{{if(!e)return null;const r=e.getBoundingClientRect();return [r.left,r.top,r.right,r.bottom]}};
          return {{card:[c.left,c.top,c.right,c.bottom],spot:s?[s.left,s.top]:null,word:R(word),kid:R(kid),W,H}}}})()""")
        shots_.append(await shot(R, f'crown_{g}{n}_{size}_{k+1}'))
        c = info['card']
        if c[1] < -5 or c[3] > info['H'] + 40: probs.append(f'map card not on screen (top {round(c[1])}, bottom {round(c[3])})')
        if info['spot'] and (abs(info['spot'][0] - (c[0] - 16)) > 6 or abs(info['spot'][1] - (c[1] - 16)) > 6):
            probs.append('light is not on the map card')
        for nm in ('word', 'kid'):
            r = info[nm]
            if not r: continue
            if ((nm == 'kid' and k == 1) or (nm == 'word' and k in (1, 2))) and (r[2] < 20 or r[0] > info['W'] - 20 or r[3] < 20 or r[1] > info['H'] - 20):
                probs.append(f'{"Alphabet Queen/King picture" if nm=="word" else "child"} is off the screen')
            if nm == 'word':
                ix = max(0, min(r[2], c[2]) - max(r[0], c[0])) * max(0, min(r[3], c[3]) - max(r[1], c[1]))
                ar = max(1, (r[2] - r[0]) * (r[3] - r[1]))
                if ix / ar > .25 and k == 1: probs.append('Alphabet Queen/King picture covers the map card')
    for _ in range(16):
        await pg.wait_for_timeout(500)
        if await pg.evaluate("!window.__surpriseOn"): break
    await pg.wait_for_timeout(300)
    fin = await pg.evaluate(f"""(()=>{{const d=document.querySelector('.card.c{n} .homeChamp img');return {{src:d?d.src.split('/').pop():null,ov:document.documentElement.style.overflow,left:document.querySelectorAll('div').length}}}})()""")
    if fin['ov'] == 'hidden' and not last: probs.append('page stays locked (cannot scroll) after the celebration')
    if R.err: probs.append('code error: ' + R.err[0])
    if g == 'boy':
        lk = leak(R, g, page)
        if lk: probs.append('girl files: ' + ', '.join(lk[:4]))
    add('Crowns', f'{g} map {n}: crown celebration', size, not probs, '; '.join(dict.fromkeys(probs)), shots_[0])
    for s in shots_[1:]: add('Crowns', f'{g} map {n}: celebration picture', size, True, '', s)
    if last:
        await pg.wait_for_timeout(3000)
        v = await pg.evaluate("(()=>{const v=document.querySelector('#allcr video');return v?[v.currentSrc.split('/').pop(),v.paused]:null})()")
        s = await shot(R, f'allcrowns_{g}_{size}')
        want = 'king_b' if g == 'boy' else 'queen'
        add('Crowns', f'{g}: 5 crowns -> Alphabet {"King" if g=="boy" else "Queen"} video', size, bool(v) and want in v[0], f'video: {v}', s)
        if v:
            await pg.click('#allcr .acx'); await pg.wait_for_timeout(900)
            st = await pg.evaluate("[!!document.getElementById('allcr'),document.documentElement.style.overflow]")
            add('Crowns', f'{g}: Skip closes the 5 crowns video', size, not st[0] and st[1] != 'hidden', f'still open: {st}')
    await R.ctx.close()

# ---------- 4. end screens (train, bubbles, tricky, tricky train) ----------
async def t_end(br, page, size):
    g = 'boy' if '_b' in page else 'girl'
    R = await open_page(br, page, size, g); pg = R.page
    await pg.wait_for_timeout(1500); await hide_intro(R)
    try:
        await pg.evaluate("(()=>{const r=document.getElementById('rvStart');if(r){r.remove();document.body.classList.remove('rvWait')}if(typeof start==='function')try{start()}catch(e){}})()")
    except Exception: pass
    await pg.wait_for_timeout(600)
    err = await pg.evaluate("(()=>{try{if(typeof finish==='function'&&!(window.CFG&&CFG.review))finish();else if(typeof reviewEnd==='function')reviewEnd();else return 'no end function';return ''}catch(e){return String(e)}})()")
    await pg.wait_for_timeout(2600)
    s = await shot(R, f'end_{page}_{size}')
    out = await in_view(R, '#end img, #end button, #end a, #end h1, #endt img')
    vis = await pg.evaluate("(()=>{const e=document.getElementById('end');if(!e)return false;const r=e.getBoundingClientRect();return r.width>20&&getComputedStyle(e).visibility!=='hidden'})()")
    msg = []
    if err: msg.append(err[:100])
    if not vis: msg.append('end screen not visible')
    if out: msg.append('cut off the screen: ' + '; '.join(out[:3]))
    if R.err: msg.append('code error: ' + R.err[0])
    if g == 'boy':
        lk = leak(R, g, page)
        if lk: msg.append('girl files: ' + ', '.join(lk[:4]))
    add('End screens', page, size, not msg, '; '.join(msg), s)
    await R.ctx.close()

# ---------- 5. worksheets: stamp + microphone message ----------
async def t_ws(br, page, size, ua=None):
    g = 'boy' if '_b' in page else 'girl'
    R = await open_page(br, page, size, g, ua=ua, extra="localStorage.setItem('ws-name','QA');")
    pg = R.page; await pg.wait_for_timeout(1500)
    await pg.evaluate("""(()=>{navigator.mediaDevices&&(navigator.mediaDevices.getUserMedia=()=>Promise.reject(new DOMException('no','NotAllowedError')));
      document.querySelectorAll('.nameGate,#nameGate,#gate').forEach(e=>e.remove())})()""")
    r = await pg.evaluate("""(()=>{const p=[...document.querySelectorAll('section.sheet')].find(x=>x.dataset.v==='sp');if(!p)return 'no speaking sheet';
      document.querySelectorAll('section.sheet').forEach(x=>x.hidden=true);p.hidden=false;p.scrollIntoView();const b=p.querySelector('.rec');if(!b)return 'no record button';b.click();return ''})()""")
    await pg.wait_for_timeout(1200)
    s = await shot(R, f'mic_{page}_{size}' + ('_iphone' if ua else ''))
    info = await pg.evaluate("(()=>{const m=document.getElementById('micHelp');if(!m)return null;const i=m.querySelector('img');const b=(i||m).getBoundingClientRect();return {img:i?i.src.split('/').pop():null,loaded:i?i.complete&&i.naturalWidth>0:false,box:[b.left,b.top,b.right,b.bottom],W:innerWidth,H:innerHeight}})()")
    msg = []
    if r: msg.append(r)
    elif not info: msg.append('microphone message did not show')
    else:
        if not info['loaded']: msg.append('picture did not load')
        b = info['box']
        if b[0] < -3 or b[1] < -3 or b[2] > info['W'] + 3 or b[3] > info['H'] + 3: msg.append('message is cut off the screen')
        want = '8c70bdb' if False else None
    if info and not msg:
        ok = await pg.query_selector('#micHelp .mh-ok')
        if ok: await ok.click(); await pg.wait_for_timeout(400)
        gone = await pg.evaluate("!document.getElementById('micHelp')||document.getElementById('micHelp').hidden||getComputedStyle(document.getElementById('micHelp')).display==='none'")
        if not gone: msg.append('"OK" button does not close the message')
    if R.err: msg.append('code error: ' + R.err[0])
    add('Worksheets', f'{page}: microphone message' + (' (iPhone)' if ua else ''), size, not msg, '; '.join(msg), s)
    # stamp after finishing a sheet with everything right
    st = await pg.evaluate("""(()=>{try{const sh=[...document.querySelectorAll('section.sheet')];let i=sh.findIndex((x,j)=>x.dataset.v!=='sp'&&NN[j]>0);if(i<0)return 'no sheet';
      sh.forEach(x=>x.hidden=true);sh[i].hidden=false;MISS[i]=0;wsDone(i);return ''}catch(e){return String(e).slice(0,120)}})()""")
    await pg.wait_for_timeout(3500)
    s2 = await shot(R, f'stamp_{page}_{size}')
    out = await in_view(R, '.wsRes img, .wsRes')
    vis = await pg.evaluate("!!document.querySelector('.wsRes')")
    msg = []
    if st: msg.append(st)
    elif not vis: msg.append('stamp did not show')
    if out: msg.append('stamp cut off: ' + out[0])
    if g == 'boy':
        lk = leak(R, g, page)
        if lk: msg.append('girl files: ' + ', '.join(lk[:4]))
    add('Worksheets', f'{page}: result stamp', size, not msg, '; '.join(msg), s2)
    await R.ctx.close()

# ---------- report ----------
def report(t0):
    fails = [r for r in results if not r['ok']]
    areas = list(dict.fromkeys(r['area'] for r in results))
    css = """body{font-family:system-ui,Arial;margin:0;background:#f4f6fb;color:#222}header{padding:18px 20px;color:#fff;background:%s}
h1{margin:0;font-size:22px}h2{margin:22px 16px 8px;font-size:18px}.f{background:#fff;margin:8px 16px;padding:10px 12px;border-radius:10px;border-left:6px solid #e8262d}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin:0 16px}.c{background:#fff;border-radius:10px;overflow:hidden;font-size:12px}
.c img{width:100%%;display:block;max-height:260px;object-fit:contain;background:#ddd}.c div{padding:6px 8px}.bad{outline:4px solid #e8262d}.ok b{color:#1a8f3a}.bad b{color:#e8262d}
details{margin:0 16px}summary{cursor:pointer;padding:6px 0;font-weight:600}""" % ('#1a8f3a' if not fails else '#e8262d')
    h = [f'<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Alphabet World check</title><style>{css}</style>']
    h.append(f'<header><h1>{"✅ No problems found" if not fails else "❌ " + str(len(fails)) + " problems found"}</h1>'
             f'<div>{len(results)} checks · {time.strftime("%Y-%m-%d %H:%M")} · {round((time.time()-t0)/60,1)} min</div></header>')
    if fails:
        h.append('<h2>Problems</h2>')
        for r in fails:
            h.append(f'<div class=f><b>{html.escape(r["area"])}</b> · {html.escape(r["name"])} · {r["size"]}<br>{html.escape(r["msg"])}'
                     + (f'<br><a href="{r["shot"]}"><img src="{r["shot"]}" style="max-width:320px;margin-top:6px"></a>' if r['shot'] else '') + '</div>')
    for a in areas:
        rs = [r for r in results if r['area'] == a]
        nb = sum(not r['ok'] for r in rs)
        h.append(f'<h2>{html.escape(a)} — {len(rs)-nb}/{len(rs)} OK</h2><details {"open" if nb else ""}><summary>Show pictures</summary><div class=grid>')
        for r in rs:
            h.append(f'<div class="c {"ok" if r["ok"] else "bad"}">' + (f'<a href="{r["shot"]}"><img loading=lazy src="{r["shot"]}"></a>' if r['shot'] else '')
                     + f'<div><b>{"✓" if r["ok"] else "✗"}</b> {html.escape(r["name"])} · {r["size"]}' + (f'<br>{html.escape(r["msg"])}' if r['msg'] else '') + '</div></div>')
        h.append('</div></details>')
    open(os.path.join(OUT, 'index.html'), 'w', encoding='utf-8').write('\n'.join(h))
    json.dump(results, open(os.path.join(OUT, 'results.json'), 'w'), ensure_ascii=False, indent=1)
    md = [f'## {"✅ No problems found" if not fails else "❌ " + str(len(fails)) + " problems found"}', f'{len(results)} checks', '']
    for a in areas:
        rs = [r for r in results if r['area'] == a]; md.append(f'- **{a}**: {sum(r["ok"] for r in rs)}/{len(rs)} OK')
    if fails:
        md += ['', '| Area | What | Size | Problem |', '|---|---|---|---|']
        md += [f'| {r["area"]} | {r["name"]} | {r["size"]} | {r["msg"][:160]} |' for r in fails[:80]]
    md += ['', 'Pictures of every screen: open **qa-report** at the bottom of this page, then index.html']
    open(os.path.join(OUT, 'summary.md'), 'w', encoding='utf-8').write('\n'.join(md))
    return fails

async def main():
    t0 = time.time()
    static_checks()
    async with async_playwright() as p:
        br = await p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required'])
        T = []
        for size in LOAD_SIZES:
            T += [job(t_load, br, f, size, 'girl') for f in GIRL] + [job(t_load, br, f, size, 'boy') for f in BOY]
        for size in ['phone-land', 'phone-port', 'laptop']:
            T.append(job(t_lock, br, size))
        for size in FLOW_SIZES:
            for start in ('index.html', 'index_b.html'):
                for pick in ('girl', 'boy'):
                    T.append(job(t_choose, br, start, pick, size))
                T.append(job(t_switch, br, start, size))
            for page in ('index.html', 'index_b.html'):
                for n in range(1, 6):
                    T.append(job(t_crown, br, page, n, size))
            for kind in ('train', 'bubble', 'tricky', 'trickytrain'):
                for n in range(1, 6):
                    for sfx in ('', '_b'):
                        f = f'{kind}{n}{sfx}.html'
                        if os.path.exists(os.path.join(ROOT, f)): T.append(job(t_end, br, f, size))
            for n in range(1, 6):
                for sfx in ('', '_b'):
                    f = f'ws{n}/index{sfx}.html'
                    if os.path.exists(os.path.join(ROOT, f)): T.append(job(t_ws, br, f, size))
        for n in (1, 3):
            T.append(job(t_ws, br, f'ws{n}/index.html', 'iphone', IPHONE_UA))
        await asyncio.gather(*T)
        await br.close()
    fails = report(t0)
    print(f'\n{len(results)} checks, {len(fails)} problems, {round((time.time()-t0)/60,1)} min')
    sys.exit(1 if fails else 0)

asyncio.run(main())
