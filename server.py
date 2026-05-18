"""
Representatives Scorecard — local backend server.
Run:  python3 server.py
Open: http://localhost:8080
"""

from flask import Flask, jsonify, request, send_from_directory
import requests, re, concurrent.futures, json, csv, io

app = Flask(__name__, static_folder='.')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'})

FEC_API = 'https://api.open.fec.gov/v1'
import os
# Built-in keys loaded from environment variables (set in Railway dashboard)
# These are used as defaults so visitors don't need to enter their own keys.
OPENSTATES_KEY = os.environ.get('OPENSTATES_KEY', '')
FEC_KEY = os.environ.get('FEC_KEY', 'DEMO_KEY')

_fec_cache = {}  # in-memory cache: (name, state, office) → (funders, cycle)
_tu_cache  = {}  # in-memory cache: (name, state) → funders list

# ── IDEOLOGY SCORES (DW-NOMINATE via Voteview) ───────────────────────────────
# nominate_dim1: -1 = most progressive, +1 = most conservative → normalized to 0-1
_ideology_by_bioguide = {}

def _load_voteview():
    for chamber in ('H', 'S'):
        url = f'https://voteview.com/static/data/out/members/{chamber}119_members.csv'
        r = requests.get(url, timeout=15)
        if not r.ok:
            continue
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            bio = row.get('bioguide_id', '').strip()
            dim1 = row.get('nominate_dim1', '').strip()
            if bio and dim1:
                try:
                    _ideology_by_bioguide[bio] = round((float(dim1) + 1) / 2, 3)
                except ValueError:
                    pass

try:
    _load_voteview()
except Exception:
    pass

_PARTY_IDEOLOGY = {
    'democrat': 0.20, 'democratic': 0.20,
    'republican': 0.80,
    'independent': 0.50,
    'green': 0.09,
    'libertarian': 0.72,
    'nonpartisan': 0.50,
}

def party_to_ideology(party):
    if not party: return None
    p = party.lower()
    for key, val in _PARTY_IDEOLOGY.items():
        if key in p: return val
    return None

_FUNDER_SKIP = frozenset({
    'RETIRED', 'SELF-EMPLOYED', 'SELF', 'HOMEMAKER', 'NONE', 'N/A',
    'NOT EMPLOYED', 'UNEMPLOYED', 'INFORMATION REQUESTED', 'REQUESTED',
    'SELF EMPLOYED', 'STUDENT', 'INFORMATION REQUESTED PER BEST EFFORTS',
    'N/A - RETIRED', 'DISABLED', 'PHYSICIAN', 'ATTORNEY', 'CONSULTANT',
    'NONE GIVEN', 'NOT GIVEN', 'INDIVIDUAL', 'VARIOUS',
})

@app.after_request
def add_cors(r):
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory('.', filename)

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def get(url, params=None, timeout=12):
    try:
        r = SESSION.get(url, params=params, timeout=timeout, allow_redirects=True)
        return r if r.ok else None
    except Exception:
        return None

def get_json(url, params=None, timeout=12):
    r = get(url, params=params, timeout=timeout)
    if r:
        try: return r.json()
        except Exception: pass
    return None

def get_html(url, params=None, timeout=12):
    r = get(url, params=params, timeout=timeout)
    return r.text if r else ''

def strip_tags(s):
    return re.sub(r'<[^>]+>', '', s).strip()

# ─────────────────────────────────────────────────────────────────────────────
# ISSUE DETECTION (from bill titles)
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_KEYWORDS = [
    ('Healthcare',           r'health|medical|medicare|medicaid|drug|hospital|prescription|opioid|fentanyl|dental|pharmacy|pharma|nurse|physician'),
    ('Immigration',          r'immigr|border|visa|asylum|deport|daca|migrant'),
    ('National Defense',     r'defense|military|army|navy|air force|marine|pentagon|weapon|nato'),
    ('Gun Policy',           r'gun|firearm|second amendment|rifle|pistol|ammunition'),
    ('Tax Policy',           r'tax|irs|revenue|tariff|fiscal|ad valorem|homestead exemption'),
    ('Education',            r'educat|school|college|student loan|teacher|university|special education|virtual learning|charter|tuition|truancy'),
    ('Energy & Environment', r'energy|climate|carbon|fossil|renewable|oil|gas|environment|emission'),
    ('Criminal Justice',     r'crime|police|law enforcement|prison|fentanyl|trafficking|cartel|sheriff|court|penal|offense|criminal'),
    ('Trade & Economy',      r'trade|export|import|manufactur|job|employ|economy|inflation'),
    ('Infrastructure',       r'infrastructure|road|bridge|highway|transit|broadband|water'),
    ('Veterans Affairs',     r'veteran|va benefit|military service|wounded warrior|peace officer'),
    ('Technology',           r'technolog|ai |artificial intelligence|cyber|data privacy|social media|electronic'),
    ('Housing',              r'housing|rent|mortgage|homeless|affordable|eviction|residential lot|zoning|landlord|tenant|squatt'),
    ('Reproductive Rights',  r'abortion|reproductive|planned parenthood'),
    ('Agriculture',          r'agricultur|farm|ranch|crop|cattle|food safety|cottage food'),
    ('Public Safety',        r'public safety|emergency|disaster|fema|first responder|fire marshal|tracking device|security committee'),
    ('Election Integrity',   r'election|ballot|voter|voting|registrar|poll worker|precinct'),
    ('Property Rights',      r'property tax|appraisal|homestead|eminent domain|ad valorem'),
]

def bills_to_issues(titles):
    freq = {}
    for title in titles:
        t = title.lower()
        for label, pattern in ISSUE_KEYWORDS:
            if re.search(pattern, t):
                freq[label] = freq.get(label, 0) + 1
    return [k for k, _ in sorted(freq.items(), key=lambda x: -x[1])][:3]

# ─────────────────────────────────────────────────────────────────────────────
# WIKIPEDIA PARTY LOOKUP (free, no key)
# ─────────────────────────────────────────────────────────────────────────────

def ballotpedia_info(name):
    """Return (party, photo_url) from Ballotpedia infobox. Very reliable for party."""
    bp_name = name.strip().replace(' ', '_')
    html = get_html(f'https://ballotpedia.org/{bp_name}')
    if not html:
        return None, None
    # Party is in the infobox CSS class e.g. class="widget-row value-only Republican"
    pm = re.search(r'class="widget-row value-only (Republican|Democrat\w*|Independent|Libertarian|Green)"', html, re.I)
    party = pm.group(1).capitalize() if pm else None
    # Photo
    fm = re.search(r'<img\s+src="(https://s3[^"]*ballotpedia[^"]*(?:jpg|jpeg|png))"[^>]*class="widget-img"', html, re.I)
    photo = fm.group(1) if fm else None
    return party, photo

def ballotpedia_photo(name):
    _, photo = ballotpedia_info(name)
    return photo

def wikipedia_info(name):
    """Return (party, photo_url) from Wikipedia for a politician."""
    data = get_json('https://en.wikipedia.org/w/api.php', {
        'action': 'query', 'titles': name,
        'prop': 'extracts|pageimages',
        'format': 'json', 'explaintext': True,
        'pithumbsize': 300,
    })
    if not data:
        return None, None
    pages = data.get('query', {}).get('pages', {})
    for page in pages.values():
        text = page.get('extract', '')
        m = re.search(r'(Republican|Democrat\w*|Independent|Libertarian|Green)', text, re.I)
        party = m.group(1).capitalize() if m else None
        photo = page.get('thumbnail', {}).get('source')
        return party, photo
    return None, None

def wikipedia_party(name):
    party, _ = wikipedia_info(name)
    return party

# ─────────────────────────────────────────────────────────────────────────────
# WIKIPEDIA WIKITEXT — infobox parsing for local officials
# ─────────────────────────────────────────────────────────────────────────────

