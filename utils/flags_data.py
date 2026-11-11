import random
import re
import unicodedata
from typing import Dict, List, Optional


_RAW = """
AF|Afghanistan|
AL|Albania|
DZ|Algeria|
AD|Andorra|
AO|Angola|
AG|Antigua and Barbuda|antigua
AR|Argentina|
AM|Armenia|
AU|Australia|
AT|Austria|
AZ|Azerbaijan|
BS|Bahamas|the bahamas
BH|Bahrain|
BD|Bangladesh|
BB|Barbados|
BY|Belarus|
BE|Belgium|
BZ|Belize|
BJ|Benin|
BT|Bhutan|
BO|Bolivia|
BA|Bosnia and Herzegovina|bosnia,bosnia herzegovina
BW|Botswana|
BR|Brazil|
BN|Brunei|
BG|Bulgaria|
BF|Burkina Faso|
BI|Burundi|
KH|Cambodia|
CM|Cameroon|
CA|Canada|
CV|Cape Verde|cabo verde
CF|Central African Republic|car
TD|Chad|
CL|Chile|
CN|China|
CO|Colombia|
KM|Comoros|
CG|Republic of the Congo|congo,congo brazzaville,congo republic
CD|DR Congo|drc,democratic republic of the congo,congo kinshasa
CR|Costa Rica|
CI|Ivory Coast|cote d'ivoire,cote divoire
HR|Croatia|
CU|Cuba|
CY|Cyprus|
CZ|Czechia|czech republic,czech
DK|Denmark|
DJ|Djibouti|
DM|Dominica|
DO|Dominican Republic|
EC|Ecuador|
EG|Egypt|
SV|El Salvador|
GQ|Equatorial Guinea|
ER|Eritrea|
EE|Estonia|
SZ|Eswatini|swaziland
ET|Ethiopia|
FJ|Fiji|
FI|Finland|
FR|France|
GA|Gabon|
GM|Gambia|the gambia
GE|Georgia|
DE|Germany|
GH|Ghana|
GR|Greece|
GD|Grenada|
GT|Guatemala|
GN|Guinea|
GW|Guinea-Bissau|guinea bissau
GY|Guyana|
HT|Haiti|
HN|Honduras|
HU|Hungary|
IS|Iceland|
IN|India|
ID|Indonesia|
IR|Iran|
IQ|Iraq|
IE|Ireland|
IL|Israel|
IT|Italy|
JM|Jamaica|
JP|Japan|
JO|Jordan|
KZ|Kazakhstan|
KE|Kenya|
KI|Kiribati|
XK|Kosovo|
KW|Kuwait|
KG|Kyrgyzstan|
LA|Laos|
LV|Latvia|
LB|Lebanon|
LS|Lesotho|
LR|Liberia|
LY|Libya|
LI|Liechtenstein|
LT|Lithuania|
LU|Luxembourg|
MG|Madagascar|
MW|Malawi|
MY|Malaysia|
MV|Maldives|
ML|Mali|
MT|Malta|
MH|Marshall Islands|
MR|Mauritania|
MU|Mauritius|
MX|Mexico|
FM|Micronesia|
MD|Moldova|
MC|Monaco|
MN|Mongolia|
ME|Montenegro|
MA|Morocco|
MZ|Mozambique|
MM|Myanmar|burma
NA|Namibia|
NR|Nauru|
NP|Nepal|
NL|Netherlands|holland,the netherlands
NZ|New Zealand|
NI|Nicaragua|
NE|Niger|
NG|Nigeria|
KP|North Korea|
MK|North Macedonia|macedonia
NO|Norway|
OM|Oman|
PK|Pakistan|
PW|Palau|
PS|Palestine|
PA|Panama|
PG|Papua New Guinea|png
PY|Paraguay|
PE|Peru|
PH|Philippines|the philippines
PL|Poland|
PT|Portugal|
QA|Qatar|
RO|Romania|
RU|Russia|russian federation
RW|Rwanda|
KN|Saint Kitts and Nevis|st kitts and nevis,saint kitts,st kitts
LC|Saint Lucia|st lucia
VC|Saint Vincent and the Grenadines|st vincent,saint vincent
WS|Samoa|
SM|San Marino|
ST|Sao Tome and Principe|sao tome
SA|Saudi Arabia|ksa,saudi
SN|Senegal|
RS|Serbia|
SC|Seychelles|
SL|Sierra Leone|
SG|Singapore|
SK|Slovakia|
SI|Slovenia|
SB|Solomon Islands|
SO|Somalia|
ZA|South Africa|
KR|South Korea|korea,republic of korea
SS|South Sudan|
ES|Spain|
LK|Sri Lanka|
SD|Sudan|
SR|Suriname|
SE|Sweden|
CH|Switzerland|
SY|Syria|
TW|Taiwan|
TJ|Tajikistan|
TZ|Tanzania|
TH|Thailand|
TL|Timor-Leste|east timor,timor leste
TG|Togo|
TO|Tonga|
TT|Trinidad and Tobago|trinidad
TN|Tunisia|
TR|Turkey|turkiye
TM|Turkmenistan|
TV|Tuvalu|
UG|Uganda|
UA|Ukraine|
AE|United Arab Emirates|uae,emirates
GB|United Kingdom|uk,britain,great britain,england
US|United States|usa,us,america,united states of america
UY|Uruguay|
UZ|Uzbekistan|
VU|Vanuatu|
VA|Vatican City|vatican,holy see
VE|Venezuela|
VN|Vietnam|viet nam
YE|Yemen|
ZM|Zambia|
ZW|Zimbabwe|
"""


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.replace("-", " ").replace("'", ""))
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def _flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


COUNTRIES: List[Dict[str, str]] = []
_ALIASES: Dict[str, str] = {}
for _line in _RAW.strip().splitlines():
    _code, _name, _al = _line.split("|")
    COUNTRIES.append({"code": _code, "name": _name, "flag": _flag(_code)})
    for _a in [_name] + [x for x in _al.split(",") if x]:
        _ALIASES[_norm(_a)] = _code

_BY_CODE = {c["code"]: c for c in COUNTRIES}
_recent: Dict[int, List[str]] = {}


def match_country(text: str) -> Optional[str]:
    key = _norm(text)
    if not key or len(key) > 60:
        return None
    return _ALIASES.get(key)


def pick_flag(uid: int) -> Dict[str, str]:
    recent = _recent.setdefault(uid, [])
    pool = [c for c in COUNTRIES if c["code"] not in recent] or COUNTRIES
    c = random.choice(pool)
    recent.append(c["code"])
    recent[:] = recent[-40:]
    return {
        "id": c["code"], "kind": "flag", "category": None,
        "question": "Which country does this flag belong to?",
        "flag": c["flag"], "answer": c["name"], "code": c["code"], "choices": [],
    }
