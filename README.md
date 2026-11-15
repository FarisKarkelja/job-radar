# 📡 Job Radar

Automatski prati oglase za posao u Bosni i Hercegovini (IT firme, banke, telekomi,
javna preduzeća, zavodi za zapošljavanje, ADS, portali) i šalje push notifikaciju
na Android preko [ntfy](https://ntfy.sh) čim se pojavi nešto novo.

- **Šest provjera dnevno** (07:07, 10:07, 13:07, 16:07, 19:07, 22:07), pokreće ih cron-job.org preko GitHub API-ja.
- **Šalju se samo IT/tech poslovi i prakse.** Prakse i pripravnički programi kod poslodavaca koje pratiš stižu i kad nisu čisto IT.
- **Lokacija:** Sarajevo bilo kako (onsite, hybrid, remote); ostatak BiH samo remote ili hybrid; van BiH samo
  studentski programi i prakse otvoreni za studente iz BiH (STAR.study, CERN, iAgora, quant/tech prakse u Evropi...).
  Kad lokacija nije jasna, radar otvori oglas i pročita je.
- **Bez ponavljanja:** isti naslov iste firme se ne šalje ponovo 60 dana (LinkedIn repostovi, isti oglas na više portala).
- **Dubinska pretraga:** kod javnih oglasa otvara i PDF i traži IT pozicije zakopane u tekstu.
- **Novi izvor** pošalje jednu poruku sa onim što je trenutno otvoreno, a poslije samo nove oglase.
- **Alarm kad monitor pukne** i **dnevni izvještaj** (koliko je poslano i koliko preskočeno i zašto).
- Sve poslano je u [OGLASI.md](OGLASI.md), a stanje svakog izvora u [reports/status.md](reports/status.md).

## Notifikacije

| Ikona | Prioritet | Značenje |
|---|---|---|
| 🚨 | 5 (max) | IT pozicija za početnike (pripravnik, praksa, junior, student...) |
| 💻 | 4 | IT pozicija |
| 🎓 | 3 | Praksa/pripravnički program poslodavca koji pratiš (nije IT po naslovu) |
| 📋 | 3 | Novi izvor: šta je trenutno otvoreno |
| 🔄 | 3-4 | Promijenila se stranica karijera koja nema listu oglasa |
| ⚠️ | 3 | Neki izvor ne radi dvije provjere zaredom |

## Podešavanje (jednom)

1. Instaliraj **ntfy** sa Play Storea, klikni **+** i pretplati se na svoj kanal (naziv je u GitHub Secretu `NTFY_TOPIC`).
2. U postavkama ntfy kanala možeš podesiti zvuk po prioritetu.
3. U repou: **Settings → Secrets and variables → Actions → New repository secret**,
   ime `NTFY_TOPIC`, vrijednost = naziv kanala.
4. **Actions → Job Radar → Run workflow** za prvo pokretanje. Prvi put radar samo zapamti
   trenutne oglase (bez gomile notifikacija), a od tada javlja samo nove.

## Dodavanje izvora

Sve je u [`sources.yaml`](sources.yaml), opis polja je na vrhu fajla. Nove izvore prvo testiraj u
[`probe.yaml`](probe.yaml): workflow **Proba izvora** ih pokrene bez notifikacija i napiše
[`reports/probe.md`](reports/probe.md) (može i ATS sonda za listu imena firmi). Za nove firme bez poznate
stranice karijera dodaj ih u [`candidates.yaml`](candidates.yaml): **Detektiv** workflow ih
automatski obiđe i u [`reports/discovery.md`](reports/discovery.md) napiše gdje su im oglasi.

## Lokalno pokretanje

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m radar.main --no-notify            # test bez slanja notifikacija
python -m radar.main --only szks,bhtelecom   # samo neki izvori
python -m radar.discover --only "Union banka"
```

Ključne riječi za IT i početničke pozicije su u [`radar/keywords.py`](radar/keywords.py).