def wikipedia_wikitext(title):
    data = get_json('https://en.wikipedia.org/w/api.php', {
        'action': 'query', 'titles': title,
        'prop': 'revisions', 'rvprop': 'content',
        'rvsection': '0', 'format': 'json', 'redirects': 1,
    })
    if not data:
        return ''
    for page in data.get('query', {}).get('pages', {}).values():
        if 'missing' in page:
            continue
        revs = page.get('revisions', [])
        if revs:
            return revs[0].get('*') or revs[0].get('content', '')
    return ''

def infobox_field(wikitext, *keys):
    for key in keys:
        m = re.search(rf'\|\s*{re.escape(key)}\s*=\s*([^\n|}}]+)', wikitext, re.I)
        if m:
            val = m.group(1).strip()
            # If there's a wikilink, take only the first one's display text
            wl = re.search(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', val)
            if wl:
                val = wl.group(1).strip()
            else:
                val = re.sub(r'\{\{[^}]+\}\}', '', val)
                val = re.sub(r'<[^>]+>', '', val)
            val = val.strip().strip("'\"").strip()
            if val and val.lower() not in ('', 'none', 'vacant', 'tbd', 'n/a'):
                return val
    return None

def local_official(name, office, state, website=None):
    bp_party, bp_photo = ballotpedia_info(name)
    party, photo = bp_party, bp_photo
    if not party or not photo:
        wiki_party, wiki_photo = wikipedia_info(name)
        party = party or wiki_party
        photo = photo or wiki_photo
    return {
        'name': name,
        'office': office,
        'level': 'local',
        'party': party or 'Unknown',
        'photoUrl': photo,
        'phones': [],
        'urls': [website or f'https://ballotpedia.org/{name.replace(" ", "_")}'],
        'emails': [],
        'address': [],
        'channels': [],
        'topIssues': [],
        'campaignPositions': [],
        'termStart': None,
        'termEnd': None,
    }

def fetch_city_mayor(city, state_name):
    for wiki_title in [city, f'{city}, {state_name}']:
        wikitext = wikipedia_wikitext(wiki_title)
        if not wikitext:
            continue
        name = infobox_field(wikitext, 'mayor', 'mayor_name', 'leader_name', 'leader1_name')
        if name:
            return local_official(name, f'Mayor of {city}', state_name)
    return None

def fetch_county_judge(county, state, state_name):
    county_display = county if 'County' in county else f'{county} County'
    county_clean = re.sub(r'\s*County\s*$', '', county_display, flags=re.I).strip().lower()
    wikitext = wikipedia_wikitext(f'{county_display}, {state_name}')
    if not wikitext:
        return None
    name = infobox_field(wikitext, 'leader_name', 'leader1_name', 'county_judge', 'judge', 'executive')
    if not name:
        return None
    raw_title = infobox_field(wikitext, 'leader_title', 'leader1_title') or (
        'County Judge' if state == 'TX' else 'County Executive'
    )
    title = raw_title.title()
    card = local_official(name, f'{title}, {county_display}', state)
    judge_data = COUNTY_JUDGE_DATA.get((county_clean, state))
    if judge_data:
        card['photoUrl'], card['topIssues'] = judge_data
    return card

def scrape_bp_officeholders(bp_url, default_office, max_results=10):
    """Scrape Ballotpedia city/county page officeholder-table for current members."""
    html = get_html(bp_url)
    if not html:
        return []
    results = []
    seen = set()
    # Ballotpedia uses id="officeholder-table" for current officeholder grids.
    # Column layout: [0] Office title, [1] Person name (link), [2] Party, [3+] dates
    for tbl_m in re.finditer(r'id="officeholder-table"', html):
        end_idx = html.find('</table>', tbl_m.start())
        table_html = html[tbl_m.start(): end_idx + 8] if end_idx > 0 else html[tbl_m.start(): tbl_m.start() + 8000]
        for row in re.split(r'</tr>', table_html)[1:]:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if len(cells) < 2:
                continue
            office_text = re.sub(r'<[^>]+>', '', cells[0]).strip()
            # Name is in cell[1] as an absolute ballotpedia.org link
            name_m = re.search(r'href="https?://ballotpedia\.org/([^"]+)"[^>]*>([^<]+)</a>', cells[1])
            if not name_m:
                continue
            bp_slug, name = name_m.group(1), name_m.group(2).strip()
            if (not name or name in seen or len(name) > 55
                    or re.search(r'District \d+|City Council$|County$|Court$', name, re.I)):
                continue
            seen.add(name)
            # Party class is on the <td> tag itself, so search the raw row HTML
            pm = re.search(r'class="partytd (\w+)"[^>]*>([^<]*)', row)
            party = 'Unknown'
            if pm:
                raw, party_text = pm.group(1), pm.group(2).strip()
                party = party_text if raw == 'Gray' else raw
            results.append({
                'name': name,
                'office': office_text or default_office,
                'level': 'local',
                'party': party or 'Nonpartisan',
                'photoUrl': None,
                'phones': [],
                'urls': [f'https://ballotpedia.org/{bp_slug}'],
                'emails': [],
                'address': [],
                'channels': [],
                'topIssues': [],
                'termStart': None,
                'termEnd': None,
            })
            if len(results) >= max_results:
                break
    return results

# ─────────────────────────────────────────────────────────────────────────────
# BALLOTPEDIA — STATED POSITIONS
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_TOPICS = frozenset({
    'see also', 'references', 'notes', 'external links', 'footnotes',
    'biography', 'elections', 'campaign finance summary', 'notable endorsements',
    'personal finance disclosures', 'committee assignments', 'analysis',
    'trending', 'information for candidates', 'get engaged', 'services',
    'election analysis', 'additional analysis', 'public policy',
    'senate leadership elections', 'house leadership elections',
    'information about voting', '2026 elections', '2025 elections',
})

_POLICY_TOPICS = re.compile(
    r'national security|economy|immigration|social issue|healthcare|health care|'
    r'education|environment|energy|gun|criminal justice|housing|infrastructure|'
    r'tax|budget|abortion|reproductive|trade|agriculture|veteran|technology|'
    r'election|voting|public safety|border', re.I
)

_BP_SKIP_TEXT = re.compile(
    r"Congress began|Congress enacted|introduced bills|comparatively|"
    r"Ballotpedia.s analysis|uncontested election|202[56] election|"
    r"analysis of|Ballotpedia tracked|Click here|See also|References|"
    r"born in|grew up|graduated|degree from|married|children|served in the military",
    re.I,
)

def scrape_bp_positions(name):
    """Scrape Ballotpedia for key policy positions. Returns [{topic, stance}, ...]."""
    clean_name = re.sub(r'\s*\[[^\]]+\]$', '', name).strip()
    bp_name = clean_name.replace(' ', '_')
    html = get_html(f'https://ballotpedia.org/{bp_name}')
    if not html:
        return []

    positions = []
    seen_topics = set()

    # Primary: structured h3 policy sections (works well for federal reps)
    for h3_m in re.finditer(
            r'<h3[^>]*>.*?<span[^>]*class="mw-headline"[^>]*>([^<]+)</span>', html, re.S):
        topic = strip_tags(h3_m.group(1)).strip()
        topic_lower = topic.lower()
        if not topic or topic_lower in _SKIP_TOPICS or topic_lower in seen_topics:
            continue
        if not _POLICY_TOPICS.search(topic):
            continue
        seen_topics.add(topic_lower)
        block_start = h3_m.end()
        next_h3 = html.find('<h3', block_start)
        block = html[block_start: next_h3 if next_h3 > 0 else block_start + 4000]
        for tag_m in re.finditer(r'<(?:p|li)[^>]*>(.*?)</(?:p|li)>', block, re.S):
            clean = re.sub(r'\[\d+\]', '', strip_tags(tag_m.group(1))).strip()
            if not _BP_SKIP_TEXT.search(clean) and len(clean) >= 40:
                positions.append({'topic': topic, 'stance': clean[:220]})
                break
        if len(positions) >= 5:
            break

    # Fallback: scan h2 sections for any substantive policy content (state/local officials)
    if not positions:
        for h2_m in re.finditer(
                r'<h2[^>]*>.*?<span[^>]*class="mw-headline"[^>]*>([^<]+)</span>', html, re.S):
            topic = strip_tags(h2_m.group(1)).strip()
            topic_lower = topic.lower()
            if not topic or topic_lower in _SKIP_TOPICS or topic_lower in seen_topics:
                continue
            if re.search(r'biography|personal|early life|career', topic_lower):
                continue
            seen_topics.add(topic_lower)
            block_start = h2_m.end()
            next_h2 = html.find('<h2', block_start)
            block = html[block_start: next_h2 if next_h2 > 0 else block_start + 3000]
            for tag_m in re.finditer(r'<(?:p|li)[^>]*>(.*?)</(?:p|li)>', block, re.S):
                clean = re.sub(r'\[\d+\]', '', strip_tags(tag_m.group(1))).strip()
                if not _BP_SKIP_TEXT.search(clean) and len(clean) >= 50:
                    # Label with whatever the h2 section was
                    label = topic if _POLICY_TOPICS.search(topic) else 'Stated Position'
                    positions.append({'topic': label, 'stance': clean[:220]})
                    break
            if len(positions) >= 4:
                break

    # Last resort: grab strong lead sentences from the intro paragraphs
    if not positions:
        intro_m = re.search(r'<div[^>]*class="[^"]*mw-parser-output[^"]*"[^>]*>([\s\S]{200,3000}?)<(?:h2|div)', html)
        if intro_m:
            intro = intro_m.group(1)
            for tag_m in re.finditer(r'<p[^>]*>(.*?)</p>', intro, re.S):
                clean = re.sub(r'\[\d+\]', '', strip_tags(tag_m.group(1))).strip()
                if not _BP_SKIP_TEXT.search(clean) and len(clean) >= 60:
                    # Only take sentences that mention policy, priorities, or focus
                    sentences = re.split(r'(?<=[.!?])\s+', clean)
                    for sent in sentences:
                        if re.search(
                            r'priorit|focus|commit|promis|platform|support|oppos|advocat|'
                            r'champion|push(?:ed|es|ing)|fight|reform|protect|expand|cut|'
                            r'tax|healthcare|education|infrastructure|public safety|'
                            r'budget|housing|environment|border|crime|gun|immigration', sent, re.I
                        ) and len(sent) >= 40:
                            positions.append({'topic': 'Campaign Focus', 'stance': sent[:220]})
                    if positions:
                        break
                if len(positions) >= 3:
                    break

    return positions[:5]

# ─────────────────────────────────────────────────────────────────────────────
# CAMPAIGN FINANCE — FEC (federal) + TEC (TX state)
# ─────────────────────────────────────────────────────────────────────────────

def get_fec_top_funders(name, state, office_letter, fec_key=None):
    """Top financial backers from FEC for a federal candidate.
    Primary: outside spending (schedule_e) — shows PACs/orgs that backed the candidate.
    Fallback: direct PAC contributions (schedule_a, committee contributors).
    office_letter: 'S' (Senate) or 'H' (House).
    Returns (list[{name, amount, cycle}], cycle) or ([], None).
    """
    import datetime
    clean = re.sub(r'\s*\[[^\]]+\]$', '', name).strip()
    key = (clean.lower(), state, office_letter)
    if key in _fec_cache:
        return _fec_cache[key]
    api_key = fec_key or FEC_KEY
    _SKIP_PAC = frozenset({
        'WINRED', 'ACTBLUE', 'WIN RED', 'ACT BLUE',
        'REPUBLICAN NATIONAL COMMITTEE', 'DEMOCRATIC NATIONAL COMMITTEE',
        'NRCC', 'DCCC', 'NRSC', 'DSCC',
    })

    # Step 1: find the candidate record to get candidate_id and election years
    cand_r = get_json(f'{FEC_API}/candidates/', {
        'q': clean, 'state': state, 'office': office_letter,
        'sort': '-receipts', 'per_page': 3, 'api_key': api_key,
    })
    if not cand_r or not cand_r.get('results'):
        _fec_cache[key] = ([], None)
        return [], None
    # Check for rate limit error
    if cand_r.get('error'):
        return [], None
    cand = cand_r['results'][0]
    cand_id = cand.get('candidate_id')
    if not cand_id:
        _fec_cache[key] = ([], None)
        return [], None

    # Most recent election that has already happened.
    # Elections are in November; in April 2026 the last completed election was Nov 2024.
    today = datetime.date.today()
    if today.year % 2 == 0 and today.month < 11:
        last_election_year = today.year - 2
    elif today.year % 2 == 1:
        last_election_year = today.year - 1
    else:
        last_election_year = today.year
    years = [y for y in (cand.get('election_years') or []) if isinstance(y, int) and y <= last_election_year]
    cycle = max(years) if years else last_election_year

    funders = []
    seen = set()

    # Step 2: outside spending — super PACs / orgs that independently spent money to support this candidate
    spend_r = get_json(f'{FEC_API}/schedules/schedule_e/by_candidate/', {
        'candidate_id': cand_id, 'support_oppose_indicator': 'S',
        'cycle': cycle, 'sort': '-total', 'per_page': 12, 'api_key': api_key,
    })
    if spend_r and not spend_r.get('error'):
        for item in spend_r.get('results', []):
            n = (item.get('committee_name') or '').strip()
            nu = n.upper()
            if not n or nu in _SKIP_PAC or nu in seen:
                continue
            words = n.split()
            display = n.title() if (n == nu and len(words) >= 3) else n
            cmte_id_val = item.get('committee_id') or ''
            funders.append({
                'name': display,
                'amount': item.get('total', 0),
                'cycle': cycle,
                'url': f'https://www.fec.gov/data/committee/{cmte_id_val}/' if cmte_id_val else '',
            })
            seen.add(nu)
            if len(funders) >= 7:
                break

    # Step 3: supplement with direct PAC contributions if outside spending was sparse
    if len(funders) < 4:
        cmte_r = get_json(f'{FEC_API}/candidate/{cand_id}/committees/', {
            'designation': 'P', 'api_key': api_key,
        })
        if cmte_r and cmte_r.get('results') and not cmte_r.get('error'):
            cmte_id = cmte_r['results'][0]['committee_id']
            pac_r = get_json(f'{FEC_API}/schedules/schedule_a/', {
                'committee_id': cmte_id, 'two_year_transaction_period': cycle,
                'contributor_type': 'committee',
                'sort': '-contribution_receipt_amount', 'per_page': 30,
                'api_key': api_key,
            })
            if pac_r and not pac_r.get('error'):
                for item in pac_r.get('results', []):
                    n = (item.get('contributor_name') or '').strip()
                    nu = n.upper()
                    if not n or nu in _SKIP_PAC or nu in seen:
                        continue
                    amt = item.get('contribution_receipt_amount', 0) or 0
                    if amt < 5000:
                        continue
                    words = n.split()
                    display = n.title() if (n == nu and len(words) >= 3) else n
                    contrib_id = item.get('contributor_id') or ''
                    funders.append({
                        'name': display,
                        'amount': amt,
                        'cycle': cycle,
                        'url': f'https://www.fec.gov/data/committee/{contrib_id}/' if contrib_id else '',
                    })
                    seen.add(nu)
                    if len(funders) >= 7:
                        break

    result = (funders, cycle) if funders else ([], None)
    _fec_cache[key] = result
    return result


# States covered by Transparency USA (transparencyusa.org)
_TRANSPARENCY_USA_STATES = frozenset({
    'AL', 'AZ', 'CA', 'CO', 'FL', 'GA', 'IL', 'IN', 'IA', 'MI', 'MN',
    'NV', 'NH', 'NM', 'NY', 'NC', 'OH', 'PA', 'SC', 'TX', 'VA', 'WA', 'WI', 'WY',
})

# Official state campaign finance disclosure sites for states not in Transparency USA
_STATE_FINANCE = {
    'AK': ('https://aws.state.ak.us/ApocReports/CampaignDisclosure/', 'Alaska APOC'),
    'AR': ('https://www.sos.arkansas.gov/elections/financial-disclosure/', 'Arkansas SOS'),
    'CT': ('https://seec.ct.gov/', 'CT SEEC'),
    'DE': ('https://elections.delaware.gov/services/candidate/candpublic.shtml', 'Delaware Elections'),
    'HI': ('https://ags.hawaii.gov/campaign/', 'Hawaii Campaign Spending'),
    'ID': ('https://elections.sos.idaho.gov/', 'Idaho SOS'),
    'KS': ('https://www.kansas.gov/cfd/', 'Kansas CFD'),
    'KY': ('https://kref.ky.gov/', 'Kentucky Registry'),
    'LA': ('https://www.ethics.la.gov/', 'Louisiana Ethics'),
    'ME': ('https://mainecampaignfinance.com/', 'Maine CFR'),
    'MD': ('https://campaignfinancemd.us/', 'Maryland SBE'),
    'MA': ('https://www.ocpf.us/', 'Massachusetts OCPF'),
    'MS': ('https://www.sos.ms.gov/Elections-Voting/Pages/Campaign-Finance.aspx', 'Mississippi SOS'),
    'MO': ('https://mec.mo.gov/', 'Missouri Ethics'),
    'MT': ('https://politicalpractices.mt.gov/', 'Montana COPP'),
    'NE': ('https://www.nadc.nebraska.gov/', 'Nebraska NADC'),
    'NJ': ('https://www.elec.state.nj.us/', 'New Jersey ELEC'),
    'ND': ('https://campaignfinance.nd.gov/', 'North Dakota SOS'),
    'OK': ('https://guardian.ok.gov/', 'Oklahoma Guardian'),
    'OR': ('https://sos.oregon.gov/elections/Pages/efs.aspx', 'Oregon ORESTAR'),
    'RI': ('https://ricampaignfinance.com/', 'Rhode Island CFR'),
    'SD': ('https://sosenterprise.sd.gov/BusinessServices/Elections/', 'South Dakota SOS'),
    'TN': ('https://www.tn.gov/tref/', 'Tennessee Registry'),
    'UT': ('https://disclosures.utah.gov/', 'Utah FCPP'),
    'VT': ('https://sos.vermont.gov/elections/campaign-finance/', 'Vermont SOS'),
    'WV': ('https://cfrs.wvsos.gov/', 'West Virginia SOS'),
}


def get_funder_url(name, state):
    """Return (url, source_name) for a state/local official's public campaign finance records."""
    clean = re.sub(r'\s*\[[^\]]+\]$', '', name).strip()
    parts = clean.split()
    if state in _TRANSPARENCY_USA_STATES:
        st = state.lower()
        if len(parts) >= 2:
            first = re.sub(r'[^a-z]', '', parts[0].lower())
            last  = re.sub(r'[^a-z]', '', parts[-1].lower())
            url = f'https://www.transparencyusa.org/{st}/candidate/{first}-{last}'
        else:
            url = f'https://www.transparencyusa.org/{st}/'
        return url, 'Transparency USA'
    fallback = _STATE_FINANCE.get(state)
    if fallback:
        return fallback  # already (url, name) tuple
    return None, None


def _parse_tu_nuxt(html, state_lower):
    """Parse top contributors from a Transparency USA candidate page's __NUXT__ blob."""
    idx = html.find('window.__NUXT__')
    if idx < 0:
        return []
    end_idx = html.find('</script>', idx)
    full = html[idx:end_idx]

    params_m = re.search(r'\(function\(([^)]+)\)\{', full)
    if not params_m:
        return []
    params = [p.strip() for p in params_m.group(1).split(',')]

    # Bracket-count to find end of function body
    depth = 0; in_str = False; str_char = None
    body_start = full.find('{', full.find('){'))
    body_end = body_start
    for i in range(body_start, len(full)):
        c = full[i]
        if in_str:
            if c == str_char and (i == 0 or full[i-1] != '\\'):
                in_str = False
            continue
        if c in ('"', "'"):
            in_str = True; str_char = c; continue
        if c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                body_end = i; break

    tail = full[body_end + 1:].strip()
    if tail.startswith('('): tail = tail[1:]
    if tail.endswith('));'): tail = tail[:-3]
    elif tail.endswith('))'): tail = tail[:-2]
    elif tail.endswith(')'): tail = tail[:-1]

    try:
        fixed = re.sub(r'(?<!["\w.\d])\.(\d+)', r'0.\1', tail)
        args = json.loads('[' + fixed + ']')
        mapping = dict(zip(params, args))
    except Exception:
        return []

    def resolve(val):
        v = val.strip()
        return mapping.get(v, v)

    # Top contributors are in the "1":{items:[...]} fetch block
    items_m = re.search(r'"1":\{items:\[(.*?)\],info', full, re.S)
    if not items_m:
        return []

    funders = []
    for entry_m in re.finditer(r'\{([^{}]+)\}', items_m.group(1)):
        entry = entry_m.group(1)

        def get_field(fname):
            m = re.search(rf'\b{fname}:("(?:[^"\\]|\\.)*"|[^,}}]+)', entry)
            if not m: return None
            raw = m.group(1)
            if raw.startswith('"'): return raw[1:-1].replace('\\"', '"')
            return resolve(raw)

        name = get_field('contributorName')
        amount = get_field('electionAmount')
        slug = get_field('contributorSlug')
        if not name or amount is None: continue
        try:
            amount = float(amount)
        except Exception:
            continue
        url = f'https://www.transparencyusa.org/{state_lower}/contributor/{slug}' if slug and isinstance(slug, str) and slug.startswith('"') is False and slug != 'null' else ''
        if isinstance(slug, str) and not slug.startswith('http'):
            url = f'https://www.transparencyusa.org/{state_lower}/contributor/{slug}'
        funders.append({'name': name, 'amount': amount, 'url': url})

    return funders[:10]


def get_transparency_usa_funders(name, state):
    """Fetch actual top donor data from Transparency USA candidate page.
    Returns list of {name, amount, url} dicts, or [] on failure.
    """
    clean = re.sub(r'\s*\[[^\]]+\]$', '', name).strip()
    parts = clean.split()
    if state not in _TRANSPARENCY_USA_STATES or len(parts) < 2:
        return []

    key = (clean.lower(), state)
    if key in _tu_cache:
        return _tu_cache[key]

    st = state.lower()
    first = re.sub(r'[^a-z]', '', parts[0].lower())
    last  = re.sub(r'[^a-z]', '', parts[-1].lower())
    slug  = f'{first}-{last}'
    url   = f'https://www.transparencyusa.org/{st}/candidate/{slug}'

    html = get_html(url)
    if not html or 'window.__NUXT__' not in html:
        _tu_cache[key] = []
        return []

    funders = _parse_tu_nuxt(html, st)
    _tu_cache[key] = funders
    return funders



# ─────────────────────────────────────────────────────────────────────────────
# GOVTRACK — FEDERAL REPS
# ─────────────────────────────────────────────────────────────────────────────

GOVTRACK = 'https://www.govtrack.us/api/v2'

def govtrack_roles(params):
    data = get_json(GOVTRACK + '/role', {**params, 'format': 'json'})
    return data.get('objects', []) if data else []

def get_top_issues(person_id):
    data = get_json(GOVTRACK + '/bill', {
        'sponsor': person_id, 'congress': 119, 'limit': 30, 'format': 'json'
    })
    if not data:
        return []
    return bills_to_issues([b.get('title', '') for b in data.get('objects', [])])

def get_key_votes(person_id, limit=6):
    """Fetch recent floor passage votes from GovTrack."""
    data = get_json(GOVTRACK + '/vote_voter', {
        'person': person_id, 'limit': 80,
        'order_by': '-created', 'format': 'json',
    })
    if not data:
        return []
    votes = []
    for vv in data.get('objects', []):
        key = (vv.get('option') or {}).get('key', '0')
        if key not in ('+', '-'):
            continue
        vote = vv.get('vote') or {}
        if vote.get('category') not in ('passage', 'passage-suspension'):
            continue
        desc = (vote.get('question') or '').strip()
        if not desc or len(desc) < 15:
            continue
        if re.search(r'adjournment|quorum|recess|journal|sine die|motion to proceed|'
                     r'Providing for consideration|previous question|rule providing', desc, re.I):
            continue
        votes.append({
            'description': desc[:110],
            'voted': 'Yes' if key == '+' else 'No',
            'month': (vote.get('created') or '')[:7],
        })
        if len(votes) >= limit:
            break
    return votes

def role_to_federal(role):
    p = role.get('person', {})
    link = p.get('link', '')
    id_match = re.search(r'/(\d+)$', link)
    person_id = id_match.group(1) if id_match else None
    name = re.sub(r'^(Sen\.|Rep\.)\s*', '', p.get('name', ''))
    state = role.get('_state', '') or role.get('state', '')
    role_type = role.get('role_type', '')
    office_letter = 'S' if role_type == 'senator' else 'H'
    fec_key = role.get('_fec_key')

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        issues_f    = ex.submit(get_top_issues,      person_id) if person_id else None
        positions_f = ex.submit(scrape_bp_positions, name)
        votes_f     = ex.submit(get_key_votes,       person_id) if person_id else None
        funders_f   = ex.submit(get_fec_top_funders, name, state, office_letter, fec_key)
        issues    = issues_f.result()    if issues_f    else []
        positions = positions_f.result()
        votes     = votes_f.result()     if votes_f     else []
        funders, f_cycle = funders_f.result()

    channels = []
    if p.get('twitterid'):
        channels.append({'type': 'Twitter', 'id': p['twitterid']})
    if p.get('youtubeid'):
        channels.append({'type': 'YouTube', 'id': p['youtubeid']})
    photo = None
    if p.get('bioguideid'):
        photo = f"https://bioguide.congress.gov/bioguide/photo/{p['bioguideid'][0]}/{p['bioguideid']}.jpg"
    extra = role.get('extra', {})
    bio = p.get('bioguideid', '')
    ideology = _ideology_by_bioguide.get(bio) if bio else None
    if ideology is None:
        ideology = party_to_ideology(role.get('party', ''))
    return {
        'name': name,
        'office': role.get('description', ''),
        'level': 'federal',
        'party': role.get('party', 'Unknown'),
        'photoUrl': photo,
        'phones': [role['phone']] if role.get('phone') else [],
        'urls': [role['website']] if role.get('website') else [],
        'emails': [],
        'address': [{'line1': extra.get('address', '')}] if extra.get('address') else [],
        'channels': channels,
        'topIssues': issues,
        'campaignPositions': positions,
        'keyVotes': votes,
        'topFunders': funders,
        'funderCycle': f_cycle,
        'ideologyScore': ideology,
        'termStart': (role.get('startdate') or '')[:4] or None,
        'termEnd': role.get('enddate') or None,
    }

def get_federal_reps(state, cd, fec_key=None):
    senators = govtrack_roles({'role_type': 'senator', 'current': 'true', 'state': state})
    reps = govtrack_roles({'role_type': 'representative', 'current': 'true', 'state': state, 'district': cd}) if cd else []
    for role in senators + reps:
        role['_state'] = state
        role['_fec_key'] = fec_key
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(role_to_federal, senators + reps))

