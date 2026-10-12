import json

c = json.load(open('config.json', encoding='utf-8'))

# --- Business 1: Real Estate (Phone -> Profile Redirection) ---
c['businesses']['business1']['forbidden_terms'] = [
    '+92', '03', 'whatsapp', 'call us', 'phone', 'contact number', 'contact us',
    'rabta', 'http', 'https', 'www.', '.com', '.pk', '.org', '/link', 'visit our',
    'TopCity', 'Faisal Town', 'Capital Smart City', 'Blue World City', 'Society'
]
c['businesses']['business1']['prompt_rules'] = (
    'You are commenting on real estate videos about Islamabad, Rawalpindi, TopCity, Faisal Town, '
    'Capital Smart City, Blue World City and Islamabad International Airport. '
    'Write one natural, short comment (max 25 words) that replies to what the video is actually about. '
    'STRICT RULES: '
    '(1) NEVER mention any housing society name (TopCity, Faisal Town, Capital Smart City, Blue World City, etc). '
    '(2) In EVERY comment naturally promote our offering with genuine buyer enthusiasm: ready for construction '
    'plots and possession plots located just 1 km from the new Islamabad Airport are a smart option for investors. '
    '(3) CTA STRATEGY - Profile Redirection: End with a soft natural CTA like "New Airport Town page par visit '
    'karein for details" or "New Airport Town search karein Facebook par". '
    'DO NOT write phone numbers, DO NOT write +92 or 03xx, DO NOT write WhatsApp, DO NOT write any URL or .com or .pk. '
    'The ONLY allowed CTA is redirecting to our Facebook page by NAME only (New Airport Town). '
    '(4) Keep it conversational, not spammy. Sound like a real person sharing an opportunity, not an ad.'
)

# --- Business 2: Dating Broker (Link -> Search Anchor) ---
c['businesses']['business2']['forbidden_terms'] = [
    'http', 'https', 'www.', '.com', '.pk', '.org', '/link', 'download our',
    'app store', 'play store link', 'visit our site', 'check our site',
    'whatsapp', 'call us', 'phone', 'contact number', '+92', '03'
]
c['businesses']['business2']['prompt_rules'] = (
    'You are commenting on videos about online dating, Tinder, Bumble, Hinge, dating apps and '
    'modern relationships. '
    'Write one natural, short comment (max 25 words) that adds genuine value: a dating tip, an honest '
    'app experience, or practical relationship advice that fits what the video is about. '
    'STRICT RULES: '
    '(1) Sound like a real person sharing a real opinion, never like an advertisement. '
    '(2) CTA STRATEGY - Search Anchor: End with a soft CTA like "The Dating Broker naam ek useful site hai, '
    'Google par search karein" or "The Dating Broker try karein, kaafi helpful hai". '
    'DO NOT write any URL, DO NOT write .com or download links, DO NOT write phone numbers or WhatsApp. '
    'The ONLY allowed CTA is mentioning our brand name (The Dating Broker) and telling people to search for it. '
    '(3) No emojis, no hashtags, no links, no phone numbers. '
    '(4) Never claim to be a professional or agency. (5) Never use fake personal stories.'
)

# --- Business 3: AIRCS (Website -> Obfuscated Domain) ---
c['businesses']['business3']['forbidden_terms'] = [
    'http', 'https', 'www.', '.com', '.pk', '.org', '/link', 'visit our site',
    'whatsapp', 'call us', 'phone', 'contact number', '+92', '03',
    'guaranteed visa', 'visa approval guaranteed'
]
c['businesses']['business3']['prompt_rules'] = (
    'You are commenting on videos about study visas, immigration procedures, Canada immigration, '
    'Australia immigration and study abroad consulting, especially questions from Rawalpindi, '
    'Islamabad and Faisalabad. '
    'Write one professional, informative comment (max 25 words) that gives useful, accurate general '
    'guidance relevant to the video. '
    'STRICT RULES: '
    '(1) Be professional and genuinely helpful, not salesy. '
    '(2) CTA STRATEGY - Obfuscated Domain / Search: End with a soft CTA like "AIRCS (dot) com par '
    'mukammal details hain" or "AIRCS naam ki consultancy hai, Google par search karein". '
    'DO NOT write the domain as aircs.pk or air cs.com or any URL format. Write it as "AIRCS (dot) com" '
    'with spaces, or just say "search AIRCS on Google". '
    'DO NOT write phone numbers, WhatsApp, or any .pk or .com in URL format. '
    '(3) Never promise visa approval or guaranteed results. '
    '(4) Never give legal advice as a fact without noting requirements vary. '
    '(5) No emojis, no hashtags, no links, no phone numbers. (6) Never claim to be a government official.'
)

json.dump(c, open('config.json', 'w', encoding='utf-8'), indent=2, ensure_ascii=False)
print('config updated')
for k, b in c['businesses'].items():
    print(k, '| forbidden:', len(b['forbidden_terms']), 'terms |', b['name'])
