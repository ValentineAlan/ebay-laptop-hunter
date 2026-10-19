"""Server-rendered buying pages. Reads cached evidence; never schedules research."""
import hashlib
import html
import json
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

ORIGIN = 'https://laptoplander.com'
CATEGORIES = {
    '/used-laptops-under-200': (
        'Used laptops under £200 UK',
        'Compare UK laptop listings below £200 including delivery. Check condition, missing accessories and upgrade costs before deciding whether a low price is good value.',
        'total < 200',
        'At this budget, prioritise a working machine with an SSD, sufficient memory and a usable battery. A missing charger, replacement battery or operating-system licence can change the total cost. Auction prices can rise before the listing ends.'),
    '/windows-11-used-laptops': (
        'Windows 11 compatible used laptops UK',
        'Browse used laptops whose detected CPU is classified as officially supported for Windows 11. Verify the complete machine requirements before buying.',
        "win11_state IN ('OFFICIAL', 'OFFICIAL_BASIC')",
        'A supported processor is only part of compatibility. Confirm TPM 2.0, UEFI Secure Boot capability, memory and storage with the seller. An advertisement saying Windows 11 is installed does not establish official support. Check the exact CPU rather than relying on a model-family name.'),
    '/usb-c-charging-used-laptops': (
        'Used laptops with USB-C charging UK',
        'Find used laptops flagged as supporting USB-C Power Delivery charging. Confirm the exact variant and required charger wattage with the seller.',
        'usbc_pd = 1',
        'A USB-C socket does not necessarily accept charging. Confirm Power Delivery input on the exact laptop, the required wattage and whether a suitable charger and cable are included. A phone charger may be insufficient even when the connector fits.'),
}


def escape(value):
    return html.escape(str(value or ''), quote=True)


def model_path(brand, model):
    name = f'{brand} {model}'
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:90]
    digest = hashlib.sha256(name.casefold().encode()).hexdigest()[:10]
    return f'/used-laptop-prices/{slug}-{digest}'