# ─────────────────────────────────────────────────────────────────────────────
# OPENSTATES — STATE REPS (optional key)
# ─────────────────────────────────────────────────────────────────────────────

STATE_NAMES = {
    'AL':'Alabama','AK':'Alaska','AZ':'Arizona','AR':'Arkansas','CA':'California',
    'CO':'Colorado','CT':'Connecticut','DE':'Delaware','FL':'Florida','GA':'Georgia',
    'HI':'Hawaii','ID':'Idaho','IL':'Illinois','IN':'Indiana','IA':'Iowa',
    'KS':'Kansas','KY':'Kentucky','LA':'Louisiana','ME':'Maine','MD':'Maryland',
    'MA':'Massachusetts','MI':'Michigan','MN':'Minnesota','MS':'Mississippi',
    'MO':'Missouri','MT':'Montana','NE':'Nebraska','NV':'Nevada','NH':'New Hampshire',
    'NJ':'New Jersey','NM':'New Mexico','NY':'New York','NC':'North Carolina',
    'ND':'North Dakota','OH':'Ohio','OK':'Oklahoma','OR':'Oregon','PA':'Pennsylvania',
    'RI':'Rhode Island','SC':'South Carolina','SD':'South Dakota','TN':'Tennessee',
    'TX':'Texas','UT':'Utah','VT':'Vermont','VA':'Virginia','WA':'Washington',
    'WV':'West Virginia','WI':'Wisconsin','WY':'Wyoming','DC':'District of Columbia',
}

