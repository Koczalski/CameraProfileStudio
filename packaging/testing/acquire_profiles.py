"""Retrieve pinned public profile sources. Input assets are NOT distribution payloads."""
from pathlib import Path
import hashlib
import json
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / 'input/ccd_profiles'
OUTPUT = ROOT / 'output/test-profile-acquisition'
REPOS = {
    'photivo': ('google-code-export/photivo', '1f7142729588698980e4a098a3026d02b41001a5'),
    'rt': ('RawTherapee/RawTherapee', 'b2ee504c47e5e6d5e054395aa12bf1fe43bd1c3c'),
    'kodak': ('Alexey-Danilchenko/Kodak-DCS-Tools', '3e80df2ca549ed49c0f966dcf036962a8736c137'),
}
SOURCES = [
    ('Sony_DSC-F828', 'photivo', 'Profiles/Camera/Standard/SONY_DSC-F828.icc'),
    ('Sony_DSC-V3', 'photivo', 'Profiles/Camera/Standard/SONY_DSC-V3.icc'),
    ('Nikon_D70', 'photivo', 'Profiles/Camera/Standard/NIKON_D70.icc'),
    ('Nikon_D70s', 'rt', 'rtdata/dcpprofiles/NIKON D70s.dcp'),
    ('Nikon_D200', 'rt', 'rtdata/iccprofiles/input/Nikon D200.icc'),
    ('Nikon_D200', 'rt', 'rtdata/dcpprofiles/Nikon D200.dcp'),
    ('Kodak_DCS760', 'photivo', 'Profiles/Camera/Standard/Kodak_DCS760C.icc'),
    ('Kodak_ProBack645', 'photivo', 'Profiles/Camera/Standard/Kodak_ProBack645.icc'),
    ('Olympus_E1', 'rt', 'rtdata/dcpprofiles/Olympus E-1.dcp'),
]
EXPECTED_SHA256 = {
    'Sony_DSC-F828.icc': '1dc3e5907345a0b5bfd3266bc71336800c58be11c7b866efc8ff27bb88a4b53f',
    'Sony_DSC-V3.icc': 'd1b83136ed8aee9eb5d8ebe57c3850b91b0cc3800adf011640cc5c9baf793cee',
    'Nikon_D70.icc': 'e497ded89833a980471f5a27ca9c467193a332bfdc1b99b0fc49ad6cc304ea65',
    'Nikon_D70s.dcp': '0fc337fafc606ef121a8ea80ad6b0e8bd06b1fdd1f37a595104f92167946a6e8',
    'Nikon_D200.icc': '82d4754ee20c3074f57aae7eeb7138f28577caabb2b4285a4078fa8e20583f1f',
    'Nikon_D200.dcp': '4892e2288b57b5d498d4dbd01ea2d16143cbbb4e87b358e654f13711a97ebc4e',
    'Kodak_DCS760.icc': '640d1b8307b4c5da388edd5528ee9cdfdae78ced9f2470af7a06d4d9f1593d41',
    'Kodak_ProBack645.icc': 'fb061e8fbaf8beef9f7395e7879bc4b7a9559ca3f43be1f2f725736d30d4e059',
    'Olympus_E1.dcp': 'aba7e8b58a08db1205a295e543871591d65b7e309ac320cd99404ec675521dde',
}
DOCUMENTS = [('photivo','COPYING'), ('photivo','COPYING.3rd.party'),
             ('rt','LICENSE'), ('rt','rtdata/dcpprofiles/README'),
             ('rt','rtdata/iccprofiles/input/README'), ('kodak','profiles.md'),
             ('photivo','Sources/ptCreateAdobeProfiles.cpp'),
             ('photivo','Sources/ptAdobeTable.h')]

def retrieve(key, path, local):
    repo, revision = REPOS[key]
    url = 'https://raw.githubusercontent.com/' + repo + '/' + revision + '/' + urllib.parse.quote(path)
    data = urllib.request.urlopen(url, timeout=90).read()
    expected = EXPECTED_SHA256.get(local.name)
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Pinned profile SHA256 mismatch: ' + local.name)
    local.write_bytes(data)
    return dict(repository=repo, revision=revision, repository_path=path,
                retrieval_url=url, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                local_input=str(local.relative_to(ROOT)).replace('\\','/'))

def main():
    INPUT.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rows=[]
    for model,key,path in SOURCES:
        local=INPUT/(model+Path(path).suffix)
        row=retrieve(key,path,local)
        row.update(model=model, redistribution='unverified; input-only; not included in output')
        rows.append(row)
        print(model, row['sha256'])
    docs=[]
    for key,path in DOCUMENTS:
        try:
            docs.append(retrieve(key,path,INPUT/(key+'_'+path.replace('/','_')+'.txt')))
        except Exception as exc:
            docs.append(dict(repository=REPOS[key][0], repository_path=path, error=str(exc)))
    if any('error' in doc for doc in docs if doc['repository_path'] in ('COPYING', 'LICENSE')):
        raise RuntimeError('Required upstream profile license acquisition failed')
    (OUTPUT/'sources.json').write_text(json.dumps(dict(profiles=rows,documents=docs),indent=2),encoding='utf-8')

if __name__=='__main__': main()