def evidence_groups(conn, assess):
    """Deduplicate sold listings, exclude uncertain conditions, and require recent GBP evidence."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    groups = {}
    seen = set()
    rows = conn.execute('''SELECT * FROM sold_comparables
        WHERE last_sold >= ? AND collected_at >= ? AND currency = 'GBP'
        AND delivered_price > 0 AND brand != '' AND model != ''
        ORDER BY collected_at DESC''', (cutoff, cutoff))
    for raw in rows:
        row = dict(raw)
        if not math.isfinite(row['delivered_price']):
            continue
        if not re.search(r'used|refurbished|pre.?owned', row.get('sold_condition') or '', re.I):
            continue
        if assess(row.get('title') or '', row['sold_condition'])['decision'] != 'USE':
            continue
        key = (row['brand'].casefold().strip(), row['model'].casefold().strip())
        identity = (key, row['item_id'])
        if identity in seen:
            continue
        seen.add(identity)
        groups.setdefault(key, []).append(row)
    return {key: rows for key, rows in groups.items() if len(rows) >= 5}


def page(path, title, description, body):
    canonical = ORIGIN + path
    schema = json.dumps({
        '@context': 'https://schema.org', '@type': 'BreadcrumbList',
        'itemListElement': [
            {'@type': 'ListItem', 'position': 1, 'name': 'LaptopLander', 'item': ORIGIN + '/'},
            {'@type': 'ListItem', 'position': 2, 'name': title, 'item': canonical},
        ],
    }).replace('<', '\\u003c')
    return f'''<!doctype html><html lang="en-GB"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} | LaptopLander</title>
<meta name="description" content="{escape(description)}">
<link rel="canonical" href="{escape(canonical)}">
<meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}">
<meta property="og:url" content="{escape(canonical)}"><meta property="og:type" content="website">
<script type="application/ld+json">{schema}</script>
<style>body{{font:17px/1.65 system-ui,sans-serif;color:#172033;background:#f5f7fb;margin:0}}
main{{max-width:960px;margin:auto;padding:24px}}a{{color:#174bb8}}nav{{display:flex;gap:20px;flex-wrap:wrap}}
h1{{line-height:1.2}}section{{background:white;border:1px solid #dce2ec;border-radius:12px;padding:20px;margin:20px 0}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #ddd}}
.scroll{{overflow:auto}}footer{{margin-top:32px}}small{{color:#536075}}</style></head>
<body><main><nav aria-label="Main"><a href="/">LaptopLander deals</a><a href="/buying-guides">Buying guides</a></nav>
<h1>{escape(title)}</h1><p>{escape(description)}</p>{body}
<footer><a href="/privacy">Privacy notice</a></footer></main></body></html>'''


def listing_table(conn, condition, params=()):
    cutoff = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(f'''SELECT * FROM listings WHERE active = 1
        AND total > 0 AND postage IS NOT NULL AND ({condition})
        AND (end_date IS NULL OR end_date = '' OR end_date > ?)
        ORDER BY total ASC LIMIT 30''', (*params, cutoff)).fetchall()
    if not rows:
        return '<p>No matching listings are currently available. The buying guidance remains available while stock changes.</p>'
    cells = []
    for row in rows:
        report = '/report?item_id=' + quote(row['item_id'], safe='')
        cells.append(f'<tr><td><a href="{report}">{escape(row["title"])}</a><br><small>{escape(row["condition"])} · {escape(row["status"])}</small></td><td>£{row["total"]:.2f}</td></tr>')
    return '<p>Cached listing prices include delivery. Availability and auction prices can change; these are matching listings, not all qualified bargains.</p><div class="scroll"><table><thead><tr><th scope="col">Laptop and condition</th><th scope="col">Current delivered price</th></tr></thead><tbody>' + ''.join(cells) + '</tbody></table></div>'


def handle(handler, path, connect, assess):
    if path == '/robots.txt':
        text = 'User-agent: *\nAllow: /\nDisallow: /settings\nDisallow: /diagnostics\nDisallow: /analytics\nDisallow: /live-deals\nDisallow: /valuation-evidence\nSitemap: ' + ORIGIN + '/sitemap.xml\n'
        send_text(handler, text, 'text/plain; charset=utf-8')
        return True
    if path not in CATEGORIES and path not in ('/buying-guides', '/sitemap.xml') and not path.startswith('/used-laptop-prices/'):
        return False
    conn = connect()
    try:
        groups = evidence_groups(conn, assess) if path not in CATEGORIES else {}
        models = {model_path(rows[0]['brand'], rows[0]['model']): rows for rows in groups.values()}
        top = sorted(models, key=lambda p: (-len(models[p]), p))[:5]
        if path == '/sitemap.xml':
            paths = ['/', '/buying-guides', *CATEGORIES, *sorted(models)]
            xml = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(f'<url><loc>{ORIGIN}{escape(p)}</loc></url>' for p in paths) + '</urlset>'
            send_text(handler, xml, 'application/xml; charset=utf-8')
            return True
        if path == '/buying-guides':
            body = '<section><h2>Choose a laptop</h2><ul>' + ''.join(f'<li><a href="{p}">{escape(v[0])}</a></li>' for p, v in CATEGORIES.items()) + '</ul></section><section><h2>Used laptop price guides</h2>'
            body += '<ul>' + ''.join(f'<li><a href="{p}">{escape(models[p][0]["brand"])} {escape(models[p][0]["model"])} used prices UK</a> — {len(models[p])} sold listings</li>' for p in top) + '</ul>' if top else '<p>Price guides appear when at least five distinct recent sold listings meet the evidence requirements.</p>'
            # Link every eligible model so sitemap entries are also discoverable in HTML.
            if len(models) > 5:
                body += '<h3>More model guides</h3><ul>' + ''.join(f'<li><a href="{p}">{escape(models[p][0]["brand"])} {escape(models[p][0]["model"])}</a></li>' for p in sorted(models) if p not in top) + '</ul>'
            body += '</section>'
            title, description = 'Used laptop buying guides and prices UK', 'Choose a used laptop by budget, Windows 11 CPU support or USB-C charging, and compare model prices using recent sold evidence.'
        elif path in CATEGORIES:
            title, description, condition, advice = CATEGORIES[path]
            body = '<section><h2>What to check before buying</h2><p>' + escape(advice) + '</p></section><section><h2>Matching UK listings</h2>' + listing_table(conn, condition) + '</section>'
        elif path in models:
            rows = models[path]
            first = rows[0]
            title = f'{first["brand"]} {first["model"]} used prices UK'
            description = f'Recent UK sold-price evidence for {first["brand"]} {first["model"]}, with specification differences and current listings.'
            prices = [r['delivered_price'] for r in rows]
            body = f'<section><h2>Observed sold prices</h2><p>Median delivered price: <strong>£{statistics.median(prices):.2f}</strong> across {len(rows)} distinct sold listings. Observed range: £{min(prices):.2f}–£{max(prices):.2f}.</p><p>These are cached GBP records sold and collected within the last 90 days. Duplicate listing IDs, new items, unknown conditions and listings with detected faults are excluded. Each listing counts once, regardless of units sold. Prices may be listing averages. No specification or condition adjustment is applied.</p><p>This is a model-level summary, not an exact valuation: CPU, RAM, storage, battery health and warranty can materially change value. Confirm those details before comparing a listing with the median.</p></section>'
            body += '<section><h2>Sold evidence by specification</h2><div class="scroll"><table><thead><tr><th scope="col">CPU / RAM / storage</th><th scope="col">Condition</th><th scope="col">Delivered</th><th scope="col">Last sold</th></tr></thead><tbody>'
            for r in rows[:50]:
                spec = f'{r["cpu"] or "CPU unknown"} / {r["ram_gb"] or "?"} GB / {r["storage_gb"] or "?"} GB'
                body += f'<tr><td>{escape(spec)}</td><td>{escape(r["sold_condition"])}</td><td>£{r["delivered_price"]:.2f}</td><td>{escape(r["last_sold"])}</td></tr>'
            body += '</tbody></table></div><p>Evidence collected: ' + escape(max(r['collected_at'] for r in rows)) + '.</p></section><section><h2>Current listings</h2>' + listing_table(conn, 'LOWER(brand) = ? AND LOWER(model) = ?', (first['brand'].lower(), first['model'].lower())) + '</section>'
        else:
            handler._send_html(page(path, 'Price guide unavailable', 'There is not enough current evidence to publish this guide.', '<p><a href="/buying-guides">Browse available guides</a></p>'), status=404)
            return True
        handler._send_html(page(path, title, description, body))
        return True
    finally:
        conn.close()


def send_text(handler, text, content_type):
    content = text.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)