def get_state_reps_openstates(state, senate_district, house_district, api_key):
    results = []
    for chamber, district, title in [
        ('upper', senate_district, 'State Senator'),
        ('lower', house_district, 'State Representative'),
    ]:
        if not district:
            continue
        data = get_json('https://v3.openstates.org/people', {
            'jurisdiction': STATE_NAMES.get(state, state),
            'district': district, 'chamber': chamber,
            'current_role': 'true', 'per_page': 3, 'apikey': api_key,
        })
        for p in (data or {}).get('results', []):
            channels = []
            if p.get('ids', {}).get('twitter'):
                channels.append({'type': 'Twitter', 'id': p['ids']['twitter']})
            links = p.get('links', [])
            results.append({
                'name': p.get('name', ''),
                'office': f"{title}, District {district}",
                'level': 'state',
                'party': p.get('party', 'Unknown'),
                'photoUrl': p.get('image'),
                'phones': [],
                'urls': [links[0]['url']] if links else [],
                'emails': [],
                'address': [],
                'channels': channels,
                'topIssues': [],
                'campaignPositions': [],
                'termStart': None,
                'termEnd': None,
            })
    return results

# ─────────────────────────────────────────────────────────────────────────────
# TLO — TEXAS LEGISLATURE BILL ISSUES + VOTING RECORD
# ─────────────────────────────────────────────────────────────────────────────

