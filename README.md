# 📡 Job Radar

Automatski prati oglase za posao u Bosni i Hercegovini (IT firme, banke, telekomi,
javna preduzeća, zavodi za zapošljavanje, ADS, portali) i šalje push notifikaciju
na Android preko [ntfy](https://ntfy.sh) čim se pojavi nešto novo.

- **Šest provjera dnevno** (07, 10, 13, 16, 19 i 22h po sarajevskom vremenu), na GitHub Actions, besplatno.
- **Svaki novi oglas** iz svakog izvora, bez filtriranja po senioritetu.
- **IT oglasi su glasni**, a IT + pripravnik/praksa/junior najglasniji (🚨).
- **Dubinska pretraga:** kod javnih oglasa otvara i PDF i traži IT pozicije zakopane u tekstu
  (npr. "Viši stručni saradnik za informatizaciju" na 3. strani).
- **Alarm kad monitor pukne** (sajt promijeni dizajn) i **dnevni izvještaj** svako jutro.
- Sve što je pronađeno je u [OGLASI.md](OGLASI.md), a stanje svakog izvora u [reports/status.md](reports/status.md).

## Notifikacije

| Ikona | Prioritet | Značenje |
|---|---|---|
| 🚨 | 5 (max) | IT pozicija za početnike (pripravnik, praksa, junior, student...) |
| 💻 | 4 | IT pozicija |
| 💼 | 3 | Novi oglas firme koju pratiš (nije IT po naslovu) |
| 🗂️ | 2 (tiho) | Grupa ne-IT oglasa sa velikih javnih izvora (SZKS, ADS...) |
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

Sve je u [`sources.yaml`](sources.yaml), opis polja je na vrhu fajla. Za nove firme bez poznate
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