_tlo_code_cache = {}

def get_tlo_member_code(full_name, chamber):
    """Return TLO member code by matching last name in the Capitol TX member list."""
    last = full_name.strip().split()[-1].lower()
    key = (last, chamber)
    if key in _tlo_code_cache:
        return _tlo_code_cache[key]
    ch = 'S' if chamber == 'senate' else 'H'
    html = get_html(f'https://www.capitol.texas.gov/Members/Members.aspx?Chamber={ch}')
    if not html:
        return None
    # Links look like: href='MemberInfo.aspx?...&Code=A1055'>\n\t\t\t\t\t\t\t\tBettencourt
    m = re.search(
        rf"href='MemberInfo\.aspx\?[^']*Code=(A\d+)'[^>]*>\s*{re.escape(last.capitalize())}",
        html, re.I
    )
    code = m.group(1) if m else None
    _tlo_code_cache[key] = code
    return code

def get_tlo_bill_results(member_code, leg_sess='89R', limit=6):
    """Return authored bills with passed/failed status as keyVotes entries."""
    html = get_html(
        f'https://capitol.texas.gov/reports/report.aspx?LegSess={leg_sess}&ID=author&Code={member_code}'
    )
    if not html:
        return []
    results = []
    for row_m in re.finditer(r'<div class="row">(.*?)(?=<div class="row">|\Z)', html, re.S):
        block = row_m.group(1)
        bill_m   = re.search(r'Bill=([\w]+)">[^<]*([HS][BCR]+\s*\d+)', block, re.I)
        caption_m = re.search(r'<b>Caption</b>.*?bill-search-result-data[^>]*>(.*?)</div>', block, re.S)
        action_m  = re.search(r'<b>Last Action:</b>.*?bill-search-result-data[^>]*>([^<]+)</div>', block, re.S)
        if not bill_m or not caption_m:
            continue
        bill_no  = bill_m.group(2).strip()
        caption  = strip_tags(caption_m.group(1)).strip()
        action   = (action_m.group(1).strip() if action_m else '').replace('\r', '').replace('\n', ' ')
        passed   = bool(re.search(r'\bE\b|Effective|signed into law|become law', action, re.I))
        date_m   = re.search(r'(\d{2})/(\d{2})/(\d{4})', action)
        month    = f"{date_m.group(3)}-{date_m.group(1)}" if date_m else ''
        results.append({
            'description': f"{bill_no} — {caption[:95]}",
            'voted': 'Passed' if passed else 'Did not pass',
            'month': month,
        })
    # Show passed bills first
    passed  = [r for r in results if r['voted'] == 'Passed'][:4]
    failed  = [r for r in results if r['voted'] != 'Passed'][:2]
    return (passed + failed)[:limit]


def get_tlo_issues(member_code, leg_sess='89R'):
    """Fetch authored bills from TLO and derive top issues."""
    if not member_code:
        return []
    html = get_html(
        f'https://capitol.texas.gov/reports/report.aspx?LegSess={leg_sess}&ID=author&Code={member_code}'
    )
    if not html:
        return []
    captions = re.findall(r'Relating to ([^<\r\n]{10,200})', html)
    return bills_to_issues(captions)

# ─────────────────────────────────────────────────────────────────────────────
# TX STATE REP STANCES — bio text + Ballotpedia committees
# ─────────────────────────────────────────────────────────────────────────────

def scrape_tx_senator_stances(district):
    """Return positions from the TX Senate member bio page."""
    html = get_html(f'https://senate.texas.gov/member.php?d={district}')
    if not html:
        return []
    # The bio text sits inside a div whose class/id contains "bio"
    bio_m = re.search(r'bio"[^>]*>([\s\S]{80,}?)(?=<div|</div>)', html)
    if not bio_m:
        return []
    bio = strip_tags(bio_m.group(1))[:1200]
    positions = []
    # Committee chair roles
    for m in re.finditer(
        r'(?:Chairman|Chair|Vice[\s-]?Chair)\s+(?:of\s+|to\s+)?(?:the\s+)?'
        r'Senate Committee on ([A-Z][^,\.;]{3,50})', bio, re.I
    ):
        positions.append({'topic': 'Committee Leadership', 'stance': f'Chair, Senate Committee on {m.group(1).strip()}'})
    # Key policy sentences — skip sentences that start with names/titles
    existing = {p['stance'] for p in positions}
    for sent in re.split(r'(?<=[.!?])\s+', bio):
        sent = sent.strip()
        # Skip sentences that open with a person's name or reference Lt. Gov.
        if re.match(r'^(Dan|Lt\.|Sen\.|He |She |His |Her |The Senator)', sent):
            continue
        if re.search(
            r'property tax|fiscal|education|election|border|public safety|healthcare|'
            r'infrastructure|housing|energy|water|criminal|veteran|authored|champion', sent, re.I
        ):
            if 40 < len(sent) < 220 and sent not in existing:
                positions.append({'topic': 'Stated Focus', 'stance': sent})
                existing.add(sent)
        if len(positions) >= 5:
            break
    return positions[:5]


def scrape_bp_committees_as_stances(name):
    """Return Ballotpedia committee assignments as issue-position entries (for House reps)."""
    clean = re.sub(r'\s*\[[^\]]+\]$', '', name).strip()
    html  = get_html(f'https://ballotpedia.org/{clean.replace(" ", "_")}')
    if not html:
        return []
    # Find first (most current) committee block
    idx = html.find('assigned to the following committees')
    if idx < 0:
        return []
    block = html[idx: idx + 1200]
    committees = re.findall(r'>([^<]+Committee[^<]*)</a>', block)
    positions = []
    seen = set()
    for comm in committees:
        comm = comm.replace('&amp;', '&').strip()
        display = re.sub(r',?\s*Texas (House|Senate) of Representatives$', '', comm).strip()
        if display in seen or len(display) < 5:
            continue
        seen.add(display)
        positions.append({'topic': 'Committee', 'stance': display})
        if len(positions) >= 4:
            break
    return positions


# ─────────────────────────────────────────────────────────────────────────────
# TX-SPECIFIC SCRAPERS (no key needed)
# ─────────────────────────────────────────────────────────────────────────────

def scrape_tx_senator(district):
    """Scrape Texas Senate member page."""
    html = get_html(f'https://senate.texas.gov/member.php?d={district}')
    if not html:
        return None
    # Name from title
    title_m = re.search(r'<title>Senator\s+([^:]+):', html, re.I)
    name = title_m.group(1).strip() if title_m else None
    if not name:
        return None
    # Party: senate page → Ballotpedia → Wikipedia → Unknown
    party_m = re.search(r'<span>Party:</span>\s*([^<]+)', html, re.I)
    bp_party, bp_photo = ballotpedia_info(name)
    wiki_party, wiki_photo = wikipedia_info(name)
    party = (party_m.group(1).strip() if party_m else None) or bp_party or wiki_party or 'Unknown'
    # Photo: official TX Senate headshot → Ballotpedia → Wikipedia
    senate_photo_url = f'https://senate.texas.gov/members/d{int(district):02d}/img/headshot.jpg'
    senate_photo_r = get(senate_photo_url)
    photo = senate_photo_url if senate_photo_r else (bp_photo or wiki_photo)
    # Phone
    phone_m = re.search(r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}', html)
    phone = phone_m.group(0).strip() if phone_m else None
    # Website from external links
    web_m = re.search(r'href="(https?://(?!senate\.texas\.gov)[^"]+)"', html)
    website = web_m.group(1) if web_m else f'https://senate.texas.gov/member.php?d={district}'
    # Occupation
    occ_m = re.search(r'<span>Occupation:</span>\s*([^<]+)', html, re.I)
    occupation = occ_m.group(1).strip() if occ_m else ''
    member_code = get_tlo_member_code(name, 'senate')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        issues_f    = ex.submit(get_tlo_issues, member_code)
        positions_f = ex.submit(scrape_tx_senator_stances, district)
        votes_f     = ex.submit(get_tlo_bill_results, member_code)
        funders_f   = ex.submit(get_transparency_usa_funders, name, 'TX')
        issues    = issues_f.result()
        positions = positions_f.result()
        votes     = votes_f.result()
        funders   = funders_f.result()
    funder_url, funder_source = get_funder_url(name, 'TX')
    return {
        'name': name,
        'office': f'Texas State Senator, District {district}',
        'level': 'state',
        'party': party,
        'photoUrl': photo,
        'phones': [phone] if phone else [],
        'urls': [website],
        'emails': [],
        'address': [],
        'channels': [],
        'topIssues': issues,
        'campaignPositions': positions,
        'keyVotes': votes,
        'topFunders': funders,
        'funderCycle': None,
        'funderUrl': funder_url,
        'funderSource': funder_source,
        'termStart': None,
        'termEnd': None,
    }

def scrape_tx_rep(district):
    """Scrape Texas House member page."""
    html = get_html(f'https://house.texas.gov/members/member-page/?district={district}')
    if not html:
        return None
    # Name from h1: "Rep. Last, First - District N"
    h1_m = re.search(r'<h1[^>]*>Rep\.\s+([^<]+)</h1>', html, re.I|re.S)
    if not h1_m:
        return None
    raw = strip_tags(h1_m.group(1))
    # Convert "Last, First - District N" → "First Last"
    name_part = re.sub(r'\s*-\s*District\s*\d+.*', '', raw).strip()
    if ',' in name_part:
        parts = name_part.split(',', 1)
        name = f"{parts[1].strip()} {parts[0].strip()}"
    else:
        name = name_part
    # Phones
    phones = re.findall(r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}', html)
    # Party: Ballotpedia infobox (most reliable) → Wikipedia → Unknown
    # Photo: Ballotpedia → Wikipedia
    bp_party, bp_photo = ballotpedia_info(name)
    wiki_party, wiki_photo = wikipedia_info(name)
    party = bp_party or wiki_party or 'Unknown'
    photo = bp_photo or wiki_photo
    member_code = get_tlo_member_code(name, 'house')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        issues_f    = ex.submit(get_tlo_issues, member_code)
        positions_f = ex.submit(scrape_bp_committees_as_stances, name)
        votes_f     = ex.submit(get_tlo_bill_results, member_code)
        funders_f   = ex.submit(get_transparency_usa_funders, name, 'TX')
        issues    = issues_f.result()
        positions = positions_f.result()
        votes     = votes_f.result()
        funders   = funders_f.result()
    funder_url, funder_source = get_funder_url(name, 'TX')
    return {
        'name': name,
        'office': f'Texas State Representative, District {district}',
        'level': 'state',
        'party': party,
        'photoUrl': photo,
        'phones': phones[:1],
        'urls': [f'https://house.texas.gov/members/{district}'],
        'emails': [],
        'address': [],
        'channels': [],
        'topIssues': issues,
        'campaignPositions': positions,
        'keyVotes': votes,
        'topFunders': funders,
        'funderCycle': None,
        'funderUrl': funder_url,
        'funderSource': funder_source,
        'termStart': None,
        'termEnd': None,
    }

# ─────────────────────────────────────────────────────────────────────────────
# COUNTY COMMISSIONER PRECINCT LOOKUP (ArcGIS REST)
# ─────────────────────────────────────────────────────────────────────────────

# ArcGIS FeatureServer URLs for commissioner precinct boundaries, keyed by
# (county_lower, state).  Add more counties here as needed.
COMMISSIONER_PRECINCT_SERVICES = {
    ('harris', 'TX'): 'https://services.arcgis.com/su8ic9KbA7PYVxPS/arcgis/rest/services/Commissioner_Precincts/FeatureServer/0/query',
}

# Current commissioners per county, keyed by (county_lower, state, precinct_no)
# Values: (name, official_photo_url, [top_issues])
COMMISSIONER_DATA = {
    ('harris', 'TX', 1): ('Rodney Ellis',   'https://www.harriscountytx.gov/Portals/49/Images/rodney-ellis.jpg',
                          ['Criminal Justice Reform', 'Environmental Justice', 'Affordable Housing']),
    ('harris', 'TX', 2): ('Adrian Garcia',  'https://www.harriscountytx.gov/Portals/49/Images/AG-Headshot.jpg',
                          ['Infrastructure & Roads', 'Public Safety', 'Economic Development']),
    ('harris', 'TX', 3): ('Tom Ramsey',     'https://www.harriscountytx.gov/Portals/49/Images/CommissionerTomRamsey.jpg',
                          ['Infrastructure & Roads', 'Flood Control', 'Public Safety']),
    ('harris', 'TX', 4): ('Lesley Briones', 'https://www.harriscountytx.gov/Portals/49/Images/Comm4Briones.jpg',
                          ['Flood Control', 'Parks & Environment', 'Public Health']),
}

# Official headshots and issues for county judges by (county_lower, state)
COUNTY_JUDGE_DATA = {
    ('harris', 'TX'): (
        'https://www.harriscountytx.gov/Portals/49/Images/Hidalgo_pose1.jpg',
        ['Flood Control & Drainage', 'Public Health', 'Budget & Finance'],
    ),
}

def get_county_commissioner(county_clean, state, lat, lon):
    """Return the specific county commissioner card for a lat/lon, or None."""
    key = (county_clean.lower(), state)
    svc_url = COMMISSIONER_PRECINCT_SERVICES.get(key)
    if not svc_url or not lat or not lon:
        return None
    data = get_json(svc_url, {
        'geometry': f'{lon},{lat}',
        'geometryType': 'esriGeometryPoint',
        'inSR': '4326',
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': 'PCT_NO',
        'f': 'json',
    })
    if not data:
        return None
    features = data.get('features', [])
    if not features:
        return None
    precinct = features[0]['attributes'].get('PCT_NO')
    if not precinct:
        return None
    entry = COMMISSIONER_DATA.get((county_clean.lower(), state, precinct))
    if not entry:
        return None
    name, official_photo, issues = entry
    card = local_official(name, f'{county_clean} County Commissioner, Precinct {precinct}', state)
    card['photoUrl'] = official_photo
    card['topIssues'] = issues
    return card

# ─────────────────────────────────────────────────────────────────────────────
# LOCAL OFFICIALS — mayor, county judge, commissioners, city council
# ─────────────────────────────────────────────────────────────────────────────

def get_local_officials(county, city, state, lat=None, lon=None):
    state_name = STATE_NAMES.get(state, state)
    county_clean = re.sub(r'\s*County\s*$', '', county or '', flags=re.I).strip()
    county_display = f'{county_clean} County' if county_clean else ''

    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        futures = {}
        if city:
            futures['mayor'] = ex.submit(fetch_city_mayor, city, state_name)
            bp_city_url = f'https://ballotpedia.org/{city.replace(" ", "_")},_{state_name.replace(" ", "_")}'
            futures['council'] = ex.submit(scrape_bp_officeholders, bp_city_url, f'City Council Member, {city}', 10)
        if county_clean:
            futures['judge'] = ex.submit(fetch_county_judge, county or '', state, state_name)
            futures['commissioner'] = ex.submit(get_county_commissioner, county_clean, state, lat, lon)

        mayor       = futures['mayor'].result()       if 'mayor'       in futures else None
        judge       = futures['judge'].result()       if 'judge'       in futures else None
        commissioner= futures['commissioner'].result()if 'commissioner'in futures else None
        council     = futures['council'].result()     if 'council'     in futures else []

    # Split council into at-large (show individually) vs district seats (one directory card)
    at_large = [m for m in council if re.search(r'at.?large|controller|comptroller|clerk|treasurer|auditor', m['office'], re.I)]
    district  = [m for m in council if m not in at_large]

    officials = []
    if mayor:        officials.append(mayor)
    if judge:        officials.append(judge)
    if commissioner: officials.append(commissioner)
    officials.extend(at_large)

    # District council members → one "find your rep" directory card
    if district and city:
        bp_city_url = f'https://ballotpedia.org/{city.replace(" ", "_")},_{state_name.replace(" ", "_")}'
        officials.append({
            'name': f'{city} City Council',
            'office': f'City Council — {len(district)} District Seats',
            'level': 'local',
            'party': 'Nonpartisan',
            'photoUrl': None,
            'phones': [],
            'urls': [bp_city_url],
            'emails': [],
            'address': [],
            'channels': [],
            'topIssues': ['Local Legislation', 'City Budget', 'Community Services'],
            'termStart': None,
            'termEnd': None,
            'isDirectory': True,
            'placeholderMsg': f'{len(district)} district seats — click to find your specific district representative.',
        })
    elif not district and not at_large and city:
        bp_city_url = f'https://ballotpedia.org/{city.replace(" ", "_")},_{state_name.replace(" ", "_")}'
        officials.append({
            'name': f'{city} City Council',
            'office': 'City Council',
            'level': 'local',
            'party': 'Nonpartisan',
            'photoUrl': None,
            'phones': [],
            'urls': [bp_city_url],
            'emails': [],
            'address': [],
            'channels': [],
            'topIssues': ['Local Legislation', 'City Budget', 'Community Services'],
            'termStart': None,
            'termEnd': None,
            'isDirectory': True,
        })

    # Always add a Commissioners Court / County Board link card
    if county_clean:
        county_slug = county_clean.lower().replace(' ', '')
        guessed_url = f'https://www.{county_slug}county{state.lower()}.gov'
        court_title = 'Commissioners Court' if state == 'TX' else 'County Board of Supervisors'
        officials.append({
            'name': f'{county_display} {court_title}',
            'office': court_title,
            'level': 'local',
            'party': 'Nonpartisan',
            'photoUrl': None,
            'phones': [],
            'urls': [guessed_url],
            'emails': [],
            'address': [],
            'channels': [],
            'topIssues': ['County Budget', 'Local Infrastructure', 'County Services'],
            'termStart': None,
            'termEnd': None,
            'isDirectory': True,
        })
    elif not officials:
        officials.append({
            'name': county_display or 'County Government',
            'office': 'County Officials',
            'level': 'local',
            'party': 'Nonpartisan',
            'photoUrl': None,
            'phones': [],
            'urls': [],
            'emails': [],
            'address': [],
            'channels': [],
            'topIssues': ['Local Government Services', 'County Administration'],
            'termStart': None,
            'termEnd': None,
            'isDirectory': True,
        })
    return officials

# ─────────────────────────────────────────────────────────────────────────────
# CENSUS GEOCODER
# ─────────────────────────────────────────────────────────────────────────────

def geocode(address):
    data = get_json(
        'https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress',
        {'address': address, 'benchmark': 'Public_AR_Current',
         'vintage': 'Current_Current', 'format': 'json'},
        timeout=20,
    )
    if not data:
        raise Exception('Census geocoder unavailable')
    matches = data.get('result', {}).get('addressMatches', [])
    if not matches:
        return None
    m = matches[0]
    geo = m.get('geographies', {})

    def first(key, field):
        items = geo.get(key, [])
        return items[0].get(field) if items else None

    raw_city = first('Incorporated Places', 'NAME')
    if not raw_city:
        sub = first('County Subdivisions', 'NAME') or ''
        # Ignore Census County Divisions (CCD) — census constructs, not real municipalities
        if sub and not re.search(r'\bCCD\b|\bcensus\b', sub, re.I):
            raw_city = sub
    city = re.sub(r'\s+(city|town|village|borough|township|municipality)$', '', raw_city or '', flags=re.I).strip() or None

    return {
        'normalizedAddress': m['matchedAddress'],
        'lat': float(m['coordinates']['y']),
        'lon': float(m['coordinates']['x']),
        'state': first('States', 'STUSAB'),
        'county': first('Counties', 'NAME'),
        'city': city,
        'cd': first('119th Congressional Districts', 'BASENAME'),
        'stateSen': first('2024 State Legislative Districts - Upper', 'BASENAME'),
        'stateHouse': first('2024 State Legislative Districts - Lower', 'BASENAME'),
    }

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG ENDPOINT — exposes built-in keys to the frontend
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/config')
def get_config():
    return jsonify({
        'openstatesKey': OPENSTATES_KEY or '',
        'fecKey': FEC_KEY if FEC_KEY != 'DEMO_KEY' else '',
    })

# MAIN API ROUTE
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/reps')
def get_reps():
    address = request.args.get('address', '').strip()
    # Use client-supplied keys, falling back to server-side built-in keys
    openstates_key = request.args.get('openstates_key', '').strip() or OPENSTATES_KEY
    fec_key = request.args.get('fec_key', '').strip() or (FEC_KEY if FEC_KEY != 'DEMO_KEY' else None)
    if not address:
        return jsonify({'error': 'Address is required'}), 400

    try:
        geo = geocode(address)
    except Exception as e:
        return jsonify({'error': f'Could not geocode address: {e}'}), 500

    if not geo:
        return jsonify({'error': 'Address not found. Try a more specific address including zip code.'}), 404

    state = geo['state']
    cd = geo['cd']
    sen_d = geo['stateSen']
    house_d = geo['stateHouse']
    officials = []

    # Run federal, state, and local fetching in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        fed_future = ex.submit(get_federal_reps, state, cd, fec_key)

        if state == 'TX':
            # Always use TX-specific scrapers — more accurate than OpenStates for TX
            tx_sen_future = ex.submit(scrape_tx_senator, sen_d) if sen_d else None
            tx_rep_future = ex.submit(scrape_tx_rep, house_d) if house_d else None
        elif openstates_key:
            state_future = ex.submit(get_state_reps_openstates, state, sen_d, house_d, openstates_key)

        local_future = ex.submit(get_local_officials, geo['county'] or '', geo.get('city') or '', state, geo.get('lat'), geo.get('lon'))

        officials.extend(fed_future.result() or [])

        if state == 'TX':
            if sen_d and tx_sen_future:
                s = tx_sen_future.result()
                if s: officials.append(s)
            if house_d and tx_rep_future:
                r = tx_rep_future.result()
                if r: officials.append(r)
        elif openstates_key:
            officials.extend(state_future.result() or [])
        else:
            # Other states without OpenStates key: show placeholder
            officials.append({
                'name': f'{STATE_NAMES.get(state, state)} State Legislature',
                'office': f'State Senate District {sen_d} · House District {house_d}',
                'level': 'state',
                'party': 'Unknown',
                'photoUrl': None,
                'phones': [],
                'urls': [],
                'emails': [],
                'address': [],
                'channels': [],
                'topIssues': [],
                'termStart': None,
                'termEnd': None,
                'isPlaceholder': True,
                'placeholderMsg': 'Add a free OpenStates API key in ⚙️ Settings to see your state representatives.',
            })

        officials.extend(local_future.result() or [])

    # Fill ideology scores — party-based fallback for officials without a DW-NOMINATE score
    for off in officials:
        if off.get('ideologyScore') is None and not off.get('isDirectory') and not off.get('isPlaceholder'):
            score = party_to_ideology(off.get('party', ''))
            if score is not None:
                off['ideologyScore'] = score

    # Fetch campaign positions (Ballotpedia) for officials that don't have them yet
    needs_positions = [
        off for off in officials
        if not off.get('isDirectory') and not off.get('isPlaceholder')
        and not off.get('campaignPositions')
    ]
    if needs_positions:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            pos_futures = {
                off['name']: ex.submit(scrape_bp_positions, off['name'])
                for off in needs_positions
            }
        for off in needs_positions:
            result = pos_futures[off['name']].result()
            if result:
                off['campaignPositions'] = result

    # Fetch actual Transparency USA funder data for non-federal officials (concurrently)
    non_fed = [
        off for off in officials
        if off.get('level') != 'federal'
        and not off.get('isDirectory')
        and not off.get('isPlaceholder')
    ]
    if non_fed and state in _TRANSPARENCY_USA_STATES:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futures = {
                off['name']: ex.submit(get_transparency_usa_funders, off['name'], state)
                for off in non_fed
                if not off.get('topFunders')  # skip TX state reps already populated
            }
        for off in non_fed:
            name_key = off['name']
            if name_key in futures:
                off['topFunders'] = futures[name_key].result()
            if not off.get('funderUrl'):
                url, source = get_funder_url(name_key, state)
                if url:
                    off['funderUrl'] = url
                    off['funderSource'] = source
    else:
        for off in non_fed:
            if not off.get('funderUrl'):
                url, source = get_funder_url(off.get('name', ''), state)
                if url:
                    off['funderUrl'] = url
                    off['funderSource'] = source

    return jsonify({
        'normalizedAddress': geo['normalizedAddress'],
        'state': state,
        'county': geo['county'],
        'cd': cd,
        'stateSenateDistrict': sen_d,
        'stateHouseDistrict': house_d,
        'officials': officials,
        'hasOpenStates': bool(openstates_key) or state == 'TX',
    })

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8080))
    print(f'\n🏛️  Luciv running at http://localhost:{port}')
    print('   Open that URL in your browser.\n')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
